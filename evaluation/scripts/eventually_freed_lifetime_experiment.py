#!/usr/bin/env python3
"""Run the eventually-freed lifetime/THP micro-experiment.

The primary comparison is paired `ordinary` versus `selective-thp` UniAlloc
work.  `system` is included as context.  Extra malloc implementations can be
added as `--external-allocator NAME=/absolute/libmalloc.so`; those execute the
same System arm under LD_PRELOAD.

The report keeps three claims separate:

* workload gate: identical completed work and normal deallocation;
* mechanism gate: allocator routing plus per-run process AnonHugePages;
* performance/memory gates: paired effects whose bootstrap lower bound is > 0.

The fixture uses oracle metadata.  Compiler prediction quality and
real-application impact remain outside this experiment's claim boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
SUPPORT_SPEC = importlib.util.spec_from_file_location(
    "unialloc_lifetime_google_tcmalloc_support",
    SCRIPT_DIR / "google_tcmalloc_support.py",
)
assert SUPPORT_SPEC is not None and SUPPORT_SPEC.loader is not None
google_tcmalloc = importlib.util.module_from_spec(SUPPORT_SPEC)
SUPPORT_SPEC.loader.exec_module(google_tcmalloc)


PRIMARY_ARMS = (
    "system",
    "policy-off",
    "ordinary",
    "selective-thp",
    "adaptive-thp",
)
FIXTURE_SOURCE = "evaluation/fixtures/eventually_freed_lifetime_workload.rs"


@dataclass(frozen=True)
class Arm:
    name: str
    fixture_arm: str
    preload: Path | None = None


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-id", default=time.strftime("eventually-freed-lifetime-%Y%m%d-%H%M%S")
    )
    parser.add_argument("--long-objects", type=int, default=2048)
    parser.add_argument("--short-objects", type=int, default=2048)
    parser.add_argument("--object-bytes", type=int, default=4096)
    parser.add_argument("--waves", type=int, default=8)
    parser.add_argument("--passes-per-wave", type=int, default=2)
    parser.add_argument(
        "--thp-deadline-ms",
        "--thp-wait-ms",
        dest="thp_deadline_ms",
        type=int,
        default=30_000,
        help="maximum claim-grade poll time for pointer-VMA THP realization",
    )
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--seed", type=int, default=20260715)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--binary", type=Path)
    parser.add_argument(
        "--external-allocator",
        action="append",
        default=[],
        metavar="NAME=/ABSOLUTE/LIBRARY",
        help="run the System arm with this allocator library through LD_PRELOAD",
    )
    parser.add_argument(
        "--compiler-wrapper",
        type=Path,
        help="optional existing rustc wrapper used during the fixture build",
    )
    parser.add_argument(
        "--allow-zero-thp",
        action="store_true",
        help="finish diagnostically when the selective arm receives no THP backing",
    )
    args = parser.parse_args()
    positive = (
        args.long_objects,
        args.short_objects,
        args.object_bytes,
        args.waves,
        args.passes_per_wave,
        args.repeats,
        args.timeout,
    )
    if (
        any(value <= 0 for value in positive)
        or args.thp_deadline_ms < 0
        or args.warmups < 0
    ):
        parser.error("counts, sizes, repeats, and timeout must be positive")
    return args


def parse_external_allocators(values: Iterable[str]) -> list[Arm]:
    arms: list[Arm] = []
    seen = set(PRIMARY_ARMS)
    for value in values:
        if "=" not in value:
            raise ValueError(f"external allocator must be NAME=PATH: {value}")
        name, raw_path = value.split("=", 1)
        name = name.strip()
        path = Path(raw_path).expanduser().resolve()
        if not name or name in seen:
            raise ValueError(f"duplicate or reserved allocator name: {name!r}")
        if not path.is_file():
            raise ValueError(f"external allocator library does not exist: {path}")
        seen.add(name)
        arms.append(Arm(name=name, fixture_arm="system", preload=path))
    return arms


def allocator_provenance(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    symbol_text = ""
    symbol_tool = None
    for command in (("nm", "-D", str(path)), ("strings", str(path))):
        if not shutil.which(command[0]):
            continue
        completed = subprocess.run(
            list(command), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        if completed.returncode == 0:
            symbol_text += completed.stdout
            symbol_tool = command[0]
    malloc_extension = "MallocExtension" in symbol_text
    hpaa_markers = any(
        marker in symbol_text
        for marker in ("HugePageFiller", "HugePageAwareAllocator", "Temeraire")
    )
    record = {
        "path": str(path),
        "sha256": digest,
        "symbol_tool": symbol_tool,
        "malloc_extension_marker": malloc_extension,
        "hpaa_marker": hpaa_markers,
        "modern_google_tcmalloc_signature_gate": malloc_extension and hpaa_markers,
        "claim_boundary": (
            "a positive signature is binary provenance evidence; allocator behavior still "
            "requires the measured runtime rows"
        ),
    }
    if path.name == google_tcmalloc.LIBRARY_NAME:
        identity = google_tcmalloc.validate_library_dir(path.parent)
        if identity["realpath"] != str(path.resolve(strict=True)):
            raise RuntimeError("modern google/tcmalloc identity names another DSO")
        record.update(
            {
                "modern_google_tcmalloc_identity": identity,
                "runtime_identity_marker_required": (
                    google_tcmalloc.RUNTIME_IDENTITY_MARKER
                ),
                "modern_google_tcmalloc_signature_gate": True,
            }
        )
    return record


def exact_identity_marker_count(stderr: str, marker: str) -> int:
    return sum(line == marker for line in stderr.splitlines(keepends=True))


def fixture_workspace(root: Path) -> Path:
    return root / "target" / "eventually_freed_lifetime_experiment"


def prepare_fixture_workspace(root: Path) -> Path:
    workspace = fixture_workspace(root)
    source_dir = workspace / "src"
    source_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / FIXTURE_SOURCE, source_dir / "main.rs")
    manifest = f"""\
