#!/usr/bin/env python3
"""Reproducible A/B for Type Isolation pointer-filter domain salting."""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import json
import math
import os
import pathlib
import statistics
import subprocess
import tempfile
import time
from typing import Any, Iterator


ROOT = pathlib.Path(__file__).resolve().parents[2]
FILTER_SCENARIOS = (
    "mixed_same_hash_2t",
    "single_recovery_1t",
    "single_retained_1t",
    "negative_query_1t",
)
HOTPATH_SCENARIOS = (
    "raw",
    "raw64",
    "raw64_under_global_recovery",
    "local_same",
    "conservative_same",
    "conservative_split",
    "conservative_unscoped_drop",
)
VARIANTS = ("baseline", "candidate")
SANITIZED_ENV_NAMES = {
    "CARGO_BUILD_RUSTC_WRAPPER",
    "CARGO_ENCODED_RUSTFLAGS",
    "CARGO_TARGET_DIR",
    "GLIBC_TUNABLES",
    "LD_PRELOAD",
    "MALLOC_ARENA_MAX",
    "MALLOC_CONF",
    "RUSTC_WORKSPACE_WRAPPER",
    "RUSTC_WRAPPER",
    "RUSTFLAGS",
    "RUSTUP_TOOLCHAIN",
    "SCUDO_OPTIONS",
    "SCUDO_RUNTIME_LIBRARY",
    "SCUDO_STANDALONE_LIBRARY",
    "TCMALLOC_LIB_DIR",
    "UNIALLOC_GOOGLE_TCMALLOC_LIBRARY",
    "UNIALLOC_GOOGLE_TCMALLOC_PREFIX",
    "UNIALLOC_SCUDO_RUNTIME_LIBRARY",
    "UNIALLOC_TCMALLOC_LIB_DIR",
}
SANITIZED_ENV_PREFIXES = ("MIMALLOC_", "TCMALLOC_")
RAW_TSV_MISSING_VALUE = "NA"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--candidate-ref", required=True)
    parser.add_argument(
        "--worker-cpus",
        help="two same-NUMA, distinct-physical-core CPUs (default: auto)",
    )
    parser.add_argument("--coordinator-cpu", type=int)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--filter-iterations", type=int, default=5_000_000)
    parser.add_argument("--hotpath-iterations", type=int, default=2_000_000)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--skip-hotpath", action="store_true")
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--svg")
    args = parser.parse_args()
    if (
        args.warmups < 0
        or args.rounds < 3
        or args.filter_iterations <= 0
        or args.hotpath_iterations <= 0
        or args.timeout <= 0
    ):
        parser.error("rounds must be >=3 and iteration/timeout values must be positive")
    return args


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in list(env):
        if name in SANITIZED_ENV_NAMES or name.startswith(SANITIZED_ENV_PREFIXES):
            env.pop(name, None)
    return env


def run(
    command: list[str],
    *,
    cwd: pathlib.Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout[-4000:]}\n"
            f"stderr:\n{completed.stderr[-4000:]}"
        )
    return completed


def git_output(root: pathlib.Path, *args: str) -> str:
    return run(["git", "-C", str(root), *args], cwd=ROOT, timeout=60).stdout.strip()


def parse_cpu_list(value: str) -> tuple[int, int]:
    fields = value.split(",")
    if len(fields) != 2:
        raise ValueError("worker CPUs must be formatted as A,B")
    cpus = tuple(int(field) for field in fields)
    if cpus[0] < 0 or cpus[1] < 0 or cpus[0] == cpus[1]:
        raise ValueError("worker CPUs must be distinct non-negative integers")
    return cpus[0], cpus[1]


def cpu_topology(cpu: int) -> dict[str, Any]:
    cpu_root = pathlib.Path(f"/sys/devices/system/cpu/cpu{cpu}")
    if not cpu_root.exists():
        raise ValueError(f"CPU {cpu} does not exist")
    core_id = int((cpu_root / "topology" / "core_id").read_text().strip())
    package_id = int(
        (cpu_root / "topology" / "physical_package_id").read_text().strip()
    )
    node_paths = sorted(cpu_root.glob("node[0-9]*"))
    node = int(node_paths[0].name[4:]) if node_paths else 0
    governor_path = cpu_root / "cpufreq" / "scaling_governor"
    frequency_path = cpu_root / "cpufreq" / "scaling_cur_freq"
    return {
        "cpu": cpu,
        "core_id": core_id,
        "package_id": package_id,
        "numa_node": node,
        "governor": governor_path.read_text().strip()
        if governor_path.exists()
        else "unknown",
        "frequency_khz": int(frequency_path.read_text().strip())
        if frequency_path.exists()
        else None,
    }


