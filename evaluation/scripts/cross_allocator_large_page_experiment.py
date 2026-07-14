#!/usr/bin/env python3
"""Run defensible UniAlloc and cross-allocator large-page comparisons.

The experiment deliberately has two families.  The semantic family compares
UniAlloc lifetime-directed THP against the identical lifetime policy with
process THP backing disabled.  The neutral family sends one malloc/free trace
to general-purpose allocators and measures each allocator's own large-page
configuration delta.  Raw timings never cross those family boundaries.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lifetime_thp_allocator_experiment as lifetime_thp


ROOT = Path(__file__).resolve().parents[2]
NEUTRAL_SOURCE = ROOT / "evaluation/probes/cross_allocator_large_page_workload.c"
NEUTRAL_BINARY = ROOT / "target/evaluation/cross_allocator_large_page_workload"
SEMANTIC_BINARY = ROOT / "target/release/examples/lifetime_hugepage_allocator_probe"
DEFAULT_TCMALLOC = Path(
    "/home/hanqing/.local/state/unialloc/runs/"
    "g001-minimal-235549b2-20260711T155042Z/evidence/"
    "pilot-controller/repairs/tcmalloc-full-runtime-v1-20260711T1717Z-attempt2/"
    "lib/libtcmalloc.so.4.6.5"
)
DEFAULT_JEMALLOC = Path("/usr/lib/x86_64-linux-gnu/libjemalloc.so.2")
LOCAL_BUILD_ROOT = ROOT / "evaluation/raw/cross-allocator-local-builds"
DEFAULT_MIMALLOC = LOCAL_BUILD_ROOT / "mimalloc-manual/libmimalloc.so"
DEFAULT_SNMALLOC = LOCAL_BUILD_ROOT / "snmalloc/libsnmallocshim.so"
SANITIZED_ENV_PREFIXES = ("MIMALLOC_", "TCMALLOC_")
SANITIZED_ENV_KEYS = {
    "LD_PRELOAD",
    "MALLOC_CONF",
    "_RJEM_MALLOC_CONF",
    "UNIALLOC_EVAL_PRELOAD_TOKEN",
    "UNIALLOC_EVAL_THP_DISABLE",
}


@dataclass(frozen=True)
class SemanticCase:
    name: str
    label: str
    policy: str
    expected_backing: str
    disable_process_thp: bool = False


@dataclass(frozen=True)
class NeutralCase:
    name: str
    label: str
    mechanism: str
    expected_backing: str
    library_key: str | None
    environment: tuple[tuple[str, str], ...] = ()
    disable_process_thp: bool = False


SEMANTIC_CASES = (
    SemanticCase("unialloc_default", "UniAlloc default", "raw-default", "system-default"),
    SemanticCase(
        "unialloc_ordinary",
        "UniAlloc ordinary arenas",
        "ordinary-segregated",
        "ordinary-no-thp",
    ),
    SemanticCase(
        "unialloc_lifetime_thp_off",
        "UniAlloc lifetime layout / THP off",
        "long-thp",
        "ordinary-no-thp",
        True,
    ),
    SemanticCase(
        "unialloc_lifetime_thp_on",
        "UniAlloc selective lifetime THP",
        "long-thp",
        "thp",
    ),
)


def neutral_cases() -> tuple[NeutralCase, ...]:
    mimalloc_common = (
        ("MIMALLOC_ALLOW_LARGE_OS_PAGES", "0"),
        ("MIMALLOC_RESERVE_HUGE_OS_PAGES", "0"),
        # Hold purge granularity constant so the THP pair isolates eligibility.
        ("MIMALLOC_MINIMAL_PURGE_SIZE", "2048"),
    )
    return (
        NeutralCase(
            "glibc_default", "glibc default", "default", "default", None
        ),
        NeutralCase(
            "mimalloc_thp_off",
            "mimalloc THP off",
            "process-wide-thp",
            "no-thp",
            "mimalloc",
            mimalloc_common + (("MIMALLOC_ALLOW_THP", "0"),),
        ),
        NeutralCase(
            "mimalloc_thp_on",
            "mimalloc THP on",
            "process-wide-thp",
            "thp",
            "mimalloc",
            mimalloc_common + (("MIMALLOC_ALLOW_THP", "1"),),
        ),
        NeutralCase(
            "jemalloc_thp_off",
            "jemalloc THP off",
            "process-wide-thp",
            "no-thp",
            "jemalloc",
            (("MALLOC_CONF", "abort_conf:true,thp:never,metadata_thp:disabled"),),
        ),
        NeutralCase(
            "jemalloc_thp_on",
            "jemalloc THP on",
            "process-wide-thp",
            "thp",
            "jemalloc",
            (("MALLOC_CONF", "abort_conf:true,thp:always,metadata_thp:disabled"),),
        ),
        NeutralCase(
            "gperftools_default",
            "gperftools TCMalloc default",
            "default",
            "default",
            "tcmalloc",
            (("TCMALLOC_MEMFS_MALLOC_PATH", ""),),
        ),
        NeutralCase(
            "gperftools_hugetlb",
            "gperftools explicit HugeTLB",
            "explicit-hugetlb",
            "hugetlb",
            "tcmalloc",
            (("TCMALLOC_MEMFS_DISABLE_FALLBACK", "1"),),
        ),
        NeutralCase(
            "snmalloc_default",
            "snmalloc default eligibility",
            "os-eligibility",
            "default",
            "snmalloc",
        ),
        NeutralCase(
            "snmalloc_os_thp_off",
            "snmalloc process THP disabled",
            "os-eligibility",
            "no-thp",
            "snmalloc",
            disable_process_thp=True,
        ),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=12)
    parser.add_argument("--warmup-blocks", type=int, default=1)
    parser.add_argument("--bootstrap-resamples", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=20260714)
    parser.add_argument("--cpu", type=int, default=15)
    parser.add_argument("--numa-node", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--objects", type=int, default=131_072)
    parser.add_argument("--slot-bytes", type=int, default=4096)
    parser.add_argument("--passes", type=int, default=32)
    parser.add_argument("--warmup-passes", type=int, default=2)
    parser.add_argument("--waves", type=int, default=2)
    parser.add_argument("--settle-ms", type=int, default=25)
    parser.add_argument("--types-per-truth", type=int, default=8)
    parser.add_argument("--false-long-rate", type=float, default=0.05)
    parser.add_argument("--false-short-rate", type=float, default=0.05)
    parser.add_argument("--min-semantic-thp-coverage", type=float, default=0.95)
    parser.add_argument("--min-neutral-thp-coverage", type=float, default=0.25)
    parser.add_argument("--mimalloc-lib", type=Path, default=DEFAULT_MIMALLOC)
    parser.add_argument("--jemalloc-lib", type=Path, default=DEFAULT_JEMALLOC)
    parser.add_argument("--tcmalloc-lib", type=Path, default=DEFAULT_TCMALLOC)
    parser.add_argument("--snmalloc-lib", type=Path, default=DEFAULT_SNMALLOC)
    parser.add_argument("--tcmalloc-memfs-prefix", type=Path, required=True)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-warmup", action="store_true")
    args = parser.parse_args(argv)
    if (
        args.repeats < 2
        or args.warmup_blocks < 0
        or args.bootstrap_resamples < 100
        or args.objects < 4
        or args.objects % 2
        or args.slot_bytes < 128
        or args.passes < 1
        or args.waves < 1
        or args.settle_ms < 0
        or args.types_per_truth < 1
        or not 0 <= args.false_long_rate <= 0.5
        or not 0 <= args.false_short_rate <= 0.5
        or not 0 < args.min_semantic_thp_coverage <= 1
        or not 0 < args.min_neutral_thp_coverage <= 1
    ):
        parser.error("invalid experiment geometry or evidence threshold")
    return args


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_output(command: list[str]) -> str:
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    ).stdout.strip()


def git_text(*args: str) -> str:
    return checked_output(["git", *args])


def hugepages_free() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("HugePages_Free:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def host_snapshot() -> str:
    paths = (
        "/sys/kernel/mm/transparent_hugepage/enabled",
        "/sys/kernel/mm/transparent_hugepage/defrag",
        "/sys/kernel/mm/transparent_hugepage/hpage_pmd_size",
    )
    parts = [
        f"captured_at={time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"uname={checked_output(['uname', '-a'])}",
        "lscpu:\n" + checked_output(["lscpu"]),
        "meminfo:\n"
        + "\n".join(
            line
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
            if "Huge" in line or line.startswith("MemAvailable:")
        ),
    ]
    for raw in paths:
        path = Path(raw)
        parts.append(f"{raw}={path.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(parts) + "\n"


def library_provenance(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    configured = {
        "mimalloc": (args.mimalloc_lib, "mimalloc core 3.3.2"),
        "jemalloc": (args.jemalloc_lib, "jemalloc 5.3.x system package"),
        "tcmalloc": (args.tcmalloc_lib, "gperftools TCMalloc 2.18.1"),
        "snmalloc": (args.snmalloc_lib, "snmalloc 0.2.27 vendored build"),
    }
    result: dict[str, dict[str, Any]] = {}
    for key, (path, version) in configured.items():
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise RuntimeError(f"missing {key} library: {resolved}")
        result[key] = {
            "path": str(resolved),
            "sha256": sha256_file(resolved),
            "version_label": version,
        }
    return result


def validate_memfs_prefix(prefix: Path) -> Path:
    resolved = prefix.expanduser().resolve()
    if not resolved.parent.is_dir() or not os.access(resolved.parent, os.W_OK):
        raise RuntimeError(f"TCMalloc memfs prefix parent is not writable: {resolved.parent}")
    filesystem = subprocess.run(
        ["stat", "-f", "-c", "%T", str(resolved.parent)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    if filesystem != "hugetlbfs":
        raise RuntimeError(f"TCMalloc memfs prefix must be on hugetlbfs, got {filesystem}")
    return resolved


def build_probes(args: argparse.Namespace) -> list[dict[str, Any]]:
    commands = [
        [
            "cc",
            "-std=c11",
            "-O3",
            "-DNDEBUG",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fno-builtin-malloc",
            str(NEUTRAL_SOURCE),
            "-o",
            str(NEUTRAL_BINARY),
        ],
        [
            "cargo",
            "build",
            "--release",
            "-p",
            "unialloc",
            "--example",
            "lifetime_hugepage_allocator_probe",
            "--features",
            "lifetime_hugepage",
        ],
    ]
    if not args.skip_build:
        NEUTRAL_BINARY.parent.mkdir(parents=True, exist_ok=True)
        for command in commands:
            subprocess.run(command, cwd=ROOT, check=True)
    for binary in (NEUTRAL_BINARY, SEMANTIC_BINARY):
        if not binary.is_file():
            raise RuntimeError(f"missing probe binary: {binary}")
    return [
        {
            "command": command,
            "binary": str(binary.relative_to(ROOT)),
            "binary_sha256": sha256_file(binary),
        }
        for command, binary in zip(commands, (NEUTRAL_BINARY, SEMANTIC_BINARY), strict=True)
    ]


def pinned(command: list[str], args: argparse.Namespace) -> list[str]:
    return [
        "numactl",
        f"--physcpubind={args.cpu}",
        f"--membind={args.numa_node}",
        *command,
    ]


def semantic_command(
    case: SemanticCase, args: argparse.Namespace, repeat_seed: int
) -> list[str]:
    command = [
        str(SEMANTIC_BINARY),
        "--policy",
        case.policy,
        "--identity-mode",
        "exact",
        "--types-per-truth",
        str(args.types_per_truth),
        "--objects",
        str(args.objects),
        "--slot-bytes",
        str(args.slot_bytes),
        "--long-fraction",
        "0.5",
        "--false-long-rate",
        str(args.false_long_rate),
        "--false-short-rate",
        str(args.false_short_rate),
        "--confidence-threshold",
        "0",
        "--correct-confidence",
        "95",
        "--error-confidence",
        "40",
        "--confidence-overlap-rate",
        "0",
        "--unknown-rate",
        "0",
        "--ephemeral-waves",
        str(args.waves),
        "--warmup-passes",
        str(args.warmup_passes),
        "--passes",
        str(args.passes),
        "--seed",
        str(repeat_seed),
    ]
    if case.disable_process_thp:
        command.extend(("--disable-process-thp", "--require-no-thp"))
    elif case.expected_backing == "thp":
        command.append("--require-thp")
    elif case.expected_backing == "ordinary-no-thp":
        command.append("--require-no-thp")
    return pinned(command, args)


def neutral_command(args: argparse.Namespace, repeat_seed: int) -> list[str]:
    return pinned(
        [
            str(NEUTRAL_BINARY),
            "--objects",
            str(args.objects),
            "--slot-bytes",
            str(args.slot_bytes),
            "--passes",
            str(args.passes),
            "--warmup-passes",
            str(args.warmup_passes),
            "--waves",
            str(args.waves),
            "--settle-ms",
            str(args.settle_ms),
            "--seed",
            str(repeat_seed),
        ],
        args,
    )


def clean_allocator_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in SANITIZED_ENV_KEYS
        and not any(key.startswith(prefix) for prefix in SANITIZED_ENV_PREFIXES)
    }


def neutral_environment(
    case: NeutralCase,
    libraries: dict[str, dict[str, Any]],
    memfs_prefix: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    env = clean_allocator_environment()
    explicit = dict(case.environment)
    if case.library_key is not None:
        library = Path(libraries[case.library_key]["path"])
        explicit["LD_PRELOAD"] = str(library)
        explicit["UNIALLOC_EVAL_PRELOAD_TOKEN"] = library.name
    if case.name == "gperftools_hugetlb":
        explicit["TCMALLOC_MEMFS_MALLOC_PATH"] = str(memfs_prefix)
    if case.disable_process_thp:
        explicit["UNIALLOC_EVAL_THP_DISABLE"] = "1"
    env.update(explicit)
    return env, explicit


def parse_single_json(stdout: str) -> dict[str, Any]:
    rows = [line for line in stdout.splitlines() if line.strip().startswith("{")]
    if len(rows) != 1:
        raise RuntimeError(f"expected one JSON row, got {len(rows)}")
    return json.loads(rows[0])


def validate_neutral_row(
    row: dict[str, Any], case: NeutralCase, min_thp_coverage: float
) -> list[str]:
    failures: list[str] = []
    if row.get("source") != "cross_allocator_large_page_workload":
        failures.append("source")
    if row.get("passed") is not True:
        failures.append("probe_failed")
    if row.get("host_thp_mode") != "madvise":
        failures.append("host_thp_mode")
    if row.get("memory_evidence_complete") is not True:
        failures.append("memory_evidence")
    if case.library_key is not None and row.get("preload_token_observed") is not True:
        failures.append("preload_not_observed")
    anon_huge = int(row.get("anon_huge_delta_kib", -1))
    hugetlb = int(row.get("hugetlb_delta_kib", -1))
    if case.expected_backing == "thp":
        if anon_huge <= 0:
            failures.append("missing_anon_thp")
        if float(row.get("peak_thp_coverage", 0)) < min_thp_coverage:
            failures.append("thp_coverage")
        if hugetlb != 0:
            failures.append("unexpected_hugetlb")
    elif case.expected_backing == "no-thp":
        if anon_huge != 0:
            failures.append("unexpected_anon_thp")
    elif case.expected_backing == "hugetlb":
        if hugetlb <= 0:
            failures.append("missing_hugetlb")
        if anon_huge != 0:
            failures.append("unexpected_anon_thp")
    if case.disable_process_thp and int(row.get("process_thp_disabled", 0)) != 1:
        failures.append("process_thp_disable")
    if case.name == "mimalloc_thp_off" and int(row.get("process_thp_disabled", 0)) != 1:
        failures.append("mimalloc_process_thp_disable")
    return failures


def run_process(
    command: list[str],
    *,
    env: dict[str, str] | None,
    timeout: int,
    stdout_path: Path,
    stderr_path: Path,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr[-4000:]}"
        )
    return completed


def semantic_topology_failures(rows: dict[str, dict[str, Any]]) -> list[str]:
    on = rows["unialloc_lifetime_thp_on"]
    off = rows["unialloc_lifetime_thp_off"]
    failures: list[str] = []
    for field in (
        "prediction_trace_digest",
        "routed_allocations",
        "routed_deallocations",
        "thp_extent_mappings",
        "ordinary_extent_mappings",
        "identity_region_assignments",
        "slot_bump_allocations",
        "slot_reuse_hits",
    ):
        if on.get(field) != off.get(field):
            failures.append(f"same_policy_topology:{field}")
    if off.get("process_thp_disable_requested") is not True:
        failures.append("forced_off_request")
    if off.get("process_thp_disabled") is not True:
        failures.append("forced_off_status")
    if int(off.get("observed_anon_huge_delta_kib", -1)) != 0:
        failures.append("forced_off_backing")
    return failures


def run_block(
    *,
    block: int,
    measured: bool,
    args: argparse.Namespace,
    libraries: dict[str, dict[str, Any]],
    memfs_prefix: Path,
    logs_dir: Path,
    randomizer: random.Random,
) -> list[dict[str, Any]]:
    repeat_seed = args.seed + block * 1_000_003
    rows: list[dict[str, Any]] = []
    semantic_order = list(SEMANTIC_CASES)
    randomizer.shuffle(semantic_order)
    semantic_rows: dict[str, dict[str, Any]] = {}
    for order_index, case in enumerate(semantic_order):
        prefix = f"block-{block:03d}-semantic-{order_index:02d}-{case.name}"
        completed = run_process(
            semantic_command(case, args, repeat_seed),
            env=clean_allocator_environment(),
            timeout=args.timeout,
            stdout_path=logs_dir / f"{prefix}.stdout",
            stderr_path=logs_dir / f"{prefix}.stderr",
        )
        row = lifetime_thp.parse_probe(
            completed.stdout,
            case.expected_backing,
            min_steady_thp_coverage=args.min_semantic_thp_coverage,
        )
        row.update(
            {
                "family": "unialloc-semantic",
                "case": case.name,
                "label": case.label,
                "mechanism": "selective-lifetime-thp",
                "block": block,
                "measured": measured,
                "order_index": order_index,
                "repeat_seed": repeat_seed,
                "command": semantic_command(case, args, repeat_seed),
            }
        )
        semantic_rows[case.name] = row
        rows.append(row)
    topology_failures = semantic_topology_failures(semantic_rows)
    if topology_failures:
        raise RuntimeError(f"semantic block {block} failed: {','.join(topology_failures)}")

    neutral_order = list(neutral_cases())
    randomizer.shuffle(neutral_order)
    neutral_rows: list[dict[str, Any]] = []
    for order_index, case in enumerate(neutral_order):
        prefix = f"block-{block:03d}-neutral-{order_index:02d}-{case.name}"
        env, explicit_env = neutral_environment(case, libraries, memfs_prefix)
        pool_before = hugepages_free()
        completed = run_process(
            neutral_command(args, repeat_seed),
            env=env,
            timeout=args.timeout,
            stdout_path=logs_dir / f"{prefix}.stdout",
            stderr_path=logs_dir / f"{prefix}.stderr",
        )
        pool_after = hugepages_free()
        row = parse_single_json(completed.stdout)
        failures = validate_neutral_row(row, case, args.min_neutral_thp_coverage)
        if case.expected_backing == "hugetlb" and pool_before != pool_after:
            failures.append("hugetlb_pool_not_restored")
        row.update(
            {
                "family": "allocator-neutral",
                "case": case.name,
                "label": case.label,
                "mechanism": case.mechanism,
                "expected_backing": case.expected_backing,
                "block": block,
                "measured": measured,
                "order_index": order_index,
                "repeat_seed": repeat_seed,
                "explicit_environment": explicit_env,
                "command": neutral_command(args, repeat_seed),
                "hugepages_free_before": pool_before,
                "hugepages_free_after": pool_after,
                "evidence_failures": failures,
            }
        )
        if failures:
            raise RuntimeError(
                f"neutral block {block}/{case.name} failed: {','.join(failures)}"
            )
        neutral_rows.append(row)
        rows.append(row)
    checksums = {str(row["checksum"]) for row in neutral_rows}
    if len(checksums) != 1:
        raise RuntimeError(f"neutral block {block} checksum mismatch: {checksums}")
    return rows


def percentile(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires samples")
    position = probability * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def paired_effect(
    rows_by_case: dict[str, list[dict[str, Any]]],
    *,
    target: str,
    baseline: str,
    field: str,
    seed: int,
    resamples: int,
    equivalence_pct: float,
) -> dict[str, Any]:
    target_by_block = {int(row["block"]): float(row[field]) for row in rows_by_case[target]}
    baseline_by_block = {
        int(row["block"]): float(row[field]) for row in rows_by_case[baseline]
    }
    blocks = sorted(set(target_by_block) & set(baseline_by_block))
    if len(blocks) != len(target_by_block) or len(blocks) != len(baseline_by_block):
        raise RuntimeError(f"incomplete pair {target}/{baseline}")
    log_ratios = [
        math.log(target_by_block[block] / baseline_by_block[block]) for block in blocks
    ]

    def improvement(values: Iterable[float]) -> float:
        values = list(values)
        return (1.0 - math.exp(statistics.fmean(values))) * 100.0

    point = improvement(log_ratios)
    rng = random.Random(seed)
    bootstrap = sorted(
        improvement(log_ratios[rng.randrange(len(log_ratios))] for _ in log_ratios)
        for _ in range(resamples)
    )
    low = percentile(bootstrap, 0.025)
    high = percentile(bootstrap, 0.975)
    if low > 0:
        conclusion = "improved"
    elif high < 0:
        conclusion = "regressed"
    elif low >= -equivalence_pct and high <= equivalence_pct:
        conclusion = "practically-equivalent"
    else:
        conclusion = "inconclusive"
    return {
        "target": target,
        "baseline": baseline,
        "metric": field,
        "sample_count": len(blocks),
        "paired_geomean_ratio": math.exp(statistics.fmean(log_ratios)),
        "improvement_pct": point,
        "ci_low_pct": low,
        "ci_high_pct": high,
        "lower_is_better": True,
        "equivalence_band_pct": equivalence_pct,
        "conclusion": conclusion,
        "block_ratios": [math.exp(value) for value in log_ratios],
    }


def median_summary(rows: list[dict[str, Any]], family: str) -> dict[str, Any]:
    fields = (
        ("ns_per_touch", "median_ns_per_touch"),
        ("max_effective_resident_kib", "median_max_effective_resident_kib"),
    )
    if family == "unialloc-semantic":
        fields += (
            (
                "allocator_lifecycle_ns_per_allocation",
                "median_lifecycle_ns_per_allocation",
            ),
            ("observed_anon_huge_delta_kib", "median_anon_huge_delta_kib"),
            ("peak_hugetlb_kib", "median_hugetlb_delta_kib"),
            ("classification_failure_rate", "median_classification_failure_rate"),
            (
                "policy_intent_placement_failure_rate",
                "median_policy_intent_placement_failure_rate",
            ),
        )
    else:
        fields += (
            ("lifecycle_ns_per_allocation", "median_lifecycle_ns_per_allocation"),
            ("anon_huge_delta_kib", "median_anon_huge_delta_kib"),
            ("hugetlb_delta_kib", "median_hugetlb_delta_kib"),
            ("peak_thp_coverage", "median_peak_thp_coverage"),
        )
    result = {output: statistics.median(float(row[source]) for row in rows) for source, output in fields}
    result.update(
        {
            "case": rows[0]["case"],
            "label": rows[0]["label"],
            "family": family,
            "mechanism": rows[0]["mechanism"],
            "samples": len(rows),
        }
    )
    return result


def summarize(
    rows: list[dict[str, Any]], args: argparse.Namespace
) -> dict[str, Any]:
    measured = [row for row in rows if row["measured"]]
    rows_by_case: dict[str, list[dict[str, Any]]] = {}
    for row in measured:
        rows_by_case.setdefault(str(row["case"]), []).append(row)
    case_summaries = {
        case: median_summary(case_rows, str(case_rows[0]["family"]))
        for case, case_rows in rows_by_case.items()
    }
    pairs = (
        (
            "unialloc_lifetime_thp",
            "unialloc_lifetime_thp_on",
            "unialloc_lifetime_thp_off",
            "UniAlloc selective THP",
            "selective-thp",
        ),
        (
            "mimalloc_thp",
            "mimalloc_thp_on",
            "mimalloc_thp_off",
            "mimalloc global THP",
            "process-wide-thp",
        ),
        (
            "jemalloc_thp",
            "jemalloc_thp_on",
            "jemalloc_thp_off",
            "jemalloc global THP",
            "process-wide-thp",
        ),
        (
            "gperftools_hugetlb",
            "gperftools_hugetlb",
            "gperftools_default",
            "gperftools HugeTLB",
            "explicit-hugetlb",
        ),
        (
            "snmalloc_os_eligibility",
            "snmalloc_default",
            "snmalloc_os_thp_off",
            "snmalloc OS eligibility",
            "os-eligibility",
        ),
    )
    comparisons: dict[str, dict[str, Any]] = {}
    toggle_effects: list[dict[str, Any]] = []
    for index, (name, target, baseline, label, mechanism) in enumerate(pairs):
        touch = paired_effect(
            rows_by_case,
            target=target,
            baseline=baseline,
            field="ns_per_touch",
            seed=args.seed + index * 11,
            resamples=args.bootstrap_resamples,
            equivalence_pct=3.0,
        )
        memory = paired_effect(
            rows_by_case,
            target=target,
            baseline=baseline,
            field="max_effective_resident_kib",
            seed=args.seed + index * 11 + 1,
            resamples=args.bootstrap_resamples,
            equivalence_pct=5.0,
        )
        comparisons[name] = {"touch": touch, "max_effective_resident": memory}
        toggle_effects.append(
            {
                "label": label,
                "mechanism": mechanism,
                "speedup_pct": touch["improvement_pct"],
                "speedup_ci_low_pct": touch["ci_low_pct"],
                "speedup_ci_high_pct": touch["ci_high_pct"],
                "speedup_conclusion": touch["conclusion"],
                "max_resident_change_pct": -memory["improvement_pct"],
                "max_resident_ci_low_pct": -memory["ci_high_pct"],
                "max_resident_ci_high_pct": -memory["ci_low_pct"],
                "max_resident_conclusion": memory["conclusion"],
            }
        )

    endpoint_names = (
        "glibc_default",
        "mimalloc_thp_off",
        "mimalloc_thp_on",
        "jemalloc_thp_off",
        "jemalloc_thp_on",
        "gperftools_default",
        "gperftools_hugetlb",
        "snmalloc_default",
        "snmalloc_os_thp_off",
    )
    pair_for = {
        "mimalloc_thp_off": "mimalloc",
        "mimalloc_thp_on": "mimalloc",
        "jemalloc_thp_off": "jemalloc",
        "jemalloc_thp_on": "jemalloc",
        "gperftools_default": "gperftools",
        "gperftools_hugetlb": "gperftools",
        "snmalloc_default": "snmalloc",
        "snmalloc_os_thp_off": "snmalloc",
        "glibc_default": "glibc",
    }
    mode_for = {
        "glibc_default": "default",
        "mimalloc_thp_off": "off",
        "mimalloc_thp_on": "on",
        "jemalloc_thp_off": "off",
        "jemalloc_thp_on": "on",
        "gperftools_default": "off",
        "gperftools_hugetlb": "on",
        "snmalloc_default": "on",
        "snmalloc_os_thp_off": "off",
    }
    endpoint_label_for = {
        "glibc_default": "glibc",
        "mimalloc_thp_off": "mimalloc off",
        "mimalloc_thp_on": "mimalloc THP",
        "jemalloc_thp_off": "jemalloc off",
        "jemalloc_thp_on": "jemalloc THP",
        "gperftools_default": "gperftools anon",
        "gperftools_hugetlb": "gperftools HugeTLB",
        "snmalloc_default": "snmalloc eligible",
        "snmalloc_os_thp_off": "snmalloc disabled",
    }
    endpoints = []
    for case in endpoint_names:
        summary = case_summaries[case]
        endpoints.append(
            {
                "label": endpoint_label_for[case],
                "mechanism": summary["mechanism"],
                "ns_per_touch": summary["median_ns_per_touch"],
                "max_effective_resident_mib": summary[
                    "median_max_effective_resident_kib"
                ]
                / 1024,
                "large_page_backing_mib": (
                    summary["median_anon_huge_delta_kib"]
                    + summary["median_hugetlb_delta_kib"]
                )
                / 1024,
                "pair": pair_for[case],
                "mode": mode_for[case],
            }
        )
    backing_names = (
        "unialloc_lifetime_thp_off",
        "unialloc_lifetime_thp_on",
        "mimalloc_thp_off",
        "mimalloc_thp_on",
        "jemalloc_thp_off",
        "jemalloc_thp_on",
        "gperftools_default",
        "gperftools_hugetlb",
        "snmalloc_default",
    )
    backing_label_for = {
        "unialloc_lifetime_thp_off": "UniAlloc off",
        "unialloc_lifetime_thp_on": "UniAlloc selective THP",
        "mimalloc_thp_off": "mimalloc off",
        "mimalloc_thp_on": "mimalloc THP",
        "jemalloc_thp_off": "jemalloc off",
        "jemalloc_thp_on": "jemalloc THP",
        "gperftools_default": "gperftools anon",
        "gperftools_hugetlb": "gperftools HugeTLB",
        "snmalloc_default": "snmalloc default",
    }
    backing = [
        {
            "label": backing_label_for[case],
            "anon_thp_mib": case_summaries[case]["median_anon_huge_delta_kib"] / 1024,
            "hugetlb_mib": case_summaries[case]["median_hugetlb_delta_kib"] / 1024,
        }
        for case in backing_names
    ]
    return {
        "schema_version": 1,
        "source": "cross_allocator_large_page_experiment",
        "claim_grade": True,
        "case_summaries": case_summaries,
        "comparisons": comparisons,
        "slide_data": {
            "toggle_effects": toggle_effects,
            "endpoints": endpoints,
            "backing": backing,
        },
        "evidence_contract": {
            "semantic_primary_pair": (
                "same long-thp policy and trace; PR_SET_THP_DISABLE changes only "
                "physical THP eligibility"
            ),
            "neutral_pairing": "same malloc/free binary and trace within each allocator",
            "actual_thp_backing": "/proc/self/smaps_rollup AnonHugePages",
            "actual_hugetlb_backing": "/proc/self/status HugetlbPages",
            "resident_metric": "max(VmRSS + HugetlbPages) across live phases",
            "cross_family_raw_timing_ranking_allowed": False,
            "gperftools_is_google_tcmalloc": False,
            "snmalloc_control_kind": "OS eligibility; snmalloc has no public THP toggle",
        },
        "invariants_passed": True,
    }


def write_summary_csv(summary: dict[str, Any], path: Path) -> None:
    fields = (
        "case",
        "label",
        "family",
        "mechanism",
        "samples",
        "median_ns_per_touch",
        "median_lifecycle_ns_per_allocation",
        "median_max_effective_resident_kib",
        "median_anon_huge_delta_kib",
        "median_hugetlb_delta_kib",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for case in sorted(summary["case_summaries"]):
            writer.writerow(summary["case_summaries"][case])


def write_toggle_csv(summary: dict[str, Any], path: Path) -> None:
    rows = summary["slide_data"]["toggle_effects"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_manifest(directory: Path, *, exclude: set[str] | None = None) -> None:
    excluded = exclude or {"manifest.json"}
    files = []
    for path in sorted(candidate for candidate in directory.rglob("*") if candidate.is_file()):
        relative = str(path.relative_to(directory))
        if relative in excluded:
            continue
        files.append(
            {"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        )
    (directory / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "files": files}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    logs_dir = output_dir / "raw"
    logs_dir.mkdir()
    (output_dir / "host-before.txt").write_text(host_snapshot(), encoding="utf-8")
    libraries = library_provenance(args)
    memfs_prefix = validate_memfs_prefix(args.tcmalloc_memfs_prefix)
    builds = build_probes(args)
    runner_manifest = {
        "schema_version": 1,
        "argv": [sys.executable, str(Path(__file__).resolve()), *(argv or sys.argv[1:])],
        "git_head": git_text("rev-parse", "HEAD"),
        "git_status": git_text("status", "--short"),
        "cpu": args.cpu,
        "numa_node": args.numa_node,
        "repeats": args.repeats,
        "warmup_blocks": 0 if args.skip_warmup else args.warmup_blocks,
        "bootstrap_resamples": args.bootstrap_resamples,
        "seed": args.seed,
        "objects": args.objects,
        "slot_bytes": args.slot_bytes,
        "passes": args.passes,
        "warmup_passes": args.warmup_passes,
        "waves": args.waves,
        "settle_ms": args.settle_ms,
        "min_semantic_thp_coverage": args.min_semantic_thp_coverage,
        "min_neutral_thp_coverage": args.min_neutral_thp_coverage,
        "libraries": libraries,
        "tcmalloc_memfs_prefix": str(memfs_prefix),
        "probe_builds": builds,
        "neutral_source_sha256": sha256_file(NEUTRAL_SOURCE),
        "semantic_source_sha256": sha256_file(
            ROOT / "unialloc/examples/lifetime_hugepage_allocator_probe.rs"
        ),
    }
    (output_dir / "runner-manifest.json").write_text(
        json.dumps(runner_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    rows: list[dict[str, Any]] = []
    randomizer = random.Random(args.seed)
    warmups = 0 if args.skip_warmup else args.warmup_blocks
    total_blocks = warmups + args.repeats
    samples_path = output_dir / "samples.jsonl"
    with samples_path.open("w", encoding="utf-8") as samples:
        for block in range(total_blocks):
            measured = block >= warmups
            block_rows = run_block(
                block=block,
                measured=measured,
                args=args,
                libraries=libraries,
                memfs_prefix=memfs_prefix,
                logs_dir=logs_dir,
                randomizer=randomizer,
            )
            rows.extend(block_rows)
            for row in block_rows:
                samples.write(json.dumps(row, sort_keys=True) + "\n")
            samples.flush()
            print(
                f"block={block} measured={measured} "
                f"samples={len(block_rows)}",
                flush=True,
            )

    summary = summarize(rows, args)
    summary.update(
        {
            "run_id": output_dir.name,
            "measured_repeats": args.repeats,
            "warmup_blocks": warmups,
            "objects": args.objects,
            "slot_bytes": args.slot_bytes,
            "passes": args.passes,
            "waves": args.waves,
            "seed": args.seed,
        }
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_summary_csv(summary, output_dir / "summary.csv")
    write_toggle_csv(summary, output_dir / "toggle-effects.csv")
    (output_dir / "host-after.txt").write_text(host_snapshot(), encoding="utf-8")
    write_manifest(output_dir)
    print(output_dir / "summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