[package]
name = "eventually_freed_lifetime_workload"
version = "0.0.0"
edition = "2021"
publish = false

[dependencies]
unialloc = {{ path = {json.dumps(str(root / 'unialloc'))}, features = ["lifetime_hugepage"] }}

[workspace]
"""
    (workspace / "Cargo.toml").write_text(manifest, encoding="utf-8")
    # Reuse the repository's reviewed/yanked-compatible dependency resolution.
    # An isolated resolver cannot newly select historical `spin = 0.9.0`.
    shutil.copyfile(root / "Cargo.lock", workspace / "Cargo.lock")
    return workspace


def build_fixture(root: Path, wrapper: Path | None, timeout: int) -> tuple[Path, dict[str, Any]]:
    workspace = prepare_fixture_workspace(root)
    env = os.environ.copy()
    audit_dir = workspace / "compiler-audit"
    compiler_evidence: dict[str, Any] = {
        "requested": wrapper is not None,
        "compiler_layer_measured": False,
        "exact_runtime_audit_join_performed": False,
    }
    if wrapper is not None:
        wrapper = wrapper.expanduser().resolve()
        if not wrapper.is_file():
            raise RuntimeError(f"compiler wrapper does not exist: {wrapper}")
        audit_dir.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "RUSTC_WRAPPER": str(wrapper),
                "UNIALLOC_RUSTC_TARGET_CRATES": "eventually_freed_lifetime_workload",
                "UNIALLOC_REWRITE_AUDIT_DIR": str(audit_dir),
                "UNIALLOC_AUTO_LIFETIME_CLASSIFIER": "1",
                "UNIALLOC_CONTINUE_COMPILATION": "1",
            }
        )
    completed = subprocess.run(
        [
            "cargo",
            "build",
            "--release",
            "--offline",
            "--manifest-path",
            str(workspace / "Cargo.toml"),
        ],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "fixture build failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    audits = sorted(str(path) for path in audit_dir.glob("*.json*"))
    compiler_evidence.update(
        {
            "audit_files": audits,
            "audit_file_count": len(audits),
            "claim_boundary": (
                "audit files expose compiler observations only; no exact callsite/type/module "
                "join to runtime rows is performed, so compiler prediction remains unmeasured"
            ),
        }
    )
    binary = workspace / "target" / "release" / "eventually_freed_lifetime_workload"
    if not binary.is_file():
        raise RuntimeError(f"fixture binary missing after build: {binary}")
    return binary, compiler_evidence


def probe_command(binary: Path, fixture_arm: str, args: argparse.Namespace) -> list[str]:
    return [
        str(binary),
        "--arm",
        fixture_arm,
        "--long-objects",
        str(args.long_objects),
        "--short-objects",
        str(args.short_objects),
        "--object-bytes",
        str(args.object_bytes),
        "--waves",
        str(args.waves),
        "--passes-per-wave",
        str(args.passes_per_wave),
        "--thp-deadline-ms",
        str(args.thp_deadline_ms),
    ]


def parse_probe(stdout: str) -> dict[str, Any]:
    rows = [line for line in stdout.splitlines() if line.lstrip().startswith("{")]
    if len(rows) != 1:
        raise RuntimeError(f"expected one fixture JSON row, got {len(rows)}")
    row = json.loads(rows[0])
    if row.get("source") != "eventually_freed_lifetime_workload":
        raise RuntimeError(f"unexpected fixture source: {row.get('source')!r}")
    if row.get("schema_version") != 2:
        raise RuntimeError(f"unexpected fixture schema: {row.get('schema_version')!r}")
    if row.get("steady_sample_phase") != "post_thp_observation":
        raise RuntimeError(
            f"unexpected steady memory sample phase: {row.get('steady_sample_phase')!r}"
        )
    return row


def run_arm(
    binary: Path,
    arm: Arm,
    repeat: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    env = os.environ.copy()
    if arm.preload is not None:
        existing = env.get("LD_PRELOAD", "")
        env["LD_PRELOAD"] = (
            f"{arm.preload}:{existing}" if existing else str(arm.preload)
        )
    started = time.perf_counter_ns()
    completed = subprocess.run(
        probe_command(binary, arm.fixture_arm, args),
        cwd=repo_root(),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=args.timeout,
    )
    wall_time_ns = time.perf_counter_ns() - started
    if completed.returncode != 0:
        raise RuntimeError(
            f"arm {arm.name} repeat {repeat} failed ({completed.returncode})\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    tcmalloc_marker_count = exact_identity_marker_count(
        completed.stderr, google_tcmalloc.RUNTIME_IDENTITY_MARKER
    )
    modern_tcmalloc = (
        arm.preload is not None
        and arm.preload.name == google_tcmalloc.LIBRARY_NAME
    )
    expected_tcmalloc_markers = 1 if modern_tcmalloc else 0
    if tcmalloc_marker_count != expected_tcmalloc_markers:
        raise RuntimeError(
            f"arm {arm.name} repeat {repeat} emitted {tcmalloc_marker_count} "
            "modern google/tcmalloc identity markers; expected "
            f"{expected_tcmalloc_markers}"
        )
    row = parse_probe(completed.stdout)
    row.update(
        {
            "experiment_arm": arm.name,
            "repeat": repeat,
            "wall_time_ns": wall_time_ns,
            "ld_preload": str(arm.preload) if arm.preload else None,
            "google_tcmalloc_identity_marker_count": tcmalloc_marker_count,
            "google_tcmalloc_target_identity_verified": (
                tcmalloc_marker_count == expected_tcmalloc_markers
            ),
        }
    )
    return row


def median(values: Iterable[float | int]) -> float:
    return float(statistics.median(list(values)))


def percentile(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        return 0.0
    index = (len(sorted_values) - 1) * probability
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return sorted_values[lower]
    fraction = index - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def paired_effects(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    metric: str,
) -> list[float]:
    by_repeat = {int(row["repeat"]): row for row in baseline}
    effects: list[float] = []
    for row in candidate:
        paired = by_repeat.get(int(row["repeat"]))
        if paired is None:
            continue
        baseline_value = float(paired[metric])
        candidate_value = float(row[metric])
        if baseline_value > 0:
            effects.append(1.0 - candidate_value / baseline_value)
    return effects


def bootstrap_median_ci(
    values: list[float], *, seed: int = 20260715, samples: int = 10_000
) -> dict[str, Any]:
    if not values:
        return {"samples": 0, "median": 0.0, "ci95": [0.0, 0.0]}
    rng = random.Random(seed)
    bootstrapped = []
    for _ in range(samples):
        draw = [values[rng.randrange(len(values))] for _ in values]
        bootstrapped.append(float(statistics.median(draw)))
    bootstrapped.sort()
    return {
        "samples": len(values),
        "median": float(statistics.median(values)),
        "ci95": [
            percentile(bootstrapped, 0.025),
            percentile(bootstrapped, 0.975),
        ],
    }


def row_workload_gate(row: dict[str, Any]) -> bool:
    expected = (
        int(row["long_objects"])
        + int(row["setup_short_objects"])
        + int(row["short_objects_per_wave"]) * int(row["waves"])
    )
    return (
        int(row["normal_long_drops"]) == int(row["long_objects"])
        and int(row["leaked_objects"]) == 0
        and int(row["expected_workload_allocations"]) == expected
        and int(row["long_site"]["type_id"]) == int(row["short_site"]["type_id"])
        and int(row["long_site"]["module_id"])
        == int(row["short_site"]["module_id"])
        and int(row["long_site"]["size"]) == int(row["short_site"]["size"])
        and int(row["long_site"]["align"]) == int(row["short_site"]["align"])
        and int(row["long_site"]["callsite"])
        != int(row["short_site"]["callsite"])
    )


def row_post_observation_memory_gate(row: dict[str, Any]) -> bool:
    return (
        row.get("steady_sample_phase") == "post_thp_observation"
        and bool(row.get("smaps_available"))
        and int(row.get("steady_pss_kib", 0)) > 0
        and int(row.get("steady_private_dirty_kib", -1)) >= 0
    )


def row_lifetime_cleanup_gate(row: dict[str, Any]) -> bool:
    if row["arm"] in {"system", "policy-off"}:
        return row_workload_gate(row)
    if row["arm"] == "adaptive-thp":
        return (
            row_workload_gate(row)
            and bool(row["adaptive_training"]["performed"])
            and int(row["adaptive_training"]["long_observations"]) >= 8
            and int(row["pre_drop"]["adaptive_long_routed_allocations"])
            == int(row["long_objects"])
            and int(row["post_drop"]["live_objects"]) == 0
            and bool(row["post_drop"]["all_mappings_released"])
        )
    return (
        row_workload_gate(row)
        and int(row["pre_drop"]["routed_allocations"])
        == int(row["expected_workload_allocations"])
        and int(row["post_drop"]["routed_deallocations"])
        == int(row["expected_workload_allocations"])
        and int(row["post_drop"]["live_objects"]) == 0
        and bool(row["post_drop"]["all_mappings_released"])
    )


def row_selective_thp_gate(row: dict[str, Any]) -> bool:
    return (
        row["experiment_arm"] == "selective-thp"
        and row_lifetime_cleanup_gate(row)
        and bool(row["smaps_available"])
        and int(row["pre_drop"]["live_long_lived_objects"])
        == int(row["long_objects"])
        and int(row["pre_drop"]["live_ephemeral_objects"]) == 0
        and int(row["pre_drop"]["thp_extent_mappings"]) > 0
        and int(row["pre_drop"]["thp_advice_successes"]) > 0
        and int(row["actual_anon_hugepages_delta_kib"]) > 0
        and bool(row["long_vma"]["available"])
        and bool(row["long_vma"]["hugepage_advised"])
        and int(row["long_vma"]["anon_hugepages_kib"]) > 0
    )


def row_ordinary_control_gate(row: dict[str, Any]) -> bool:
    return (
        row["experiment_arm"] == "ordinary"
        and row_lifetime_cleanup_gate(row)
        and bool(row["long_vma"]["available"])
        and not bool(row["long_vma"]["hugepage_advised"])
        and int(row["long_vma"]["anon_hugepages_kib"]) == 0
        and int(row["actual_anon_hugepages_delta_kib"]) == 0
    )


def row_adaptive_thp_gate(row: dict[str, Any]) -> bool:
    return (
        row["experiment_arm"] == "adaptive-thp"
        and row_lifetime_cleanup_gate(row)
        and bool(row["long_vma"]["available"])
        and bool(row["long_vma"]["hugepage_advised"])
        and int(row["long_vma"]["anon_hugepages_kib"]) > 0
    )


def summarize(rows: list[dict[str, Any]], *, seed: int = 20260715) -> dict[str, Any]:
    rows_by_arm: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        rows_by_arm.setdefault(str(row["experiment_arm"]), []).append(row)
    for arm_rows in rows_by_arm.values():
        arm_rows.sort(key=lambda row: int(row["repeat"]))

    arm_summaries: dict[str, Any] = {}
    for name, arm_rows in sorted(rows_by_arm.items()):
        arm_summaries[name] = {
            "samples": len(arm_rows),
            "all_workload_gates_passed": all(row_workload_gate(row) for row in arm_rows),
            "all_cleanup_gates_passed": all(
                row_lifetime_cleanup_gate(row) for row in arm_rows
            ),
            "median_work_elapsed_ns": median(row["work_elapsed_ns"] for row in arm_rows),
            "median_wall_time_ns": median(row["wall_time_ns"] for row in arm_rows),
            "median_peak_rss_kib": median(row["peak_rss_kib"] for row in arm_rows),
            "median_minor_faults": median(row["minor_faults_delta"] for row in arm_rows),
            "median_major_faults": median(row["major_faults_delta"] for row in arm_rows),
            "median_steady_pss_kib": median(row["steady_pss_kib"] for row in arm_rows),
            "median_steady_private_dirty_kib": median(
                row["steady_private_dirty_kib"] for row in arm_rows
            ),
            "median_actual_anon_hugepages_delta_kib": median(
                row["actual_anon_hugepages_delta_kib"] for row in arm_rows
            ),
        }

    ordinary = rows_by_arm.get("ordinary", [])
    policy_off = rows_by_arm.get("policy-off", [])
    selective = rows_by_arm.get("selective-thp", [])
    adaptive = rows_by_arm.get("adaptive-thp", [])
    paired_repeats = sorted(
        {int(row["repeat"]) for row in ordinary}.intersection(
            int(row["repeat"]) for row in selective
        )
    )
    arena_paired_repeats = sorted(
        {int(row["repeat"]) for row in policy_off}.intersection(
            int(row["repeat"]) for row in ordinary
        )
    )
    memory_rows = [*policy_off, *ordinary, *selective]
    post_observation_memory_samples = bool(memory_rows) and all(
        row_post_observation_memory_gate(row) for row in memory_rows
    )
    checksum_by_repeat: dict[int, set[int]] = {}
    for row in rows:
        checksum_by_repeat.setdefault(int(row["repeat"]), set()).add(int(row["checksum"]))
    matched_work = all(len(checksums) == 1 for checksums in checksum_by_repeat.values())
    mechanism_ready = (
        bool(ordinary)
        and bool(selective)
        and all(row_ordinary_control_gate(row) for row in ordinary)
        and all(row_selective_thp_gate(row) for row in selective)
    )
    adaptive_mechanism_ready = bool(adaptive) and all(
        row_adaptive_thp_gate(row) for row in adaptive
    )
    timing = bootstrap_median_ci(
        paired_effects(ordinary, selective, "work_elapsed_ns"), seed=seed
    )
    peak_rss = bootstrap_median_ci(
        paired_effects(ordinary, selective, "peak_rss_kib"), seed=seed + 1
    )
    pss = bootstrap_median_ci(
        paired_effects(ordinary, selective, "steady_pss_kib"), seed=seed + 2
    )
    arena_time = bootstrap_median_ci(
        paired_effects(policy_off, ordinary, "work_elapsed_ns"), seed=seed + 3
    )
    arena_peak_rss = bootstrap_median_ci(
        paired_effects(policy_off, ordinary, "peak_rss_kib"), seed=seed + 4
    )
    arena_pss = bootstrap_median_ci(
        paired_effects(policy_off, ordinary, "steady_pss_kib"), seed=seed + 5
    )
    enough_pairs = len(paired_repeats) >= 5
    performance_ready = (
        mechanism_ready
        and matched_work
        and enough_pairs
        and float(timing["ci95"][0]) > 0.0
    )
    memory_ready = (
        mechanism_ready
        and matched_work
        and enough_pairs
        and all(row_post_observation_memory_gate(row) for row in [*ordinary, *selective])
        and float(peak_rss["ci95"][0]) > 0.0
        and float(pss["ci95"][0]) > 0.0
    )
    arena_rows_ready = (
        bool(policy_off)
        and bool(ordinary)
        and all(
            row_workload_gate(row) and row_lifetime_cleanup_gate(row)
            for row in [*policy_off, *ordinary]
        )
    )
    arena_memory_ready = (
        arena_rows_ready
        and matched_work
        and len(arena_paired_repeats) >= 5
        and all(row_post_observation_memory_gate(row) for row in [*policy_off, *ordinary])
        and float(arena_pss["ci95"][0]) > 0.0
    )
    return {
        "schema_version": 2,
        "source": "eventually_freed_lifetime_experiment",
        "arms": arm_summaries,
        "paired_repeats": paired_repeats,
        "arena_paired_repeats": arena_paired_repeats,
        "comparisons": {
            "selective_thp_vs_ordinary_work_time_reduction": timing,
            "selective_thp_vs_ordinary_peak_rss_reduction": peak_rss,
            "selective_thp_vs_ordinary_steady_pss_reduction": pss,
            "dedicated_lifetime_arena_incremental_effect_vs_policy_off": {
                "work_time_reduction": arena_time,
                "peak_rss_reduction": arena_peak_rss,
                "steady_pss_reduction": arena_pss,
                "claim_boundary": (
                    "policy-off carries lifetime metadata into the base allocator identity; "
                    "this comparison measures the incremental dedicated lifetime arena only"
                ),
            },
        },
        "claim_gates": {
            "matched_work": matched_work,
            "enough_paired_repeats": enough_pairs,
            "mechanism_claim_ready": mechanism_ready,
            "runtime_adaptive_mechanism_claim_ready": adaptive_mechanism_ready,
            "performance_claim_ready": performance_ready,
            "memory_reduction_claim_ready": memory_ready,
            "post_observation_memory_samples": post_observation_memory_samples,
            "dedicated_lifetime_arena_steady_pss_reduction_claim_ready": (
                arena_memory_ready
            ),
        },
        "claim_boundary": (
            "The mechanism gate proves an eventually-freed oracle-Long cohort was "
            "routed to allocator-owned THP-eligible mappings and obtained process "
            "AnonHugePages in every selective sample. Performance and memory gates "
            "cover only this paired micro-workload. The adaptive gate separately proves "
            "eight completed pressure-Long training outcomes can route a later normal-drop "
            "cohort. Compiler inference and external "
            "application generalization require separate evidence."
        ),
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def blocked_schedule(
    arms: list[Arm], repeats: int, *, seed: int
) -> list[tuple[int, Arm]]:
    """Keep paired samples time-local while randomizing order inside each block."""

    rng = random.Random(seed)
    schedule: list[tuple[int, Arm]] = []
    for repeat in range(repeats):
        block = list(arms)
        rng.shuffle(block)
        schedule.extend((repeat, arm) for arm in block)
    return schedule


def main() -> int:
    args = parse_args()
    root = repo_root()
    external = parse_external_allocators(args.external_allocator)
    compiler_evidence: dict[str, Any] = {
        "requested": False,
        "compiler_layer_measured": False,
        "exact_runtime_audit_join_performed": False,
        "claim_boundary": "measured arms do not use compiler-generated lifetime hints",
    }
    if args.binary is not None:
        binary = args.binary.expanduser().resolve()
    elif args.skip_build:
        binary = (
            fixture_workspace(root)
            / "target"
            / "release"
            / "eventually_freed_lifetime_workload"
        )
    else:
        binary, compiler_evidence = build_fixture(
            root, args.compiler_wrapper, args.timeout
        )
    if not binary.is_file():
        raise RuntimeError(f"fixture binary does not exist: {binary}")

    arms = [Arm(name=name, fixture_arm=name) for name in PRIMARY_ARMS]
    arms.extend(external)
    for warmup, arm in blocked_schedule(arms, args.warmups, seed=args.seed ^ 0xA55A):
        run_arm(binary, arm, -(warmup + 1), args)
    schedule = blocked_schedule(arms, args.repeats, seed=args.seed)
    rows: list[dict[str, Any]] = []
    for repeat, arm in schedule:
        row = run_arm(binary, arm, repeat, args)
        rows.append(row)
        print(json.dumps(row, sort_keys=True), flush=True)

    summary = summarize(rows, seed=args.seed)
    summary["run_id"] = args.run_id
    summary["compiler_evidence"] = compiler_evidence
    summary["configuration"] = {
        "long_objects": args.long_objects,
        "short_objects": args.short_objects,
        "object_bytes": args.object_bytes,
        "waves": args.waves,
        "passes_per_wave": args.passes_per_wave,
        "thp_deadline_ms": args.thp_deadline_ms,
        "warmups": args.warmups,
        "repeats": args.repeats,
        "seed": args.seed,
        "external_allocators": [arm.name for arm in external],
        "external_allocator_provenance": {
            arm.name: allocator_provenance(arm.preload)
            for arm in external
            if arm.preload is not None
        },
    }
    raw_path = root / "evaluation" / "raw" / args.run_id / "rows.json"
    result_path = root / "evaluation" / "results" / args.run_id / "summary.json"
    write_json(raw_path, rows)
    write_json(result_path, summary)
    print(json.dumps({"summary": summary, "result_path": str(result_path)}, sort_keys=True))
    if not args.allow_zero_thp and (
        not summary["claim_gates"]["mechanism_claim_ready"]
        or not summary["claim_gates"]["runtime_adaptive_mechanism_claim_ready"]
    ):
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"eventually_freed_lifetime_experiment: {error}", file=sys.stderr)
        raise SystemExit(2)