def select_cpus(
    worker_value: str | None, coordinator_value: int | None
) -> tuple[tuple[int, int], int, list[dict[str, Any]]]:
    allowed = sorted(os.sched_getaffinity(0))
    topology = {cpu: cpu_topology(cpu) for cpu in allowed}
    if worker_value:
        workers = parse_cpu_list(worker_value)
    else:
        workers = None
        for first in allowed:
            for second in allowed:
                if first >= second:
                    continue
                left = topology[first]
                right = topology[second]
                if (
                    left["numa_node"] == right["numa_node"]
                    and left["package_id"] == right["package_id"]
                    and left["core_id"] != right["core_id"]
                ):
                    workers = (first, second)
                    break
            if workers:
                break
        if workers is None:
            raise RuntimeError("no same-NUMA distinct-core worker pair is available")
    if any(cpu not in topology for cpu in workers):
        raise ValueError("worker CPU is outside this process affinity mask")
    left, right = (topology[cpu] for cpu in workers)
    if (
        left["numa_node"] != right["numa_node"]
        or left["package_id"] != right["package_id"]
        or left["core_id"] == right["core_id"]
    ):
        raise ValueError("worker CPUs must be same-NUMA distinct physical cores")

    coordinator = coordinator_value
    if coordinator is None:
        coordinator = next(
            (
                cpu
                for cpu in allowed
                if cpu not in workers
                and topology[cpu]["numa_node"] == left["numa_node"]
                and topology[cpu]["package_id"] == left["package_id"]
                and topology[cpu]["core_id"]
                not in {left["core_id"], right["core_id"]}
            ),
            None,
        )
        if coordinator is None:
            raise RuntimeError("no distinct coordinator core is available")
    if coordinator not in topology:
        raise ValueError("coordinator CPU is outside this process affinity mask")
    coordinator_topology = topology[coordinator]
    if (
        coordinator in workers
        or coordinator_topology["numa_node"] != left["numa_node"]
        or coordinator_topology["package_id"] != left["package_id"]
        or coordinator_topology["core_id"] in {left["core_id"], right["core_id"]}
    ):
        raise ValueError("coordinator CPU must be a third physical core on the same NUMA node")
    return workers, coordinator, [
        topology[workers[0]],
        topology[workers[1]],
        coordinator_topology,
    ]


@contextlib.contextmanager
def detached_worktrees(
    baseline_ref: str, candidate_ref: str, temporary: pathlib.Path
) -> Iterator[dict[str, pathlib.Path]]:
    roots: dict[str, pathlib.Path] = {}
    try:
        for variant, ref in zip(VARIANTS, (baseline_ref, candidate_ref)):
            root = temporary / f"{variant}-src"
            run(
                ["git", "worktree", "add", "--detach", str(root), ref],
                cwd=ROOT,
                timeout=120,
            )
            roots[variant] = root
        yield roots
    finally:
        for root in roots.values():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(root)],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def build_binaries(
    source_root: pathlib.Path,
    target_dir: pathlib.Path,
    *,
    timeout: int,
) -> tuple[dict[str, pathlib.Path], dict[str, list[str]]]:
    filter_target = target_dir / "filter"
    hotpath_target = target_dir / "allocator-hotpath"
    filter_command = [
        "cargo",
        "build",
        "--release",
        "--locked",
        "--offline",
        "-p",
        "unialloc",
        "--example",
        "typeiso_pointer_filter_bench",
        "--features",
        "typeiso_pointer_filter_bench",
        "--target-dir",
        str(filter_target),
    ]
    hotpath_command = [
        "cargo",
        "build",
        "--release",
        "--locked",
        "--offline",
        "-p",
        "unialloc",
        "--example",
        "typeiso_allocator_hotpath",
        "--features",
        "type_isolation",
        "--target-dir",
        str(hotpath_target),
    ]
    env = clean_environment()
    env.update({"CARGO_INCREMENTAL": "0", "RUSTFLAGS": "-C target-cpu=native"})
    run(filter_command, cwd=source_root, timeout=timeout, env=env)
    run(hotpath_command, cwd=source_root, timeout=timeout, env=env)
    binaries = {
        "filter": filter_target
        / "release"
        / "examples"
        / "typeiso_pointer_filter_bench",
        "allocator_hotpath": hotpath_target
        / "release"
        / "examples"
        / "typeiso_allocator_hotpath",
    }
    missing = [str(binary) for binary in binaries.values() if not binary.is_file()]
    if missing:
        raise RuntimeError(f"benchmark binaries missing: {missing}")
    return binaries, {
        "filter": filter_command,
        "allocator_hotpath": hotpath_command,
    }


