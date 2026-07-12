#!/usr/bin/env python3
"""Regression tests for C007 constrained-platform evidence gates."""

from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import pathlib
import subprocess
import tempfile
import textwrap
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
EVALUATE_PATH = ROOT / "evaluation" / "scripts" / "evaluate.py"


spec = importlib.util.spec_from_file_location("unialloc_evaluate", EVALUATE_PATH)
assert spec is not None and spec.loader is not None
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)


def source_fingerprint_fixture(digest_byte: str = "a") -> dict:
    return {
        "schema_version": evaluate.REPOSITORY_SOURCE_FINGERPRINT_SCHEMA_VERSION,
        "algorithm": "sha256",
        "source_digest": digest_byte * 64,
    }


def source_fingerprint_marker_fields(source_fingerprint: dict) -> str:
    return (
        f"schema_version={source_fingerprint.get('schema_version')} "
        f"algorithm={source_fingerprint.get('algorithm')} "
        f"source_digest={source_fingerprint.get('source_digest')}"
    )


def source_bind_platform_matrix(matrix: dict) -> dict:
    fingerprint = evaluate.repository_source_fingerprint()
    return {
        name: evaluate.evidence_source_bound_payload(entry, fingerprint=fingerprint)
        if isinstance(entry, dict)
        else entry
        for name, entry in matrix.items()
    }


def valid_constrained_boot_marker(platform: str = "blogos") -> str:
    return (
        f"{evaluate.CONSTRAINED_BOOT_SAMPLE_MARKER} "
        f"platform={platform} "
        "boot_cycle=1 "
        "fixed_heap_ready=true "
        "c_abi_invoked=true "
        "c_abi_ready_after_init=true "
        "c_abi_round_trips=2 "
        "c_abi_invalid_layouts_rejected=3 "
        "c_abi_over_page_alignment_checked=true "
        "c_abi_over_page_alignment=8192 "
        "allocator_total_allocations=4 "
        "allocator_total_deallocations=4 "
        "allocator_typed_allocations=1 "
        "allocator_typed_deallocations=1 "
        "allocator_fallback_allocations=3 "
        "allocator_fallback_deallocations=3 "
        "allocator_total_allocated_bytes=4096 "
        "allocator_typed_allocated_bytes=64 "
        "allocator_fallback_allocated_bytes=4032 "
        "allocator_coverage_basis_points=2500 "
        "allocator_type_stats_rows=1 "
        "allocator_type_stats_dropped_events=0 "
        "allocator_type_stats_probe_matched=true "
        "c_abi_probe_passed=true"
    )


def valid_constrained_boot_provenance_marker(
    platform: str = "blogos",
    *,
    source_fingerprint: dict | None = None,
    include_source_fingerprint: bool = True,
) -> str:
    marker = (
        f"{evaluate.CONSTRAINED_BOOT_PROVENANCE_MARKER} "
        f"platform={platform} "
        f"image_sha256={'1' * 64} "
        f"boot_config_sha256={'2' * 64} "
        "emulator=qemu-system-x86_64 "
        "emulator_version=8.2.0"
    )
    if include_source_fingerprint:
        marker += " " + source_fingerprint_marker_fields(
            source_fingerprint or evaluate.repository_source_fingerprint()
        )
    return marker


def constrained_boot_provenance_marker_for_artifacts(
    platform: str,
    image: pathlib.Path,
    boot_config: pathlib.Path,
    *,
    source_fingerprint: dict | None = None,
) -> str:
    return (
        f"{evaluate.CONSTRAINED_BOOT_PROVENANCE_MARKER} "
        f"platform={platform} "
        f"image_sha256={evaluate.file_sha256(image)} "
        f"boot_config_sha256={evaluate.file_sha256(boot_config)} "
        "emulator=qemu-system-x86_64 "
        "emulator_version=8.2.0 "
        + source_fingerprint_marker_fields(
            source_fingerprint or evaluate.repository_source_fingerprint()
        )
    )


def valid_rust_for_linux_kernel_build_log() -> str:
    return textwrap.dedent(
        """
        make[1]: Entering directory '/src/linux-rfl'
          RUSTC [M] kernel/kernel-modules/benchmarking/rust_bench.o
          LD [M] kernel/kernel-modules/benchmarking/rust_bench.ko
          MODPOST Module.symvers
        modules.order generated
        Rust-for-Linux kernel module rust_bench.ko build complete
        BUILD SUCCESS
        """
    ).strip()


def valid_rust_for_linux_cycle_counts() -> dict:
    cycle_records = [
        {
            "source_marker": evaluate.RUST_FOR_LINUX_CYCLE_SAMPLE_MARKER,
            "benchmark": "bench_new",
            "cycle_count": 1000,
            "bytes": 0,
        },
        {
            "source_marker": evaluate.RUST_FOR_LINUX_CYCLE_SAMPLE_MARKER,
            "benchmark": "bench_with_capacity_0100",
            "cycle_count": 1200,
            "bytes": 100,
        },
    ]
    boot_records = evaluate.extract_constrained_boot_sample_records_from_text(
        valid_constrained_boot_marker("rust-for-linux"),
        platform_name="rust-for-linux",
    )
    return {
        "schema_version": 1,
        "source": "rust-for-linux-kernel-cycle-counts",
        "kind": "cycle_counts",
        "platform": "rust-for-linux",
        "passed": True,
        "claim_grade": True,
        "complete_for_claim": True,
        "sample_count": len(cycle_records),
        "cycle_count_samples": [record["cycle_count"] for record in cycle_records],
        "cycle_sample_records": cycle_records,
        "allocator_stats_samples": [
            {
                "source_marker": evaluate.RUST_FOR_LINUX_ALLOCATOR_STATS_MARKER,
                "semantic_bridge_dealloc_ok": 1,
                "allocator_total_allocations": 4,
                "allocator_total_deallocations": 4,
                "allocator_typed_allocations": 1,
                "allocator_typed_deallocations": 1,
                "allocator_fallback_allocations": 3,
                "allocator_fallback_deallocations": 3,
                "allocator_total_allocated_bytes": 4096,
                "allocator_typed_allocated_bytes": 64,
                "allocator_fallback_allocated_bytes": 4032,
                "allocator_coverage_basis_points": 2500,
                "allocator_type_stats_rows": 1,
                "allocator_type_stats_dropped_events": 0,
                "allocator_type_stats_probe_matched": 1,
            }
        ],
        "constrained_boot_sample_records": boot_records,
        "source_artifact": "/artifacts/rfl-kernel-run.log",
        "source_artifact_sha256": "0" * 64,
        "evidence_source_fingerprint": evaluate.repository_source_fingerprint(),
        "measurement_scope": (
            "Rust-for-Linux kernel module cycle-count samples plus checked UniAlloc "
            "allocator stats and constrained boot sample from an external kernel build/run."
        ),
    }


