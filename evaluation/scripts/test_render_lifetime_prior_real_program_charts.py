#!/usr/bin/env python3
"""Focused tests for the real-program lifetime-prior chart renderer."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "evaluation"
    / "scripts"
    / "render_lifetime_prior_real_program_charts.py"
)
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"

ARM_NAMES = (
    "default",
    "adaptive-ordinary-all-unknown",
    "adaptive-ordinary-compiler-prior",
    "adaptive-selective-thp-all-unknown",
    "adaptive-selective-thp-compiler-prior",
)
THP_ARMS = {
    "adaptive-selective-thp-all-unknown",
    "adaptive-selective-thp-compiler-prior",
}
COMPILER_ARMS = {
    "adaptive-ordinary-compiler-prior",
    "adaptive-selective-thp-compiler-prior",
}
ALL_UNKNOWN_ARMS = set(ARM_NAMES) - COMPILER_ARMS
COMPARISON_SPECS = (
    (
        "runtime_only_vs_default",
        "default",
        "adaptive-ordinary-all-unknown",
    ),
    (
        "compiler_prior_vs_runtime_only",
        "adaptive-ordinary-all-unknown",
        "adaptive-ordinary-compiler-prior",
    ),
    (
        "runtime_thp_vs_runtime_ordinary",
        "adaptive-ordinary-all-unknown",
        "adaptive-selective-thp-all-unknown",
    ),
    (
        "compiler_prior_thp_vs_runtime_thp",
        "adaptive-selective-thp-all-unknown",
        "adaptive-selective-thp-compiler-prior",
    ),
    (
        "selective_thp_vs_compiler_ordinary",
        "adaptive-ordinary-compiler-prior",
        "adaptive-selective-thp-compiler-prior",
    ),
    (
        "compiler_thp_end_to_end_vs_default",
        "default",
        "adaptive-selective-thp-compiler-prior",
    ),
)
TARGET_SOURCE_ROLES = {
    "oxipng": "stage-a-oxipng-redb",
    "redb": "stage-a-oxipng-redb",
    "polars": "stage-a-polars",
    "swc": "stage-a-swc",
    "rustpython": "stage-a-rustpython",
    "actix_web": "stage-a-actix",
}
SOURCE_ROLES = (
    "stage-a-oxipng-redb",
    "stage-a-polars",
    "stage-a-swc",
    "stage-a-rustpython",
    "stage-a-actix",
    "stage-b-polars",
)


def _percent_improvement(baseline: float, candidate: float) -> float:
    return 100.0 * (baseline - candidate) / baseline


def _stage_a_target(target_id: str, index: int) -> dict[str, object]:
    pre_gate = target_id in {"oxipng", "redb"}
    if target_id == "oxipng":
        long_bytes, short_bytes, censored = 1_000, 10_000_000, 100
        confirmed_short = 9_000_000
    elif target_id == "redb":
        long_bytes, short_bytes, censored = 1_000, 1_000_000, 0
        confirmed_short = 900_000
    elif target_id == "polars":
        long_bytes, short_bytes, censored = 6_000_000, 9_000_000, 200_000
        confirmed_short = 8_500_000
    elif target_id == "swc":
        long_bytes, short_bytes, censored = 75_000_000, 225_000_000, 12_000_000
        confirmed_short = 224_000_000
    elif target_id == "rustpython":
        long_bytes, short_bytes, censored = 180_000, 1_000_000, 80_000
        confirmed_short = 950_000
    else:
        long_bytes, short_bytes, censored = 1_600, 9_700_000_000, 0
        confirmed_short = 9_699_000_000
    prior_long = min(long_bytes, 100 + index * 100)
    prior_short = 50 if target_id in {"oxipng", "redb", "polars", "swc"} else 0
    matched_sites = 1 if target_id in {"rustpython", "actix_web"} else index + 2
    return {
        "target_id": target_id,
        "source_role": TARGET_SOURCE_ROLES[target_id],
        "allocator_revision": ("b" if pre_gate else "a") * 40,
        "source_commit": format(index + 1, "040x"),
        "exact_layout_gate": not pre_gate,
        "wall_seconds": 30.0 + index,
        "runtime_exact_site_count": 10 + index,
        "allocation_requested_bytes": long_bytes + short_bytes + censored,
        "long_requested_bytes": long_bytes,
        "short_requested_bytes": short_bytes,
        "censored_requested_bytes": censored,
        "live_right_censored_requested_bytes": 0,
        "confirmed_short_requested_bytes": confirmed_short,
        "long_gate_passed": long_bytes >= 2_097_152,
        "short_gate_passed": confirmed_short >= 8_388_608,
        "prior_quality": {
            "matched_site_count": matched_sites,
            "long_requested_bytes": prior_long,
            "short_requested_bytes": prior_short,
            "censored_requested_bytes": 0,
        },
    }


def _sample(
    arm: str,
    repeat: int,
    *,
    blocked_thp: bool,
) -> dict[str, object]:
    base_seconds = {
        "default": 1.00,
        "adaptive-ordinary-all-unknown": 0.98,
        "adaptive-ordinary-compiler-prior": 0.95,
        "adaptive-selective-thp-all-unknown": 0.96,
        "adaptive-selective-thp-compiler-prior": 0.90,
    }[arm]
    base_rss = {
        "default": 100_000,
        "adaptive-ordinary-all-unknown": 98_000,
        "adaptive-ordinary-compiler-prior": 97_000,
        "adaptive-selective-thp-all-unknown": 96_000,
        "adaptive-selective-thp-compiler-prior": 94_000,
    }[arm]
    gate_fails = (
        blocked_thp
        and repeat == 0
        and arm == "adaptive-selective-thp-all-unknown"
    )
    if arm in THP_ARMS:
        gate: dict[str, object] = {
            "passed": not gate_fails,
            "backing": {
                "passed": not gate_fails,
                "reasons": ["no resident AnonHugePages"] if gate_fails else [],
            },
            "allocator_activity": {"passed": True, "reasons": []},
        }
        gate_field = "thp_pair_backing_gate"
    else:
        gate = {"passed": True, "reasons": []}
        gate_field = "ordinary_backing_gate"
    return {
        "arm": arm,
        "repeat": repeat,
        "primary_metric_value_seconds": base_seconds + repeat * 0.01,
        "procfs": {"peak_rss_kib": base_rss + repeat * 100},
        "duration_eligible": True,
        "cross_arm_output_equivalent": True,
        "performance_claim_eligible": not gate_fails,
        "full_executed_prior_coverage": True if arm in COMPILER_ARMS else None,
        "compiler_prior_effect_observed": True if arm in COMPILER_ARMS else None,
        "all_unknown_control_clean": True if arm in ALL_UNKNOWN_ARMS else None,
        gate_field: gate,
    }


def _sample_gate_passes(sample: dict[str, object]) -> bool:
    gate_name = (
        "thp_pair_backing_gate"
        if sample["arm"] in THP_ARMS
        else "ordinary_backing_gate"
    )
    return bool(sample[gate_name]["passed"])  # type: ignore[index]


def _comparison(
    name: str,
    baseline_arm: str,
    candidate_arm: str,
    samples: list[dict[str, object]],
    repeats: int,
) -> dict[str, object]:
    by_key = {(row["arm"], row["repeat"]): row for row in samples}
    pairs: list[dict[str, object]] = []
    for repeat in range(repeats):
        baseline = by_key[(baseline_arm, repeat)]
        candidate = by_key[(candidate_arm, repeat)]
        baseline_seconds = float(baseline["primary_metric_value_seconds"])
        candidate_seconds = float(candidate["primary_metric_value_seconds"])
        baseline_rss = float(baseline["procfs"]["peak_rss_kib"])  # type: ignore[index]
        candidate_rss = float(candidate["procfs"]["peak_rss_kib"])  # type: ignore[index]
        backing = _sample_gate_passes(baseline) and _sample_gate_passes(candidate)
        compiler = all(
            row["full_executed_prior_coverage"] is True
            and row["compiler_prior_effect_observed"] is True
            for row in (baseline, candidate)
            if row["arm"] in COMPILER_ARMS
        )
        controls = all(
            row["all_unknown_control_clean"] is True
            for row in (baseline, candidate)
            if row["arm"] in ALL_UNKNOWN_ARMS
        )
        diagnostic = bool(backing and compiler and controls)
        preliminary = bool(
            diagnostic
            and baseline["performance_claim_eligible"]
            and candidate["performance_claim_eligible"]
        )
        claim = preliminary and repeats >= 4
        pairs.append(
            {
                "repeat": repeat,
                "diagnostic_eligible": diagnostic,
                "preliminary_eligible": preliminary,
                "claim_eligible": claim,
                "backing_eligible": backing,
                "compiler_prior_effect_eligible": compiler,
                "runtime_controls_eligible": controls,
                "baseline_seconds": baseline_seconds,
                "candidate_seconds": candidate_seconds,
                "percent_improvement": _percent_improvement(
                    baseline_seconds, candidate_seconds
                ),
                "baseline_peak_rss_kib": baseline_rss,
                "candidate_peak_rss_kib": candidate_rss,
                "peak_rss_percent_reduction": _percent_improvement(
                    baseline_rss, candidate_rss
                ),
            }
        )

    def selected(flag: str) -> list[dict[str, object]]:
        return [pair for pair in pairs if pair[flag] is True]

    diagnostic_pairs = selected("diagnostic_eligible")
    preliminary_pairs = selected("preliminary_eligible")
    claim_pairs = selected("claim_eligible")

    def median(rows: list[dict[str, object]], field: str) -> float | None:
        if not rows:
            return None
        return float(statistics.median(float(row[field]) for row in rows))

    return {
        "baseline_arm": baseline_arm,
        "candidate_arm": candidate_arm,
        "pairs": pairs,
        "diagnostic_eligible_pair_count": len(diagnostic_pairs),
        "preliminary_eligible_pair_count": len(preliminary_pairs),
        "claim_eligible_pair_count": len(claim_pairs),
        "median_percent_improvement": median(
            diagnostic_pairs, "percent_improvement"
        ),
        "preliminary_median_percent_improvement": median(
            preliminary_pairs, "percent_improvement"
        ),
        "claim_median_percent_improvement": median(
            claim_pairs, "percent_improvement"
        ),
        "median_peak_rss_percent_reduction": median(
            diagnostic_pairs, "peak_rss_percent_reduction"
        ),
        "diagnostic_eligible": len(diagnostic_pairs) == repeats,
        "preliminary_eligible": len(preliminary_pairs) == repeats,
        "claim_eligible": len(claim_pairs) == repeats,
    }


def valid_summary(*, blocked_thp: bool = False, repeats: int = 4) -> dict[str, object]:
    target_ids = (
        "oxipng",
        "redb",
        "polars",
        "swc",
        "rustpython",
        "actix_web",
    )
    samples = [
        _sample(arm, repeat, blocked_thp=blocked_thp)
        for arm in ARM_NAMES
        for repeat in range(repeats)
    ]
    comparisons = {
        name: _comparison(name, baseline, candidate, samples, repeats)
        for name, baseline, candidate in COMPARISON_SPECS
    }
    return {
        "schema_version": 1,
        "source": "lifetime-prior-real-program-evidence-v1",
        "sources": [
            {
                "role": role,
                "path": f"retained-sources/{role}.json",
                "sha256": "0" * 64,
            }
            for role in SOURCE_ROLES
        ],
        "stage_a": {
            "pressure_basis": "process_wide_requested_generation_bytes",
            "force_track_all": True,
            "minimum_long_requested_bytes": 2_097_152,
            "minimum_confirmed_short_requested_bytes": 8_388_608,
            "targets": [
                _stage_a_target(target_id, index)
                for index, target_id in enumerate(target_ids)
            ],
        },
        "stage_b": {
            "target_id": "polars",
            "source_role": "stage-b-polars",
            "pressure_basis": "eligible_exact_site_payload_capacity",
            "force_track_all": False,
            "repeats": repeats,
            "presentation_repeat_count_eligible": repeats >= 4,
            "arms": list(ARM_NAMES),
            "samples": samples,
            "all_thp_pairs_backed": not blocked_thp,
            "paired_comparisons": comparisons,
            "claim_scope": {
                "per_sample_thp_backing_required": True,
                "allocator_vma_residency_attribution": False,
                "thp_causality_scope": (
                    "process-level backing correlated with positive allocator activity"
                ),
            },
        },
    }


def run_renderer(
    summary_path: Path, output_dir: Path, repo_root: Path | None = None
) -> subprocess.CompletedProcess[str]:
    verified_root = repo_root if repo_root is not None else summary_path.parent
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--summary",
            str(summary_path),
            "--output-dir",
            str(output_dir),
            "--repo-root",
            str(verified_root),
            "--no-png",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class LifetimePriorRealProgramChartTests(unittest.TestCase):
    def _write_summary(self, path: Path, document: dict[str, object]) -> bytes:
        for source in document["sources"]:  # type: ignore[index]
            source_path = path.parent / source["path"]
            source_path.parent.mkdir(parents=True, exist_ok=True)
            content = f"retained evidence for {source['role']}\n".encode("utf-8")
            source_path.write_bytes(content)
            source["sha256"] = hashlib.sha256(content).hexdigest()
        content = json.dumps(document, indent=2, sort_keys=True).encode("utf-8")
        path.write_bytes(content)
        return content

    def test_cli_renders_two_editable_figures_and_long_form_csvs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary_path = root / "summary.json"
            source = self._write_summary(summary_path, valid_summary())
            output_dir = root / "figures"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = {
                "stage-a-opportunity.svg",
                "stage-a-opportunity.csv",
                "polars-stage-b-ablation.svg",
                "polars-stage-b-ablation.csv",
            }
            self.assertEqual({path.name for path in output_dir.iterdir()}, expected)

            for name in ("stage-a-opportunity.svg", "polars-stage-b-ablation.svg"):
                xml_root = ET.parse(output_dir / name).getroot()
                self.assertEqual(xml_root.tag, SVG_NAMESPACE + "svg")
                self.assertEqual(xml_root.attrib["width"], "1600")
                self.assertEqual(xml_root.attrib["height"], "900")
                self.assertEqual(xml_root.attrib["viewBox"], "0 0 1600 900")
                self.assertEqual(
                    xml_root.findall(".//" + SVG_NAMESPACE + "image"), []
                )
                rendered = (output_dir / name).read_text(encoding="utf-8")
                self.assertIn(hashlib.sha256(source).hexdigest(), rendered)

            stage_a = (output_dir / "stage-a-opportunity.svg").read_text(
                encoding="utf-8"
            )
            self.assertIn("Rust lifetime priors expose structure", stage_a)
            self.assertIn("Oxipng [pre-gate triage]", stage_a)
            self.assertIn("Actix Web", stage_a)
            self.assertIn("[one-site support]", stage_a)
            self.assertIn("process-wide requested-generation clock", stage_a)

            stage_b = (output_dir / "polars-stage-b-ablation.svg").read_text(
                encoding="utf-8"
            )
            self.assertIn("CLAIM ELIGIBLE", stage_b)
            self.assertIn("Runtime learning / selective THP", stage_b)
            self.assertIn("Compiler THP / Default", stage_b)
            self.assertIn("allocator-VMA attribution is unmeasured", stage_b)

            with (output_dir / "stage-a-opportunity.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                stage_a_rows = list(csv.DictReader(handle))
            self.assertEqual(len(stage_a_rows), 18)
            with (output_dir / "polars-stage-b-ablation.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                stage_b_rows = list(csv.DictReader(handle))
            self.assertEqual(len(stage_b_rows), 50)
            self.assertEqual(
                sum(row["row_type"] == "arm_sample" for row in stage_b_rows), 20
            )
            self.assertEqual(
                sum(row["row_type"] == "paired_effect" for row in stage_b_rows),
                24,
            )

    def test_outputs_are_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary_path = root / "summary.json"
            self._write_summary(summary_path, valid_summary())
            first = root / "first"
            second = root / "second"

            first_result = run_renderer(summary_path, first)
            second_result = run_renderer(summary_path, second)
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            for name in (
                "stage-a-opportunity.svg",
                "stage-a-opportunity.csv",
                "polars-stage-b-ablation.svg",
                "polars-stage-b-ablation.csv",
            ):
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())

    def test_blocked_backing_keeps_values_and_exposes_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary_path = root / "summary.json"
            self._write_summary(summary_path, valid_summary(blocked_thp=True))
            output_dir = root / "figures"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = (output_dir / "polars-stage-b-ablation.svg").read_text(
                encoding="utf-8"
            )
            self.assertIn("THP CAUSAL CLAIM BLOCKED", rendered)
            root_reason = "THP backing: no resident AnonHugePages"
            generic_reason = "Runtime THP / Runtime ordinary: diagnostic gate failed"
            self.assertIn(root_reason, rendered)
            self.assertLess(rendered.index(root_reason), rendered.index(generic_reason))
            self.assertIn("CLAIM ELIGIBLE", rendered)
            self.assertIn("endpoint-sample", rendered)
            self.assertIn("960.000 ms/work", rendered)
            with (output_dir / "polars-stage-b-ablation.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(
                any(root_reason in row["blocked_reasons"] for row in rows)
            )
            self.assertTrue(
                all(
                    row["campaign_status"] == "THP CAUSAL CLAIM BLOCKED"
                    for row in rows
                )
            )
            sample_rows = [row for row in rows if row["row_type"] == "arm_sample"]
            pair_rows = [row for row in rows if row["row_type"] == "paired_effect"]
            self.assertTrue(all(row["claim_eligible"] == "" for row in sample_rows))
            self.assertTrue(any(row["claim_eligible"] == "True" for row in pair_rows))
            ordinary = next(
                row
                for row in pair_rows
                if row["comparison"] == "compiler_prior_vs_runtime_only"
            )
            self.assertEqual(ordinary["comparison_status"], "CLAIM ELIGIBLE")

    def test_two_repeat_valid_campaign_is_preliminary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary_path = root / "summary.json"
            self._write_summary(summary_path, valid_summary(repeats=2))
            output_dir = root / "figures"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = (output_dir / "polars-stage-b-ablation.svg").read_text(
                encoding="utf-8"
            )
            self.assertIn("PRELIMINARY", rendered)
            self.assertIn("requires at least four paired repeats", rendered)

    def test_nonfinite_metric_fails_before_output_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = valid_summary()
            document["stage_b"]["samples"][0][  # type: ignore[index]
                "primary_metric_value_seconds"
            ] = float("nan")
            summary_path = root / "summary.json"
            self._write_summary(summary_path, document)
            output_dir = root / "figures"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must be finite", result.stderr)
            self.assertFalse(output_dir.exists())

    def test_inconsistent_pair_delta_fails_before_output_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = valid_summary()
            document["stage_b"]["paired_comparisons"][  # type: ignore[index]
                "runtime_only_vs_default"
            ]["pairs"][0]["percent_improvement"] += 1.0
            summary_path = root / "summary.json"
            self._write_summary(summary_path, document)
            output_dir = root / "figures"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("percent_improvement is inconsistent", result.stderr)
            self.assertFalse(output_dir.exists())

    def test_missing_arm_sample_fails_before_output_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = valid_summary()
            document["stage_b"]["samples"].pop()  # type: ignore[index]
            summary_path = root / "summary.json"
            self._write_summary(summary_path, document)
            output_dir = root / "figures"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must contain every arm and repeat", result.stderr)
            self.assertFalse(output_dir.exists())

    def test_thp_gate_cannot_pass_without_allocator_activity_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = valid_summary()
            thp_sample = next(
                row
                for row in document["stage_b"]["samples"]  # type: ignore[index]
                if row["arm"] == "adaptive-selective-thp-all-unknown"
            )
            del thp_sample["thp_pair_backing_gate"]["allocator_activity"]
            summary_path = root / "summary.json"
            self._write_summary(summary_path, document)
            output_dir = root / "figures"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("allocator_activity is required", result.stderr)
            self.assertFalse(output_dir.exists())

    def test_thp_root_cannot_pass_when_allocator_activity_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = valid_summary()
            thp_sample = next(
                row
                for row in document["stage_b"]["samples"]  # type: ignore[index]
                if row["arm"] == "adaptive-selective-thp-all-unknown"
            )
            activity = thp_sample["thp_pair_backing_gate"]["allocator_activity"]
            activity["passed"] = False
            activity["reasons"] = ["allocator activity unavailable"]
            summary_path = root / "summary.json"
            self._write_summary(summary_path, document)
            output_dir = root / "figures"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must equal the conjunction", result.stderr)
            self.assertFalse(output_dir.exists())

    def test_source_sha_is_verified_under_explicit_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = valid_summary()
            summary_path = root / "summary.json"
            self._write_summary(summary_path, document)
            source = document["sources"][0]  # type: ignore[index]
            (root / source["path"]).write_text("mutated\n", encoding="utf-8")
            output_dir = root / "figures"

            result = run_renderer(summary_path, output_dir, root)
            self.assertEqual(result.returncode, 2)
            self.assertIn("SHA-256 mismatch", result.stderr)
            self.assertFalse(output_dir.exists())

    def test_stage_a_target_must_bind_its_canonical_source_role(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = valid_summary()
            document["stage_a"]["targets"][0][  # type: ignore[index]
                "source_role"
            ] = "stage-a-polars"
            summary_path = root / "summary.json"
            self._write_summary(summary_path, document)
            output_dir = root / "figures"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("source_role must bind", result.stderr)
            self.assertFalse(output_dir.exists())

    def test_optional_swc_diagnostic_source_is_verified_and_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = valid_summary()
            document["sources"].append(  # type: ignore[index]
                {
                    "role": "stage-b-swc-diagnostic",
                    "path": "retained-sources/stage-b-swc-diagnostic.json",
                    "sha256": "0" * 64,
                }
            )
            summary_path = root / "summary.json"
            self._write_summary(summary_path, document)
            output_dir = root / "figures"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((output_dir / "polars-stage-b-ablation.svg").is_file())


if __name__ == "__main__":
    unittest.main()