def parse_probe(stdout: str, expected_source: str) -> dict[str, Any]:
    rows = [line.strip() for line in stdout.splitlines() if line.lstrip().startswith("{")]
    if len(rows) != 1:
        raise RuntimeError(f"expected exactly one probe JSON row, got {len(rows)}")
    row = json.loads(rows[0])
    if row.get("source") != expected_source:
        raise RuntimeError(f"unexpected probe source: {row.get('source')}")
    if row.get("final_count", 0) != 0 or row.get("query_hits", 0) != 0:
        raise RuntimeError(f"probe left live state: {row}")
    return row


def variant_order(round_number: int) -> tuple[str, str]:
    return VARIANTS if round_number % 2 == 1 else tuple(reversed(VARIANTS))


def run_filter_sample(
    binary: pathlib.Path,
    scenario: str,
    iterations: int,
    worker_cpus: tuple[int, int],
    coordinator_cpu: int,
    *,
    cwd: pathlib.Path,
    timeout: int,
) -> dict[str, Any]:
    command = [
        str(binary),
        scenario,
        str(iterations),
        str(worker_cpus[0]),
        str(worker_cpus[1]),
        str(coordinator_cpu),
    ]
    completed = run(command, cwd=cwd, timeout=timeout, env=clean_environment())
    row = parse_probe(completed.stdout, "typeiso_pointer_filter_bench")
    row["command"] = command
    return row


def run_hotpath_sample(
    binary: pathlib.Path,
    scenario: str,
    iterations: int,
    cpu: int,
    *,
    cwd: pathlib.Path,
    timeout: int,
) -> dict[str, Any]:
    command = [str(binary), scenario, str(iterations), str(cpu)]
    completed = run(command, cwd=cwd, timeout=timeout, env=clean_environment())
    row = parse_probe(completed.stdout, "typeiso_allocator_hotpath")
    row["command"] = command
    return row


def measure_panel(
    *,
    binaries: dict[str, pathlib.Path],
    scenarios: tuple[str, ...],
    warmups: int,
    rounds: int,
    sample,
) -> list[dict[str, Any]]:
    for warmup in range(1, warmups + 1):
        for scenario in scenarios:
            for variant in variant_order(warmup):
                sample(binaries[variant], scenario)
    rows: list[dict[str, Any]] = []
    for round_number in range(1, rounds + 1):
        for scenario in scenarios:
            for variant in variant_order(round_number):
                row = sample(binaries[variant], scenario)
                row.update(
                    {"variant": variant, "round": round_number, "scenario": scenario}
                )
                rows.append(row)
    return rows


