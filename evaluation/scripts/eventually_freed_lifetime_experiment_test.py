import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eventually_freed_lifetime_experiment as experiment


ROOT = Path(__file__).resolve().parents[2]


def row(
    arm: str,
    repeat: int,
    *,
    work_ns: int,
    peak_rss_kib: int,
    pss_kib: int,
    anon_huge_kib: int = 0,
    checksum: int = 99,
) -> dict:
    long_objects = 512
    setup_short_objects = long_objects
    short_objects = 256
    waves = 4
    expected = long_objects + setup_short_objects + short_objects * waves
    system = arm == "system" or arm == "policy-off" or arm.startswith("external-")
    selective = arm == "selective-thp"
    adaptive = arm == "adaptive-thp"
    return {
        "schema_version": 2,
        "source": "eventually_freed_lifetime_workload",
        "arm": "system" if system else arm,
        "experiment_arm": arm,
        "repeat": repeat,
        "wall_time_ns": work_ns + 10_000,
        "work_elapsed_ns": work_ns,
        "checksum": checksum,
        "long_objects": long_objects,
        "short_objects_per_wave": short_objects,
        "object_bytes": 4096,
        "waves": waves,
        "passes_per_wave": 2,
        "setup_short_objects": setup_short_objects,
        "expected_workload_allocations": expected,
        "normal_long_drops": long_objects,
        "leaked_objects": 0,
        "minor_faults_delta": 100,
        "major_faults_delta": 0,
        "peak_rss_kib": peak_rss_kib,
        "smaps_available": True,
        "steady_pss_kib": pss_kib,
        "steady_private_dirty_kib": pss_kib - 100,
        "steady_sample_phase": "post_thp_observation",
        "actual_anon_hugepages_delta_kib": anon_huge_kib,
        "long_site": {
            "callsite": 1,
            "type_id": 7,
            "module_id": 8,
            "size": 4096,
            "align": 64,
        },
        "short_site": {
            "callsite": 2,
            "type_id": 7,
            "module_id": 8,
            "size": 4096,
            "align": 64,
        },
        "long_vma": {
            "available": True,
            "anon_hugepages_kib": anon_huge_kib,
            "pss_kib": pss_kib,
            "private_dirty_kib": pss_kib - 100,
            "hugepage_advised": selective or adaptive,
            "vm_flags": "rd wr mr mw me ac sd hg" if selective or adaptive else "rd wr",
        },
        "pre_drop": {
            "routed_allocations": 0 if system else expected,
            "live_objects": 0 if system else long_objects,
            "live_long_lived_objects": 0 if system else long_objects,
            "live_ephemeral_objects": 0,
            "ordinary_extent_mappings": 0 if system or selective else 4,
            "thp_extent_mappings": 4 if selective else 0,
            "thp_advice_attempts": 4 if selective else 0,
            "thp_advice_successes": 4 if selective else 0,
            "phase_advances": 0 if system else waves + 1,
            "adaptive_long_routed_allocations": long_objects if adaptive else 0,
            "adaptive_short_bypassed_allocations": expected - long_objects
            if adaptive
            else 0,
        },
        "post_drop": {
            "routed_deallocations": 0 if system else expected,
            "live_objects": 0,
            "runtime_validated_objects": 0 if system else expected,
            "runtime_validated_bytes": 0 if system else expected * 4096,
            "predictor_true_positive_objects": long_objects if selective else 0,
            "predictor_true_negative_objects": short_objects * waves if not system else 0,
            "predictor_false_positive_objects": 0,
            "predictor_false_negative_objects": 0,
            "all_mappings_released": not system,
        },
        "adaptive_training": {
            "performed": adaptive,
            "long_sites": 1 if adaptive else 0,
            "long_observations": 8 if adaptive else 0,
            "decisive_observation_bytes": 8 * 4096 if adaptive else 0,
        },
    }


