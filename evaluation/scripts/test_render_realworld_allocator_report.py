#!/usr/bin/env python3
"""Unit tests for the real-world allocator report renderer."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "render_realworld_allocator_report.py"

spec = importlib.util.spec_from_file_location("render_realworld_allocator_report", SCRIPT)
assert spec is not None and spec.loader is not None
renderer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = renderer
spec.loader.exec_module(renderer)


def summary(app: str, variant: str, multiplier: float) -> dict[str, object]:
    row: dict[str, object] = {
        "app": app,
        "variant": variant,
        "sample_count": 3,
        "median_wall_seconds": 0.1 * multiplier,
        "wall_mad_seconds": 0,
        "min_wall_seconds": 0.1 * multiplier,
        "max_wall_seconds": 0.1 * multiplier,
        "median_user_cpu_seconds": 0.07 * multiplier,
        "median_system_cpu_seconds": 0.01 * multiplier,
        "median_total_cpu_seconds": 0.08 * multiplier,
        "median_cpu_percent": 80,
        "median_peak_rss_kib": 4096 * multiplier,
        "min_peak_rss_kib": 4096 * multiplier,
        "max_peak_rss_kib": 4096 * multiplier,
        "rss_mad_kib": 0,
        "median_major_page_faults": 0,
        "median_minor_page_faults": 100 * multiplier,
        "median_involuntary_context_switches": multiplier,
        "median_voluntary_context_switches": 2 * multiplier,
        "median_throughput_per_second": 320 * (1 / multiplier),
        "throughput_unit": "MiB/s",
        "output_sha256": "output-hash",
        "performance_eligible": True,
    }
    if variant == "unialloc":
        row["paired_wall_ratio_vs_mimalloc"] = 0.95
        row["paired_rss_ratio_vs_mimalloc"] = 0.95
        row["paired_wall_ratio_vs_tcmalloc"] = 0.95 / 0.99
        row["paired_rss_ratio_vs_tcmalloc"] = 0.95 / 0.99
    if variant == "typeiso_perf":
        row["paired_wall_ratio_vs_typed_plain"] = 1.04 / 1.02
        row["paired_rss_ratio_vs_typed_plain"] = 1.04 / 1.02
    return row


def campaign(app: str, *, success: bool = True, apps: list[str] | None = None) -> dict[str, object]:
    variants = {
        "system": 1.01,
        "jemalloc": 1.01,
        "unialloc": 0.95,
        "mimalloc": 1.00,
        "tcmalloc": 0.99,
        "typed_plain": 1.02,
        "typeiso_perf": 1.04,
    }
    measurements = []
    for variant, multiplier in variants.items():
        for round_index in range(5):
            warmup = round_index < 2
            measurements.append(
                {
                    "app": app,
                    "variant": variant,
                    "round": round_index,
                    "warmup": warmup,
                    "measurement_index": None if warmup else round_index - 2,
                    "command": [f"/tmp/{variant}", "--threads", "1"],
                    "measured_command": [
                        "taskset",
                        "-c",
                        "2",
                        f"/tmp/{variant}",
                    ],
                    "exit_code": 0,
                    "gnu_time_exit_status": 0,
                    "timed_out": False,
                    "wall_seconds": 0.1 * multiplier,
                    "user_cpu_seconds": 0.07 * multiplier,
                    "system_cpu_seconds": 0.01 * multiplier,
                    "cpu_percent": 80,
                    "peak_rss_kib": 4096 * multiplier,
                    "major_page_faults": 0,
                    "minor_page_faults": 100 * multiplier,
                    "involuntary_context_switches": multiplier,
                    "voluntary_context_switches": 2 * multiplier,
                    "stdout_sha256": "stdout-hash",
                    "stderr_sha256": "stderr-hash",
                    "output_sha256": "output-hash",
                    "work_amount": 32 * 1024 * 1024,
                    "work_unit": "bytes",
                }
            )
    builds = []
    for variant in variants:
        build = {
            "app": app,
            "variant": variant,
            "source_head": "source-commit",
            "implementation_sha256": "implementation-hash",
            "allocator_route": "injected",
            "binary": f"/tmp/{variant}",
            "binary_sha256": f"{variant}-binary-hash",
            "command": ["cargo", "build", "--release", "--bin", app],
            "success": True,
            "build_exit_code": 0,
            "build_timed_out": False,
            "raw_field_marker": f"preserved-{variant}",
        }
        if variant == "tcmalloc":
            build["allocator_route"] = "system-api-ld-preload"
            build["binary"] = "/tmp/system"
            build["binary_sha256"] = "system-binary-hash"
            build["preload_proof"] = {
                "success": True,
                "maps_contains_library": True,
            }
        if variant == "mimalloc":
            build["allocator_provenance"] = {
                "wrapper_package": {"version": "0.1.25"},
                "sys_package": {"version": "0.1.49"},
                "core_generation": "v3",
                "core_version_number": 30302,
                "secure_feature_enabled": False,
            }
        builds.append(build)
    return {
        "schema_version": 2,
        "source": "unialloc-realworld-type-isolation-matrix",
        "success": success,
        "quick": False,
        "warmups": 2,
        "repetitions": 3,
        "apps": apps if apps is not None else [app],
        "variants": list(variants),
        "toolchain": "nightly-test",
        "implementation_sha256": "implementation-hash",
        "pass_source_sha256": "pass-hash",
        "glibc_rseq_mode": "libc_default",
        "host": {"cpu_model": "Synthetic CPU", "logical_cpu_count": 8},
        "measurement_affinity": {
            "cpu_list": "2",
            "numa_node": 0,
            "command_prefix": ["taskset", "-c", "2"],
        },
        "checkouts": {
            app: {"head": "source-commit", "path": f"/tmp/{app}", "status": ""}
        },
        "corpus": {
            "file_count": 32,
            "file_bytes": 1024 * 1024,
            "tree_sha256": "corpus-tree-hash",
        },
        "fd_tree": {
            "file_count": 32 * 1024 * 1024,
            "tree_sha256": "fd-tree-hash",
        },
        "wrapper": {"path": "/tmp/wrapper", "sha256": "wrapper-hash"},
        "sysroot": "/tmp/sysroot",
        "tcmalloc_runtime": {
            "label": "gperftools-tcmalloc",
            "library": "/usr/lib/libtcmalloc.so",
            "library_sha256": "tcmalloc-library-hash",
        },
        "workloads": {
            app: {
                "description": "synthetic single-thread scan",
                "work_amount": 32 * 1024 * 1024,
                "work_unit": "bytes",
            }
        },
        "builds": builds,
        "measurements": measurements,
        "summaries": [summary(app, variant, multiplier) for variant, multiplier in variants.items()],
    }


class RenderRealworldAllocatorReportTests(unittest.TestCase):
    def test_cli_renders_repeated_campaigns_and_preserves_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            inputs: dict[str, pathlib.Path] = {}
            source_documents: dict[str, dict[str, object]] = {}
            for app in ("ripgrep", "fd"):
                document = campaign(app)
                path = root / f"{app}.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                inputs[app] = path
                source_documents[app] = document
            markdown_path = root / "report.md"
            json_path = root / "evidence.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--campaign",
                    f"ripgrep={inputs['ripgrep']}",
                    "--campaign",
                    f"fd={inputs['fd']}",
                    "--markdown-out",
                    str(markdown_path),
                    "--json-out",
                    str(json_path),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual([row["app"] for row in evidence["campaigns"]], ["ripgrep", "fd"])
            self.assertEqual(len(evidence["aggregate_direct_comparisons"]), 3)
            self.assertAlmostEqual(
                evidence["aggregate_direct_comparisons"][0]["geomean_wall_ratio"],
                0.95,
            )
            for rendered, app in zip(evidence["campaigns"], ("ripgrep", "fd"), strict=True):
                self.assertEqual(rendered["measurements"], source_documents[app]["measurements"])
                self.assertEqual(len(rendered["direct_comparisons"]), 3)
                rendered_builds = rendered["provenance"]["builds"]
                self.assertEqual(len(rendered_builds), len(source_documents[app]["builds"]))
                self.assertEqual(
                    [row["raw_field_marker"] for row in rendered_builds],
                    [row["raw_field_marker"] for row in source_documents[app]["builds"]],
                )
                self.assertEqual(
                    rendered["provenance"]["source"]["corpus"],
                    source_documents[app]["corpus"],
                )
            unialloc_vs_mimalloc = evidence["campaigns"][0]["direct_comparisons"][0]
            self.assertEqual(unialloc_vs_mimalloc["ratio_method"]["wall"], "paired median")
            self.assertAlmostEqual(unialloc_vs_mimalloc["wall_delta_percent"], -5.0)
            self.assertAlmostEqual(
                unialloc_vs_mimalloc["throughput_ratio"], 1.0 / 0.95
            )
            self.assertEqual(
                unialloc_vs_mimalloc["ratio_method"]["throughput"],
                "inverse of paired wall median",
            )

            markdown = markdown_path.read_text(encoding="utf-8")
            for expected in (
                "## ripgrep",
                "## fd",
                "Wall MAD",
                "Peak RSS median [range]",
                "UniAlloc",
                "mimalloc",
                "TCMalloc",
                "Type Isolation",
                "Provenance and controls",
                "Cross-application comparison",
                "Interpretation boundaries",
                "## Headline",
                "peak-RSS geomean",
                "## Same-host reproduction",
                "realworld_type_isolation_matrix.py",
                "--toolchain nightly-test",
                "## Profile priority",
                "base typed compiler/runtime path",
                "tcmalloc-library-hash",
                "--gperftools-legacy-library /usr/lib/libtcmalloc.so",
                "gperftools_legacy",
                "Output-equivalence SHA-256",
                "Workload input SHA-256",
            ):
                self.assertIn(expected, markdown)

    def test_reproduction_command_uses_modern_google_tcmalloc_prefix(self) -> None:
        document = campaign("ripgrep")
        document["tcmalloc_runtime"] = {
            "variant": "tcmalloc",
            "label": "google-tcmalloc-modern-hpaa-adaptive-subrelease",
            "library": "/opt/unialloc-tcmalloc/lib/libunialloc_google_tcmalloc.so",
            "library_sha256": "modern-library-hash",
            "identity": {
                "allocator_family": "google/tcmalloc",
                "variant": "modern-hpaa-adaptive-subrelease",
                "provenance_path": "/opt/unialloc-tcmalloc/google-tcmalloc-provenance.json",
            },
        }
        command = renderer.reproduction_command(
            {
                "app": "ripgrep",
                "configuration": {
                    "variants": list(document["variants"]),
                    "quick": False,
                    "warmups": 2,
                    "repetitions": 3,
                    "toolchain": "nightly-test",
                    "run_output_retained": True,
                },
                "provenance": {
                    "measurement_affinity": document["measurement_affinity"],
                    "tcmalloc_runtime": document["tcmalloc_runtime"],
                    "builds": document["builds"],
                },
                "workload": document["workloads"]["ripgrep"],
                "input_path": "/tmp/results.json",
            }
        )
        self.assertIn(
            "--google-tcmalloc-prefix /opt/unialloc-tcmalloc", command
        )
        self.assertIn("tcmalloc", command)
        self.assertNotIn("gperftools_legacy", command)
        self.assertNotIn("--tcmalloc-library", command)

    def test_modern_report_requires_target_process_identity_markers(self) -> None:
        document = campaign("ripgrep")
        revision = "12f255231938d30493186b0a037feedd70f5a1c1"
        document["tcmalloc_runtime"] = {
            "variant": "tcmalloc",
            "label": "google-tcmalloc-modern-hpaa-adaptive-subrelease",
            "library": "/opt/tcmalloc/lib/libunialloc_google_tcmalloc.so",
            "library_sha256": "modern-library-hash",
            "identity": {
                "allocator_family": "google/tcmalloc",
                "variant": "modern-hpaa-adaptive-subrelease",
                "upstream_revision": revision,
                "provenance_path": "/opt/tcmalloc/google-tcmalloc-provenance.json",
            },
            "runtime_requirements": {
                "revision": revision,
                "hpaa_active": 1,
                "malloc_provider_is_self": 1,
                "exact_mapped_path": "/opt/tcmalloc/lib/libunialloc_google_tcmalloc.so",
            },
        }
        for row in document["measurements"]:
            expected = 1 if row["variant"] == "tcmalloc" else 0
            row["google_tcmalloc_identity_marker_count"] = expected
            row["google_tcmalloc_target_identity_verified"] = True
        tcmalloc = next(
            row for row in document["builds"] if row["variant"] == "tcmalloc"
        )
        tcmalloc["preload_proof"].update(
            {
                "artifact_preflight_only": True,
                "target_binary_sha256": tcmalloc["binary_sha256"],
                "runtime_identity": {
                    "revision": revision,
                    "hpaa_active": 1,
                    "malloc_provider_is_self": 1,
                    "exact_library_mapped": True,
                },
            }
        )

        renderer.validate_campaign_measurements(document, "ripgrep")
        renderer.build_provenance(document)

        modern_row = next(
            row for row in document["measurements"] if row["variant"] == "tcmalloc"
        )
        modern_row["google_tcmalloc_identity_marker_count"] = 0
        modern_row["google_tcmalloc_target_identity_verified"] = False
        with self.assertRaisesRegex(renderer.ReportError, "target-bound"):
            renderer.validate_campaign_measurements(document, "ripgrep")

    def test_rejects_unsuccessful_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "failed.json"
            path.write_text(json.dumps(campaign("ripgrep", success=False)), encoding="utf-8")
            with self.assertRaisesRegex(renderer.ReportError, "not successful"):
                renderer.load_campaign("ripgrep", path)

    def test_rejects_multi_application_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "multi.json"
            path.write_text(
                json.dumps(campaign("ripgrep", apps=["ripgrep", "fd"])),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(renderer.ReportError, "exactly one matching app"):
                renderer.load_campaign("ripgrep", path)

    def test_rejects_missing_comparator_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "missing.json"
            document = campaign("ripgrep")
            document["summaries"] = [
                row for row in document["summaries"] if row["variant"] != "tcmalloc"
            ]
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(renderer.ReportError, "tcmalloc"):
                renderer.load_campaign("ripgrep", path)

    def test_rejects_nonidentical_system_and_tcmalloc_binaries(self) -> None:
        document = campaign("ripgrep")
        tcmalloc = next(
            row for row in document["builds"] if row["variant"] == "tcmalloc"
        )
        tcmalloc["binary_sha256"] = "different-binary-hash"
        with self.assertRaisesRegex(renderer.ReportError, "not identical"):
            renderer.build_evidence([document])

    def test_rejects_tcmalloc_without_system_provenance(self) -> None:
        document = campaign("ripgrep")
        document["variants"] = [
            variant for variant in document["variants"] if variant != "system"
        ]
        for field in ("builds", "measurements", "summaries"):
            document[field] = [
                row for row in document[field] if row["variant"] != "system"
            ]
        with self.assertRaisesRegex(renderer.ReportError, "requires a System build"):
            renderer.build_evidence([document])

    def test_rejects_non_equivalent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "output-mismatch.json"
            document = campaign("ripgrep")
            document["measurements"][0]["output_sha256"] = "different-output"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(renderer.ReportError, "not equivalent"):
                renderer.load_campaign("ripgrep", path)

    def test_rejects_stale_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "stale-summary.json"
            document = campaign("ripgrep")
            document["summaries"][0]["median_wall_seconds"] = 99
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(renderer.ReportError, "raw measurements"):
                renderer.load_campaign("ripgrep", path)


if __name__ == "__main__":
    unittest.main()
