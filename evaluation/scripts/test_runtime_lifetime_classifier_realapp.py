#!/usr/bin/env python3
"""Unit tests for the marker-free real-application evaluation wrapper."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "evaluation" / "scripts"
SCRIPT = SCRIPTS / "runtime_lifetime_classifier_realapp.py"

sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("runtime_lifetime_classifier_realapp", SCRIPT)
assert spec is not None and spec.loader is not None
wrapper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wrapper
spec.loader.exec_module(wrapper)


def valid_stats(**overrides: int | bool) -> dict[str, int | bool]:
    stats: dict[str, int | bool] = {field: 0 for field in wrapper.RUNTIME_REQUIRED_FIELDS}
    stats["all_mappings_released"] = True
    stats.update(overrides)
    return stats


class RuntimeLifetimeClassifierRealAppTests(unittest.TestCase):
    def setUp(self) -> None:
        wrapper._RUNTIME_MODE = "adaptive"

    def test_adaptive_variants_add_only_lifetime_hugepage_feature(self) -> None:
        _dependency, performance = wrapper.adaptive_dependency_for_variant("typeiso_perf")
        _dependency, coverage = wrapper.adaptive_dependency_for_variant("typeiso_coverage")
        baseline_dependency, baseline_features = wrapper.adaptive_dependency_for_variant("unialloc")

        self.assertEqual(performance, ("type_isolation", "lifetime_hugepage"))
        self.assertEqual(coverage, ("stats", "type_isolation", "lifetime_hugepage"))
        self.assertEqual(
            (baseline_dependency, baseline_features),
            wrapper._ORIGINAL_DEPENDENCY_FOR_VARIANT("unialloc"),
        )

    def test_injected_sources_enable_adaptive_thp_before_workload(self) -> None:
        performance = wrapper.adaptive_allocator_source("typeiso_perf")
        coverage = wrapper.adaptive_allocator_source("typeiso_coverage")

        for source in (performance, coverage):
            self.assertEqual(source.count("AdaptiveRuntimeHugepage"), 1)
            self.assertEqual(source.count("TransparentHugepage"), 1)
            self.assertEqual(source.count(".init_array"), 1)
        self.assertNotIn(wrapper.RUNTIME_STATS_PREFIX, performance)
        self.assertEqual(coverage.count(wrapper.RUNTIME_STATS_PREFIX), 1)
        self.assertLess(
            coverage.index("lifetime_hugepage_configure_with_backend"),
            coverage.index("semantic_stats_recording_enable"),
        )

    def test_generated_coverage_source_is_valid_rust_syntax(self) -> None:
        source = wrapper.adaptive_allocator_source("typeiso_coverage")
        with tempfile.NamedTemporaryFile(suffix=".rs") as rust_source:
            rust_source.write(source.encode("utf-8"))
            rust_source.flush()
            result = subprocess.run(
                ["rustfmt", "--edition", "2024", "--emit", "stdout", rust_source.name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_off_mode_preserves_features_and_explicitly_disables_policy(self) -> None:
        wrapper._RUNTIME_MODE = "off"
        _dependency, features = wrapper.adaptive_dependency_for_variant("typeiso_perf")
        source = wrapper.adaptive_allocator_source("typeiso_perf")

        self.assertEqual(features, ("type_isolation", "lifetime_hugepage"))
        self.assertIn("LifetimeHugepagePolicy::Disabled", source)
        self.assertNotIn("AdaptiveRuntimeHugepage", source)

    def test_runtime_parser_uses_last_valid_record(self) -> None:
        first = valid_stats(policy=5, adaptive_long_observations=1)
        second = valid_stats(policy=5, adaptive_long_observations=2)
        stderr = "\n".join(
            (
                wrapper.RUNTIME_STATS_PREFIX + json.dumps(first),
                f"{wrapper.RUNTIME_STATS_PREFIX}not-json",
                wrapper.RUNTIME_STATS_PREFIX + json.dumps(second),
            )
        )
        self.assertEqual(wrapper.parse_runtime_stats(stderr), second)
        self.assertIsNone(wrapper.parse_runtime_stats("ordinary stderr"))
        self.assertIsNone(wrapper.parse_runtime_stats(wrapper.RUNTIME_STATS_PREFIX + "{}"))

    def test_aggregate_excludes_warmups_and_sums_event_counters(self) -> None:
        rows = [
            {
                "warmup": True,
                "runtime_lifetime_stats": {"adaptive_long_observations": 100},
            },
            {
                "warmup": False,
                "runtime_lifetime_stats": {
                    "adaptive_long_observations": 2,
                    "predictor_tp": 1,
                },
            },
            {
                "warmup": False,
                "runtime_lifetime_stats": {
                    "adaptive_long_observations": 3,
                    "predictor_tp": 2,
                },
            },
        ]
        aggregate = wrapper.aggregate_runtime_stats(rows)

        assert aggregate is not None
        self.assertEqual(aggregate["measured_runs"], 2)
        self.assertEqual(aggregate["totals"]["adaptive_long_observations"], 5)
        self.assertEqual(aggregate["totals"]["predictor_tp"], 3)
        self.assertEqual(len(aggregate["per_run"]), 2)

    def test_attach_preserves_per_run_stats_and_adds_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw_dir = pathlib.Path(temporary)
            run_dir = raw_dir / "runs" / "ripgrep" / "round-00" / "typeiso_coverage"
            run_dir.mkdir(parents=True)
            stats = valid_stats(
                policy=5,
                adaptive_site_count=1,
                adaptive_long_sites=1,
                adaptive_eligible_allocations=4,
                adaptive_long_routed_allocations=4,
                adaptive_long_observations=4,
                adaptive_decisive_observation_bytes=256,
                predictor_tp=3,
                static_hint_abstained=4,
                static_hint_abstained_bytes=256,
                phase_advances=0,
            )
            (run_dir / "stderr.bin").write_text(
                wrapper.RUNTIME_STATS_PREFIX + json.dumps(stats) + "\n",
                encoding="utf-8",
            )
            result = {
                "measurements": [
                    {
                        "app": "ripgrep",
                        "variant": "typeiso_coverage",
                        "round": 0,
                        "warmup": False,
                    }
                ],
                "summaries": [
                    {"app": "ripgrep", "variant": "typeiso_coverage"}
                ],
                "variants": ["typeiso_coverage"],
            }
            (raw_dir / "results.json").write_text(json.dumps(result), encoding="utf-8")

            result_path = wrapper.attach_runtime_stats(raw_dir)
            attached = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(attached["measurements"][0]["runtime_lifetime_stats"], stats)
        aggregate = attached["summaries"][0]["runtime_lifetime_stats"]
        self.assertEqual(aggregate["totals"]["adaptive_long_observations"], 4)
        self.assertFalse(
            attached["runtime_lifetime_classifier"]["semantic_epoch_markers_required"]
        )
        self.assertTrue(attached["runtime_lifetime_classifier"]["enabled"])

    def test_mode_parser_and_digest_distinguish_feature_parity_arms(self) -> None:
        mode, cleaned = wrapper.extract_runtime_mode(
            ["--runtime-lifetime-mode=off", "--raw-dir", "/tmp/evidence"]
        )
        self.assertEqual(mode, "off")
        self.assertEqual(cleaned, ["--raw-dir", "/tmp/evidence"])
        wrapper._RUNTIME_MODE = "off"
        off_digest = wrapper.runtime_implementation_digest()
        wrapper._RUNTIME_MODE = "adaptive"
        adaptive_digest = wrapper.runtime_implementation_digest()
        self.assertNotEqual(off_digest, adaptive_digest)

    def test_runtime_accounting_validation_rejects_inconsistent_evidence(self) -> None:
        stats = valid_stats(
            policy=5,
            adaptive_site_count=2,
            adaptive_cold_sites=1,
            adaptive_eligible_allocations=4,
            adaptive_training_allocations=3,
        )
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_stats(stats, "adaptive")

        stats["adaptive_site_count"] = 1
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_stats(stats, "adaptive")

        stats["adaptive_eligible_allocations"] = 3
        stats["adaptive_short_observations"] = 3
        stats["static_hint_abstained"] = 3
        wrapper.validate_runtime_stats(stats, "adaptive")

        stats["mapping_failures"] = 1
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_stats(stats, "adaptive")
        stats["mapping_failures"] = 0

        stats["adaptive_short_observations"] = 2
        stats["static_hint_abstained"] = 2
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_stats(stats, "adaptive")

    def test_attach_rejects_missing_coverage_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw_dir = pathlib.Path(temporary)
            run_dir = raw_dir / "runs" / "fd" / "round-00" / "typeiso_coverage"
            run_dir.mkdir(parents=True)
            (run_dir / "stderr.bin").write_text("no telemetry\n", encoding="utf-8")
            (raw_dir / "results.json").write_text(
                json.dumps(
                    {
                        "measurements": [
                            {
                                "app": "fd",
                                "variant": "typeiso_coverage",
                                "round": 0,
                                "warmup": False,
                            }
                        ],
                        "summaries": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(wrapper.matrix.MatrixError):
                wrapper.attach_runtime_stats(raw_dir)


if __name__ == "__main__":
    unittest.main()