class EventuallyFreedLifetimeExperimentTests(unittest.TestCase):
    def test_parse_probe_requires_exact_source_and_one_row(self) -> None:
        value = row(
            "system", 0, work_ns=100, peak_rss_kib=10_000, pss_kib=9_000
        )
        parsed = experiment.parse_probe("noise\n" + json.dumps(value) + "\n")
        self.assertEqual(parsed["checksum"], 99)
        with self.assertRaises(RuntimeError):
            experiment.parse_probe("{}\n{}\n")

        value["schema_version"] = 1
        with self.assertRaisesRegex(RuntimeError, "unexpected fixture schema"):
            experiment.parse_probe(json.dumps(value) + "\n")

    def test_steady_memory_is_sampled_after_thp_observation(self) -> None:
        source = (
            ROOT / "evaluation/fixtures/eventually_freed_lifetime_workload.rs"
        ).read_text(encoding="utf-8")
        observation = source.index("let observation_elapsed_ms")
        steady = source.index("let steady_memory = MemorySnapshot::read();")
        drop_long = source.index("for object in long.drain(..)")
        self.assertLess(observation, steady)
        self.assertLess(steady, drop_long)
        self.assertIn("steady_sample_phase", source)
        self.assertIn("post_thp_observation", source)

    def test_selective_mechanism_gate_requires_observed_thp(self) -> None:
        missing = row(
            "selective-thp",
            0,
            work_ns=90,
            peak_rss_kib=9_000,
            pss_kib=8_000,
            anon_huge_kib=0,
        )
        self.assertFalse(experiment.row_selective_thp_gate(missing))
        missing["actual_anon_hugepages_delta_kib"] = 8192
        missing["long_vma"]["anon_hugepages_kib"] = 8192
        self.assertTrue(experiment.row_selective_thp_gate(missing))

    def test_adaptive_gate_requires_eight_completed_long_outcomes(self) -> None:
        adaptive = row(
            "adaptive-thp",
            0,
            work_ns=90,
            peak_rss_kib=9_000,
            pss_kib=8_000,
            anon_huge_kib=8192,
        )
        self.assertTrue(experiment.row_adaptive_thp_gate(adaptive))
        adaptive["adaptive_training"]["long_observations"] = 7
        self.assertFalse(experiment.row_adaptive_thp_gate(adaptive))

    def test_summary_separates_mechanism_performance_and_memory(self) -> None:
        rows = []
        for repeat in range(7):
            rows.extend(
                [
                    row(
                        "system",
                        repeat,
                        work_ns=120_000 + repeat,
                        peak_rss_kib=12_000,
                        pss_kib=11_500,
                    ),
                    row(
                        "ordinary",
                        repeat,
                        work_ns=100_000 + repeat,
                        peak_rss_kib=10_000,
                        pss_kib=9_500,
                    ),
                    row(
                        "selective-thp",
                        repeat,
                        work_ns=80_000 + repeat,
                        peak_rss_kib=9_000,
                        pss_kib=8_500,
                        anon_huge_kib=8192,
                    ),
                ]
            )
        summary = experiment.summarize(rows)
        self.assertTrue(summary["claim_gates"]["matched_work"])
        self.assertTrue(summary["claim_gates"]["mechanism_claim_ready"])
        self.assertTrue(summary["claim_gates"]["performance_claim_ready"])
        self.assertTrue(summary["claim_gates"]["memory_reduction_claim_ready"])

        rows[-1]["steady_sample_phase"] = "pre_thp_observation"
        invalid = experiment.summarize(rows)
        self.assertFalse(invalid["claim_gates"]["memory_reduction_claim_ready"])
        self.assertFalse(invalid["claim_gates"]["post_observation_memory_samples"])

    def test_summary_rejects_mismatched_work(self) -> None:
        rows = []
        for repeat in range(5):
            ordinary = row(
                "ordinary",
                repeat,
                work_ns=100,
                peak_rss_kib=1000,
                pss_kib=900,
            )
            selective = row(
                "selective-thp",
                repeat,
                work_ns=80,
                peak_rss_kib=800,
                pss_kib=700,
                anon_huge_kib=2048,
                checksum=100,
            )
            rows.extend([ordinary, selective])
        summary = experiment.summarize(rows)
        self.assertFalse(summary["claim_gates"]["matched_work"])
        self.assertTrue(summary["claim_gates"]["mechanism_claim_ready"])
        self.assertFalse(summary["claim_gates"]["performance_claim_ready"])
        self.assertFalse(summary["claim_gates"]["memory_reduction_claim_ready"])

    def test_setup_short_interleaving_is_in_matched_work_contract(self) -> None:
        value = row(
            "ordinary", 0, work_ns=100, peak_rss_kib=1000, pss_kib=900
        )
        self.assertTrue(experiment.row_workload_gate(value))
        value["setup_short_objects"] += 1
        self.assertFalse(experiment.row_workload_gate(value))

    def test_policy_off_comparison_is_bounded_to_incremental_arena_effect(self) -> None:
        rows = []
        for repeat in range(5):
            rows.extend(
                [
                    row(
                        "policy-off",
                        repeat,
                        work_ns=100,
                        peak_rss_kib=1000,
                        pss_kib=900,
                    ),
                    row(
                        "ordinary",
                        repeat,
                        work_ns=90,
                        peak_rss_kib=950,
                        pss_kib=850,
                    ),
                ]
            )
        comparison = experiment.summarize(rows)["comparisons"][
            "dedicated_lifetime_arena_incremental_effect_vs_policy_off"
        ]
        self.assertGreater(comparison["peak_rss_reduction"]["median"], 0)
        self.assertIn("incremental dedicated lifetime arena", comparison["claim_boundary"])

    def test_dedicated_arena_memory_gate_requires_paired_positive_ci(self) -> None:
        rows = []
        for repeat in range(7):
            rows.extend(
                [
                    row(
                        "policy-off",
                        repeat,
                        work_ns=100,
                        peak_rss_kib=1000,
                        pss_kib=1000,
                    ),
                    row(
                        "ordinary",
                        repeat,
                        work_ns=300,
                        peak_rss_kib=1005,
                        pss_kib=500,
                    ),
                ]
            )
        summary = experiment.summarize(rows)
        self.assertTrue(
            summary["claim_gates"][
                "dedicated_lifetime_arena_steady_pss_reduction_claim_ready"
            ]
        )
        self.assertEqual(summary["arena_paired_repeats"], list(range(7)))

        incomplete = experiment.summarize(
            [
                value
                for value in rows
                if value["experiment_arm"] != "ordinary" or value["repeat"] < 4
            ]
        )
        self.assertFalse(
            incomplete["claim_gates"][
                "dedicated_lifetime_arena_steady_pss_reduction_claim_ready"
            ]
        )

    def test_external_allocator_parser_validates_name_and_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "liballocator.so"
            library.write_bytes(b"placeholder")
            arms = experiment.parse_external_allocators(
                [f"external-google-tcmalloc={library}"]
            )
            self.assertEqual(arms[0].fixture_arm, "system")
            self.assertEqual(arms[0].preload, library.resolve())
            with self.assertRaises(ValueError):
                experiment.parse_external_allocators([f"ordinary={library}"])

    def test_google_tcmalloc_marker_requires_an_exact_line(self) -> None:
        marker = experiment.google_tcmalloc.RUNTIME_IDENTITY_MARKER
        self.assertEqual(experiment.exact_identity_marker_count(marker, marker), 1)
        self.assertEqual(
            experiment.exact_identity_marker_count("prefix " + marker, marker), 0
        )

    def test_probe_command_keeps_matched_work_parameters(self) -> None:
        args = SimpleNamespace(
            long_objects=11,
            short_objects=12,
            object_bytes=4096,
            waves=3,
            passes_per_wave=4,
            thp_deadline_ms=5,
        )
        ordinary = experiment.probe_command(Path("/tmp/probe"), "ordinary", args)
        selective = experiment.probe_command(
            Path("/tmp/probe"), "selective-thp", args
        )
        self.assertEqual(ordinary[:2], ["/tmp/probe", "--arm"])
        self.assertEqual(ordinary[3:], selective[3:])

    def test_schedule_keeps_each_repeat_in_one_randomized_block(self) -> None:
        arms = [experiment.Arm(name=name, fixture_arm=name) for name in ("a", "b", "c")]
        schedule = experiment.blocked_schedule(arms, 3, seed=7)
        self.assertEqual([repeat for repeat, _ in schedule], [0, 0, 0, 1, 1, 1, 2, 2, 2])
        for offset in range(0, len(schedule), 3):
            self.assertEqual(
                {arm.name for _, arm in schedule[offset : offset + 3]},
                {"a", "b", "c"},
            )


if __name__ == "__main__":
    unittest.main()
