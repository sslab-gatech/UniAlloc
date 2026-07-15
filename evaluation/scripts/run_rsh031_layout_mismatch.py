#!/usr/bin/env python3
"""Run the bounded RSH-031 recovery-layout treatment/control matrix."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
HARNESS_DIR = ROOT / "evaluation" / "harnesses" / "rustsec_heap" / "RSH-031"
SCENARIO_PATH = HARNESS_DIR / "derived_layout_scenario.json"
SIGNAL_PREFIX = "UNIALLOC_RSH031_LAYOUT_SIGNAL="
TOOLCHAIN = "nightly-2026-06-11"
POLICY_FLAGS = {"typed_plain": 0, "typeiso": 1}
IMPLEMENTATION_PATHS = (
    ROOT / "unialloc" / "src" / "alloc_api" / "type_isolation.rs",
    ROOT / "unialloc" / "src" / "cache" / "mod.rs",
    ROOT / "unialloc" / "src" / "alloc_api" / "mod.rs",
    ROOT / "unialloc" / "src" / "lib.rs",
)
DEFAULT_GROUND_TRUTH = (
    ROOT
    / "evaluation"
    / "raw"
    / "rustsec-reclaim-checks-base-20260714"
    / "RSH-031-upstream"
    / "experiment.json"
)


def upstream_miri_ground_truth(path: pathlib.Path, minimum_repetitions: int) -> dict[str, Any]:
    """Validate the pinned vulnerable/patched source-level Miri controls."""
    if not path.is_file():
        return {
            "valid": False,
            "reason": "upstream_experiment_missing",
            "vulnerable_finding_count": 0,
            "patched_clean_exit_count": 0,
        }
    try:
        experiment = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {
            "valid": False,
            "reason": f"upstream_experiment_unreadable:{type(error).__name__}",
            "vulnerable_finding_count": 0,
            "patched_clean_exit_count": 0,
        }

    if experiment.get("scenario_id") != "RSH-031-upstream":
        return {
            "valid": False,
            "reason": "upstream_scenario_mismatch",
            "vulnerable_finding_count": 0,
            "patched_clean_exit_count": 0,
        }
    ground_truth_arms = {
        arm.get("archive_variant"): arm
        for arm in experiment.get("arms", [])
        if arm.get("allocator_variant") == "system" and arm.get("ground_truth_arm") is True
    }
    vulnerable = ground_truth_arms.get("vulnerable", {})
    patched = ground_truth_arms.get("patched", {})
    vulnerable_signatures = vulnerable.get("oracle_validation", {}).get(
        "tool_finding_signatures", []
    )
    vulnerable_finding_count = sum(
        signature == "miri_undefined_behavior" for signature in vulnerable_signatures
    )
    patched_summary = patched.get("repetition_summary", {})
    patched_clean_exit_count = patched_summary.get("clean_exit_count", 0)
    valid = (
        vulnerable.get("execution_mode") == "miri"
        and vulnerable.get("execution_status") == "completed"
        and vulnerable.get("oracle_validation", {}).get("status")
        == "tool_finding_observed"
        and vulnerable_finding_count >= minimum_repetitions
        and patched.get("execution_mode") == "miri"
        and patched.get("execution_status") == "completed"
        and patched.get("oracle_validation", {}).get("status")
        == "clean_tool_runs_observed"
        and patched_clean_exit_count >= minimum_repetitions
        and patched.get("oracle_validation", {}).get("tool_finding_signatures") == []
    )
    return {
        "valid": valid,
        "reason": "matched_miri_source_controls" if valid else "miri_source_controls_invalid",
        "vulnerable_finding_count": vulnerable_finding_count,
        "patched_clean_exit_count": patched_clean_exit_count,
    }


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact(path: pathlib.Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        display = resolved.relative_to(ROOT).as_posix()
    except ValueError:
        display = str(resolved)
    return {
        "path": display,
        "bytes": resolved.stat().st_size,
        "sha256": sha256(resolved),
    }


def combined_sha256(paths: tuple[pathlib.Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "little"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "little"))
        digest.update(payload)
    return digest.hexdigest()


def command_output(command: list[str]) -> str:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ).stdout.strip()


def witness_module(source: pathlib.Path) -> str:
    text = source.read_text(encoding="utf-8")
    marker = "fn main() {"
    if text.count(marker) != 1:
        raise RuntimeError(f"expected one main function in {source}")
    return text.replace(marker, "pub fn run() {", 1)


def parse_signal(stderr: str) -> dict[str, Any]:
    lines = [line for line in stderr.splitlines() if line.startswith(SIGNAL_PREFIX)]
    if len(lines) != 1:
        raise RuntimeError(f"expected one RSH-031 signal line, observed {len(lines)}")
    value = json.loads(lines[0][len(SIGNAL_PREFIX) :])
    if not isinstance(value, dict):
        raise RuntimeError("RSH-031 signal payload must be an object")
    return value


def write_project(project: pathlib.Path, variant: str) -> None:
    source = HARNESS_DIR / f"derived_layout_{variant}.rs"
    src = project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "witness.rs").write_text(witness_module(source), encoding="utf-8")
    (src / "main.rs").write_text(
        "use unialloc::UniAlloc;\n"
        "#[global_allocator]\n"
        "static ALLOCATOR: UniAlloc = UniAlloc;\n"
        "mod witness;\n"
        "fn main() { witness::run(); }\n",
        encoding="utf-8",
    )


def run_arm(
    project: pathlib.Path,
    target_dir: pathlib.Path,
    *,
    archive_variant: str,
    allocator_variant: str,
    repetitions: int,
) -> dict[str, Any]:
    write_project(project, archive_variant)
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(target_dir)
    env["UNIALLOC_RSH031_POLICY_FLAGS"] = str(POLICY_FLAGS[allocator_variant])
    command = [
        "cargo",
        f"+{TOOLCHAIN}",
        "run",
        "--quiet",
        "--offline",
        "--manifest-path",
        str(project / "Cargo.toml"),
    ]
    runs: list[dict[str, Any]] = []
    for repetition in range(1, repetitions + 1):
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        signal = parse_signal(completed.stderr) if completed.returncode == 0 else None
        runs.append(
            {
                "repetition": repetition,
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "signal": signal,
            }
        )
    return {
        "archive_variant": archive_variant,
        "allocator_variant": allocator_variant,
        "policy_flags": POLICY_FLAGS[allocator_variant],
        "command": command,
        "runs": runs,
    }


def arm_valid(arm: dict[str, Any], expected: dict[str, int]) -> bool:
    policy_flags = arm["policy_flags"]
    for run in arm["runs"]:
        signal = run["signal"]
        if run["exit_code"] != 0 or signal is None:
            return False
        if signal.get("policy_flags") != policy_flags:
            return False
        if any(signal.get(key) != value for key, value in expected.items()):
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--mechanism-output", type=pathlib.Path)
    parser.add_argument("--ground-truth", type=pathlib.Path, default=DEFAULT_GROUND_TRUTH)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--target-dir", type=pathlib.Path)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")

    scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="unialloc-rsh031-layout-") as temporary:
        project = pathlib.Path(temporary) / "project"
        project.mkdir(parents=True)
        (project / "Cargo.toml").write_text(
            "[package]\n"
            'name = "unialloc-rsh031-layout"\n'
            'version = "0.0.0"\n'
            'edition = "2021"\n\n'
            "[dependencies]\n"
            f'unialloc = {{ path = "{ROOT / "unialloc"}", features = ["stats", "type_isolation"] }}\n',
            encoding="utf-8",
        )
        shutil.copy2(ROOT / "Cargo.lock", project / "Cargo.lock")
        target_dir = (
            args.target_dir.resolve()
            if args.target_dir is not None
            else pathlib.Path(temporary) / "target"
        )
        arms = [
            run_arm(
                project,
                target_dir,
                archive_variant=archive_variant,
                allocator_variant=allocator_variant,
                repetitions=args.repetitions,
            )
            for archive_variant in ("vulnerable", "patched")
            for allocator_variant in scenario["expected_variants"]
        ]

    vulnerable_expected = scenario["expected_vulnerable_signal"]
    patched_expected = scenario["expected_patched_signal"]
    for arm in arms:
        expected = (
            vulnerable_expected
            if arm["archive_variant"] == "vulnerable"
            else patched_expected
        )
        arm["strict_expected_signal"] = arm_valid(arm, expected)

    valid = all(arm["strict_expected_signal"] for arm in arms)
    result = {
        "schema_version": 1,
        "source": "unialloc-rsh031-derived-layout-matrix",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scenario": scenario,
        "repetitions_requested": args.repetitions,
        "toolchain": {
            "name": TOOLCHAIN,
            "rustc": command_output(["rustc", f"+{TOOLCHAIN}", "-vV"]),
            "cargo": command_output(["cargo", f"+{TOOLCHAIN}", "-V"]),
        },
        "implementation": {
            "combined_sha256": combined_sha256(IMPLEMENTATION_PATHS),
            "files": [
                {
                    "path": path.relative_to(ROOT).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
                for path in IMPLEMENTATION_PATHS
            ],
        },
        "arms": arms,
        "checks": {
            "all_arms_completed": all(
                run["exit_code"] == 0 for arm in arms for run in arm["runs"]
            ),
            "vulnerable_exact_signal_all_repetitions": all(
                arm["strict_expected_signal"]
                for arm in arms
                if arm["archive_variant"] == "vulnerable"
            ),
            "patched_signal_free_all_repetitions": all(
                arm["strict_expected_signal"]
                for arm in arms
                if arm["archive_variant"] == "patched"
            ),
            "policy_independent_signal": all(
                arm["strict_expected_signal"]
                for arm in arms
                if arm["archive_variant"] == "vulnerable"
            ),
            "valid": valid,
        },
        "claim_grade": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    mechanism_valid = True
    if args.mechanism_output is not None:
        baseline_path = args.ground_truth.expanduser().resolve()
        baseline_validation = upstream_miri_ground_truth(
            baseline_path, scenario["minimum_repetitions"]
        )
        checks = {
            "matched_arms_present": len(arms) == 4,
            "minimum_repetitions_met": args.repetitions >= 3,
            "upstream_miri_baseline_bound": baseline_validation["valid"],
            "vulnerable_typed_plain_exact_signal_in_all_repetitions": next(
                arm["strict_expected_signal"]
                for arm in arms
                if (arm["archive_variant"], arm["allocator_variant"])
                == ("vulnerable", "typed_plain")
            ),
            "vulnerable_typeiso_exact_signal_in_all_repetitions": next(
                arm["strict_expected_signal"]
                for arm in arms
                if (arm["archive_variant"], arm["allocator_variant"])
                == ("vulnerable", "typeiso")
            ),
            "patched_typed_plain_control_signal_free": next(
                arm["strict_expected_signal"]
                for arm in arms
                if (arm["archive_variant"], arm["allocator_variant"])
                == ("patched", "typed_plain")
            ),
            "patched_typeiso_control_signal_free": next(
                arm["strict_expected_signal"]
                for arm in arms
                if (arm["archive_variant"], arm["allocator_variant"])
                == ("patched", "typeiso")
            ),
            "signal_independent_of_type_isolation_policy": result["checks"][
                "policy_independent_signal"
            ],
            "strict_matrix_valid": valid,
        }
        true_positive = all(checks.values())
        mechanism_valid = true_positive
        mechanism_result = {
            "advisory_id": "RUSTSEC-2023-0017",
            "allocator_signal_signatures": {
                "unialloc_recovery_deallocation_layout_mismatch": args.repetitions
            },
            "baseline_finding_signatures": {
                "miri_undefined_behavior": baseline_validation[
                    "vulnerable_finding_count"
                ]
            },
            "case_id": "RSH-031",
            "checks": checks,
            "claim_scope": (
                "exact same-pointer recovery-record allocation/deallocation layout "
                "mismatch on the RSH-031 Vec Drop edge"
            ),
            "evidence": artifact(args.output),
            "ground_truth_evidence": artifact(baseline_path),
            "ground_truth_validation": baseline_validation,
            "mechanism": "recovery_layout_validation",
            "negative_boundary": None,
            "outcome": "detected" if true_positive else "inconclusive",
            "positive_scope": (
                "allocation_deallocation_layout_mismatch_edge"
                if true_positive
                else None
            ),
            "reason": (
                "matched_exact_recovery_layout_diagnostic"
                if true_positive
                else "recovery_layout_evidence_gap"
            ),
            "repetitions": args.repetitions,
            "result_semantics": (
                "exact_diagnostic_true_positive" if true_positive else "evidence_gap"
            ),
            "scenario_id": "RSH-031-derived-layout-validation",
            "true_positive": true_positive,
        }
        fragment = {
            "schema_version": 1,
            "source": "unialloc-rsh031-recovery-layout-mechanism-results",
            "claim_grade": False,
            "minimum_repetitions": 3,
            "boundary": scenario["boundary"],
            "layout_validation_input": artifact(args.output),
            "upstream_ground_truth_input": artifact(baseline_path),
            "counts": {
                "results": 1,
                "detected": int(true_positive),
                "mitigated": 0,
                "no_signal": 0,
                "inconclusive": int(not true_positive),
                "true_positive": int(true_positive),
            },
            "results": [mechanism_result],
        }
        args.mechanism_output.parent.mkdir(parents=True, exist_ok=True)
        args.mechanism_output.write_text(
            json.dumps(fragment, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return 0 if valid and mechanism_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
