#!/usr/bin/env python3
"""Measure Rsedis with mimalloc THP toggles and default TCMalloc.

The runner consumes already-built Rsedis binaries so compilation is outside the
timed region.  Every repetition starts a fresh server, checks SET/GET
correctness, runs redis-benchmark, captures process memory evidence, and then
terminates the server.  It exits unsuccessfully when any requested evidence or
measurement is missing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
VARIANTS = (
    "mimalloc_thp_default",
    "mimalloc_thp_off",
    "tcmalloc_default",
)
BASELINE_VARIANT = "mimalloc_thp_default"
ALLOCATOR_ENV_KEYS = (
    "LD_PRELOAD",
    "GLIBC_TUNABLES",
    "MIMALLOC_ALLOW_THP",
    "MIMALLOC_ALLOW_LARGE_OS_PAGES",
    "MIMALLOC_RESERVE_HUGE_OS_PAGES",
    "TCMALLOC_MEMFS_MALLOC_PATH",
)


@dataclass(frozen=True)
class RuntimeCell:
    name: str
    binary: Path
    environment: dict[str, str]
    removed_inherited_allocator_keys: tuple[str, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clean_allocator_environment(
    source: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return an environment with allocator controls and rseq tunables removed."""

    env = dict(os.environ if source is None else source)
    removed: list[str] = []
    for key in tuple(env):
        if (
            key == "LD_PRELOAD"
            or key == "GLIBC_TUNABLES"
            or key in {"MALLOC_CONF", "MALLOC_CONF_"}
            or key.startswith("MIMALLOC_")
            or key.startswith("TCMALLOC_")
        ):
            removed.append(key)
            env.pop(key, None)
    return env, tuple(sorted(removed))


def allocator_environment_snapshot(env: Mapping[str, str]) -> dict[str, str | None]:
    """Record every allocator setting whose state changes the three-cell contract."""

    return {key: env.get(key) if key in env else None for key in ALLOCATOR_ENV_KEYS}


def make_runtime_cell(
    variant: str,
    *,
    mimalloc_binary: Path,
    system_binary: Path,
    tcmalloc_library: Path,
    source_environment: Mapping[str, str] | None = None,
) -> RuntimeCell:
    if variant not in VARIANTS:
        raise ValueError(f"unknown runtime cell: {variant}")
    env, removed = clean_allocator_environment(source_environment)
    if variant.startswith("mimalloc_"):
        env.update(
            {
                "MIMALLOC_ALLOW_THP": "1" if variant == "mimalloc_thp_default" else "0",
                "MIMALLOC_ALLOW_LARGE_OS_PAGES": "0",
                "MIMALLOC_RESERVE_HUGE_OS_PAGES": "0",
            }
        )
        binary = mimalloc_binary
    else:
        env.update(
            {
                "LD_PRELOAD": str(tcmalloc_library),
                # An empty value explicitly selects the gperftools default path:
                # no memfs-backed allocator arena is requested by this runner.
                "TCMALLOC_MEMFS_MALLOC_PATH": "",
            }
        )
        binary = system_binary
    return RuntimeCell(variant, binary, env, removed)


def rotated_order(round_index: int) -> tuple[str, ...]:
    offset = round_index % len(VARIANTS)
    return VARIANTS[offset:] + VARIANTS[:offset]


