#!/usr/bin/env python3
"""Run a bounded, fail-closed std_bench allocator-feature campaign.

The campaign builds one binary for each ``bench_*`` allocator selector, checks
that every binary exposes the same benchmark inventory, and runs each selected
leaf in a fresh pinned process.  One warmup is discarded; the retained samples
are summarized with a per-cell median.  Scudo is admitted only after the
repository's package and behavioral authenticity probe succeeds and every
benchmark process emits the exact runtime-identity marker.

This is a current-toolchain diagnostic.  It intentionally does not claim to
reproduce the historical paper configuration.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable


SCUDO_MARKER = "unialloc: verified Scudo runtime identity\n"
ALLOCATOR_SELECTORS = frozenset(
    {
        "bench_ourself",
        "bench_ptmalloc",
        "bench_jemalloc",
        "bench_mimalloc",
        "bench_tcmalloc",
        "bench_snmalloc",
        "bench_scudo",
    }
)


@dataclass(frozen=True)
class Variant:
    allocator: str
    feature: str
    label: str


VARIANTS = (
    Variant("unialloc", "bench_ourself", "UniAlloc"),
    Variant("ptmalloc", "bench_ptmalloc", "ptmalloc"),
    Variant("jemalloc", "bench_jemalloc", "jemalloc"),
    Variant("mimalloc", "bench_mimalloc", "mimalloc"),
    Variant("tcmalloc", "bench_tcmalloc", "TCMalloc"),
    Variant("snmalloc", "bench_snmalloc", "snmalloc"),
    Variant("scudo", "bench_scudo", "Scudo"),
)

# Every leaf performs allocation or reallocation in the timed closure.  The
# prior family-smoke campaign's optimizer-visible, sub-100 ns leaves are left
# out so code elimination and call overhead do not dominate the comparison.
BENCHMARKS = (
    "binary_heap::bench_from_vec",
    "btree::map::from_iter_rand_10_000",
    "btree::set::clone_10k_and_remove_half",
    "slice::random_inserts",
    "string::bench_push_char_one_byte",
    "vec::bench_from_elem_1000",
    "vec::bench_extend_1000_1000",
    "vec_deque::bench_grow_1025",
)

BENCH_RE = re.compile(
    r"^test\s+(?P<name>\S+)\s+\.\.\.\s+bench:\s+"
    r"(?P<ns>[0-9][0-9,]*(?:\.[0-9]+)?)\s+ns/iter"
    r"(?:\s+\(\+/-\s+(?P<dev>[0-9][0-9,]*(?:\.[0-9]+)?)\))?",
    re.MULTILINE,
)
TIME_KEYS = {
    "User time (seconds)": "user_seconds",
    "System time (seconds)": "system_seconds",
    "Percent of CPU this job got": "cpu_percent_text",
    "Elapsed (wall clock) time (h:mm:ss or m:ss)": "wall_time_text",
    "Maximum resident set size (kbytes)": "peak_rss_kib",
    "Major (requiring I/O) page faults": "major_page_faults",
    "Minor (reclaiming a frame) page faults": "minor_page_faults",
    "Voluntary context switches": "voluntary_context_switches",
    "Involuntary context switches": "involuntary_context_switches",
    "Exit status": "time_exit_status",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def command_output(
    command: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return result.stdout


def prepend_path(existing: str | None, path: Path) -> str:
    items = [item for item in str(existing or "").split(":") if item]
    resolved = str(path)
    return ":".join([resolved, *(item for item in items if item != resolved)])


def clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    exact_names = {
        "CARGO_ENCODED_RUSTFLAGS",
        "GLIBC_TUNABLES",
        "LD_PRELOAD",
        "MALLOC_ARENA_MAX",
        "MALLOC_CONF",
        "RUSTFLAGS",
        "SCUDO_OPTIONS",
        "SCUDO_RUNTIME_LIBRARY",
        "SCUDO_STANDALONE_LIBRARY",
        "TCMALLOC_LIB_DIR",
        "UNIALLOC_SCUDO_RUNTIME_LIBRARY",
        "UNIALLOC_TCMALLOC_LIB_DIR",
    }
    prefixes = ("MIMALLOC_", "TCMALLOC_")
    for name in list(env):
        if name in exact_names or name.startswith(prefixes):
            env.pop(name, None)
    env["UNIALLOC_STD_BENCH_DISABLE_TYPE_STATS"] = "1"
    env["UNIALLOC_STD_BENCH_DISABLE_AGGREGATE_STATS"] = "1"
    return env


def build_environment(
    variant: Variant, target_dir: Path, tcmalloc_lib_dir: Path | None
) -> dict[str, str]:
    env = clean_environment()
    env["CARGO_TARGET_DIR"] = str(target_dir)
    if variant.allocator == "tcmalloc":
        if tcmalloc_lib_dir is None:
            raise RuntimeError("TCMalloc requires --tcmalloc-lib-dir")
        env["UNIALLOC_TCMALLOC_LIB_DIR"] = str(tcmalloc_lib_dir)
        env["LIBRARY_PATH"] = prepend_path(env.get("LIBRARY_PATH"), tcmalloc_lib_dir)
        env["LD_LIBRARY_PATH"] = prepend_path(
            env.get("LD_LIBRARY_PATH"), tcmalloc_lib_dir
        )
    return env


def runtime_environment(
    variant: Variant,
    *,
    tcmalloc_lib_dir: Path | None,
    scudo_runtime: Path | None,
) -> dict[str, str]:
    env = clean_environment()
    if variant.allocator == "tcmalloc":
        if tcmalloc_lib_dir is None:
            raise RuntimeError("TCMalloc requires --tcmalloc-lib-dir")
        env["UNIALLOC_TCMALLOC_LIB_DIR"] = str(tcmalloc_lib_dir)
        env["LD_LIBRARY_PATH"] = prepend_path(
            env.get("LD_LIBRARY_PATH"), tcmalloc_lib_dir
        )
    if variant.allocator == "scudo":
        if scudo_runtime is None:
            raise RuntimeError("Scudo requires an authenticated runtime")
        env["UNIALLOC_SCUDO_RUNTIME_LIBRARY"] = str(scudo_runtime)
        env["LD_PRELOAD"] = str(scudo_runtime)
    return env


def load_paper_driver(source_root: Path) -> ModuleType:
    path = source_root / "evaluation/scripts/paper_workload_driver.py"
    spec = importlib.util.spec_from_file_location(
        "unialloc_paper_workload_driver", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import Scudo authenticity helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def authenticate_scudo(
    source_root: Path, requested: Path | None
) -> tuple[Path, dict[str, Any]]:
    driver = load_paper_driver(source_root)
    runtime = requested
    if runtime is None:
        runtime = driver.discover_scudo_runtime_library()
    if runtime is None:
        raise RuntimeError("no Scudo standalone runtime was discovered")
    runtime = runtime.expanduser().resolve(strict=True)
    authenticity = driver.scudo_runtime_authenticity_probe(runtime)
    if not (
        authenticity.get("ok") is True
        and authenticity.get("runtime_authenticity_verified") is True
    ):
        raise RuntimeError(
            "Scudo runtime failed authenticity verification: "
            + json.dumps(authenticity.get("blockers", []))
        )
    identity = authenticity.get("runtime_library_identity")
    if not isinstance(identity, dict) or identity.get("realpath") != str(runtime):
        raise RuntimeError(
            "Scudo authenticity evidence does not bind the selected realpath"
        )
    return runtime, authenticity


def validate_tcmalloc_dir(path: Path | None) -> Path:
    if path is None:
        raise RuntimeError("--tcmalloc-lib-dir is required for the TCMalloc variant")
    resolved = path.expanduser().resolve(strict=True)
    candidates = [resolved / name for name in ("libtcmalloc.so.4", "libtcmalloc.so")]
    if not any(candidate.exists() for candidate in candidates):
        raise RuntimeError(f"no full TCMalloc shared runtime found in {resolved}")
    return resolved


def tcmalloc_runtime_identity(path: Path) -> dict[str, Any]:
    candidates = [path / name for name in ("libtcmalloc.so.4", "libtcmalloc.so")]
    library = next(candidate for candidate in candidates if candidate.exists())
    realpath = library.resolve(strict=True)
    return {
        "path": str(library),
        "realpath": str(realpath),
        "size_bytes": realpath.stat().st_size,
        "sha256": sha256_file(realpath),
    }


def parse_cargo_executable(stdout: bytes) -> Path:
    executables: set[Path] = set()
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        target = record.get("target") if isinstance(record, dict) else None
        if (
            record.get("reason") == "compiler-artifact"
            and isinstance(target, dict)
            and target.get("name") == "std_bench"
            and "bench" in target.get("kind", [])
            and record.get("executable")
        ):
            executables.add(Path(str(record["executable"])).resolve())
    if len(executables) != 1:
        raise RuntimeError(
            f"expected one std_bench executable, got {sorted(executables)}"
        )
    executable = next(iter(executables))
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError(
            f"Cargo reported a missing/non-executable benchmark: {executable}"
        )
    return executable


def resolved_unialloc_features(
    metadata: dict[str, Any], source_root: Path
) -> list[str]:
    packages = metadata.get("packages")
    resolve = metadata.get("resolve")
    if not isinstance(packages, list) or not isinstance(resolve, dict):
        raise RuntimeError("Cargo metadata omitted package resolution")
    manifest = (source_root / "unialloc/Cargo.toml").resolve()
    package_ids = {
        str(package["id"])
        for package in packages
        if isinstance(package, dict)
        and package.get("name") == "unialloc"
        and Path(str(package.get("manifest_path"))).resolve() == manifest
    }
    nodes = resolve.get("nodes")
    matches = (
        [
            node
            for node in nodes
            if isinstance(node, dict) and str(node.get("id")) in package_ids
        ]
        if isinstance(nodes, list)
        else []
    )
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one resolved unialloc package, got {len(matches)}"
        )
    features = sorted(str(item) for item in matches[0].get("features", []))
    return features


def build_variant(
    source_root: Path,
    output_dir: Path,
    variant: Variant,
    tcmalloc_lib_dir: Path | None,
) -> dict[str, Any]:
    build_dir = output_dir / "builds" / variant.allocator
    target_dir = build_dir / "target"
    build_dir.mkdir(parents=True, exist_ok=True)
    env = build_environment(variant, target_dir, tcmalloc_lib_dir)
    command = [
        "cargo",
        "bench",
        "-p",
        "unialloc",
        "--bench",
        "std_bench",
        "--features",
        variant.feature,
        "--no-run",
        "--message-format=json-render-diagnostics",
    ]
    started = utc_now()
    proc = subprocess.run(
        command,
        cwd=source_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    (build_dir / "build.stdout").write_bytes(proc.stdout)
    (build_dir / "build.stderr").write_bytes(proc.stderr)
    write_json(build_dir / "build-command.json", command)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{variant.allocator} build failed ({proc.returncode}): "
            + proc.stderr.decode("utf-8", errors="replace")[-4000:]
        )
    binary = parse_cargo_executable(proc.stdout)

    metadata_command = [
        "cargo",
        "metadata",
        "--format-version",
        "1",
        "--features",
        variant.feature,
    ]
    metadata_proc = subprocess.run(
        metadata_command,
        cwd=source_root,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    metadata = json.loads(metadata_proc.stdout)
    resolved_features = resolved_unialloc_features(metadata, source_root)
    selected = ALLOCATOR_SELECTORS.intersection(resolved_features)
    if selected != {variant.feature}:
        raise RuntimeError(
            f"{variant.allocator}: expected only {variant.feature}, resolved {sorted(selected)}"
        )
    (build_dir / "binary-path.txt").write_text(str(binary) + "\n")
    (build_dir / "binary.sha256").write_text(sha256_file(binary) + "\n")
    write_json(build_dir / "resolved-unialloc-features.json", resolved_features)
    return {
        "allocator": variant.allocator,
        "feature": variant.feature,
        "label": variant.label,
        "build_started_utc": started,
        "build_finished_utc": utc_now(),
        "build_command": command,
        "build_stdout_sha256": sha256_bytes(proc.stdout),
        "build_stderr_sha256": sha256_bytes(proc.stderr),
        "binary": str(binary),
        "binary_sha256": sha256_file(binary),
        "resolved_unialloc_features": resolved_features,
    }


def parse_inventory(stdout: bytes) -> list[str]:
    names = [
        line.removesuffix(": benchmark")
        for line in stdout.decode("utf-8", errors="replace").splitlines()
        if line.endswith(": benchmark")
    ]
    if not names or len(names) != len(set(names)):
        raise RuntimeError("benchmark inventory is empty or contains duplicate names")
    return names


def scudo_marker_count(stderr: bytes) -> int:
    lines = stderr.decode("utf-8", errors="replace").splitlines(keepends=True)
    return sum(line == SCUDO_MARKER for line in lines)


def inventory_variant(
    source_root: Path,
    output_dir: Path,
    variant: Variant,
    binary: Path,
    *,
    tcmalloc_lib_dir: Path | None,
    scudo_runtime: Path | None,
) -> tuple[list[str], dict[str, Any]]:
    env = runtime_environment(
        variant, tcmalloc_lib_dir=tcmalloc_lib_dir, scudo_runtime=scudo_runtime
    )
    proc = subprocess.run(
        [str(binary), "--list", "--format", "terse"],
        cwd=source_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    marker_count = scudo_marker_count(proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{variant.allocator} inventory failed ({proc.returncode}): "
            + proc.stderr.decode("utf-8", errors="replace")[-2000:]
        )
    if variant.allocator == "scudo" and marker_count != 1:
        raise RuntimeError(f"Scudo inventory emitted {marker_count} identity markers")
    if variant.allocator != "scudo" and marker_count != 0:
        raise RuntimeError(f"{variant.allocator} unexpectedly emitted a Scudo marker")
    names = parse_inventory(proc.stdout)
    missing = sorted(set(BENCHMARKS) - set(names))
    if missing:
        raise RuntimeError(f"{variant.allocator} inventory is missing {missing}")
    build_dir = output_dir / "builds" / variant.allocator
    (build_dir / "bench-list.txt").write_bytes(proc.stdout)
    (build_dir / "bench-list.stderr.txt").write_bytes(proc.stderr)
    ldd = subprocess.run(
        ["ldd", str(binary)],
        cwd=source_root,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ).stdout
    (build_dir / "ldd.txt").write_bytes(ldd)
    if variant.allocator == "tcmalloc":
        assert tcmalloc_lib_dir is not None
        if str(tcmalloc_lib_dir) not in ldd.decode("utf-8", errors="replace"):
            raise RuntimeError(
                "TCMalloc binary did not resolve through the pinned runtime directory"
            )
    return names, {
        "benchmark_count": len(names),
        "selected_count": len(BENCHMARKS),
        "selected_missing": missing,
        "list_sha256": sha256_bytes(proc.stdout),
        "stderr_sha256": sha256_bytes(proc.stderr),
        "scudo_identity_marker_count": marker_count,
        "ldd_sha256": sha256_bytes(ldd),
        "runtime_env_contract": {
            "glibc_tunables_present": "GLIBC_TUNABLES" in env,
            "ld_preload": env.get("LD_PRELOAD"),
            "scudo_runtime_library": env.get("UNIALLOC_SCUDO_RUNTIME_LIBRARY"),
            "tcmalloc_lib_dir": env.get("UNIALLOC_TCMALLOC_LIB_DIR"),
        },
    }


def parse_wall_seconds(value: str) -> float:
    fields = value.strip().split(":")
    if len(fields) == 2:
        minutes, seconds = fields
        return float(minutes) * 60 + float(seconds)
    if len(fields) == 3:
        hours, minutes, seconds = fields
        return float(hours) * 3600 + float(minutes) * 60 + float(seconds)
    raise ValueError(f"unexpected GNU time wall format: {value!r}")


def parse_time(path: Path) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        for source, destination in TIME_KEYS.items():
            prefix = f"{source}: "
            if line.startswith(prefix):
                parsed[destination] = line[len(prefix) :].strip()
                break
    missing = sorted(set(TIME_KEYS.values()) - set(parsed))
    if missing:
        raise RuntimeError(f"GNU time output is missing {missing}")
    for name in ("user_seconds", "system_seconds"):
        parsed[name] = float(str(parsed[name]))
    for name in (
        "peak_rss_kib",
        "major_page_faults",
        "minor_page_faults",
        "voluntary_context_switches",
        "involuntary_context_switches",
        "time_exit_status",
    ):
        parsed[name] = int(str(parsed[name]))
    parsed["cpu_percent"] = float(str(parsed["cpu_percent_text"]).rstrip("%"))
    parsed["wall_seconds"] = parse_wall_seconds(str(parsed["wall_time_text"]))
    return parsed


def terminate_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=2)


def run_one(
    *,
    source_root: Path,
    output_dir: Path,
    variant: Variant,
    binary: Path,
    benchmark: str,
    phase: str,
    round_index: int,
    cpu: int,
    numa_node: int,
    timeout_seconds: int,
    tcmalloc_lib_dir: Path | None,
    scudo_runtime: Path | None,
) -> dict[str, Any]:
    safe_name = benchmark.replace("::", "__").replace("/", "_")
    run_dir = (
        output_dir
        / "runs"
        / phase
        / f"round-{round_index}"
        / variant.allocator
        / safe_name
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / "stdout.txt"
    stderr_path = run_dir / "stderr.txt"
    time_path = run_dir / "time.txt"
    command = [
        "/usr/bin/time",
        "-v",
        "-o",
        str(time_path),
        "numactl",
        f"--cpunodebind={numa_node}",
        f"--membind={numa_node}",
        "taskset",
        "-c",
        str(cpu),
        str(binary),
        "--bench",
        "--exact",
        benchmark,
        "--test-threads=1",
    ]
    env = runtime_environment(
        variant, tcmalloc_lib_dir=tcmalloc_lib_dir, scudo_runtime=scudo_runtime
    )
    write_json(run_dir / "command.json", command)
    started = utc_now()
    start_loadavg = Path("/proc/loadavg").read_text().strip()
    frequency_path = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq")
    start_frequency = (
        int(frequency_path.read_text().strip()) if frequency_path.exists() else None
    )
    started_monotonic = time.monotonic()
    proc = subprocess.Popen(
        command,
        cwd=source_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_group(proc)
        stdout, stderr = proc.communicate()
    supervisor_elapsed = time.monotonic() - started_monotonic
    end_frequency = (
        int(frequency_path.read_text().strip()) if frequency_path.exists() else None
    )
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    marker_count = scudo_marker_count(stderr)
    record: dict[str, Any] = {
        "schema_version": 1,
        "diagnostic_label": "current-toolchain allocation-heavy std_bench subset; non-paper-exact",
        "claim_grade": False,
        "phase": phase,
        "round": round_index,
        "allocator": variant.allocator,
        "feature": variant.feature,
        "variant_label": variant.label,
        "benchmark": benchmark,
        "started_utc": started,
        "finished_utc": utc_now(),
        "supervisor_elapsed_seconds": supervisor_elapsed,
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "exit_code": proc.returncode,
        "command": command,
        "cpu": cpu,
        "numa_node": numa_node,
        "glibc_tunables_present": "GLIBC_TUNABLES" in env,
        "libc_rseq_policy": "glibc default; GLIBC_TUNABLES absent",
        "scudo_identity_marker_count": marker_count,
        "scudo_runtime_library": env.get("UNIALLOC_SCUDO_RUNTIME_LIBRARY"),
        "start_loadavg": start_loadavg,
        "end_loadavg": None,
        "start_cpu_frequency_khz": start_frequency,
        "end_cpu_frequency_khz": end_frequency,
        "binary": str(binary),
        "binary_sha256": sha256_file(binary),
        "stdout_path": str(stdout_path.relative_to(output_dir)),
        "stderr_path": str(stderr_path.relative_to(output_dir)),
        "time_path": str(time_path.relative_to(output_dir)),
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
    }
    record["end_loadavg"] = Path("/proc/loadavg").read_text().strip()
    if time_path.exists():
        record["time_sha256"] = sha256_file(time_path)
        try:
            record.update(parse_time(time_path))
        except Exception as exc:  # retained as fail-closed evidence
            record["time_parse_error"] = str(exc)
    matches = list(BENCH_RE.finditer(stdout.decode("utf-8", errors="replace")))
    if len(matches) == 1:
        match = matches[0]
        record["reported_benchmark"] = match.group("name")
        record["ns_per_iter"] = float(match.group("ns").replace(",", ""))
        deviation = match.group("dev")
        record["ns_per_iter_deviation"] = (
            float(deviation.replace(",", "")) if deviation is not None else None
        )
    else:
        record["benchmark_line_count"] = len(matches)
    marker_valid = (
        marker_count == 1 if variant.allocator == "scudo" else marker_count == 0
    )
    record["valid"] = bool(
        not timed_out
        and proc.returncode == 0
        and len(matches) == 1
        and matches[0].group("name") == benchmark
        and record.get("time_exit_status") == 0
        and not record.get("time_parse_error")
        and record.get("glibc_tunables_present") is False
        and marker_valid
    )
    write_json(run_dir / "record.json", record)
    return record


def median_absolute_deviation(values: Iterable[float]) -> float:
    numbers = [float(value) for value in values]
    center = statistics.median(numbers)
    return statistics.median(abs(value - center) for value in numbers)


def geometric_mean(values: Iterable[float]) -> float:
    numbers = [float(value) for value in values]
    if not numbers or any(value <= 0 or not math.isfinite(value) for value in numbers):
        raise ValueError("geometric mean requires finite positive values")
    return math.exp(sum(math.log(value) for value in numbers) / len(numbers))


def summarize(
    records: list[dict[str, Any]], *, measured_rounds: int, output_dir: Path
) -> dict[str, Any]:
    measured = [record for record in records if record.get("phase") == "measured"]
    cells: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for benchmark in BENCHMARKS:
        for variant in VARIANTS:
            samples = sorted(
                (
                    record
                    for record in measured
                    if record.get("benchmark") == benchmark
                    and record.get("allocator") == variant.allocator
                    and record.get("valid") is True
                ),
                key=lambda record: int(record["round"]),
            )
            rounds = [int(record["round"]) for record in samples]
            if rounds != list(range(1, measured_rounds + 1)):
                raise RuntimeError(
                    f"{variant.allocator}/{benchmark}: measured rounds are {rounds}"
                )
            values = [float(record["ns_per_iter"]) for record in samples]
            rss_values = [float(record["peak_rss_kib"]) for record in samples]
            cell = {
                "benchmark": benchmark,
                "allocator": variant.allocator,
                "feature": variant.feature,
                "variant_label": variant.label,
                "samples": len(values),
                "rounds": rounds,
                "ns_per_iter_samples": values,
                "median_ns_per_iter": statistics.median(values),
                "mad_ns_per_iter": median_absolute_deviation(values),
                "min_ns_per_iter": min(values),
                "max_ns_per_iter": max(values),
                "median_peak_rss_kib": statistics.median(rss_values),
                "min_peak_rss_kib": min(rss_values),
                "max_peak_rss_kib": max(rss_values),
            }
            cells.append(cell)
            by_key[(benchmark, variant.allocator)] = cell
    for benchmark in BENCHMARKS:
        baseline = float(by_key[(benchmark, "unialloc")]["median_ns_per_iter"])
        for variant in VARIANTS:
            cell = by_key[(benchmark, variant.allocator)]
            cell["ns_ratio_vs_unialloc"] = float(cell["median_ns_per_iter"]) / baseline

    aggregates = []
    for variant in VARIANTS:
        variant_cells = [
            cell for cell in cells if cell["allocator"] == variant.allocator
        ]
        aggregates.append(
            {
                "allocator": variant.allocator,
                "feature": variant.feature,
                "variant_label": variant.label,
                "benchmarks": len(variant_cells),
                "geomean_ns_ratio_vs_unialloc": geometric_mean(
                    float(cell["ns_ratio_vs_unialloc"]) for cell in variant_cells
                ),
                "median_ns_ratio_vs_unialloc": statistics.median(
                    float(cell["ns_ratio_vs_unialloc"]) for cell in variant_cells
                ),
                "min_ns_ratio_vs_unialloc": min(
                    float(cell["ns_ratio_vs_unialloc"]) for cell in variant_cells
                ),
                "max_ns_ratio_vs_unialloc": max(
                    float(cell["ns_ratio_vs_unialloc"]) for cell in variant_cells
                ),
                "total_measured_processes": len(variant_cells) * measured_rounds,
            }
        )

    load_values = [float(str(record["start_loadavg"]).split()[0]) for record in records]
    frequency_values = [
        int(value)
        for record in records
        for value in (
            record.get("start_cpu_frequency_khz"),
            record.get("end_cpu_frequency_khz"),
        )
        if value is not None
    ]
    summary = {
        "schema_version": 1,
        "generated_utc": utc_now(),
        "diagnostic_label": "current-toolchain allocation-heavy std_bench subset; non-paper-exact",
        "claim_grade": False,
        "methodology": {
            "benchmarks": list(BENCHMARKS),
            "allocators": [variant.allocator for variant in VARIANTS],
            "variants": [
                {
                    "allocator": variant.allocator,
                    "feature": variant.feature,
                    "label": variant.label,
                }
                for variant in VARIANTS
            ],
            "warmup_fresh_processes_per_cell": 1,
            "measured_fresh_processes_per_cell": measured_rounds,
            "build_excluded_from_timing": True,
            "direct_benchmark_binary": True,
            "libc_rseq": "glibc default; GLIBC_TUNABLES absent",
            "allocator_tuning": "defaults",
            "performance_statistic": (
                f"median of {measured_rounds} libtest ns/iter samples per cell; "
                "geometric mean of per-benchmark cell-median ratios"
            ),
        },
        "record_counts": {
            "all": len(records),
            "warmup": sum(record.get("phase") == "warmup" for record in records),
            "measured": len(measured),
            "valid": sum(record.get("valid") is True for record in records),
        },
        "cells": cells,
        "aggregates": aggregates,
        "host_activity": {
            "measurement_started_utc": min(
                str(record["started_utc"]) for record in records
            ),
            "measurement_finished_utc": max(
                str(record["finished_utc"]) for record in records
            ),
            "load1_min": min(load_values),
            "load1_max": max(load_values),
            "load1_mean": statistics.mean(load_values),
            "cpu_frequency_khz_min": min(frequency_values)
            if frequency_values
            else None,
            "cpu_frequency_khz_max": max(frequency_values)
            if frequency_values
            else None,
            "involuntary_context_switches_total": sum(
                int(record["involuntary_context_switches"]) for record in records
            ),
            "shared_host_label": "background activity present; diagnostic only",
        },
        "limitations": [
            "The selected current-nightly allocation-heavy subset differs from the paper's historical full Collections setup.",
            "Three retained samples per cell support descriptive medians, MAD, and range; they do not support confidence intervals.",
            "The shared host had background activity; CPU and NUMA pinning reduce but do not eliminate interference.",
            "Cross-benchmark comparison uses ratios before aggregation; heterogeneous raw nanoseconds are never averaged.",
            "GNU time peak RSS includes harness/runtime startup and remains coarse for these short microprocesses.",
        ],
    }
    write_json(output_dir / "summary.json", summary)
    with (output_dir / "cell-summary.csv").open("w", newline="") as handle:
        fieldnames = [name for name in cells[0] if name != "ns_per_iter_samples"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(cells)
    with (output_dir / "aggregate-summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(aggregates[0]))
        writer.writeheader()
        writer.writerows(aggregates)
    return summary


def load_completed_records(
    output_dir: Path,
) -> dict[tuple[str, int, str, str], dict[str, Any]]:
    completed: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    path = output_dir / "records.jsonl"
    if not path.exists():
        return completed
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        key = (
            str(record["phase"]),
            int(record["round"]),
            str(record["allocator"]),
            str(record["benchmark"]),
        )
        if record.get("valid") is not True:
            raise RuntimeError(
                "records.jsonl contains a failed record; use a new output directory"
            )
        if key in completed:
            raise RuntimeError(f"records.jsonl contains a duplicate process key: {key}")
        completed[key] = record
    return completed


def append_record(output_dir: Path, record: dict[str, Any]) -> None:
    with (output_dir / "records.jsonl").open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def ensure_host_tools() -> None:
    for command in ("cargo", "git", "ldd", "numactl", "taskset", "/usr/bin/time"):
        if shutil.which(command) is None:
            raise RuntimeError(f"required command is missing: {command}")


def validate_clean_source(source_root: Path) -> str:
    if (
        not (source_root / ".git").exists()
        and not command_output(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=source_root
        ).strip()
        == "true"
    ):
        raise RuntimeError(f"source root is not a Git worktree: {source_root}")
    status = command_output(["git", "status", "--porcelain"], cwd=source_root)
    if status:
        raise RuntimeError("benchmark source worktree must be clean")
    return command_output(["git", "rev-parse", "HEAD"], cwd=source_root).strip()


def default_tcmalloc_dir() -> Path | None:
    configured = os.environ.get("UNIALLOC_TCMALLOC_LIB_DIR") or os.environ.get(
        "TCMALLOC_LIB_DIR"
    )
    return Path(configured) if configured else None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("evaluation/raw/std-bench-allocator-variants-20260714"),
    )
    parser.add_argument("--measured-rounds", type=int, choices=range(3, 6), default=3)
    parser.add_argument("--cpu", type=int, default=20)
    parser.add_argument("--numa-node", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--tcmalloc-lib-dir", type=Path, default=default_tcmalloc_dir())
    parser.add_argument("--scudo-runtime-library", type=Path)
    return parser.parse_args(argv)


def run_campaign(args: argparse.Namespace) -> dict[str, Any]:
    ensure_host_tools()
    source_root = args.source_root.expanduser().resolve(strict=True)
    output_dir = args.output_dir.expanduser()
    if not output_dir.is_absolute():
        output_dir = (source_root / output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    head = validate_clean_source(source_root)
    tcmalloc_lib_dir = validate_tcmalloc_dir(args.tcmalloc_lib_dir)
    tcmalloc_identity = tcmalloc_runtime_identity(tcmalloc_lib_dir)
    scudo_runtime, scudo_authenticity = authenticate_scudo(
        source_root, args.scudo_runtime_library
    )
    scudo_identity = scudo_authenticity.get("runtime_library_identity")
    if not isinstance(scudo_identity, dict):
        raise RuntimeError("Scudo authenticity evidence omitted runtime identity")
    runner_path = Path(__file__).resolve()
    runner_sha256 = sha256_file(runner_path)
    rustc_vv = command_output(["rustc", "-Vv"], cwd=source_root)
    cargo_vv = command_output(["cargo", "-Vv"], cwd=source_root)

    config = {
        "schema_version": 1,
        "source_root": str(source_root),
        "repo_head": head,
        "runner_sha256": runner_sha256,
        "rustc_vv_sha256": sha256_bytes(rustc_vv.encode()),
        "cargo_vv_sha256": sha256_bytes(cargo_vv.encode()),
        "benchmarks": list(BENCHMARKS),
        "variants": [variant.__dict__ for variant in VARIANTS],
        "warmups": 1,
        "measured_rounds": args.measured_rounds,
        "cpu": args.cpu,
        "numa_node": args.numa_node,
        "timeout_seconds": args.timeout_seconds,
        "tcmalloc_lib_dir": str(tcmalloc_lib_dir),
        "tcmalloc_runtime_sha256": tcmalloc_identity["sha256"],
        "scudo_runtime": str(scudo_runtime),
        "scudo_runtime_sha256": scudo_identity["sha256"],
    }
    config_bytes = (json.dumps(config, indent=2, sort_keys=True) + "\n").encode()
    config_sha = sha256_bytes(config_bytes)
    config_path = output_dir / "campaign-config.json"
    if config_path.exists() and sha256_file(config_path) != config_sha:
        raise RuntimeError(
            "existing campaign configuration differs; choose a new output directory"
        )
    config_path.write_bytes(config_bytes)

    builds: dict[str, dict[str, Any]] = {}
    for variant in VARIANTS:
        builds[variant.allocator] = build_variant(
            source_root, output_dir, variant, tcmalloc_lib_dir
        )

    inventories: dict[str, dict[str, Any]] = {}
    canonical_inventory: list[str] | None = None
    canonical_list_sha: str | None = None
    for variant in VARIANTS:
        binary = Path(str(builds[variant.allocator]["binary"]))
        inventory, evidence = inventory_variant(
            source_root,
            output_dir,
            variant,
            binary,
            tcmalloc_lib_dir=tcmalloc_lib_dir,
            scudo_runtime=scudo_runtime,
        )
        if canonical_inventory is None:
            canonical_inventory = inventory
            canonical_list_sha = evidence["list_sha256"]
        elif (
            inventory != canonical_inventory
            or evidence["list_sha256"] != canonical_list_sha
        ):
            raise RuntimeError(
                f"{variant.allocator}: benchmark surface differs byte-for-byte"
            )
        inventories[variant.allocator] = {**builds[variant.allocator], **evidence}

    provenance = {
        "schema_version": 1,
        "generated_utc": utc_now(),
        "diagnostic_label": "current-toolchain allocation-heavy std_bench subset; non-paper-exact",
        "claim_grade": False,
        "repo_head": head,
        "repo_status": "",
        "source_unialloc_tree": command_output(
            ["git", "rev-parse", "HEAD:unialloc"], cwd=source_root
        ).strip(),
        "runner_path": str(runner_path),
        "runner_sha256": runner_sha256,
        "rustc": rustc_vv,
        "cargo": cargo_vv,
        "uname": command_output(["uname", "-a"], cwd=source_root).strip(),
        "lscpu": command_output(["lscpu"], cwd=source_root),
        "numactl_hardware": command_output(["numactl", "--hardware"], cwd=source_root),
        "glibc": command_output(
            ["getconf", "GNU_LIBC_VERSION"], cwd=source_root
        ).strip(),
        "cpu": args.cpu,
        "numa_node": args.numa_node,
        "cpu_thread_siblings": Path(
            f"/sys/devices/system/cpu/cpu{args.cpu}/topology/thread_siblings_list"
        )
        .read_text()
        .strip(),
        "cpu_governor": Path(
            f"/sys/devices/system/cpu/cpu{args.cpu}/cpufreq/scaling_governor"
        )
        .read_text()
        .strip(),
        "campaign_config_sha256": config_sha,
        "canonical_benchmark_count": len(canonical_inventory or []),
        "canonical_benchmark_list_sha256": canonical_list_sha,
        "inventories": inventories,
        "scudo_runtime_authenticity": scudo_authenticity,
        "tcmalloc_runtime": {
            "library_dir": str(tcmalloc_lib_dir),
            "files": sorted(
                path.name for path in tcmalloc_lib_dir.glob("libtcmalloc.so*")
            ),
            "identity": tcmalloc_identity,
        },
    }
    write_json(output_dir / "provenance.json", provenance)

    phases = [("warmup", 0)] + [
        ("measured", round_index) for round_index in range(1, args.measured_rounds + 1)
    ]
    expected = len(phases) * len(BENCHMARKS) * len(VARIANTS)
    expected_keys = {
        (phase, round_index, variant.allocator, benchmark)
        for phase, round_index in phases
        for benchmark in BENCHMARKS
        for variant in VARIANTS
    }
    completed = load_completed_records(output_dir)
    unexpected_keys = set(completed) - expected_keys
    if unexpected_keys:
        raise RuntimeError(
            f"records.jsonl contains unexpected process keys: {sorted(unexpected_keys)}"
        )
    variants_by_allocator = {variant.allocator: variant for variant in VARIANTS}
    for key, record in completed.items():
        allocator = key[2]
        variant = variants_by_allocator[allocator]
        if record.get("feature") != variant.feature:
            raise RuntimeError(f"resumed record has the wrong feature: {key}")
        if record.get("binary_sha256") != builds[allocator]["binary_sha256"]:
            raise RuntimeError(f"resumed record has a different binary identity: {key}")
        if record.get("cpu") != args.cpu or record.get("numa_node") != args.numa_node:
            raise RuntimeError(f"resumed record has different CPU placement: {key}")
        if allocator == "scudo" and record.get("scudo_runtime_library") != str(
            scudo_runtime
        ):
            raise RuntimeError(f"resumed Scudo record has a different runtime: {key}")
    for phase, round_index in phases:
        for benchmark_index, benchmark in enumerate(BENCHMARKS):
            rotation = (round_index + benchmark_index) % len(VARIANTS)
            order = VARIANTS[rotation:] + VARIANTS[:rotation]
            for variant in order:
                key = (phase, round_index, variant.allocator, benchmark)
                if key in completed:
                    continue
                record = run_one(
                    source_root=source_root,
                    output_dir=output_dir,
                    variant=variant,
                    binary=Path(str(builds[variant.allocator]["binary"])),
                    benchmark=benchmark,
                    phase=phase,
                    round_index=round_index,
                    cpu=args.cpu,
                    numa_node=args.numa_node,
                    timeout_seconds=args.timeout_seconds,
                    tcmalloc_lib_dir=tcmalloc_lib_dir,
                    scudo_runtime=scudo_runtime,
                )
                append_record(output_dir, record)
                if record.get("valid") is not True:
                    write_json(
                        output_dir / "campaign-state.json",
                        {
                            "status": "failed_closed",
                            "failed_key": key,
                            "record": record,
                            "updated_utc": utc_now(),
                        },
                    )
                    raise RuntimeError(f"campaign failed closed at {key}")
                completed[key] = record
                write_json(
                    output_dir / "campaign-state.json",
                    {
                        "status": "running",
                        "completed_valid_processes": len(completed),
                        "expected_processes": expected,
                        "last_key": key,
                        "updated_utc": utc_now(),
                    },
                )

    if len(completed) != expected:
        raise RuntimeError(f"expected {expected} valid processes, got {len(completed)}")
    summary = summarize(
        list(completed.values()),
        measured_rounds=args.measured_rounds,
        output_dir=output_dir,
    )
    write_json(
        output_dir / "campaign-state.json",
        {
            "status": "complete",
            "completed_valid_processes": len(completed),
            "expected_processes": expected,
            "updated_utc": utc_now(),
            "summary_sha256": sha256_file(output_dir / "summary.json"),
            "records_sha256": sha256_file(output_dir / "records.jsonl"),
        },
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_campaign(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        raise