def sample_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        raise ValueError("sample set must be non-empty")
    return {
        "samples": values,
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def summarize_panel(
    rows: list[dict[str, Any]], metric: str, scenarios: tuple[str, ...]
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    ratios = []
    for scenario in scenarios:
        samples = {
            variant: [
                float(row[metric])
                for row in rows
                if row["scenario"] == scenario and row["variant"] == variant
            ]
            for variant in VARIANTS
        }
        if len(samples["baseline"]) != len(samples["candidate"]):
            raise ValueError(f"unbalanced samples for {scenario}")
        baseline = sample_stats(samples["baseline"])
        candidate = sample_stats(samples["candidate"])
        ratio = candidate["median"] / baseline["median"]
        ratios.append(ratio)
        paired_wins = sum(
            candidate_value < baseline_value
            for baseline_value, candidate_value in zip(
                samples["baseline"], samples["candidate"]
            )
        )
        summary[scenario] = {
            "baseline": baseline,
            "candidate": candidate,
            "candidate_to_baseline_ratio": ratio,
            "runtime_delta_percent": (ratio - 1.0) * 100.0,
            "paired_candidate_wins": paired_wins,
            "paired_rounds": len(samples["baseline"]),
        }
    geometric_mean = math.exp(sum(math.log(ratio) for ratio in ratios) / len(ratios))
    return {
        "metric": metric,
        "scenarios": summary,
        "geometric_mean_runtime_delta_percent": (geometric_mean - 1.0) * 100.0,
    }


def write_raw_tsv(path: pathlib.Path, panels: dict[str, list[dict[str, Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=(
                "panel",
                "variant",
                "round",
                "scenario",
                "elapsed_ns",
                "metric",
                "value",
                "recovery_index",
                "retained_index",
            ),
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        for panel, rows in panels.items():
            metric = (
                "ns_per_iteration"
                if panel == "pointer_filter"
                else "ns_per_iteration"
            )
            for row in rows:
                writer.writerow(
                    {
                        "panel": panel,
                        "variant": row["variant"],
                        "round": row["round"],
                        "scenario": row["scenario"],
                        "elapsed_ns": row["elapsed_ns"],
                        "metric": metric,
                        "value": row[metric],
                        "recovery_index": row.get(
                            "recovery_index", RAW_TSV_MISSING_VALUE
                        ),
                        "retained_index": row.get(
                            "retained_index", RAW_TSV_MISSING_VALUE
                        ),
                    }
                )


def render_svg(report: dict[str, Any], path: pathlib.Path) -> None:
    rows: list[tuple[str, float, str]] = []
    for scenario, item in report["pointer_filter"]["scenarios"].items():
        rows.append((f"filter / {scenario}", item["runtime_delta_percent"], "filter"))
    hotpath = report.get("allocator_hotpath")
    if hotpath:
        for scenario, item in hotpath["scenarios"].items():
            rows.append((f"allocator / {scenario}", item["runtime_delta_percent"], "hotpath"))
    width = 1180
    height = 120 + len(rows) * 42
    left = 390
    right = 100
    values = [value for _, value, _ in rows] + [-1.0, 1.0]
    low = min(-5.0, math.floor(min(values) / 5.0) * 5.0)
    high = max(5.0, math.ceil(max(values) / 5.0) * 5.0)
    plot_width = width - left - right

    def x(value: float) -> float:
        return left + (value - low) / (high - low) * plot_width

    zero = x(0.0)
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fbfbfa"/>',
        '<style>text{font-family:Inter,system-ui,sans-serif;fill:#202124}.title{font-size:22px;font-weight:700}.sub{font-size:13px;fill:#5f6368}.label{font-size:13px}.value{font-size:12px;font-weight:700}</style>',
        '<text x="36" y="34" class="title">Type Isolation pointer-filter domain salting</text>',
        f'<text x="36" y="58" class="sub">Candidate runtime delta vs baseline; lower is faster. {report["protocol"]["rounds"]} counterbalanced process runs.</text>',
        f'<line x1="{zero:.2f}" y1="78" x2="{zero:.2f}" y2="{height - 24}" stroke="#6b7280" stroke-width="1.5"/>',
    ]
    for index, (label, value, panel) in enumerate(rows):
        y = 86 + index * 42
        value_x = x(value)
        bar_x = min(zero, value_x)
        bar_width = max(2.0, abs(value_x - zero))
        color = "#16865c" if value < 0 else "#c65348"
        opacity = "0.92" if panel == "filter" else "0.72"
        lines.extend(
            [
                f'<text x="36" y="{y + 17}" class="label">{label}</text>',
                f'<rect x="{bar_x:.2f}" y="{y}" width="{bar_width:.2f}" height="23" rx="4" fill="{color}" opacity="{opacity}"/>',
                f'<text x="{value_x + (7 if value >= 0 else -7):.2f}" y="{y + 16}" text-anchor="{"start" if value >= 0 else "end"}" class="value">{value:+.2f}%</text>',
            ]
        )
    lines.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    worker_cpus, coordinator_cpu, topology = select_cpus(
        args.worker_cpus, args.coordinator_cpu
    )
    output_prefix = pathlib.Path(args.output_prefix)
    if not output_prefix.is_absolute():
        output_prefix = ROOT / output_prefix
    output_prefix = output_prefix.resolve()
    result_path = output_prefix.with_suffix(".json")
    raw_path = output_prefix.with_suffix(".raw.tsv")
    metadata_path = output_prefix.with_suffix(".meta.json")
    svg_path = pathlib.Path(args.svg) if args.svg else output_prefix.with_suffix(".svg")
    if not svg_path.is_absolute():
        svg_path = ROOT / svg_path
    svg_path = svg_path.resolve()
    try:
        result_path.relative_to(ROOT)
        raw_relative = raw_path.relative_to(ROOT)
        metadata_relative = metadata_path.relative_to(ROOT)
        output_prefix_relative = output_prefix.relative_to(ROOT)
        svg_relative = svg_path.relative_to(ROOT)
    except ValueError as error:
        raise ValueError("output-prefix and svg must stay inside the repository") from error

    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    load_before = os.getloadavg()
    with tempfile.TemporaryDirectory(prefix="unialloc-typeiso-filter-ab-") as tmp:
        temporary = pathlib.Path(tmp)
        with detached_worktrees(
            args.baseline_ref, args.candidate_ref, temporary
        ) as source_roots:
            identities = {
                variant: {
                    "commit": git_output(root, "rev-parse", "HEAD"),
                    "tree": git_output(root, "rev-parse", "HEAD^{tree}"),
                    "type_isolation_sha256": sha256(
                        root / "unialloc" / "src" / "alloc_api" / "type_isolation.rs"
                    ),
                }
                for variant, root in source_roots.items()
            }
            filter_binaries: dict[str, pathlib.Path] = {}
            hotpath_binaries: dict[str, pathlib.Path] = {}
            build_commands: dict[str, dict[str, list[str]]] = {}
            for variant, root in source_roots.items():
                binaries, command = build_binaries(
                    root, temporary / f"filter-target-{variant}", timeout=args.timeout
                )
                filter_binaries[variant] = binaries["filter"]
                hotpath_binaries[variant] = binaries["allocator_hotpath"]
                build_commands[variant] = command

            filter_rows = measure_panel(
                binaries=filter_binaries,
                scenarios=FILTER_SCENARIOS,
                warmups=args.warmups,
                rounds=args.rounds,
                sample=lambda binary, scenario: run_filter_sample(
                    binary,
                    scenario,
                    args.filter_iterations,
                    worker_cpus,
                    coordinator_cpu,
                    cwd=ROOT,
                    timeout=args.timeout,
                ),
            )

            hotpath_rows: list[dict[str, Any]] = []
            if not args.skip_hotpath:
                hotpath_rows = measure_panel(
                    binaries=hotpath_binaries,
                    scenarios=HOTPATH_SCENARIOS,
                    warmups=args.warmups,
                    rounds=args.rounds,
                    sample=lambda binary, scenario: run_hotpath_sample(
                        binary,
                        scenario,
                        args.hotpath_iterations,
                        worker_cpus[0],
                        cwd=ROOT,
                        timeout=args.timeout,
                    ),
                )

            panels = {"pointer_filter": filter_rows}
            if hotpath_rows:
                panels["allocator_hotpath"] = hotpath_rows
            write_raw_tsv(raw_path, panels)

            filter_summary = summarize_panel(
                filter_rows, "ns_per_iteration", FILTER_SCENARIOS
            )
            hotpath_summary = (
                summarize_panel(hotpath_rows, "ns_per_iteration", HOTPATH_SCENARIOS)
                if hotpath_rows
                else None
            )
            mixed_delta = filter_summary["scenarios"]["mixed_same_hash_2t"][
                "runtime_delta_percent"
            ]
            filter_controls = [
                item["runtime_delta_percent"]
                for scenario, item in filter_summary["scenarios"].items()
                if scenario != "mixed_same_hash_2t"
            ]
            candidate_filter_rows = [
                row for row in filter_rows if row["variant"] == "candidate"
            ]
            baseline_filter_rows = [
                row for row in filter_rows if row["variant"] == "baseline"
            ]
            baseline_indices_collide = all(
                row["recovery_index"] == row["retained_index"]
                for row in baseline_filter_rows
            )
            distinct_indices = all(
                row["recovery_index"] != row["retained_index"]
                for row in candidate_filter_rows
            )
            hotpath_deltas = (
                [
                    item["runtime_delta_percent"]
                    for item in hotpath_summary["scenarios"].values()
                ]
                if hotpath_summary
                else []
            )
            admission = {
                "mixed_same_hash_improves_over_one_percent": mixed_delta < -1.0,
                "baseline_domain_indices_collide": baseline_indices_collide,
                "candidate_domain_indices_are_distinct": distinct_indices,
                "single_domain_and_query_max_regression_below_one_percent": max(
                    filter_controls
                )
                < 1.0,
                "allocator_hotpath_each_leaf_below_one_percent": bool(hotpath_deltas)
                and max(hotpath_deltas) < 1.0,
            }
            admission["passed"] = all(admission.values())

            metadata = {
                "schema_version": 1,
                "started_at": started_at,
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "kernel": run(["uname", "-sr"], cwd=ROOT, timeout=30).stdout.strip(),
                "rustc": run(["rustc", "--version", "--verbose"], cwd=ROOT, timeout=30).stdout.strip(),
                "cargo": run(["cargo", "--version"], cwd=ROOT, timeout=30).stdout.strip(),
                "worker_cpus": list(worker_cpus),
                "coordinator_cpu": coordinator_cpu,
                "cpu_topology": topology,
                "load_average_before": load_before,
                "load_average_after": os.getloadavg(),
                "warmups": args.warmups,
                "rounds": args.rounds,
                "filter_iterations_per_worker": args.filter_iterations,
                "hotpath_iterations": args.hotpath_iterations,
                "raw_tsv_serialization": {
                    "line_ending": "LF",
                    "missing_value": RAW_TSV_MISSING_VALUE,
                },
                "source": identities,
                "build_commands": build_commands,
                "sanitized_environment_names": sorted(SANITIZED_ENV_NAMES),
                "sanitized_environment_prefixes": list(SANITIZED_ENV_PREFIXES),
                "binaries": {
                    "filter": {
                        variant: {"sha256": sha256(binary), "size_bytes": binary.stat().st_size}
                        for variant, binary in filter_binaries.items()
                    },
                    "allocator_hotpath": {
                        variant: {"sha256": sha256(binary), "size_bytes": binary.stat().st_size}
                        for variant, binary in hotpath_binaries.items()
                    },
                },
            }
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

            report = {
                "schema_version": 1,
                "generated_at": metadata["finished_at"],
                "protocol": {
                    "warmups": args.warmups,
                    "rounds": args.rounds,
                    "summary_statistic": "median",
                    "order": "AB/BA alternating by round",
                },
                "metric_convention": "runtime_delta_percent = (candidate / baseline - 1) * 100; negative is faster",
                "experiment_boundary": "The forced same-hash panel isolates cross-domain atomic contention. The allocator hot-path panel is the merge guard for ordinary and feature-on paths.",
                "verdict": "keep_domain_salted_indices"
                if admission["passed"]
                else "reject_domain_salted_indices",
                "admission": admission,
                "pointer_filter": filter_summary,
                "allocator_hotpath": hotpath_summary,
                "raw_artifacts": {
                    "tsv": str(raw_relative),
                    "tsv_sha256": sha256(raw_path),
                    "metadata": str(metadata_relative),
                    "metadata_sha256": sha256(metadata_path),
                },
                "reproduce": {
                    "command": " ".join(
                        [
                            "uv run python evaluation/scripts/run_typeiso_pointer_filter_ab.py",
                            f"--baseline-ref {identities['baseline']['commit']}",
                            f"--candidate-ref {identities['candidate']['commit']}",
                            f"--worker-cpus {worker_cpus[0]},{worker_cpus[1]}",
                            f"--coordinator-cpu {coordinator_cpu}",
                            f"--warmups {args.warmups}",
                            f"--rounds {args.rounds}",
                            f"--filter-iterations {args.filter_iterations}",
                            f"--hotpath-iterations {args.hotpath_iterations}",
                            f"--timeout {args.timeout}",
                            f"--output-prefix {output_prefix_relative}",
                            f"--svg {svg_relative}",
                            "--skip-hotpath" if args.skip_hotpath else "",
                        ]
                    ).strip()
                },
            }
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            render_svg(report, svg_path)

    print(result_path)
    print(raw_path)
    print(metadata_path)
    print(svg_path)
    print(report["verdict"])
    return 0 if report["admission"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