def run_command(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> dict[str, Any]:
    normalized_command = [os.fspath(item) for item in command]
    started = time.perf_counter_ns()
    try:
        process = subprocess.run(
            normalized_command,
            cwd=cwd,
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        returncode = process.returncode
        stdout = process.stdout
        stderr = process.stderr
        timed_out = False
    except subprocess.TimeoutExpired as error:
        returncode = 124
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        timed_out = True
    return {
        "command": normalized_command,
        "exit_code": returncode,
        "timed_out": timed_out,
        "elapsed_seconds": (time.perf_counter_ns() - started) / 1_000_000_000,
        "stdout": stdout,
        "stderr": stderr,
    }


def persist_command(raw_dir: Path, base: Path, result: Mapping[str, Any]) -> dict[str, Any]:
    stdout_path = base.with_suffix(".stdout")
    stderr_path = base.with_suffix(".stderr")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_bytes(bytes(result["stdout"]))
    stderr_path.write_bytes(bytes(result["stderr"]))
    return {
        "command": list(result["command"]),
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "elapsed_seconds": result["elapsed_seconds"],
        "stdout_path": str(stdout_path.relative_to(raw_dir)),
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_path": str(stderr_path.relative_to(raw_dir)),
        "stderr_sha256": sha256_file(stderr_path),
    }


def parse_status_text(text: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for line in text.splitlines():
        key, separator, raw_value = line.partition(":")
        if not separator:
            continue
        value = raw_value.strip()
        kib = re.fullmatch(r"(\d+)\s+kB", value)
        if kib:
            output[key + "_kib"] = int(kib.group(1))
        elif key == "THP_enabled" and value in {"0", "1"}:
            output[key] = int(value)
        elif key in {"Name", "State", "Cpus_allowed_list", "Mems_allowed_list", "Threads"}:
            output[key] = value
    return output


def parse_smaps_rollup_text(text: str) -> dict[str, int]:
    output: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, raw_value = line.partition(":")
        if not separator:
            continue
        match = re.fullmatch(r"\s*(\d+)\s+kB", raw_value)
        if match:
            output[key + "_kib"] = int(match.group(1))
    if "Hugetlb_kib" not in output:
        shared = output.get("Shared_Hugetlb_kib")
        private = output.get("Private_Hugetlb_kib")
        if shared is not None and private is not None:
            output["Hugetlb_kib"] = shared + private
    return output


def read_proc_file(pid: int, name: str) -> str:
    try:
        return Path(f"/proc/{pid}/{name}").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def process_snapshot(pid: int) -> dict[str, Any]:
    return {
        "status": parse_status_text(read_proc_file(pid, "status")),
        "smaps_rollup": parse_smaps_rollup_text(read_proc_file(pid, "smaps_rollup")),
    }


def required_memory_evidence_present(snapshot: Mapping[str, Any]) -> bool:
    status = snapshot.get("status", {})
    smaps = snapshot.get("smaps_rollup", {})
    return (
        isinstance(status, Mapping)
        and isinstance(smaps, Mapping)
        and all(key in status for key in ("VmRSS_kib", "VmHWM_kib", "THP_enabled"))
        and all(key in smaps for key in ("AnonHugePages_kib", "Hugetlb_kib"))
    )


def parse_redis_csv(data: bytes | str) -> dict[str, float]:
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    output: dict[str, float] = {}
    for row in csv.reader(text.splitlines()):
        if len(row) < 2:
            continue
        name = row[0].strip().upper()
        try:
            value = float(row[1].replace(",", ""))
        except ValueError:
            continue
        if math.isfinite(value):
            output[name] = value
    return output


def stop_server(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    signal_sent: str | None = None
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            signal_sent = "SIGTERM"
        except ProcessLookupError:
            pass
    try:
        exit_code = process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            signal_sent = "SIGKILL"
        except ProcessLookupError:
            pass
        exit_code = process.wait(timeout=5)
    return {"signal_sent": signal_sent, "exit_code": exit_code}


def client_command(args: argparse.Namespace, port: int, *redis_args: str) -> list[str]:
    return [
        str(args.numactl),
        f"--physcpubind={args.client_cpus}",
        f"--membind={args.numa_node}",
        str(args.redis_cli),
        "--raw",
        "-h",
        "127.0.0.1",
        "-p",
        str(port),
        *redis_args,
    ]


def run_redis_cli(args: argparse.Namespace, port: int, *redis_args: str) -> dict[str, Any]:
    env, _ = clean_allocator_environment()
    return run_command(
        client_command(args, port, *redis_args),
        cwd=args.raw_dir,
        env=env,
        timeout=5,
    )


def wait_ready(
    args: argparse.Namespace,
    port: int,
    process: subprocess.Popen[bytes],
) -> tuple[bool, dict[str, Any]]:
    deadline = time.monotonic() + args.startup_timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False, last
        last = run_redis_cli(args, port, "PING")
        if last["exit_code"] == 0 and last["stdout"].strip() == b"PONG":
            return True, last
        time.sleep(0.05)
    return False, last


def normalized_mapped_file_paths(maps_text: str) -> list[str]:
    """Extract normalized, live file paths from Linux proc maps text."""

    paths: list[str] = []
    for line in maps_text.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6:
            continue
        mapped_path = fields[5]
        if not mapped_path.startswith("/") or mapped_path.endswith(" (deleted)"):
            continue
        paths.append(str(Path(mapped_path).resolve(strict=False)))
    return sorted(set(paths))


def exact_mapped_path_matches(maps_text: str, expected_path: Path) -> list[str]:
    expected = str(expected_path.resolve(strict=False))
    return [path for path in normalized_mapped_file_paths(maps_text) if path == expected]


def maps_proof(
    raw_dir: Path,
    run_dir: Path,
    process: subprocess.Popen[bytes],
    cell: RuntimeCell,
    tcmalloc_library: Path,
) -> dict[str, Any]:
    maps_path = run_dir / "server.maps"
    maps_text = read_proc_file(process.pid, "maps")
    maps_path.write_text(maps_text, encoding="utf-8")
    try:
        executable = Path(os.readlink(f"/proc/{process.pid}/exe")).resolve()
    except OSError:
        executable = None
    mapped_paths = normalized_mapped_file_paths(maps_text)
    matched_tcmalloc_paths = exact_mapped_path_matches(maps_text, tcmalloc_library)
    return {
        "path": str(maps_path.relative_to(raw_dir)),
        "sha256": sha256_file(maps_path),
        "nonempty": bool(maps_text),
        "executable": str(executable) if executable else None,
        "expected_executable": str(cell.binary.resolve()),
        "executable_matches": executable == cell.binary.resolve() if executable else False,
        "mapped_file_path_count": len(mapped_paths),
        "tcmalloc_library": str(tcmalloc_library.resolve(strict=False)),
        "matched_tcmalloc_mapped_paths": matched_tcmalloc_paths,
        "tcmalloc_library_mapped": bool(matched_tcmalloc_paths),
    }


def command_excerpt(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "exit_code": result.get("exit_code"),
        "stdout": bytes(result.get("stdout", b"")).decode("utf-8", errors="replace")[-1000:],
        "stderr": bytes(result.get("stderr", b"")).decode("utf-8", errors="replace")[-1000:],
    }


def run_one(
    args: argparse.Namespace,
    cell: RuntimeCell,
    *,
    round_index: int,
    order_index: int,
) -> dict[str, Any]:
    measured = round_index >= args.warmups
    repetition = round_index - args.warmups + 1 if measured else 0
    label = f"round-{round_index:02d}-order-{order_index:02d}-{cell.name}"
    run_dir = args.raw_dir / "runs" / label
    run_dir.mkdir(parents=True, exist_ok=False)
    port = args.base_port + round_index * len(VARIANTS) + order_index
    config_path = run_dir / "rsedis.conf"
    config_path.write_text(
        "bind 127.0.0.1\n"
        f"port {port}\n"
        "daemonize no\n"
        "appendonly no\n"
        f"dir {run_dir.resolve()}\n",
        encoding="utf-8",
    )
    server_command = [
        str(args.numactl),
        f"--physcpubind={args.server_cpus}",
        f"--membind={args.numa_node}",
        str(cell.binary),
        str(config_path),
    ]
    stdout_path = run_dir / "server.stdout"
    stderr_path = run_dir / "server.stderr"
    stdout_handle = stdout_path.open("wb")
    stderr_handle = stderr_path.open("wb")
    process: subprocess.Popen[bytes] | None = None
    ready = False
    ping_result: dict[str, Any] = {}
    idle_snapshot: dict[str, Any] = {}
    post_snapshot: dict[str, Any] = {}
    proof: dict[str, Any] = {}
    correctness: dict[str, bool] = {"ping": False, "set": False, "get": False}
    post_correctness = False
    benchmark_record: dict[str, Any] = {}
    throughput: dict[str, float] = {}
    combined_rps: float | None = None
    stop: dict[str, Any] = {}
    error: str | None = None
    started = time.perf_counter_ns()
    try:
        process = subprocess.Popen(
            server_command,
            cwd=run_dir,
            env=cell.environment,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
        ready, ping_result = wait_ready(args, port, process)
        key = f"unialloc:thp:{round_index}:{cell.name}"
        value = f"value-{round_index}-{cell.name}"
        set_result: dict[str, Any] = {}
        get_result: dict[str, Any] = {}
        if ready:
            set_result = run_redis_cli(args, port, "SET", key, value)
            get_result = run_redis_cli(args, port, "GET", key)
        correctness = {
            "ping": ready and ping_result.get("stdout", b"").strip() == b"PONG",
            "set": set_result.get("exit_code") == 0 and set_result.get("stdout", b"").strip() == b"OK",
            "get": get_result.get("exit_code") == 0
            and get_result.get("stdout", b"").decode("utf-8", errors="replace").strip() == value,
        }
        correctness["passed"] = all(correctness.values())
        idle_snapshot = process_snapshot(process.pid) if ready else {}
        proof = maps_proof(args.raw_dir, run_dir, process, cell, args.gperftools_library)
        if ready and correctness["passed"]:
            client_env, _ = clean_allocator_environment()
            benchmark_command = [
                str(args.numactl),
                f"--physcpubind={args.client_cpus}",
                f"--membind={args.numa_node}",
                str(args.redis_benchmark),
                "-h",
                "127.0.0.1",
                "-p",
                str(port),
                "-t",
                args.tests,
                "-n",
                str(args.requests),
                "-c",
                str(args.clients),
                "-d",
                str(args.data_size),
                "--csv",
            ]
            benchmark_result = run_command(
                benchmark_command,
                cwd=run_dir,
                env=client_env,
                timeout=args.benchmark_timeout_seconds,
            )
            benchmark_record = persist_command(
                args.raw_dir, run_dir / "redis-benchmark", benchmark_result
            )
            throughput = parse_redis_csv(benchmark_result["stdout"])
            if benchmark_result["exit_code"] == 0 and benchmark_result["elapsed_seconds"] > 0:
                combined_rps = (
                    len([name for name in args.tests.split(",") if name.strip()])
                    * args.requests
                    / benchmark_result["elapsed_seconds"]
                )
        if process.poll() is None:
            post_snapshot = process_snapshot(process.pid)
            post_get = run_redis_cli(args, port, "GET", key)
            post_correctness = (
                post_get.get("exit_code") == 0
                and post_get.get("stdout", b"").decode("utf-8", errors="replace").strip() == value
            )
    except Exception as caught:  # Retain a machine-readable failure before exiting.
        error = f"{type(caught).__name__}: {caught}"
    finally:
        if process is not None:
            stop = stop_server(process)
        stdout_handle.close()
        stderr_handle.close()

    required_tests = {name.strip().upper() for name in args.tests.split(",") if name.strip()}
    benchmark_ok = (
        benchmark_record.get("exit_code") == 0
        and required_tests == {"SET", "GET"}
        and required_tests.issubset(throughput)
        and all(throughput[name] > 0 for name in required_tests)
        and combined_rps is not None
    )
    environment = allocator_environment_snapshot(cell.environment)
    environment_ok = (
        environment["GLIBC_TUNABLES"] is None
        and (
            cell.name.startswith("mimalloc_")
            and environment["MIMALLOC_ALLOW_THP"]
            == ("1" if cell.name == "mimalloc_thp_default" else "0")
            and environment["MIMALLOC_ALLOW_LARGE_OS_PAGES"] == "0"
            and environment["MIMALLOC_RESERVE_HUGE_OS_PAGES"] == "0"
            and environment["LD_PRELOAD"] is None
            or cell.name == "tcmalloc_default"
            and environment["LD_PRELOAD"] == str(args.gperftools_library)
            and environment["TCMALLOC_MEMFS_MALLOC_PATH"] == ""
            and all(environment[key] is None for key in ALLOCATOR_ENV_KEYS if key.startswith("MIMALLOC_"))
        )
    )
    maps_ok = bool(proof.get("nonempty")) and bool(proof.get("executable_matches"))
    if cell.name == "tcmalloc_default":
        maps_ok = maps_ok and bool(proof.get("tcmalloc_library_mapped"))
    evidence_ok = required_memory_evidence_present(idle_snapshot) and required_memory_evidence_present(post_snapshot)
    success = (
        error is None
        and ready
        and correctness.get("passed") is True
        and benchmark_ok
        and post_correctness
        and environment_ok
        and maps_ok
        and evidence_ok
    )
    record = {
        "schema_version": SCHEMA_VERSION,
        "variant": cell.name,
        "round_index": round_index,
        "order_index": order_index,
        "phase": "measured" if measured else "warmup",
        "repetition": repetition,
        "port": port,
        "server_command": server_command,
        "server_ready": ready,
        "correctness": correctness,
        "post_benchmark_correctness": post_correctness,
        "benchmark": benchmark_record,
        "throughput_requests_per_second": throughput,
        "combined_effective_requests_per_second": combined_rps,
        "idle": idle_snapshot,
        "post": post_snapshot,
        "peak_rss_kib": post_snapshot.get("status", {}).get("VmHWM_kib"),
        "maps_proof": proof,
        "effective_allocator_environment": environment,
        "removed_inherited_allocator_keys": list(cell.removed_inherited_allocator_keys),
        "libc_rseq_mode": "glibc-default; GLIBC_TUNABLES removed; no rseq-disable setting",
        "environment_contract_passed": environment_ok,
        "memory_evidence_complete": evidence_ok,
        "maps_proof_passed": maps_ok,
        "config_path": str(config_path.relative_to(args.raw_dir)),
        "config_sha256": sha256_file(config_path),
        "server_stdout_path": str(stdout_path.relative_to(args.raw_dir)),
        "server_stdout_sha256": sha256_file(stdout_path),
        "server_stderr_path": str(stderr_path.relative_to(args.raw_dir)),
        "server_stderr_sha256": sha256_file(stderr_path),
        "ping_result": command_excerpt(ping_result),
        "stop": stop,
        "fresh_server_elapsed_seconds": (time.perf_counter_ns() - started) / 1_000_000_000,
        "error": error,
        "success": success,
    }
    write_json(run_dir / "record.json", record)
    return record


def median(values: Sequence[float | int]) -> float | None:
    return statistics.median(values) if values else None


def nested_numeric(record: Mapping[str, Any], path: Sequence[str]) -> float | None:
    value: Any = record
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def summarize_variant(
    variant: str,
    records: Sequence[Mapping[str, Any]],
    *,
    repetitions: int,
) -> dict[str, Any]:
    measured = [
        record
        for record in records
        if record.get("variant") == variant and record.get("phase") == "measured"
    ]
    successful = [record for record in measured if record.get("success") is True]

    def values(path: Sequence[str]) -> list[float]:
        return [value for record in successful if (value := nested_numeric(record, path)) is not None]

    return {
        "variant": variant,
        "measured_repetitions": len(measured),
        "successful_repetitions": len(successful),
        "all_successful": len(measured) == repetitions and len(successful) == repetitions,
        "benchmark_elapsed_seconds_median": median(values(("benchmark", "elapsed_seconds"))),
        "combined_effective_rps_median": median(values(("combined_effective_requests_per_second",))),
        "set_rps_median": median(values(("throughput_requests_per_second", "SET"))),
        "get_rps_median": median(values(("throughput_requests_per_second", "GET"))),
        "idle_rss_kib_median": median(values(("idle", "status", "VmRSS_kib"))),
        "post_rss_kib_median": median(values(("post", "status", "VmRSS_kib"))),
        "peak_rss_kib_median": median(values(("post", "status", "VmHWM_kib"))),
        "idle_anon_huge_pages_kib_median": median(values(("idle", "smaps_rollup", "AnonHugePages_kib"))),
        "post_anon_huge_pages_kib_median": median(values(("post", "smaps_rollup", "AnonHugePages_kib"))),
        "post_hugetlb_kib_median": median(values(("post", "smaps_rollup", "Hugetlb_kib"))),
        "post_thp_enabled_median": median(values(("post", "status", "THP_enabled"))),
    }


def paired_comparisons(
    records: Sequence[Mapping[str, Any]],
    *,
    warmups: int,
    repetitions: int,
) -> list[dict[str, Any]]:
    indexed = {
        (int(record["round_index"]), str(record["variant"])): record
        for record in records
        if record.get("phase") == "measured" and record.get("success") is True
    }
    output: list[dict[str, Any]] = []
    for variant in VARIANTS:
        if variant == BASELINE_VARIANT:
            continue
        rows: list[dict[str, float]] = []
        for round_index in range(warmups, warmups + repetitions):
            baseline = indexed.get((round_index, BASELINE_VARIANT))
            candidate = indexed.get((round_index, variant))
            if baseline is None or candidate is None:
                continue
            baseline_rps = nested_numeric(baseline, ("combined_effective_requests_per_second",))
            candidate_rps = nested_numeric(candidate, ("combined_effective_requests_per_second",))
            baseline_elapsed = nested_numeric(baseline, ("benchmark", "elapsed_seconds"))
            candidate_elapsed = nested_numeric(candidate, ("benchmark", "elapsed_seconds"))
            baseline_peak = nested_numeric(baseline, ("post", "status", "VmHWM_kib"))
            candidate_peak = nested_numeric(candidate, ("post", "status", "VmHWM_kib"))
            baseline_anon = nested_numeric(baseline, ("post", "smaps_rollup", "AnonHugePages_kib"))
            candidate_anon = nested_numeric(candidate, ("post", "smaps_rollup", "AnonHugePages_kib"))
            if None in {
                baseline_rps,
                candidate_rps,
                baseline_elapsed,
                candidate_elapsed,
                baseline_peak,
                candidate_peak,
                baseline_anon,
                candidate_anon,
            } or baseline_rps == 0 or baseline_elapsed == 0 or baseline_peak == 0:
                continue
            rows.append(
                {
                    "throughput_ratio": candidate_rps / baseline_rps,
                    "elapsed_ratio": candidate_elapsed / baseline_elapsed,
                    "peak_rss_ratio": candidate_peak / baseline_peak,
                    "post_anon_huge_pages_delta_kib": candidate_anon - baseline_anon,
                }
            )
        throughput_ratio = median([row["throughput_ratio"] for row in rows])
        peak_ratio = median([row["peak_rss_ratio"] for row in rows])
        output.append(
            {
                "variant": variant,
                "baseline": BASELINE_VARIANT,
                "paired_rounds": len(rows),
                "all_rounds_paired": len(rows) == repetitions,
                "median_throughput_ratio": throughput_ratio,
                "median_throughput_delta_percent": 100 * (throughput_ratio - 1) if throughput_ratio is not None else None,
                "median_elapsed_ratio": median([row["elapsed_ratio"] for row in rows]),
                "median_peak_rss_ratio": peak_ratio,
                "median_peak_rss_delta_percent": 100 * (peak_ratio - 1) if peak_ratio is not None else None,
                "median_post_anon_huge_pages_delta_kib": median(
                    [row["post_anon_huge_pages_delta_kib"] for row in rows]
                ),
                "per_round": rows,
            }
        )
    return output


def executable_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise argparse.ArgumentTypeError(f"executable does not exist or is not executable: {value}")
    return path


def file_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"file does not exist: {value}")
    return path


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def nonnegative_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mimalloc-rsedis-binary", required=True, type=executable_path)
    parser.add_argument("--system-rsedis-binary", required=True, type=executable_path)
    parser.add_argument("--gperftools-library", required=True, type=file_path)
    parser.add_argument("--raw-dir", required=True, type=lambda value: Path(value).expanduser().resolve())
    parser.add_argument("--redis-benchmark", default="/usr/bin/redis-benchmark", type=executable_path)
    parser.add_argument("--redis-cli", default="/usr/bin/redis-cli", type=executable_path)
    parser.add_argument("--numactl", default="/usr/bin/numactl", type=executable_path)
    parser.add_argument("--warmups", type=nonnegative_integer, default=1)
    parser.add_argument("--repetitions", type=positive_integer, default=5)
    parser.add_argument("--tests", choices=("set,get",), default="set,get")
    parser.add_argument("--requests", type=positive_integer, default=100_000)
    parser.add_argument("--clients", type=positive_integer, default=50)
    parser.add_argument("--data-size", type=positive_integer, default=64)
    parser.add_argument("--server-cpus", default="64-67")
    parser.add_argument("--client-cpus", default="68-71")
    parser.add_argument("--numa-node", type=nonnegative_integer, default=0)
    parser.add_argument("--base-port", type=positive_integer, default=17_400)
    parser.add_argument("--startup-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--benchmark-timeout-seconds", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.startup_timeout_seconds <= 0 or args.benchmark_timeout_seconds <= 0:
        parser.error("timeouts must be positive")
    if args.raw_dir.exists() and any(args.raw_dir.iterdir()):
        parser.error(f"raw directory must be absent or empty: {args.raw_dir}")
    return args


def host_evidence() -> dict[str, Any]:
    thp_enabled = Path("/sys/kernel/mm/transparent_hugepage/enabled")
    thp_defrag = Path("/sys/kernel/mm/transparent_hugepage/defrag")
    return {
        "uname": " ".join(os.uname()),
        "transparent_hugepage_enabled": thp_enabled.read_text(encoding="utf-8").strip() if thp_enabled.is_file() else None,
        "transparent_hugepage_defrag": thp_defrag.read_text(encoding="utf-8").strip() if thp_defrag.is_file() else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    cells = {
        variant: make_runtime_cell(
            variant,
            mimalloc_binary=args.mimalloc_rsedis_binary,
            system_binary=args.system_rsedis_binary,
            tcmalloc_library=args.gperftools_library,
        )
        for variant in VARIANTS
    }
    config = {
        "schema_version": SCHEMA_VERSION,
        "variants": list(VARIANTS),
        "baseline_variant": BASELINE_VARIANT,
        "warmups": args.warmups,
        "measured_repetitions": args.repetitions,
        "tests": args.tests,
        "requests_per_test": args.requests,
        "clients": args.clients,
        "data_size_bytes": args.data_size,
        "server_cpus": args.server_cpus,
        "client_cpus": args.client_cpus,
        "numa_node": args.numa_node,
        "base_port": args.base_port,
        "libc_rseq_mode": "glibc default; no rseq-disable tunable",
        "binaries": {
            "mimalloc_rsedis": {
                "path": str(args.mimalloc_rsedis_binary),
                "sha256": sha256_file(args.mimalloc_rsedis_binary),
            },
            "system_rsedis": {
                "path": str(args.system_rsedis_binary),
                "sha256": sha256_file(args.system_rsedis_binary),
            },
            "gperftools_tcmalloc": {
                "path": str(args.gperftools_library),
                "sha256": sha256_file(args.gperftools_library),
            },
        },
        "cell_environments": {
            variant: allocator_environment_snapshot(cell.environment)
            for variant, cell in cells.items()
        },
        "host": host_evidence(),
    }
    write_json(args.raw_dir / "config.json", config)
    records: list[dict[str, Any]] = []
    for round_index in range(args.warmups + args.repetitions):
        for order_index, variant in enumerate(rotated_order(round_index)):
            record = run_one(
                args,
                cells[variant],
                round_index=round_index,
                order_index=order_index,
            )
            records.append(record)
    write_json(args.raw_dir / "records.json", records)
    summaries = [
        summarize_variant(variant, records, repetitions=args.repetitions)
        for variant in VARIANTS
    ]
    comparisons = paired_comparisons(
        records,
        warmups=args.warmups,
        repetitions=args.repetitions,
    )
    validation = {
        "exactly_three_runtime_cells": {record["variant"] for record in records} == set(VARIANTS),
        "all_runs_successful": all(record["success"] for record in records),
        "all_measured_cells_complete": all(summary["all_successful"] for summary in summaries),
        "all_paired_comparisons_complete": all(row["all_rounds_paired"] for row in comparisons),
    }
    validation["all_passed"] = all(validation.values())
    report = {
        "schema_version": SCHEMA_VERSION,
        "source": "rsedis-thp-runtime-matrix",
        "paper_exact": False,
        "claim_grade": False,
        "config": config,
        "summaries": summaries,
        "paired_comparisons": comparisons,
        "validation": validation,
        "records_path": "records.json",
        "records_sha256": sha256_file(args.raw_dir / "records.json"),
    }
    write_json(args.raw_dir / "report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if validation["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