class PlatformEvidenceGateTests(unittest.TestCase):
    def test_repository_source_fingerprint_is_content_scoped_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "unialloc/src").mkdir(parents=True)
            (root / "docs").mkdir()
            (root / "evaluation/results").mkdir(parents=True)
            (root / "evaluation/scripts").mkdir(parents=True)
            (root / "evaluation/external/fixture").mkdir(parents=True)
            (root / ".cargo").mkdir()
            source = root / "unialloc/src/lib.rs"
            source.write_text("pub fn value() -> u32 { 1 }\n", encoding="utf-8")
            (root / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
            (root / "docs/note.md").write_text("one\n", encoding="utf-8")
            (root / "evaluation/results/result.json").write_text("{}\n", encoding="utf-8")
            test_script = root / "evaluation/scripts/test_gate.py"
            test_script.write_text("assert True\n", encoding="utf-8")
            (root / ".gitignore").write_text(
                "evaluation/external/*/workload_config.local.json\n",
                encoding="utf-8",
            )
            local_config = root / "evaluation/external/fixture/workload_config.local.json"
            local_config.write_text('{"configured": false}\n', encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)

            baseline = evaluate.repository_source_fingerprint(root)
            (root / "docs/note.md").write_text("two\n", encoding="utf-8")
            (root / "evaluation/results/result.json").write_text('{"new": true}\n', encoding="utf-8")
            self.assertEqual(
                baseline["source_digest"],
                evaluate.repository_source_fingerprint(root)["source_digest"],
            )
            test_script.write_text("assert False\n", encoding="utf-8")
            self.assertEqual(
                baseline["source_digest"],
                evaluate.repository_source_fingerprint(root)["source_digest"],
            )
            local_config.write_text('{"configured": true}\n', encoding="utf-8")
            self.assertNotEqual(
                baseline["source_digest"],
                evaluate.repository_source_fingerprint(root)["source_digest"],
            )
            local_config.write_text('{"configured": false}\n', encoding="utf-8")

            source.chmod(0o664)
            self.assertEqual(
                baseline["source_digest"],
                evaluate.repository_source_fingerprint(root)["source_digest"],
            )
            source.chmod(0o775)
            self.assertNotEqual(
                baseline["source_digest"],
                evaluate.repository_source_fingerprint(root)["source_digest"],
            )
            source.chmod(0o644)
            (root / ".cargo/config.toml").write_text("[build]\nrustflags=[]\n", encoding="utf-8")
            self.assertNotEqual(
                baseline["source_digest"],
                evaluate.repository_source_fingerprint(root)["source_digest"],
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(RuntimeError, "git source inventory"):
                evaluate.repository_source_fingerprint(pathlib.Path(tmpdir))

    def test_cached_current_claim_report_requires_matching_source_fingerprint(self) -> None:
        current = {
            "schema_version": evaluate.REPOSITORY_SOURCE_FINGERPRINT_SCHEMA_VERSION,
            "algorithm": "sha256",
            "source_digest": "1" * 64,
        }
        report = {
            "source": "current",
            "current_repository_source_fingerprint": dict(current),
        }
        self.assertEqual(
            evaluate.claim_check_report_refresh_blockers(
                report,
                "current",
                current=current,
            ),
            [],
        )

        stale = dict(current)
        stale["source_digest"] = "2" * 64
        report["current_repository_source_fingerprint"] = stale
        self.assertIn(
            "does not match the current working tree",
            " ".join(
                evaluate.claim_check_report_refresh_blockers(
                    report,
                    "current",
                    current=current,
                )
            ),
        )

        self.assertEqual(
            evaluate.claim_check_report_refresh_blockers(
                {"source": "paper"},
                "paper",
                current=current,
            ),
            [],
        )

    def test_remote_platform_uris_are_not_treated_as_verified_local_evidence(self) -> None:
        fingerprint = evaluate.repository_source_fingerprint()
        entry = {
            "target": "Windows x86_64 allocator workload",
            "passed": True,
            "claim_grade": True,
            "complete_for_claim": True,
            "host": {"system": "Windows"},
            "target_triple": "x86_64-pc-windows-msvc",
            "rust_target": "x86_64-pc-windows-msvc",
            "platform_target": "Windows 11 target host",
            "os_version": "Windows 11",
            "target_metadata": {"runtime": "native"},
            "evidence_source_fingerprint": fingerprint,
            "evidence": [
                {
                    "uri": "https://example.invalid/windows/build.log",
                    "kind": "build_log",
                    "sha256": "1" * 64,
                },
                {
                    "uri": "https://example.invalid/windows/run.json",
                    "kind": "run_summary",
                    "sha256": "2" * 64,
                },
                {
                    "uri": "https://example.invalid/windows/performance.json",
                    "kind": "performance_data",
                    "sha256": "3" * 64,
                },
            ],
        }

        audit = evaluate.audit_platform_entry(
            "windows",
            entry,
            ROOT,
            current_fingerprint=fingerprint,
        )

        self.assertFalse(audit["ready_for_claim_grade_import"])
        self.assertEqual(audit["verified_evidence"], [])
        self.assertEqual(len(audit["missing_evidence"]), 3)
        self.assertIn("https://example.invalid/windows/build.log", audit["missing_evidence"])

    def test_reusable_platform_entry_is_freshly_hash_audited(self) -> None:
        fingerprint = evaluate.repository_source_fingerprint()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            build_log = tmp / "windows-build.log"
            run_summary = tmp / "windows-run.json"
            performance = tmp / "windows-performance.json"
            build_log.write_text(
                "cargo build --target x86_64-pc-windows-msvc\nFinished release build\n",
                encoding="utf-8",
            )
            run_summary.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "claim_grade": True,
                        "complete_for_claim": True,
                        "command_executed": True,
                        "runtime_samples": 2,
                    }
                ),
                encoding="utf-8",
            )
            performance.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "claim_grade": True,
                        "complete_for_claim": True,
                        "sample_count": 2,
                        "metric": "ns_per_iter",
                        "samples": [100, 101],
                    }
                ),
                encoding="utf-8",
            )
            entry = {
                "target": "Windows x86_64 allocator workload",
                "passed": True,
                "claim_grade": True,
                "complete_for_claim": True,
                "host": {"system": "Windows"},
                "target_triple": "x86_64-pc-windows-msvc",
                "rust_target": "x86_64-pc-windows-msvc",
                "platform_target": "Windows 11 target host",
                "os_version": "Windows 11",
                "target_metadata": {"runtime": "native"},
                "evidence_source_fingerprint": fingerprint,
                "evidence": [
                    {
                        "path": build_log.name,
                        "kind": "build_log",
                        "sha256": evaluate.file_sha256(build_log),
                    },
                    {
                        "path": run_summary.name,
                        "kind": "run_summary",
                        "sha256": evaluate.file_sha256(run_summary),
                    },
                    {
                        "path": performance.name,
                        "kind": "performance_data",
                        "sha256": evaluate.file_sha256(performance),
                    },
                ],
                "evidence_verified": True,
                "platform_preflight_ready_for_claim_grade": True,
                "verified_evidence": [
                    str(build_log),
                    str(run_summary),
                    str(performance),
                ],
            }

            self.assertTrue(
                evaluate.reusable_platform_entry_is_claim_ready(
                    entry,
                    platform_name="windows",
                    source_base=tmp,
                    current_fingerprint=fingerprint,
                )
            )
            build_log.write_text("tampered\n", encoding="utf-8")
            self.assertFalse(
                evaluate.reusable_platform_entry_is_claim_ready(
                    entry,
                    platform_name="windows",
                    source_base=tmp,
                    current_fingerprint=fingerprint,
                )
            )

            build_log.write_text(
                "cargo build --target x86_64-pc-windows-msvc\nFinished release build\n",
                encoding="utf-8",
            )
            entry["evidence"][0].pop("sha256")
            self.assertFalse(
                evaluate.reusable_platform_entry_is_claim_ready(
                    entry,
                    platform_name="windows",
                    source_base=tmp,
                    current_fingerprint=fingerprint,
                )
            )

    def test_coverage_source_gate_requires_every_essential_artifact(self) -> None:
        current = evaluate.repository_source_fingerprint()
        stale = dict(current)
        stale["source_digest"] = "0" * 64
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            compiler = tmp / "compiler.json"
            runtime = tmp / "runtime.json"
            compiler.write_text(
                json.dumps({"evidence_source_fingerprint": stale}),
                encoding="utf-8",
            )
            runtime.write_text(
                json.dumps({"evidence_source_fingerprint": current}),
                encoding="utf-8",
            )
            result = {
                "status": "pass",
                "metric": "coverage_percent",
                "evidence": [str(compiler), str(runtime)],
                "source_artifacts": [str(compiler), str(runtime)],
            }
            gated = evaluate.apply_claim_repository_source_binding(
                result,
                {},
                current=current,
            )
        self.assertEqual(gated["status"], "missing")
        self.assertFalse(gated["repository_source_binding"]["ready"])

    def test_constrained_boot_gate_rejects_dropped_type_stats_events(self) -> None:
        marker = valid_constrained_boot_marker("blogos").replace(
            "allocator_type_stats_dropped_events=0",
            "allocator_type_stats_dropped_events=3",
        )
        records = evaluate.extract_constrained_boot_sample_records_from_text(
            marker,
            platform_name="blogos",
        )
        blockers = evaluate.constrained_boot_sample_blockers(
            records,
            platform_name="blogos",
        )
        self.assertIn("dropped 3 semantic type-stats events", " | ".join(blockers))

    def test_imported_constrained_boot_log_accepts_target_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            boot_log = tmp / "blogos-boot.log"
            boot_log.write_text(
                textwrap.dedent(
                    f"""
                    BlogOS target image booting UniAlloc fixed heap
                    unialloc allocator initialized
                    BOOT_OK
                    {valid_constrained_boot_provenance_marker("blogos")}
                    {valid_constrained_boot_marker("blogos")}
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            record, log_path, passed, blockers = evaluate.collect_imported_constrained_boot_log(
                out_dir=tmp,
                platform_name="blogos",
                display_name="BlogOS",
                artifact_path=boot_log,
                success_pattern="BOOT_OK",
                required_log_markers=["blogos", "unialloc"],
            )

            self.assertTrue(passed, blockers)
            self.assertEqual(blockers, [])
            self.assertTrue(record["passed"])
            self.assertEqual(record["boot_sample_record_count"], 1)
            self.assertEqual(record["boot_provenance_blockers"], [])
            self.assertEqual(record["boot_provenance_record_blockers"], [])
            self.assertTrue(log_path.exists())

    def test_imported_constrained_boot_log_requires_current_source_fingerprint(self) -> None:
        current = source_fingerprint_fixture("a")
        stale = source_fingerprint_fixture("b")
        malformed = {
            "schema_version": evaluate.REPOSITORY_SOURCE_FINGERPRINT_SCHEMA_VERSION,
            "algorithm": "sha256",
            "source_digest": "not-a-64-hex-digest",
        }
        for platform_name in ("blogos", "redox"):
            cases = [
                ("missing", [], False),
                ("matching", [current], True),
                ("stale", [stale], False),
                ("malformed", [malformed], False),
                ("conflicting", [current, stale], False),
            ]
            for case_name, fingerprints, expected_passed in cases:
                with self.subTest(platform=platform_name, case=case_name):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        tmp = pathlib.Path(tmpdir)
                        provenance_markers = [
                            valid_constrained_boot_provenance_marker(
                                platform_name,
                                source_fingerprint=fingerprint,
                            )
                            for fingerprint in fingerprints
                        ]
                        if not fingerprints:
                            provenance_markers = [
                                valid_constrained_boot_provenance_marker(
                                    platform_name,
                                    include_source_fingerprint=False,
                                )
                            ]
                        boot_log = tmp / f"{platform_name}-{case_name}-boot.log"
                        boot_log.write_text(
                            "\n".join(
                                [
                                    f"{platform_name} target image booting UniAlloc fixed heap",
                                    "UNIALLOC_BOOT_OK",
                                    *provenance_markers,
                                    valid_constrained_boot_marker(platform_name),
                                ]
                            )
                            + "\n",
                            encoding="utf-8",
                        )
                        with mock.patch.object(
                            evaluate,
                            "repository_source_fingerprint",
                            return_value=current,
                        ):
                            record, _log_path, passed, blockers = (
                                evaluate.collect_imported_constrained_boot_log(
                                    out_dir=tmp,
                                    platform_name=platform_name,
                                    display_name=platform_name.title(),
                                    artifact_path=boot_log,
                                    success_pattern="UNIALLOC_BOOT_OK",
                                    required_log_markers=[platform_name, "unialloc"],
                                )
                            )
                    self.assertEqual(passed, expected_passed, blockers)
                    if expected_passed:
                        self.assertEqual(
                            record.get("evidence_source_fingerprint"),
                            current,
                        )
                    else:
                        self.assertIn("fingerprint", " | ".join(blockers).lower())

    def test_imported_rust_for_linux_cycle_counts_require_current_source_fingerprint(self) -> None:
        current = source_fingerprint_fixture("c")
        stale = source_fingerprint_fixture("d")
        malformed = {
            "schema_version": evaluate.REPOSITORY_SOURCE_FINGERPRINT_SCHEMA_VERSION,
            "algorithm": "sha256",
            "source_digest": "invalid",
        }
        cases = [
            ("missing", None, False),
            ("matching", current, True),
            ("stale", stale, False),
            ("malformed", malformed, False),
        ]
        for case_name, source_fingerprint, expected_ready in cases:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmp = pathlib.Path(tmpdir)
                    cycle_counts = valid_rust_for_linux_cycle_counts()
                    if source_fingerprint is None:
                        cycle_counts.pop("evidence_source_fingerprint", None)
                    else:
                        cycle_counts["evidence_source_fingerprint"] = source_fingerprint
                    cycle_path = tmp / f"rfl-cycle-counts-{case_name}.json"
                    cycle_path.write_text(
                        json.dumps(cycle_counts, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    with mock.patch.object(
                        evaluate,
                        "repository_source_fingerprint",
                        return_value=current,
                    ):
                        blockers = evaluate.platform_evidence_content_blockers(
                            "cycle_counts",
                            cycle_path,
                            platform_name="rust-for-linux",
                        )
                self.assertEqual(not blockers, expected_ready, blockers)
                if not expected_ready:
                    self.assertIn("fingerprint", " | ".join(blockers).lower())

    def test_imported_constrained_boot_log_rejects_missing_boot_provenance_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            boot_log = tmp / "blogos-boot-missing-provenance.log"
            boot_log.write_text(
                textwrap.dedent(
                    f"""
                    BlogOS target image booting UniAlloc fixed heap
                    unialloc allocator initialized
                    BOOT_OK
                    {valid_constrained_boot_marker("blogos")}
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            record, _log_path, passed, blockers = evaluate.collect_imported_constrained_boot_log(
                out_dir=tmp,
                platform_name="blogos",
                display_name="BlogOS",
                artifact_path=boot_log,
                success_pattern="BOOT_OK",
                required_log_markers=["blogos", "unialloc"],
            )

            self.assertFalse(passed)
            self.assertFalse(record["passed"])
            self.assertEqual(record["boot_sample_blockers"], [])
            self.assertIn("constrained boot provenance marker", " ".join(blockers))

    def test_boot_provenance_hashes_match_local_image_and_boot_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            image = tmp / "blogos.img"
            boot_config = tmp / "blogos-boot.toml"
            image.write_bytes(b"B" * (64 * 1024))
            boot_config.write_text("[boot]\nserial=true\n", encoding="utf-8")
            image_hash = evaluate.file_sha256(image)
            boot_config_hash = evaluate.file_sha256(boot_config)
            record = {
                "source_marker": evaluate.CONSTRAINED_BOOT_PROVENANCE_MARKER,
                "platform": "blogos",
                "image_sha256": image_hash,
                "boot_config_sha256": boot_config_hash,
                "emulator": "qemu-system-x86_64",
                "emulator_version": "8.2.0",
            }

            self.assertEqual(
                evaluate.constrained_boot_provenance_artifact_hash_blockers(
                    [record],
                    platform_name="blogos",
                    image_status=evaluate.local_path_status(str(image)),
                    boot_config_status=evaluate.optional_boot_config_status(str(boot_config)),
                ),
                [],
            )
            bad_record = {**record, "image_sha256": "0" * 64}
            blockers = evaluate.constrained_boot_provenance_artifact_hash_blockers(
                [bad_record],
                platform_name="blogos",
                image_status=evaluate.local_path_status(str(image)),
                boot_config_status=evaluate.optional_boot_config_status(str(boot_config)),
            )
            self.assertIn("image_sha256 does not match", " ".join(blockers))

    def test_boot_transcript_validation_accepts_hash_bound_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            image = tmp / "redox.img"
            boot_config = tmp / "redox-boot.toml"
            image.write_bytes(b"redox image bytes\n")
            boot_config.write_text("[boot]\nserial=true\n", encoding="utf-8")
            transcript = "\n".join(
                [
                    "redox target image booting UniAlloc fixed heap",
                    "unialloc allocator initialized",
                    "BOOT_OK",
                    constrained_boot_provenance_marker_for_artifacts(
                        "redox",
                        image,
                        boot_config,
                    ),
                    valid_constrained_boot_marker("redox"),
                ]
            )

            validation = evaluate.validate_constrained_boot_transcript_evidence(
                transcript,
                platform_name="redox",
                image_status=evaluate.local_path_status(str(image)),
                boot_config_status=evaluate.optional_boot_config_status(str(boot_config)),
            )

            self.assertEqual(validation["blockers"], [])
            self.assertEqual(validation["boot_sample_record_count"], 1)
            self.assertEqual(validation["boot_provenance_record_count"], 1)

    def test_boot_transcript_validation_rejects_missing_provenance_for_local_boot(self) -> None:
        transcript = "\n".join(
            [
                "redox target image booting UniAlloc fixed heap",
                "unialloc allocator initialized",
                "BOOT_OK",
                valid_constrained_boot_marker("redox"),
            ]
        )

        validation = evaluate.validate_constrained_boot_transcript_evidence(
            transcript,
            platform_name="redox",
        )

        self.assertEqual(validation["boot_sample_blockers"], [])
        self.assertIn(
            "constrained boot provenance marker",
            " ".join(validation["blockers"]),
        )

    def test_boot_transcript_validation_rejects_mismatched_artifact_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            image = tmp / "redox.img"
            boot_config = tmp / "redox-boot.toml"
            image.write_bytes(b"redox image bytes\n")
            boot_config.write_text("[boot]\nserial=true\n", encoding="utf-8")
            transcript = "\n".join(
                [
                    "redox target image booting UniAlloc fixed heap",
                    "unialloc allocator initialized",
                    "BOOT_OK",
                    valid_constrained_boot_provenance_marker("redox"),
                    valid_constrained_boot_marker("redox"),
                ]
            )

            validation = evaluate.validate_constrained_boot_transcript_evidence(
                transcript,
                platform_name="redox",
                image_status=evaluate.local_path_status(str(image)),
                boot_config_status=evaluate.optional_boot_config_status(str(boot_config)),
            )

            joined = " ".join(validation["blockers"])
            self.assertIn("image_sha256 does not match", joined)
            self.assertIn("boot_config_sha256 does not match", joined)

    def test_imported_constrained_boot_log_rejects_mismatched_local_artifact_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            image = tmp / "redox.img"
            boot_config = tmp / "redox-boot.toml"
            image.write_bytes(b"redox image bytes\n")
            boot_config.write_text("[boot]\nserial=true\n", encoding="utf-8")
            boot_log = tmp / "redox-boot.log"
            boot_log.write_text(
                "\n".join(
                    [
                        "redox target image booting UniAlloc fixed heap",
                        "unialloc allocator initialized",
                        "BOOT_OK",
                        valid_constrained_boot_provenance_marker("redox"),
                        valid_constrained_boot_marker("redox"),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            record, _log_path, passed, blockers = evaluate.collect_imported_constrained_boot_log(
                out_dir=tmp,
                platform_name="redox",
                display_name="Redox",
                artifact_path=boot_log,
                success_pattern="BOOT_OK",
                required_log_markers=["redox", "unialloc"],
                image_status=evaluate.local_path_status(str(image)),
                boot_config_status=evaluate.optional_boot_config_status(str(boot_config)),
            )

        self.assertFalse(passed)
        self.assertFalse(record["passed"])
        joined = " ".join(blockers)
        self.assertIn("image_sha256 does not match", joined)
        self.assertIn("boot_config_sha256 does not match", joined)
        self.assertIn("image_sha256 does not match", " ".join(record["boot_provenance_artifact_hash_blockers"]))
        self.assertIn("boot_config_sha256 does not match", " ".join(record["boot_provenance_artifact_hash_blockers"]))

    def test_emit_constrained_boot_provenance_marker_is_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            image = tmp / "redox.img"
            boot_config = tmp / "redox-boot.toml"
            out_dir = tmp / "out"
            image.write_bytes(b"redox image bytes\n")
            boot_config.write_text("[boot]\nserial=true\n", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                rc = evaluate.emit_constrained_boot_provenance_marker(
                    types.SimpleNamespace(
                        platform="redox",
                        image=str(image),
                        emulator="/bin/echo",
                        boot_config=str(boot_config),
                        emulator_version="8.2.0",
                        timeout=1,
                        run_id=None,
                        output_dir=str(out_dir),
                    )
                )

            self.assertEqual(rc, 0)
            result = json.loads((out_dir / "redox-boot-provenance-marker.json").read_text())
            marker = result["marker"]
            self.assertIn(evaluate.CONSTRAINED_BOOT_PROVENANCE_MARKER, marker)
            record = evaluate.parse_constrained_boot_provenance_marker(marker)
            self.assertIsNotNone(record)
            self.assertEqual(
                evaluate.constrained_boot_provenance_record_blockers(
                    [record],
                    platform_name="redox",
                ),
                [],
            )
            self.assertEqual(
                evaluate.constrained_boot_provenance_artifact_hash_blockers(
                    [record],
                    platform_name="redox",
                    image_status=evaluate.local_path_status(str(image)),
                    boot_config_status=evaluate.optional_boot_config_status(str(boot_config)),
                ),
                [],
            )

    def test_file_uri_metadata_must_resolve_to_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            image = tmp / "blogos.img"
            image.write_bytes(b"image\n")

            ok = evaluate.local_path_status(image.as_uri())
            self.assertTrue(ok["usable"], ok)
            self.assertEqual(ok["kind"], "file_uri")
            self.assertEqual(ok["resolved"], str(image))

            missing = evaluate.local_path_status("file://blogos-provenance-fixture.img")
            self.assertFalse(missing["usable"], missing)
            self.assertEqual(missing["kind"], "file_uri")

    def test_imported_constrained_boot_log_rejects_host_small_heap_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            boot_log = tmp / "host-smoke-misfiled-as-blogos.log"
            boot_log.write_text(
                textwrap.dedent(
                    f"""
                    label: blogos-fixed-heap-small-heap
                    BlogOS target image booting UniAlloc fixed heap
                    unialloc allocator initialized
                    BOOT_OK
                    {valid_constrained_boot_marker("blogos")}
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            record, _log_path, passed, blockers = evaluate.collect_imported_constrained_boot_log(
                out_dir=tmp,
                platform_name="blogos",
                display_name="BlogOS",
                artifact_path=boot_log,
                success_pattern="BOOT_OK",
                required_log_markers=["blogos", "unialloc"],
            )

            self.assertFalse(passed)
            self.assertFalse(record["passed"])
            self.assertEqual(record["boot_sample_blockers"], [])
            self.assertTrue(record["boot_provenance_blockers"])
            self.assertIn("host fixed-heap small_heap smoke provenance", " ".join(blockers))

    def test_imported_constrained_boot_log_rejects_fallback_only_marker(self) -> None:
        marker = (
            f"{evaluate.CONSTRAINED_BOOT_SAMPLE_MARKER} "
            "platform=blogos "
            "boot_cycle=1 "
            "fixed_heap_ready=true "
            "c_abi_invoked=true "
            "c_abi_ready_after_init=true "
            "c_abi_round_trips=2 "
            "c_abi_invalid_layouts_rejected=3 "
            "c_abi_over_page_alignment_checked=true "
            "c_abi_over_page_alignment=8192 "
            "allocator_total_allocations=4 "
            "allocator_total_deallocations=4 "
            "allocator_typed_allocations=0 "
            "allocator_typed_deallocations=0 "
            "allocator_fallback_allocations=4 "
            "allocator_fallback_deallocations=4 "
            "allocator_total_allocated_bytes=4096 "
            "allocator_typed_allocated_bytes=0 "
            "allocator_fallback_allocated_bytes=4096 "
            "allocator_coverage_basis_points=0 "
            "allocator_type_stats_rows=0 "
            "allocator_type_stats_dropped_events=0 "
            "allocator_type_stats_probe_matched=false "
            "c_abi_probe_passed=true"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            boot_log = tmp / "blogos-fallback-only-boot.log"
            boot_log.write_text(
                "\n".join(
                    [
                        "BlogOS target image booting UniAlloc fixed heap",
                        "unialloc allocator initialized",
                        "BOOT_OK",
                        marker,
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            record, _log_path, passed, blockers = evaluate.collect_imported_constrained_boot_log(
                out_dir=tmp,
                platform_name="blogos",
                display_name="BlogOS",
                artifact_path=boot_log,
                success_pattern="BOOT_OK",
                required_log_markers=["blogos", "unialloc"],
            )

            self.assertFalse(passed)
            self.assertFalse(record["passed"])
            joined = " ".join(blockers)
            self.assertIn("no positive typed semantic allocation counter", joined)
            self.assertIn("no positive semantic type-stats rows", joined)
            self.assertIn("semantic type-stats probe row matched", joined)

    def test_imported_constrained_boot_log_rejects_failed_c_abi_probe_flag(self) -> None:
        marker = valid_constrained_boot_marker("redox").replace(
            "c_abi_probe_passed=true",
            "c_abi_probe_passed=false",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            boot_log = tmp / "redox-failed-cabi-boot.log"
            boot_log.write_text(
                "\n".join(
                    [
                        "redox target image booting UniAlloc fixed heap",
                        "unialloc allocator initialized",
                        "BOOT_OK",
                        marker,
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            record, _log_path, passed, blockers = evaluate.collect_imported_constrained_boot_log(
                out_dir=tmp,
                platform_name="redox",
                display_name="Redox",
                artifact_path=boot_log,
                success_pattern="BOOT_OK",
                required_log_markers=["redox", "unialloc"],
            )

            self.assertFalse(passed)
            self.assertFalse(record["passed"])
            self.assertIn("c_abi_probe_passed=true", " ".join(blockers))

    def test_imported_constrained_boot_log_rejects_missing_c_abi_probe_flag(self) -> None:
        marker = valid_constrained_boot_marker("redox").replace(
            " c_abi_probe_passed=true",
            "",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            boot_log = tmp / "redox-missing-cabi-flag-boot.log"
            boot_log.write_text(
                "\n".join(
                    [
                        "redox target image booting UniAlloc fixed heap",
                        "unialloc allocator initialized",
                        "BOOT_OK",
                        marker,
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            record, _log_path, passed, blockers = evaluate.collect_imported_constrained_boot_log(
                out_dir=tmp,
                platform_name="redox",
                display_name="Redox",
                artifact_path=boot_log,
                success_pattern="BOOT_OK",
                required_log_markers=["redox", "unialloc"],
            )

            self.assertFalse(passed)
            self.assertFalse(record["passed"])
            self.assertIn("did not report c_abi_probe_passed=true", " ".join(blockers))

    def test_provenance_gate_rejects_structured_host_marker(self) -> None:
        transcript = "\n".join(
            [
                "redox unialloc BOOT_OK",
                valid_constrained_boot_marker("redox"),
                valid_constrained_boot_marker("host-fixed-heap"),
            ]
        )

        blockers = evaluate.constrained_boot_transcript_provenance_blockers(
            transcript,
            platform_name="redox",
        )

        self.assertTrue(blockers)
        self.assertIn("host fixed-heap small_heap smoke provenance", blockers[0])

    def test_platform_matrix_audit_rejects_host_small_heap_log_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            build_log = tmp / "blogos-build.log"
            build_log.write_text("BlogOS UniAlloc build complete\n", encoding="utf-8")
            boot_log = tmp / "blogos-boot.log"
            boot_log.write_text(
                textwrap.dedent(
                    f"""
                    label: blogos-fixed-heap-small-heap
                    BlogOS target image booting UniAlloc fixed heap
                    unialloc allocator initialized
                    BOOT_OK
                    {valid_constrained_boot_marker("blogos")}
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = {
                "claims": [
                    {
                        "id": "C007-cross-platform-retargeting",
                        "platforms": ["blogos"],
                    }
                ]
            }
            matrix = {
                "blogos": {
                    "target": "BlogOS target image boot-cycle validation",
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "image": "blogos.img",
                    "target_triple": "x86_64-blogos-none",
                    "platform_target": "blogos",
                    "emulator": "qemu-system-x86_64",
                    "boot_config": "blogos-boot.toml",
                    "target_metadata": {"boot": "fixture"},
                    "evidence": [
                        {
                            "path": build_log.name,
                            "kind": "build_log",
                            "sha256": evaluate.file_sha256(build_log),
                        },
                        {
                            "path": boot_log.name,
                            "kind": "boot_cycles",
                            "sha256": evaluate.file_sha256(boot_log),
                        },
                    ],
                }
            }

            audit = evaluate.build_platform_matrix_audit(
                cfg,
                tmp / "platform-matrix.generated.json",
                matrix,
            )

        blogos_audit = audit["platforms"]["blogos"]
        self.assertFalse(blogos_audit["ready_for_claim_grade_import"])
        self.assertIn("evidence content blockers present: 1", blogos_audit["blockers"])
        self.assertIn(
            "host fixed-heap small_heap smoke provenance",
            " ".join(blogos_audit["evidence_content_blockers"]),
        )

    def test_platform_matrix_audit_accepts_structured_boot_json_with_host_diagnostic_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            build_log = tmp / "blogos-build.log"
            image = tmp / "blogos.img"
            boot_config = tmp / "blogos-boot.toml"
            image.write_bytes(b"B" * (64 * 1024))
            boot_config.write_text("[boot]\nserial=true\n", encoding="utf-8")
            build_log.write_text("BlogOS UniAlloc build complete\n", encoding="utf-8")
            boot_records = evaluate.extract_constrained_boot_sample_records_from_text(
                valid_constrained_boot_marker("blogos"),
                platform_name="blogos",
            )
            provenance_records = evaluate.extract_constrained_boot_provenance_records_from_text(
                constrained_boot_provenance_marker_for_artifacts(
                    "blogos",
                    image,
                    boot_config,
                ),
            )
            boot_cycles = tmp / "blogos-boot-cycles.json"
            boot_cycles.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "blogos-platform-boot-cycle",
                        "kind": "boot_cycles",
                        "platform": "blogos",
                        "passed": True,
                        "claim_grade": True,
                        "complete_for_claim": True,
                        "boot_cycle_count": 1,
                        "boot_sample_records": boot_records,
                        "boot_sample_record_count": len(boot_records),
                        "boot_provenance_records": provenance_records,
                        "boot_provenance_record_count": len(provenance_records),
                        "boot_provenance_blockers": [],
                        "boot_provenance_record_blockers": [],
                        "image": str(image),
                        "image_status": evaluate.local_path_status(str(image)),
                        "boot_config": str(boot_config),
                        "boot_config_status": evaluate.optional_boot_config_status(str(boot_config)),
                        "host_fixed_heap_workload": {
                            "command": ["cargo", "run", "--example", "small_heap"],
                            "parsed_event": {"label": "host-fixed-heap"},
                            "log": "blogos-fixed-heap-small_heap.log",
                        },
                        "measurement_scope": "BlogOS constrained heap target image boot-cycle validated.",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = {
                "claims": [
                    {
                        "id": "C007-cross-platform-retargeting",
                        "platforms": ["blogos"],
                    }
                ]
            }
            matrix = {
                "blogos": {
                    "target": "BlogOS target image boot-cycle validation",
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "image": str(image),
                    "target_triple": "x86_64-blogos-none",
                    "platform_target": "blogos",
                    "emulator": "qemu-system-x86_64",
                    "boot_config": str(boot_config),
                    "target_metadata": {
                        "boot": "validated-target",
                        "image_status": evaluate.local_path_status(str(image)),
                        "boot_config_status": evaluate.optional_boot_config_status(str(boot_config)),
                    },
                    "evidence": [
                        {
                            "path": build_log.name,
                            "kind": "build_log",
                            "sha256": evaluate.file_sha256(build_log),
                        },
                        {
                            "path": boot_cycles.name,
                            "kind": "boot_cycles",
                            "sha256": evaluate.file_sha256(boot_cycles),
                        },
                    ],
                }
            }

            audit = evaluate.build_platform_matrix_audit(
                cfg,
                tmp / "platform-matrix.generated.json",
                source_bind_platform_matrix(matrix),
            )

        blogos_audit = audit["platforms"]["blogos"]
        self.assertTrue(blogos_audit["ready_for_claim_grade_import"], blogos_audit)
        self.assertEqual(blogos_audit["evidence_content_blockers"], [])

    def test_boot_emulator_recognition_keeps_arbitrary_docker_uri_blocked(self) -> None:
        status = evaluate.executable_status("docker://busybox:latest")

        self.assertFalse(evaluate.recognized_boot_emulator("docker://busybox:latest", status))

    def test_platform_matrix_audit_accepts_redox_docker_redoxer_uri_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            build_log = tmp / "redox-build.log"
            image = tmp / "redox.img"
            boot_config = tmp / "redox-boot.toml"
            image.write_bytes(b"R" * (64 * 1024))
            boot_config.write_text("[boot]\nserial=true\n", encoding="utf-8")
            build_log.write_text("Redox UniAlloc build complete\n", encoding="utf-8")
            boot_records = evaluate.extract_constrained_boot_sample_records_from_text(
                valid_constrained_boot_marker("redox"),
                platform_name="redox",
            )
            provenance_records = evaluate.extract_constrained_boot_provenance_records_from_text(
                constrained_boot_provenance_marker_for_artifacts(
                    "redox",
                    image,
                    boot_config,
                ),
            )
            boot_cycles = tmp / "redox-boot-cycles.json"
            boot_cycles.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "redox-platform-boot-cycle",
                        "kind": "boot_cycles",
                        "platform": "redox",
                        "passed": True,
                        "claim_grade": True,
                        "complete_for_claim": True,
                        "boot_cycle_count": 1,
                        "boot_sample_records": boot_records,
                        "boot_sample_record_count": len(boot_records),
                        "boot_provenance_records": provenance_records,
                        "boot_provenance_record_count": len(provenance_records),
                        "boot_provenance_blockers": [],
                        "boot_provenance_record_blockers": [],
                        "image": str(image),
                        "boot_config": str(boot_config),
                        "measurement_scope": "Redox constrained heap target image boot-cycle validated through Docker/redoxer.",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = {
                "claims": [
                    {
                        "id": "C007-cross-platform-retargeting",
                        "platforms": ["redox"],
                    }
                ]
            }
            matrix = {
                "redox": {
                    "target": "Redox constrained heap target image boot-cycle validation",
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "image": str(image),
                    "target_triple": "x86_64-unknown-redox",
                    "platform_target": "Redox constrained heap target image",
                    "emulator": "docker://redoxos/redoxer:latest",
                    "boot_config": str(boot_config),
                    "target_metadata": {
                        "boot": "validated-target",
                        "runner": "redoxer/QEMU through Docker",
                    },
                    "evidence": [
                        {
                            "path": build_log.name,
                            "kind": "build_log",
                            "sha256": evaluate.file_sha256(build_log),
                        },
                        {
                            "path": boot_cycles.name,
                            "kind": "boot_cycles",
                            "sha256": evaluate.file_sha256(boot_cycles),
                        },
                    ],
                }
            }

            audit = evaluate.build_platform_matrix_audit(
                cfg,
                tmp / "platform-matrix.generated.json",
                source_bind_platform_matrix(matrix),
            )

        redox_audit = audit["platforms"]["redox"]
        self.assertTrue(redox_audit["ready_for_claim_grade_import"], redox_audit)
        self.assertEqual(redox_audit["metadata_integrity_blockers"], [])

    def test_platform_matrix_audit_rejects_imported_boot_json_without_provenance_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            build_log = tmp / "blogos-build.log"
            build_log.write_text("BlogOS UniAlloc build complete\n", encoding="utf-8")
            boot_records = evaluate.extract_constrained_boot_sample_records_from_text(
                valid_constrained_boot_marker("blogos"),
                platform_name="blogos",
            )
            boot_cycles = tmp / "blogos-boot-cycles.json"
            boot_cycles.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "blogos-platform-boot-cycle",
                        "kind": "boot_cycles",
                        "platform": "blogos",
                        "passed": True,
                        "claim_grade": True,
                        "complete_for_claim": True,
                        "boot_cycle_count": 1,
                        "boot_evidence_source": "external-import",
                        "boot_log_import_requested": True,
                        "boot_sample_records": boot_records,
                        "boot_sample_record_count": len(boot_records),
                        "boot_provenance_blockers": [],
                        "boot_provenance_record_blockers": [],
                        "measurement_scope": "BlogOS constrained heap target image boot-cycle validated.",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = {
                "claims": [
                    {
                        "id": "C007-cross-platform-retargeting",
                        "platforms": ["blogos"],
                    }
                ]
            }
            matrix = {
                "blogos": {
                    "target": "BlogOS target image boot-cycle validation",
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "image": "blogos.img",
                    "target_triple": "x86_64-blogos-none",
                    "platform_target": "blogos",
                    "emulator": "qemu-system-x86_64",
                    "boot_config": "blogos-boot.toml",
                    "target_metadata": {"boot": "fixture"},
                    "evidence": [
                        {
                            "path": build_log.name,
                            "kind": "build_log",
                            "sha256": evaluate.file_sha256(build_log),
                        },
                        {
                            "path": boot_cycles.name,
                            "kind": "boot_cycles",
                            "sha256": evaluate.file_sha256(boot_cycles),
                        },
                    ],
                }
            }

            audit = evaluate.build_platform_matrix_audit(
                cfg,
                tmp / "platform-matrix.generated.json",
                matrix,
            )

        blogos_audit = audit["platforms"]["blogos"]
        self.assertFalse(blogos_audit["ready_for_claim_grade_import"])
        self.assertIn(
            "constrained boot provenance marker",
            " ".join(blogos_audit["evidence_content_blockers"]),
        )

    def test_platform_matrix_audit_ignores_diagnostic_host_smoke_evidence_for_claim_grade(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            build_log = tmp / "blogos-build.log"
            image = tmp / "blogos.img"
            boot_config = tmp / "blogos-boot.toml"
            image.write_bytes(b"B" * (64 * 1024))
            boot_config.write_text("[boot]\nserial=true\n", encoding="utf-8")
            build_log.write_text("BlogOS UniAlloc build complete\n", encoding="utf-8")
            host_smoke = tmp / "blogos-fixed-heap-small-heap.log"
            host_smoke.write_text(
                "label: blogos-fixed-heap-small-heap\nhost-fixed-heap small_heap passed\n",
                encoding="utf-8",
            )
            boot_records = evaluate.extract_constrained_boot_sample_records_from_text(
                valid_constrained_boot_marker("blogos"),
                platform_name="blogos",
            )
            provenance_records = evaluate.extract_constrained_boot_provenance_records_from_text(
                constrained_boot_provenance_marker_for_artifacts(
                    "blogos",
                    image,
                    boot_config,
                ),
            )
            boot_cycles = tmp / "blogos-boot-cycles.json"
            boot_cycles.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "blogos-platform-boot-cycle",
                        "kind": "boot_cycles",
                        "platform": "blogos",
                        "passed": True,
                        "claim_grade": True,
                        "complete_for_claim": True,
                        "boot_cycle_count": 1,
                        "boot_sample_records": boot_records,
                        "boot_sample_record_count": len(boot_records),
                        "boot_provenance_records": provenance_records,
                        "boot_provenance_record_count": len(provenance_records),
                        "boot_provenance_blockers": [],
                        "boot_provenance_record_blockers": [],
                        "image": str(image),
                        "image_status": evaluate.local_path_status(str(image)),
                        "boot_config": str(boot_config),
                        "boot_config_status": evaluate.optional_boot_config_status(str(boot_config)),
                        "measurement_scope": "BlogOS constrained heap target image boot-cycle validated.",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = {
                "claims": [
                    {
                        "id": "C007-cross-platform-retargeting",
                        "platforms": ["blogos"],
                    }
                ]
            }
            matrix = {
                "blogos": {
                    "target": "BlogOS target image boot-cycle validation",
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "image": str(image),
                    "target_triple": "x86_64-blogos-none",
                    "platform_target": "blogos",
                    "emulator": "qemu-system-x86_64",
                    "boot_config": str(boot_config),
                    "target_metadata": {
                        "boot": "validated-target",
                        "image_status": evaluate.local_path_status(str(image)),
                        "boot_config_status": evaluate.optional_boot_config_status(str(boot_config)),
                    },
                    "evidence": [
                        {
                            "path": build_log.name,
                            "kind": "build_log",
                            "sha256": evaluate.file_sha256(build_log),
                        },
                        {
                            "path": host_smoke.name,
                            "kind": "run_summary",
                            "diagnostic_only": True,
                            "evidence_role": "diagnostic",
                            "sha256": evaluate.file_sha256(host_smoke),
                        },
                        {
                            "path": boot_cycles.name,
                            "kind": "boot_cycles",
                            "sha256": evaluate.file_sha256(boot_cycles),
                        },
                    ],
                }
            }

            audit = evaluate.build_platform_matrix_audit(
                cfg,
                tmp / "platform-matrix.generated.json",
                source_bind_platform_matrix(matrix),
            )

        blogos_audit = audit["platforms"]["blogos"]
        self.assertTrue(blogos_audit["ready_for_claim_grade_import"], blogos_audit)
        self.assertEqual(blogos_audit["evidence_content_blockers"], [])
        diagnostic_records = [
            record for record in blogos_audit["evidence_records"] if record.get("diagnostic_only")
        ]
        self.assertEqual(len(diagnostic_records), 1)
        self.assertNotIn(str(host_smoke), blogos_audit["verified_evidence"])

    def test_platform_matrix_audit_accepts_structured_rust_for_linux_cycle_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            kernel_log = tmp / "rfl-kernel-build.log"
            kernel_log.write_text(valid_rust_for_linux_kernel_build_log() + "\n", encoding="utf-8")
            cycle_counts = tmp / "rfl-cycle-counts.json"
            cycle_counts.write_text(
                json.dumps(valid_rust_for_linux_cycle_counts(), sort_keys=True) + "\n",
                encoding="utf-8",
            )
            cfg = {
                "claims": [
                    {
                        "id": "C007-cross-platform-retargeting",
                        "platforms": ["rust-for-linux"],
                    }
                ]
            }
            matrix = {
                "rust-for-linux": {
                    "target": "Rust-for-Linux kernel module build/cycle-count validation",
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "kernel_version": "6.9.0-rfl-validated",
                    "kernel_tree": "/src/linux-rfl",
                    "kernel_config": "rust.config",
                    "target_triple": "aarch64-unknown-none",
                    "platform_target": "rust-for-linux",
                    "target_metadata": {"kernel": "validated-target"},
                    "evidence": [
                        {
                            "path": kernel_log.name,
                            "kind": "kernel_build_log",
                            "sha256": evaluate.file_sha256(kernel_log),
                        },
                        {
                            "path": cycle_counts.name,
                            "kind": "cycle_counts",
                            "sha256": evaluate.file_sha256(cycle_counts),
                        },
                    ],
                }
            }

            audit = evaluate.build_platform_matrix_audit(
                cfg,
                tmp / "platform-matrix.generated.json",
                source_bind_platform_matrix(matrix),
            )

        rfl_audit = audit["platforms"]["rust-for-linux"]
        self.assertTrue(rfl_audit["ready_for_claim_grade_import"], rfl_audit)
        self.assertEqual(rfl_audit["evidence_content_blockers"], [])

    def test_platform_matrix_audit_rejects_fake_rust_for_linux_kernel_build_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            kernel_log = tmp / "rfl-kernel-build.log"
            kernel_log.write_text(
                "BUILD SUCCESS\nrust_bench test fixture passed\n",
                encoding="utf-8",
            )
            cycle_counts = tmp / "rfl-cycle-counts.json"
            cycle_counts.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "rust-for-linux-kernel-cycle-counts",
                        "platform": "rust-for-linux",
                        "passed": True,
                        "claim_grade": True,
                        "complete_for_claim": True,
                        "sample_count": 1,
                        "cycle_count_samples": [1000],
                        "allocator_stats_samples": [
                            {
                                "allocator_total_allocations": 4,
                                "allocator_total_deallocations": 4,
                                "allocator_typed_allocations": 1,
                                "allocator_typed_deallocations": 1,
                                "allocator_total_allocated_bytes": 4096,
                                "allocator_type_stats_rows": 1,
                                "allocator_type_stats_dropped_events": 0,
                                "allocator_type_stats_probe_matched": 1,
                            }
                        ],
                        "kernel_log": valid_constrained_boot_marker("rust-for-linux"),
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = {
                "claims": [
                    {
                        "id": "C007-cross-platform-retargeting",
                        "platforms": ["rust-for-linux"],
                    }
                ]
            }
            matrix = {
                "rust-for-linux": {
                    "target": "Rust-for-Linux kernel module build/cycle-count validation",
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "kernel_version": "6.9.0-rfl-fixture",
                    "kernel_tree": "/src/linux-rfl",
                    "kernel_config": "rust.config",
                    "target_triple": "aarch64-unknown-none",
                    "platform_target": "rust-for-linux",
                    "target_metadata": {"kernel": "fixture"},
                    "evidence": [
                        {
                            "path": kernel_log.name,
                            "kind": "kernel_build_log",
                            "sha256": evaluate.file_sha256(kernel_log),
                        },
                        {
                            "path": cycle_counts.name,
                            "kind": "cycle_counts",
                            "sha256": evaluate.file_sha256(cycle_counts),
                        },
                    ],
                }
            }

            audit = evaluate.build_platform_matrix_audit(
                cfg,
                tmp / "platform-matrix.generated.json",
                matrix,
            )

        rfl_audit = audit["platforms"]["rust-for-linux"]
        self.assertFalse(rfl_audit["ready_for_claim_grade_import"])
        self.assertTrue(
            any(blocker.startswith("evidence content blockers present:") for blocker in rfl_audit["blockers"]),
            rfl_audit["blockers"],
        )
        joined = " ".join(rfl_audit["evidence_content_blockers"])
        self.assertIn("lacks Linux kernel/Kbuild provenance markers", joined)
        self.assertIn("lacks rust_bench kernel module compile/link markers", joined)
        self.assertIn(f"no structured {evaluate.RUST_FOR_LINUX_CYCLE_SAMPLE_MARKER} records", joined)
        self.assertIn(evaluate.RUST_FOR_LINUX_ALLOCATOR_STATS_MARKER, joined)

    def test_platform_matrix_audit_uses_source_matrix_base_for_imported_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            raw_dir = tmp / "raw" / "rfl-run"
            raw_dir.mkdir(parents=True)
            results_dir = tmp / "results"
            results_dir.mkdir()
            raw_matrix = raw_dir / "platform-matrix.generated.json"
            raw_matrix.write_text("{}\n", encoding="utf-8")
            kernel_log = raw_dir / "rfl-kernel-build.log"
            kernel_log.write_text(
                "BUILD SUCCESS\nrust_bench test fixture passed\n",
                encoding="utf-8",
            )
            cfg = {
                "claims": [
                    {
                        "id": "C007-cross-platform-retargeting",
                        "platforms": ["rust-for-linux"],
                    }
                ]
            }
            matrix = {
                "_meta": {"source_matrix": str(raw_matrix)},
                "rust-for-linux": {
                    "target": "Rust-for-Linux kernel module build/cycle-count validation",
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "kernel_version": "6.9.0-rfl-fixture",
                    "kernel_tree": "/src/linux-rfl",
                    "kernel_config": "rust.config",
                    "target_triple": "aarch64-unknown-none",
                    "platform_target": "rust-for-linux",
                    "target_metadata": {"kernel": "fixture"},
                    "evidence": [
                        {
                            "path": kernel_log.name,
                            "kind": "kernel_build_log",
                            "sha256": evaluate.file_sha256(kernel_log),
                        }
                    ],
                },
            }

            audit = evaluate.build_platform_matrix_audit(
                cfg,
                results_dir / "platform_matrix.json",
                matrix,
            )

        rfl_audit = audit["platforms"]["rust-for-linux"]
        self.assertIn(str(kernel_log.resolve()), rfl_audit["verified_evidence"])
        self.assertIn(
            "lacks Linux kernel/Kbuild provenance markers",
            " ".join(rfl_audit["evidence_content_blockers"]),
        )

    def test_validate_constrained_boot_log_matrix_uses_preserved_image_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            source_dir = tmp / "source"
            out_dir = tmp / "out"
            source_dir.mkdir()
            image = source_dir / "blogos.img"
            boot_config = source_dir / "blogos-boot.toml"
            build_log = source_dir / "blogos-build.log"
            boot_log = source_dir / "blogos-boot.log"
            matrix_out = out_dir / "platform-matrix.generated.json"

            image.write_bytes(b"B" * (64 * 1024))
            boot_config.write_text("[boot]\nserial=true\n", encoding="utf-8")
            build_log.write_text("BlogOS UniAlloc target image build complete\n", encoding="utf-8")
            emulator = source_dir / "qemu-system-x86_64"
            emulator.write_text("#!/bin/sh\necho 'QEMU emulator version 8.2.0'\n", encoding="utf-8")
            emulator.chmod(0o755)
            boot_log.write_text(
                "\n".join(
                    [
                        "BlogOS target image booting UniAlloc fixed heap",
                        "unialloc allocator initialized",
                        "BOOT_OK",
                        constrained_boot_provenance_marker_for_artifacts(
                            "blogos",
                            image,
                            boot_config,
                        ),
                        valid_constrained_boot_marker("blogos"),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                rc = evaluate.validate_constrained_boot_log(
                    types.SimpleNamespace(
                        platform="blogos",
                        boot_log=str(boot_log),
                        image=image.as_uri(),
                        emulator=str(emulator),
                        boot_config=str(boot_config),
                        build_log=str(build_log),
                        target_triple="x86_64-blogos-none",
                        boot_success_pattern="BOOT_OK",
                        boot_log_required_marker=["blogos", "unialloc"],
                        run_id="blogos-preserved-artifacts-test",
                        output_dir=str(out_dir),
                        matrix_entry_out=None,
                        matrix_out=str(matrix_out),
                    )
                )

            self.assertEqual(rc, 0)
            matrix = json.loads(matrix_out.read_text(encoding="utf-8"))
            entry = matrix["blogos"]
            preserved_image = pathlib.Path(entry["image"])
            preserved_boot_config = pathlib.Path(entry["boot_config"])
            self.assertTrue(preserved_image.exists(), entry)
            self.assertTrue(preserved_boot_config.exists(), entry)
            self.assertNotEqual(preserved_image, image)
            self.assertNotEqual(preserved_boot_config, boot_config)
            self.assertTrue(str(preserved_image).startswith(str(out_dir)))
            self.assertTrue(str(preserved_boot_config).startswith(str(out_dir)))

            image.unlink()
            boot_config.unlink()
            cfg = {
                "claims": [
                    {
                        "id": "C007-cross-platform-retargeting",
                        "platforms": ["blogos"],
                    }
                ]
            }
            unbound_audit = evaluate.build_platform_matrix_audit(
                cfg,
                matrix_out,
                {"blogos": entry},
            )
            audit = evaluate.build_platform_matrix_audit(
                cfg,
                matrix_out,
                source_bind_platform_matrix({"blogos": entry}),
            )

        blogos_audit = audit["platforms"]["blogos"]
        self.assertFalse(
            unbound_audit["platforms"]["blogos"]["ready_for_claim_grade_import"]
        )
        self.assertIn(
            "missing evidence_source_fingerprint",
            " ".join(unbound_audit["platforms"]["blogos"]["blockers"]),
        )
        self.assertTrue(blogos_audit["ready_for_claim_grade_import"], blogos_audit)
        self.assertEqual(blogos_audit["evidence_content_blockers"], [])


if __name__ == "__main__":
    unittest.main()
