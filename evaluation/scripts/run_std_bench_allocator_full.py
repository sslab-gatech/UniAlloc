#!/usr/bin/env python3
"""Run the complete canonical std_bench inventory across allocator variants.

Every allocator/benchmark cell is first attempted once in a fresh warm-up
process.  Cells which finish then receive three measured fresh processes.  A
bounded timeout is a terminal, right-censored observation for that cell;
identity, parse, build, and non-timeout execution failures remain fail-closed.

Benchmarks are assigned permanently to explicitly selected CPU lanes.  A lane
runs one benchmark at a time and rotates allocator order between warm-up and
measured rounds, so variants of the same benchmark never execute concurrently.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, Mapping, Sequence

SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[2]
BASE_SCRIPT = SCRIPT_PATH.with_name("run_std_bench_allocator_variants.py")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.scripts import (  # noqa: E402
    type_isolation_suite_contract as suite_contract,
)

EXPECTED_CANONICAL_BENCHMARK_COUNT = 468
MEASURED_ROUNDS = 3
RATIO_FLOOR_NS = 100.0
DIAGNOSTIC_LABEL = (
    "current-toolchain complete canonical std_bench inventory; "
    "timeout-censored; non-paper-exact"
)


def load_base_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "unialloc_std_bench_allocator_subset_runner", BASE_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import campaign helpers from {BASE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base = load_base_runner()
VARIANTS = base.VARIANTS
Variant = base.Variant

PUBLICATION_VARIANT_IDS = (
    "unialloc",
    "jemalloc",
    "mimalloc",
    "mimalloc_no_thp",
    "google_tcmalloc",
)
EXTRA_VARIANTS = (
    Variant("mimalloc_no_thp", "bench_mimalloc", "mimalloc (THP off)"),
    Variant("google_tcmalloc", "bench_tcmalloc", "Google TCMalloc"),
    Variant("system", "bench_ptmalloc", "System/ptmalloc"),
)
SUPPORTED_VARIANTS = (*VARIANTS, *EXTRA_VARIANTS)
VARIANT_BY_ID = {variant.allocator: variant for variant in SUPPORTED_VARIANTS}
SELECTION_SCHEMA_VERSION = 1
MEASUREMENT_LOCK_ATTEMPT_SCHEMA_VERSION = 1
MEASUREMENT_LOCK_ATTEMPTS_FILE = "measurement-lock-attempts.jsonl"
MIMALLOC_THP_RUNTIME_SOURCE = r"""#define _GNU_SOURCE
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <unistd.h>

#ifndef PR_SET_THP_DISABLE
#define PR_SET_THP_DISABLE 41
#endif
#ifndef PR_GET_THP_DISABLE
#define PR_GET_THP_DISABLE 42
#endif

static const char *expected_text(void) {
    const char *value = getenv("UNIALLOC_MIMALLOC_THP_EXPECTED");
    if (value != NULL && (strcmp(value, "0") == 0 || strcmp(value, "1") == 0)) {
        return value;
    }
    return NULL;
}

static void emit(const char *message) {
    (void)write(STDERR_FILENO, message, strlen(message));
}

__attribute__((constructor)) static void set_and_prove_policy(void) {
    const char *text = expected_text();
    if (text == NULL) {
        emit("UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE_START=error\n");
        _exit(86);
    }
    const int expected = text[0] - '0';
    if (prctl(PR_SET_THP_DISABLE, expected, 0, 0, 0) != 0) {
        emit("UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE_START=error\n");
        _exit(86);
    }
    const int observed = prctl(PR_GET_THP_DISABLE, 0, 0, 0, 0);
    if (observed == 0) {
        emit("UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE_START=0\n");
    } else if (observed == 1) {
        emit("UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE_START=1\n");
    } else {
        emit("UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE_START=error\n");
    }
    if (observed != expected) {
        _exit(86);
    }
}

__attribute__((destructor)) static void prove_final_policy(void) {
    const char *text = expected_text();
    const int observed = prctl(PR_GET_THP_DISABLE, 0, 0, 0, 0);
    if (observed == 0) {
        emit("UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE=0\n");
    } else if (observed == 1) {
        emit("UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE=1\n");
    } else {
        emit("UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE=error\n");
    }
    if (text == NULL || observed != text[0] - '0') {
        _exit(86);
    }
}
"""
MIMALLOC_THP_RUNTIME_POLICY = (
    "constructor PR_SET_THP_DISABLE plus initial and process-exit "
    "PR_GET_THP_DISABLE proofs"
)


@dataclass(frozen=True)
class Lane:
    index: int
    cpu: int
    benchmarks: tuple[str, ...]


@dataclass(frozen=True)
class CellState:
    status: str
    valid_measured_rounds: tuple[int, ...]
    timeout_phase: str | None = None
    timeout_round: int | None = None
    timeout_record: Mapping[str, Any] | None = None


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def parse_variant_ids(value: str) -> tuple[str, ...]:
    variant_ids = tuple(item.strip() for item in value.split(",") if item.strip())
    selected_variants(variant_ids)
    return variant_ids


def selected_variants(variant_ids: Sequence[str]) -> tuple[Any, ...]:
    ids = tuple(str(item) for item in variant_ids)
    if not ids:
        raise ValueError("at least one allocator variant is required")
    if len(ids) != len(set(ids)):
        raise ValueError("allocator variant ids must be unique")
    unknown = sorted(set(ids) - set(VARIANT_BY_ID))
    if unknown:
        raise ValueError(f"unknown allocator variant ids: {unknown}")
    if "unialloc" not in ids:
        raise ValueError("the unialloc baseline must be selected")
    aliases = (
        {"tcmalloc", "google_tcmalloc"},
        {"ptmalloc", "system"},
    )
    for alias_set in aliases:
        if alias_set.issubset(ids):
            raise ValueError(
                "allocator selector aliases cannot be selected together: "
                f"{sorted(alias_set)}"
            )
    return tuple(VARIANT_BY_ID[variant_id] for variant_id in ids)


def selection_payload(variants: Sequence[Any]) -> dict[str, Any]:
    variant_rows = [variant.__dict__ for variant in variants]
    variant_ids = [variant.allocator for variant in variants]
    digest_payload = {
        "variant_ids": variant_ids,
        "variants": variant_rows,
    }
    return {
        "schema_version": SELECTION_SCHEMA_VERSION,
        **digest_payload,
        "selection_sha256": sha256_bytes(
            json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ),
    }


def bind_campaign_selection(
    output_dir: Path, variants: Sequence[Any]
) -> dict[str, Any]:
    """Bind the ordered variant set before any build or benchmark process starts."""

    output_dir.mkdir(parents=True, exist_ok=True)
    expected = selection_payload(variants)
    path = output_dir / "campaign-selection.json"
    if path.exists():
        try:
            observed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("existing campaign selection is unreadable") from error
        if observed != expected:
            raise RuntimeError(
                "existing full campaign variant selection differs; "
                "choose a new output directory"
            )
        return expected

    legacy_config = output_dir / "campaign-config.json"
    if legacy_config.exists():
        try:
            config = json.loads(legacy_config.read_text(encoding="utf-8"))
            rows = config["variants"]
        except (
            KeyError,
            TypeError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
        ) as error:
            raise RuntimeError(
                "existing campaign configuration is unreadable"
            ) from error
        if rows != expected["variants"]:
            raise RuntimeError(
                "existing full campaign variant selection differs; "
                "choose a new output directory"
            )
    elif (output_dir / "records.jsonl").exists():
        raise RuntimeError("existing process records have no bound campaign selection")
    base.write_json(path, expected)
    return expected


def validate_existing_campaign_request(
    output_dir: Path,
    *,
    source_root: Path,
    args: argparse.Namespace,
    variants: Sequence[Any],
    selection: Mapping[str, Any],
) -> None:
    """Reject known campaign drift before any helper or build process starts."""

    path = output_dir / "campaign-config.json"
    if not path.exists():
        return
    try:
        observed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("existing campaign configuration is unreadable") from error
    if not isinstance(observed, dict):
        raise RuntimeError("existing campaign configuration is not an object")
    expected = {
        "schema_version": 2,
        "source_root": str(source_root),
        "runner_sha256": base.sha256_file(SCRIPT_PATH),
        "base_runner_sha256": base.sha256_file(BASE_SCRIPT),
        "variant_ids": [variant.allocator for variant in variants],
        "variants": [variant.__dict__ for variant in variants],
        "selection_sha256": selection["selection_sha256"],
        "warmups": 1,
        "measured_rounds": MEASURED_ROUNDS,
        "timeout_seconds": args.timeout_seconds,
        "jobs": args.jobs,
        "cpus": list(args.cpus),
        "numa_node": args.numa_node,
        "ratio_floor_ns_per_iter": RATIO_FLOOR_NS,
    }
    drift = [field for field, value in expected.items() if observed.get(field) != value]
    if drift:
        raise RuntimeError(
            "existing full campaign configuration differs before build: "
            + ", ".join(drift)
            + "; choose a new output directory"
        )


def ensure_mimalloc_thp_runtime(output_dir: Path) -> dict[str, Any]:
    """Build or validate the process-wide THP policy and proof DSO."""

    helper_dir = output_dir / "helpers" / "mimalloc-thp-runtime"
    helper_dir.mkdir(parents=True, exist_ok=True)
    source = helper_dir / "runtime.c"
    binary = helper_dir / "libunialloc_mimalloc_thp_runtime.so"
    record_path = helper_dir / "build.json"
    compiler_version_path = helper_dir / "compiler.version"
    build_stdout_path = helper_dir / "build.stdout"
    build_stderr_path = helper_dir / "build.stderr"
    source_bytes = MIMALLOC_THP_RUNTIME_SOURCE.encode("utf-8")
    source_sha = sha256_bytes(source_bytes)
    compiler = shutil.which("cc")
    if compiler is None:
        raise RuntimeError("required command is missing: cc")
    command = [
        compiler,
        "-std=c11",
        "-O2",
        "-fPIC",
        "-shared",
        "-Wall",
        "-Wextra",
        "-Wl,-z,defs",
        "-Wl,--build-id=none",
        str(source),
        "-o",
        str(binary),
    ]

    if record_path.exists():
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("mimalloc THP runtime record is unreadable") from error
        artifacts = (
            source,
            binary,
            compiler_version_path,
            build_stdout_path,
            build_stderr_path,
        )
        if not all(path.is_file() for path in artifacts) or not os.access(
            binary, os.X_OK
        ):
            raise RuntimeError("mimalloc THP runtime reuse failed identity validation")
        expected_record = {
            "schema_version": 1,
            "source": str(source),
            "source_sha256": source_sha,
            "binary": str(binary),
            "binary_sha256": base.sha256_file(binary),
            "command": command,
            "compiler_version": str(compiler_version_path),
            "compiler_version_sha256": base.sha256_file(compiler_version_path),
            "build_stdout": str(build_stdout_path),
            "build_stdout_sha256": base.sha256_file(build_stdout_path),
            "build_stderr": str(build_stderr_path),
            "build_stderr_sha256": base.sha256_file(build_stderr_path),
            "policy": MIMALLOC_THP_RUNTIME_POLICY,
        }
        if source.read_bytes() != source_bytes or record != expected_record:
            raise RuntimeError("mimalloc THP runtime reuse failed identity validation")
        return record

    if any(
        path.exists()
        for path in (
            source,
            binary,
            compiler_version_path,
            build_stdout_path,
            build_stderr_path,
        )
    ):
        raise RuntimeError("mimalloc THP runtime artifacts lack a build record")
    source.write_bytes(source_bytes)
    compiler_version = subprocess.run(
        [compiler, "--version"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ).stdout
    compiler_version_path.write_bytes(compiler_version)
    build = subprocess.run(
        command,
        cwd=helper_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    build_stdout_path.write_bytes(build.stdout)
    build_stderr_path.write_bytes(build.stderr)
    if build.returncode != 0:
        raise RuntimeError(
            "mimalloc THP runtime build failed: "
            + build.stderr.decode("utf-8", errors="replace")[-2000:]
        )
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError("mimalloc THP runtime build produced no shared library")
    record = {
        "schema_version": 1,
        "source": str(source),
        "source_sha256": source_sha,
        "binary": str(binary),
        "binary_sha256": base.sha256_file(binary),
        "command": command,
        "compiler_version": str(compiler_version_path),
        "compiler_version_sha256": sha256_bytes(compiler_version),
        "build_stdout": str(build_stdout_path),
        "build_stdout_sha256": sha256_bytes(build.stdout),
        "build_stderr": str(build_stderr_path),
        "build_stderr_sha256": sha256_bytes(build.stderr),
        "policy": MIMALLOC_THP_RUNTIME_POLICY,
    }
    base.write_json(record_path, record)
    return record


def parse_cpu_list(value: str) -> tuple[int, ...]:
    try:
        cpus = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as error:
        raise ValueError("CPU list must contain comma-separated integers") from error
    if not cpus or any(cpu < 0 for cpu in cpus):
        raise ValueError("CPU list must contain non-negative integers")
    if len(cpus) != len(set(cpus)):
        raise ValueError("CPU list entries must be unique")
    return cpus


def validate_canonical_inventory(names: Sequence[str]) -> tuple[str, ...]:
    inventory = tuple(names)
    if len(inventory) != EXPECTED_CANONICAL_BENCHMARK_COUNT:
        raise RuntimeError(
            "canonical std_bench inventory must contain exactly "
            f"{EXPECTED_CANONICAL_BENCHMARK_COUNT} benchmarks; got {len(inventory)}"
        )
    if len(inventory) != len(set(inventory)):
        raise RuntimeError("canonical std_bench inventory contains duplicate names")
    if any(not isinstance(name, str) or not name for name in inventory):
        raise RuntimeError("canonical std_bench inventory contains an empty name")
    return inventory


def assign_benchmark_lanes(
    benchmarks: Sequence[str], cpus: Sequence[int]
) -> tuple[Lane, ...]:
    if not cpus:
        raise ValueError("at least one explicit CPU is required")
    if len(cpus) != len(set(cpus)):
        raise ValueError("CPU lane assignments must be unique")
    if len(cpus) > len(benchmarks):
        raise ValueError("jobs cannot exceed the benchmark inventory size")
    return tuple(
        Lane(
            index=index, cpu=int(cpu), benchmarks=tuple(benchmarks[index :: len(cpus)])
        )
        for index, cpu in enumerate(cpus)
    )


def record_key(record: Mapping[str, Any]) -> tuple[str, int, str, str]:
    return (
        str(record["phase"]),
        int(record["round"]),
        str(record["allocator"]),
        str(record["benchmark"]),
    )


def record_kind(record: Mapping[str, Any]) -> str:
    if record.get("valid") is True and record.get("timed_out") is not True:
        if record.get("status") not in (None, "valid"):
            raise RuntimeError(
                f"valid record has contradictory status: {record_key(record)}"
            )
        return "valid"
    if (
        record.get("timed_out") is True
        and record.get("valid") is not True
        and record.get("status") in (None, "timeout_censored")
    ):
        return "timeout"
    raise RuntimeError(f"invalid process record: {record_key(record)}")


def classify_cell_history(
    records: Sequence[Mapping[str, Any]], *, measured_rounds: int
) -> CellState:
    if not records:
        return CellState("pending", ())
    identities = {
        (str(record.get("allocator")), str(record.get("benchmark")))
        for record in records
    }
    if len(identities) != 1:
        raise RuntimeError("cell history mixes allocator or benchmark identities")
    by_slot: dict[tuple[str, int], Mapping[str, Any]] = {}
    for record in records:
        phase = str(record.get("phase"))
        round_index = int(record.get("round", -1))
        if phase == "warmup" and round_index != 0:
            raise RuntimeError("warmup record must use round 0")
        if phase == "measured" and not 1 <= round_index <= measured_rounds:
            raise RuntimeError("measured record has an out-of-range round")
        if phase not in ("warmup", "measured"):
            raise RuntimeError(f"unknown campaign phase {phase!r}")
        slot = (phase, round_index)
        if slot in by_slot:
            raise RuntimeError(f"duplicate cell process slot: {slot}")
        record_kind(record)
        by_slot[slot] = record

    warmup = by_slot.get(("warmup", 0))
    measured = {
        round_index: record
        for (phase, round_index), record in by_slot.items()
        if phase == "measured"
    }
    if warmup is None:
        if measured:
            raise RuntimeError("measured records exist before the warmup")
        return CellState("pending", ())
    if record_kind(warmup) == "timeout":
        if measured:
            raise RuntimeError("measured record exists after a timeout")
        return CellState(
            "censored",
            (),
            timeout_phase="warmup",
            timeout_round=0,
            timeout_record=warmup,
        )

    if measured:
        rounds = sorted(measured)
        if rounds != list(range(1, max(rounds) + 1)):
            raise RuntimeError(f"measured rounds are not a contiguous prefix: {rounds}")
    valid_rounds: list[int] = []
    timeout_record: Mapping[str, Any] | None = None
    timeout_round: int | None = None
    for round_index in sorted(measured):
        record = measured[round_index]
        if timeout_record is not None:
            raise RuntimeError("measured record exists after a timeout")
        if record_kind(record) == "timeout":
            timeout_record = record
            timeout_round = round_index
        else:
            valid_rounds.append(round_index)
    if timeout_record is not None:
        return CellState(
            "censored",
            tuple(valid_rounds),
            timeout_phase="measured",
            timeout_round=timeout_round,
            timeout_record=timeout_record,
        )
    if valid_rounds == list(range(1, measured_rounds + 1)):
        return CellState("complete", tuple(valid_rounds))
    return CellState("pending", tuple(valid_rounds))


def load_completed_records(
    output_dir: Path,
) -> dict[tuple[str, int, str, str], dict[str, Any]]:
    completed: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    path = output_dir / "records.jsonl"
    if not path.exists():
        return completed
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    f"invalid records.jsonl line {line_number}: {error}"
                ) from error
            if not isinstance(value, dict):
                raise RuntimeError(f"invalid records.jsonl entry at line {line_number}")
            key = record_key(value)
            record_kind(value)
            if key in completed:
                raise RuntimeError(
                    f"records.jsonl contains a duplicate process key: {key}"
                )
            completed[key] = value
    return completed


def validate_resumed_record_artifacts(
    record: Mapping[str, Any], *, output_dir: Path
) -> None:
    """Rehash every persisted process artifact before reusing its record."""

    root = output_dir.resolve()
    key = record_key(record)
    for prefix, require_size in (
        ("stdout", True),
        ("stderr", True),
        ("time", False),
    ):
        raw_path = record.get(f"{prefix}_path")
        expected_sha256 = record.get(f"{prefix}_sha256")
        if not isinstance(raw_path, str) or not raw_path:
            raise RuntimeError(f"resumed record lacks {prefix} path: {key}")
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise RuntimeError(f"resumed record lacks {prefix} digest: {key}")
        path = (root / raw_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise RuntimeError(
                f"resumed record {prefix} path escapes the campaign: {key}"
            ) from error
        if not path.is_file() or base.sha256_file(path) != expected_sha256:
            raise RuntimeError(
                f"resumed record {prefix} artifact digest mismatch: {key}"
            )
        if require_size and record.get(f"{prefix}_bytes") != path.stat().st_size:
            raise RuntimeError(f"resumed record {prefix} artifact size mismatch: {key}")

    stdout_path = (root / str(record["stdout_path"])).resolve()
    record_path = stdout_path.parent / "record.json"
    try:
        persisted = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"resumed process record artifact is unreadable: {key}"
        ) from error
    if persisted != record:
        raise RuntimeError(f"resumed process record artifact differs: {key}")


def append_record(output_dir: Path, record: Mapping[str, Any]) -> None:
    with (output_dir / "records.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def finite_nonnegative(value: Any, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{field} must be numeric") from error
    if not math.isfinite(result) or result < 0:
        raise RuntimeError(f"{field} must be finite and non-negative")
    return result


def benchmark_family(benchmark: str) -> str:
    return benchmark.split("::", 1)[0]


def percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def ratio_quantiles(values: Sequence[float]) -> dict[str, float]:
    return {
        "p05": percentile(values, 0.05),
        "p25": percentile(values, 0.25),
        "p50": percentile(values, 0.50),
        "p75": percentile(values, 0.75),
        "p95": percentile(values, 0.95),
    }


def ratio_distribution(
    rows: Sequence[tuple[str, float, float, float]],
) -> dict[str, Any]:
    if not rows:
        return {
            "benchmark_count": 0,
            "geomean": None,
            "family_balanced_geomean": None,
            "family_count": 0,
            "quantiles": None,
            "minimum": None,
            "maximum": None,
        }
    ratios = [float(row[1]) for row in rows]
    if any(not math.isfinite(value) or value <= 0 for value in ratios):
        raise RuntimeError("ratio distributions require finite positive ratios")
    minimum = min(rows, key=lambda row: (row[1], row[0]))
    maximum = min(rows, key=lambda row: (-row[1], row[0]))
    family_ratios: dict[str, list[float]] = {}
    for benchmark, ratio, _, _ in rows:
        family_ratios.setdefault(benchmark_family(benchmark), []).append(float(ratio))
    family_geomeans = [base.geometric_mean(values) for values in family_ratios.values()]

    def extreme(row: tuple[str, float, float, float]) -> dict[str, Any]:
        benchmark, ratio, allocator_median, reference_median = row
        return {
            "benchmark": benchmark,
            "family": benchmark_family(benchmark),
            "ratio_vs_unialloc": float(ratio),
            "allocator_median_ns_per_iter": float(allocator_median),
            "unialloc_median_ns_per_iter": float(reference_median),
            "tie_count": sum(candidate[1] == ratio for candidate in rows),
        }

    return {
        "benchmark_count": len(rows),
        "geomean": base.geometric_mean(ratios),
        "family_balanced_geomean": base.geometric_mean(family_geomeans),
        "family_count": len(family_ratios),
        "quantiles": ratio_quantiles(ratios),
        "minimum": extreme(minimum),
        "maximum": extreme(maximum),
    }


def _cell_records(
    records: Sequence[Mapping[str, Any]], allocator: str, benchmark: str
) -> list[Mapping[str, Any]]:
    return [
        record
        for record in records
        if record.get("allocator") == allocator and record.get("benchmark") == benchmark
    ]


def summarize(
    records: Sequence[Mapping[str, Any]],
    *,
    benchmarks: Sequence[str],
    measured_rounds: int,
    output_dir: Path,
    require_terminal: bool,
    timeout_seconds: int | None = None,
    jobs: int | None = None,
    cpus: Sequence[int] | None = None,
    variants: Sequence[Any] = VARIANTS,
    measurement_lock: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    benchmark_order = tuple(benchmarks)
    selected = tuple(variants)
    benchmark_set = set(benchmark_order)
    allocator_set = {variant.allocator for variant in selected}
    for record in records:
        if record.get("benchmark") not in benchmark_set:
            raise RuntimeError(
                f"record has an unknown benchmark: {record.get('benchmark')}"
            )
        if record.get("allocator") not in allocator_set:
            raise RuntimeError(
                f"record has an unknown allocator: {record.get('allocator')}"
            )
        record_kind(record)

    cells: list[dict[str, Any]] = []
    cell_map: dict[tuple[str, str], dict[str, Any]] = {}
    censored_cells: list[dict[str, Any]] = []
    for benchmark in benchmark_order:
        for variant in selected:
            history = _cell_records(records, variant.allocator, benchmark)
            state = classify_cell_history(history, measured_rounds=measured_rounds)
            if require_terminal and state.status == "pending":
                raise RuntimeError(
                    f"incomplete terminal state for {variant.allocator}/{benchmark}"
                )
            valid_measured = sorted(
                (
                    record
                    for record in history
                    if record.get("phase") == "measured"
                    and record_kind(record) == "valid"
                ),
                key=lambda record: int(record["round"]),
            )
            values = [
                finite_nonnegative(
                    record.get("ns_per_iter"),
                    field=f"{variant.allocator}/{benchmark} ns_per_iter",
                )
                for record in valid_measured
            ]
            cell: dict[str, Any] = {
                "benchmark": benchmark,
                "family": benchmark_family(benchmark),
                "allocator": variant.allocator,
                "feature": variant.feature,
                "variant_label": variant.label,
                "status": state.status,
                "valid_measured_rounds": list(state.valid_measured_rounds),
                "ns_per_iter_samples": values,
                "median_ns_per_iter": None,
                "mad_ns_per_iter": None,
                "min_ns_per_iter": min(values) if values else None,
                "max_ns_per_iter": max(values) if values else None,
                "ratio_vs_unialloc": None,
                "ratio_status": "pending_comparison",
            }
            if state.status == "complete":
                cell["median_ns_per_iter"] = statistics.median(values)
                cell["mad_ns_per_iter"] = base.median_absolute_deviation(values)
            if state.status == "censored":
                timeout = state.timeout_record or {}
                censored = {
                    "benchmark": benchmark,
                    "family": benchmark_family(benchmark),
                    "allocator": variant.allocator,
                    "feature": variant.feature,
                    "phase": state.timeout_phase,
                    "round": state.timeout_round,
                    "valid_measured_rounds_before_timeout": list(
                        state.valid_measured_rounds
                    ),
                    "timeout_seconds": timeout.get("timeout_seconds"),
                    "stdout_path": timeout.get("stdout_path"),
                    "stderr_path": timeout.get("stderr_path"),
                }
                censored_cells.append(censored)
                cell["censoring"] = censored
            cells.append(cell)
            cell_map[(variant.allocator, benchmark)] = cell

    complete_benchmarks: list[str] = []
    ratio_benchmarks: list[str] = []
    robust_benchmarks: list[str] = []
    non_positive_median_benchmarks: list[str] = []
    robustness_exclusions: list[dict[str, Any]] = []
    for benchmark in benchmark_order:
        benchmark_cells = [
            cell_map[(variant.allocator, benchmark)] for variant in selected
        ]
        if all(cell["status"] == "complete" for cell in benchmark_cells):
            complete_benchmarks.append(benchmark)
            medians = [float(cell["median_ns_per_iter"]) for cell in benchmark_cells]
            if all(value > 0 for value in medians):
                ratio_benchmarks.append(benchmark)
                baseline = float(
                    cell_map[("unialloc", benchmark)]["median_ns_per_iter"]
                )
                for cell in benchmark_cells:
                    cell["ratio_vs_unialloc"] = (
                        float(cell["median_ns_per_iter"]) / baseline
                    )
                    cell["ratio_status"] = "defined"
                if min(medians) >= RATIO_FLOOR_NS:
                    robust_benchmarks.append(benchmark)
                else:
                    minimum = min(
                        benchmark_cells,
                        key=lambda cell: (
                            float(cell["median_ns_per_iter"]),
                            str(cell["allocator"]),
                        ),
                    )
                    robustness_exclusions.append(
                        {
                            "benchmark": benchmark,
                            "reason": "minimum_median_below_ratio_floor",
                            "minimum_median_ns_per_iter": minimum["median_ns_per_iter"],
                            "minimum_median_allocator": minimum["allocator"],
                        }
                    )
            else:
                non_positive_median_benchmarks.append(benchmark)
                for cell in benchmark_cells:
                    cell["ratio_status"] = "excluded_non_positive_median"
                minimum = min(
                    benchmark_cells,
                    key=lambda cell: (
                        float(cell["median_ns_per_iter"]),
                        str(cell["allocator"]),
                    ),
                )
                robustness_exclusions.append(
                    {
                        "benchmark": benchmark,
                        "reason": "non_positive_allocator_median",
                        "minimum_median_ns_per_iter": minimum["median_ns_per_iter"],
                        "minimum_median_allocator": minimum["allocator"],
                    }
                )
        else:
            available = [
                cell
                for cell in benchmark_cells
                if cell["median_ns_per_iter"] is not None
            ]
            for cell in benchmark_cells:
                cell["ratio_status"] = "excluded_incomplete_comparison"
            minimum = (
                min(
                    available,
                    key=lambda cell: (
                        float(cell["median_ns_per_iter"]),
                        str(cell["allocator"]),
                    ),
                )
                if available
                else None
            )
            robustness_exclusions.append(
                {
                    "benchmark": benchmark,
                    "reason": "censored_or_incomplete_allocator_cell",
                    "minimum_median_ns_per_iter": (
                        minimum["median_ns_per_iter"] if minimum else None
                    ),
                    "minimum_median_allocator": (
                        minimum["allocator"] if minimum else None
                    ),
                }
            )

    aggregates: list[dict[str, Any]] = []
    for variant in selected:
        raw_rows = [
            (
                benchmark,
                float(cell_map[(variant.allocator, benchmark)]["ratio_vs_unialloc"]),
                float(cell_map[(variant.allocator, benchmark)]["median_ns_per_iter"]),
                float(cell_map[("unialloc", benchmark)]["median_ns_per_iter"]),
            )
            for benchmark in ratio_benchmarks
        ]
        robust_rows = [row for row in raw_rows if row[0] in set(robust_benchmarks)]
        aggregates.append(
            {
                "allocator": variant.allocator,
                "feature": variant.feature,
                "variant_label": variant.label,
                "raw": ratio_distribution(raw_rows),
                "robust": ratio_distribution(robust_rows),
            }
        )

    warmup_records = [record for record in records if record.get("phase") == "warmup"]
    measured_records = [
        record for record in records if record.get("phase") == "measured"
    ]
    observed_timeouts = {
        int(record["timeout_seconds"])
        for record in records
        if record.get("timeout_seconds") is not None
    }
    if timeout_seconds is None:
        if len(observed_timeouts) > 1:
            raise RuntimeError(
                f"records use inconsistent process timeouts: {sorted(observed_timeouts)}"
            )
        timeout_seconds = next(iter(observed_timeouts), None)
    elif observed_timeouts and observed_timeouts != {timeout_seconds}:
        raise RuntimeError(
            "records disagree with the configured timeout_seconds_per_process"
        )
    expected_warmups = len(benchmark_order) * len(selected)
    if require_terminal and len(warmup_records) != expected_warmups:
        raise RuntimeError(
            f"all {expected_warmups} allocator/benchmark warmups must be attempted"
        )
    summary: dict[str, Any] = {
        "schema_version": 2,
        "generated_utc": base.utc_now(),
        "diagnostic_label": DIAGNOSTIC_LABEL,
        "claim_grade": False,
        "measurement_lock": (
            dict(measurement_lock) if measurement_lock is not None else None
        ),
        "methodology": {
            "benchmarks": list(benchmark_order),
            "allocators": [variant.allocator for variant in selected],
            "variants": [variant.__dict__ for variant in selected],
            "warmup_fresh_processes_per_cell": 1,
            "measured_fresh_processes_per_complete_cell": measured_rounds,
            "measured_fresh_processes_per_cell": measured_rounds,
            "timeout_seconds_per_process": timeout_seconds,
            "jobs": jobs,
            "cpus": list(cpus) if cpus is not None else None,
            "timeout_censoring": (
                "a warmup timeout terminates the cell; a measured timeout retains "
                "prior raw observations and suppresses the cell median"
            ),
            "ratio_floor_ns_per_iter": RATIO_FLOOR_NS,
            "ratio_direction": "allocator median / UniAlloc median; lower is faster",
            "family_definition": "benchmark name prefix before the first ::",
        },
        "record_counts": {
            "all": len(records),
            "warmup": len(warmup_records),
            "measured": len(measured_records),
            "valid": sum(record_kind(record) == "valid" for record in records),
            "timeout_censored": sum(
                record_kind(record) == "timeout" for record in records
            ),
            "expected_warmup_attempts": expected_warmups,
            "all_warmups_attempted": len(warmup_records) == expected_warmups,
        },
        "coverage": {
            "canonical_inventory_count": len(benchmark_order),
            "allocator_cells": len(benchmark_order) * len(selected),
            "complete_cells": sum(cell["status"] == "complete" for cell in cells),
            "censored_cells": len(censored_cells),
            "pending_cells": sum(cell["status"] == "pending" for cell in cells),
            "all_allocator_complete_benchmarks": len(complete_benchmarks),
            "ratio_defined_benchmarks": len(ratio_benchmarks),
            "robust_benchmarks": len(robust_benchmarks),
        },
        "comparison_selection": {
            "rule": (
                f"all {len(selected)} selected allocator cells completed "
                f"{measured_rounds}/{measured_rounds} rounds with positive medians"
            ),
            "complete_all_allocator_benchmarks": complete_benchmarks,
            "comparable_benchmarks": ratio_benchmarks,
            "non_positive_median_benchmarks": non_positive_median_benchmarks,
            "comparable_count": len(ratio_benchmarks),
            "excluded_count": len(benchmark_order) - len(ratio_benchmarks),
        },
        "robustness_selection": {
            "rule": (
                f"all {len(selected)} selected allocator cell medians are at least "
                f"{RATIO_FLOOR_NS:g} ns/iter"
            ),
            "threshold_ns_per_iter": RATIO_FLOOR_NS,
            "selected_benchmarks": robust_benchmarks,
            "comparable_count": len(ratio_benchmarks),
            "selected_count": len(robust_benchmarks),
            "excluded_count": len(benchmark_order) - len(robust_benchmarks),
            "excluded_benchmarks": robustness_exclusions,
        },
        "cells": cells,
        "aggregates": aggregates,
        "censored_cells": censored_cells,
        "limitations": [
            "Timeout-censored cells have no inferred ns/iter value or ratio.",
            (
                "Raw ratio extrema include positive measurements below 100 "
                "ns/iter and may be dominated by timer resolution."
            ),
            (
                "Robust aggregates require every allocator median for a "
                "benchmark to be at least 100 ns/iter."
            ),
            "Three observations support descriptive medians and ranges, not confidence intervals.",
        ],
    }
    write_summary_artifacts(output_dir, summary)
    return summary


def write_summary_artifacts(output_dir: Path, summary: Mapping[str, Any]) -> None:
    base.write_json(output_dir / "summary.json", summary)
    cells = list(summary["cells"])
    cell_fields = (
        "benchmark",
        "family",
        "allocator",
        "feature",
        "status",
        "valid_measured_rounds",
        "ns_per_iter_samples",
        "median_ns_per_iter",
        "mad_ns_per_iter",
        "min_ns_per_iter",
        "max_ns_per_iter",
        "ratio_vs_unialloc",
        "ratio_status",
    )
    with (output_dir / "cell-summary.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=cell_fields, lineterminator="\n")
        writer.writeheader()
        for cell in cells:
            row = {field: cell.get(field) for field in cell_fields}
            row["valid_measured_rounds"] = json.dumps(row["valid_measured_rounds"])
            row["ns_per_iter_samples"] = json.dumps(row["ns_per_iter_samples"])
            writer.writerow(row)

    aggregate_fields = (
        "allocator",
        "feature",
        "scope",
        "benchmark_count",
        "geomean",
        "family_balanced_geomean",
        "family_count",
        "p05",
        "p25",
        "p50",
        "p75",
        "p95",
        "minimum_ratio",
        "minimum_benchmark",
        "maximum_ratio",
        "maximum_benchmark",
    )
    with (output_dir / "aggregate-summary.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle, fieldnames=aggregate_fields, lineterminator="\n"
        )
        writer.writeheader()
        for aggregate in summary["aggregates"]:
            for scope in ("raw", "robust"):
                distribution = aggregate[scope]
                quantiles = distribution.get("quantiles") or {}
                minimum = distribution.get("minimum") or {}
                maximum = distribution.get("maximum") or {}
                writer.writerow(
                    {
                        "allocator": aggregate["allocator"],
                        "feature": aggregate["feature"],
                        "scope": scope,
                        "benchmark_count": distribution["benchmark_count"],
                        "geomean": distribution["geomean"],
                        "family_balanced_geomean": distribution[
                            "family_balanced_geomean"
                        ],
                        "family_count": distribution["family_count"],
                        **{
                            name: quantiles.get(name)
                            for name in ("p05", "p25", "p50", "p75", "p95")
                        },
                        "minimum_ratio": minimum.get("ratio_vs_unialloc"),
                        "minimum_benchmark": minimum.get("benchmark"),
                        "maximum_ratio": maximum.get("ratio_vs_unialloc"),
                        "maximum_benchmark": maximum.get("benchmark"),
                    }
                )

    censored_fields = (
        "benchmark",
        "family",
        "allocator",
        "feature",
        "phase",
        "round",
        "valid_measured_rounds_before_timeout",
        "timeout_seconds",
        "stdout_path",
        "stderr_path",
    )
    with (output_dir / "censored-cells.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=censored_fields, lineterminator="\n")
        writer.writeheader()
        for censored in summary["censored_cells"]:
            row = {field: censored.get(field) for field in censored_fields}
            row["valid_measured_rounds_before_timeout"] = json.dumps(
                row["valid_measured_rounds_before_timeout"]
            )
            writer.writerow(row)


def validate_timeout_identity(record: Mapping[str, Any], allocator: str) -> bool:
    expected_scudo_markers = 1 if allocator == "scudo" else 0
    expected_tcmalloc_markers = 1 if allocator in {"tcmalloc", "google_tcmalloc"} else 0
    reported = record.get("reported_benchmark")
    benchmark = record.get("benchmark")
    return bool(
        record.get("timed_out") is True
        and record.get("glibc_tunables_present") is False
        and record.get("scudo_identity_marker_count") == expected_scudo_markers
        and record.get("google_tcmalloc_identity_marker_count", 0)
        == expected_tcmalloc_markers
        and reported in (None, benchmark)
        and int(record.get("benchmark_line_count", 0)) <= 1
    )


def validate_process_record_identity(
    record: Mapping[str, Any],
    *,
    variant: Any,
    binary_sha256: str,
    cpu: int,
    numa_node: int,
    timeout_seconds: int,
    scudo_runtime: Path | None,
    mimalloc_thp_runtime_sha256: str | None = None,
) -> None:
    """Re-authenticate a fresh or resumed process record against this campaign."""

    key = record_key(record)
    kind = record_kind(record)
    if record.get("feature") != variant.feature:
        raise RuntimeError(f"process record has the wrong feature: {key}")
    if record.get("binary_sha256") != binary_sha256:
        raise RuntimeError(f"process record has a different binary identity: {key}")
    if record.get("cpu") != cpu or record.get("numa_node") != numa_node:
        raise RuntimeError(f"process record has different CPU/NUMA placement: {key}")
    if record.get("timeout_seconds") != timeout_seconds:
        raise RuntimeError(f"process record has a different timeout bound: {key}")
    if record.get("glibc_tunables_present") is not False:
        raise RuntimeError(
            f"process record violated the clean glibc environment: {key}"
        )

    expected_markers = 1 if variant.allocator == "scudo" else 0
    if record.get("scudo_identity_marker_count") != expected_markers:
        raise RuntimeError(f"process record has the wrong Scudo marker count: {key}")
    if variant.allocator == "scudo":
        if scudo_runtime is None or record.get("scudo_runtime_library") != str(
            scudo_runtime
        ):
            raise RuntimeError(f"process record has a different Scudo runtime: {key}")

    expected_tcmalloc_markers = 1 if base.is_google_tcmalloc_variant(variant) else 0
    if (
        record.get("google_tcmalloc_identity_marker_count", 0)
        != expected_tcmalloc_markers
    ):
        raise RuntimeError(
            f"process record has the wrong Google TCMalloc marker count: {key}"
        )

    expected_thp_state = base.mimalloc_thp_expected_state(variant)
    if expected_thp_state is not None:
        runtime = record.get("mimalloc_thp_runtime")
        if (
            not isinstance(runtime, Mapping)
            or runtime.get("required") is not True
            or runtime.get("initial_verified") is not True
            or runtime.get("initial_marker_count") != 1
            or runtime.get("initial_pr_get_thp_disable") != expected_thp_state
        ):
            raise RuntimeError(
                f"process record has invalid initial mimalloc THP runtime proof: {key}"
            )
        if kind == "valid" and (
            runtime.get("verified") is not True
            or runtime.get("marker_count") != 1
            or runtime.get("pr_get_thp_disable") != expected_thp_state
        ):
            raise RuntimeError(
                f"process record has invalid final mimalloc THP runtime proof: {key}"
            )
        if (
            mimalloc_thp_runtime_sha256 is None
            or record.get("mimalloc_thp_runtime_sha256") != mimalloc_thp_runtime_sha256
        ):
            raise RuntimeError(
                f"process record has a different mimalloc THP runtime: {key}"
            )
    else:
        runtime = record.get("mimalloc_thp_runtime")
        if isinstance(runtime, Mapping) and runtime.get("marker_count", 0) != 0:
            raise RuntimeError(
                f"process record has an unexpected mimalloc THP marker: {key}"
            )

    if kind == "timeout":
        if not validate_timeout_identity(record, variant.allocator):
            raise RuntimeError(f"timeout record failed runtime identity checks: {key}")
        return

    if record.get("exit_code") != 0 or record.get("time_exit_status") != 0:
        raise RuntimeError(f"valid process record has a nonzero exit status: {key}")
    if record.get("reported_benchmark") != record.get("benchmark"):
        raise RuntimeError(f"valid process record did not report its exact leaf: {key}")
    if record.get("time_parse_error") is not None:
        raise RuntimeError(f"valid process record has a GNU time parse error: {key}")
    finite_nonnegative(record.get("ns_per_iter"), field=f"{key} ns_per_iter")


def annotate_process_record(
    record: dict[str, Any], *, allocator: str, output_dir: Path
) -> dict[str, Any]:
    record["schema_version"] = 2
    record["diagnostic_label"] = DIAGNOSTIC_LABEL
    if record.get("valid") is True:
        record["status"] = "valid"
    elif validate_timeout_identity(record, allocator):
        record["status"] = "timeout_censored"
    else:
        record["status"] = "invalid"
    stdout_path = output_dir / str(record["stdout_path"])
    base.write_json(stdout_path.parent / "record.json", record)
    return record


def variant_order(
    benchmark_index: int,
    round_index: int,
    variants: Sequence[Any] = VARIANTS,
) -> tuple[Any, ...]:
    selected = tuple(variants)
    rotation = (benchmark_index + round_index) % len(selected)
    return selected[rotation:] + selected[:rotation]


def expected_process_slots(
    benchmarks: Sequence[str],
    variants: Sequence[Any],
    *,
    measured_rounds: int,
) -> set[tuple[str, int, str, str]]:
    phases = (
        ("warmup", 0),
        *(("measured", index) for index in range(1, measured_rounds + 1)),
    )
    return {
        (phase, round_index, variant.allocator, benchmark)
        for benchmark in benchmarks
        for variant in variants
        for phase, round_index in phases
    }


@contextmanager
def exclusive_campaign_lock(output_dir: Path) -> Iterator[None]:
    """Hold a nonblocking inter-process lock for one campaign output tree."""

    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / ".campaign.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                f"another campaign is already active for {output_dir}"
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nstarted_utc={base.utc_now()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def write_or_validate_campaign_config(path: Path, config: Mapping[str, Any]) -> None:
    """Keep append-only lock evidence dynamic while campaign inputs stay fixed."""

    expected = dict(config)
    if path.exists():
        try:
            observed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "existing campaign configuration is unreadable"
            ) from error
        if not isinstance(observed, dict):
            raise RuntimeError("existing campaign configuration is not an object")
        observed.pop("measurement_lock", None)
        if observed != expected:
            raise RuntimeError(
                "existing full campaign configuration differs; "
                "choose a new output directory"
            )
        return
    base.write_json(path, expected)


def validate_measurement_lock_evidence(
    value: Mapping[str, Any], *, output_dir: Path
) -> dict[str, Any]:
    """Validate the canonical lock record after its protected scope exits."""

    evidence = dict(value)
    expected = {
        "lock_path": str(suite_contract.HOST_PRIMARY_MEASUREMENT_LOCK),
        "runner": SCRIPT_PATH.name,
        "raw_root": str(output_dir.resolve()),
        "target_id": None,
        "phase": "warmup-and-measured",
    }
    for field, expected_value in expected.items():
        if evidence.get(field) != expected_value:
            raise RuntimeError(f"measurement lock evidence has invalid {field}")
    for field in ("pid", "wait_seconds", "acquired_unix", "released_unix"):
        value = evidence.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"measurement lock evidence lacks numeric {field}")
    if evidence["pid"] <= 0 or evidence["wait_seconds"] < 0:
        raise RuntimeError("measurement lock evidence has invalid process or wait data")
    if evidence["released_unix"] < evidence["acquired_unix"]:
        raise RuntimeError("measurement lock release predates acquisition")
    return evidence


def measurement_lock_attempt(
    evidence: Mapping[str, Any], *, output_dir: Path
) -> dict[str, Any]:
    """Give one completed canonical lock scope a content-derived identity."""

    validated = validate_measurement_lock_evidence(evidence, output_dir=output_dir)
    payload = {
        "schema_version": MEASUREMENT_LOCK_ATTEMPT_SCHEMA_VERSION,
        "evidence": validated,
    }
    return {
        **payload,
        "attempt_id": sha256_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ),
    }


def load_measurement_lock_attempts(output_dir: Path) -> list[dict[str, Any]]:
    """Revalidate the complete append-only host-lock attempt history."""

    path = output_dir / MEASUREMENT_LOCK_ATTEMPTS_FILE
    if not path.exists():
        return []
    attempts: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise RuntimeError(
                    f"measurement lock attempt log has blank line {line_number}"
                )
            try:
                observed = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    f"invalid measurement lock attempt line {line_number}"
                ) from error
            if not isinstance(observed, dict) or not isinstance(
                observed.get("evidence"), dict
            ):
                raise RuntimeError(
                    f"invalid measurement lock attempt record {line_number}"
                )
            expected = measurement_lock_attempt(
                observed["evidence"], output_dir=output_dir
            )
            if observed != expected:
                raise RuntimeError(
                    f"measurement lock attempt identity mismatch at line {line_number}"
                )
            attempt_id = str(observed["attempt_id"])
            if attempt_id in seen:
                raise RuntimeError("measurement lock attempt log contains a duplicate")
            seen.add(attempt_id)
            attempts.append(observed)
    return attempts


def measurement_lock_binding(
    output_dir: Path, attempts: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Bind an ordered lock-attempt history to a campaign artifact."""

    path = output_dir / MEASUREMENT_LOCK_ATTEMPTS_FILE
    normalized = [dict(attempt) for attempt in attempts]
    return {
        "lock_path": str(suite_contract.HOST_PRIMARY_MEASUREMENT_LOCK),
        "attempt_log": str(path.resolve()),
        "attempt_log_sha256": base.sha256_file(path) if path.is_file() else None,
        "attempt_count": len(normalized),
        "attempt_ids": [attempt["attempt_id"] for attempt in normalized],
        "attempts": normalized,
        "latest_attempt": normalized[-1] if normalized else None,
    }


def validate_preexisting_measurement_lock_bindings(
    output_dir: Path,
    *,
    attempts: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Reject attempt-log truncation or reordering before replacing bindings."""

    history = (
        load_measurement_lock_attempts(output_dir)
        if attempts is None
        else [dict(attempt) for attempt in attempts]
    )
    expected = measurement_lock_binding(output_dir, history)
    for name in ("campaign-config.json", "provenance.json"):
        path = output_dir / name
        if not path.exists():
            if history:
                raise RuntimeError(
                    f"{name} is missing its measurement lock attempt binding"
                )
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"{name} is unreadable") from error
        if not isinstance(document, dict):
            raise RuntimeError(f"{name} is not an object")
        observed = document.get("measurement_lock")
        if observed is None:
            if history:
                raise RuntimeError(
                    f"{name} is missing its measurement lock attempt binding"
                )
            continue
        if not isinstance(observed, dict):
            raise RuntimeError(f"{name} has an invalid measurement lock binding")
        required = (
            "attempt_log_sha256",
            "attempt_count",
            "attempt_ids",
            "attempts",
        )
        for field in required:
            if observed.get(field) != expected[field]:
                raise RuntimeError(
                    f"{name} measurement lock {field} differs from the attempt log"
                )
        if observed != expected:
            raise RuntimeError(
                f"{name} measurement lock binding differs from the attempt log"
            )
    return history


def bind_measurement_lock_attempts(
    output_dir: Path,
    *,
    config: dict[str, Any],
    provenance: dict[str, Any],
    attempts: Sequence[Mapping[str, Any]] | None = None,
    validate_existing: bool = True,
) -> dict[str, Any]:
    """Bind the full validated attempt history into campaign artifacts."""

    history = (
        load_measurement_lock_attempts(output_dir)
        if attempts is None
        else [dict(attempt) for attempt in attempts]
    )
    if validate_existing:
        validate_preexisting_measurement_lock_bindings(output_dir, attempts=history)
    binding = measurement_lock_binding(output_dir, history)
    config["measurement_lock"] = binding
    provenance["measurement_lock"] = binding
    base.write_json(output_dir / "campaign-config.json", config)
    base.write_json(output_dir / "provenance.json", provenance)
    return binding


def persist_measurement_lock_attempt(
    output_dir: Path,
    *,
    config: dict[str, Any],
    provenance: dict[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one completed lock attempt and refresh its campaign bindings."""

    attempt = measurement_lock_attempt(evidence, output_dir=output_dir)
    existing = validate_preexisting_measurement_lock_bindings(output_dir)
    if attempt["attempt_id"] in {item["attempt_id"] for item in existing}:
        raise RuntimeError("measurement lock attempt was already persisted")
    path = output_dir / MEASUREMENT_LOCK_ATTEMPTS_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(attempt, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    history = load_measurement_lock_attempts(output_dir)
    if history != [*existing, attempt]:
        raise RuntimeError("measurement lock attempt log did not append exactly once")
    return bind_measurement_lock_attempts(
        output_dir,
        config=config,
        provenance=provenance,
        attempts=history,
        validate_existing=False,
    )


def run_timed_lane_pool(
    *,
    output_dir: Path,
    lanes: Sequence[Lane],
    jobs: int,
    run_lane: Any,
    stop_event: threading.Event,
    attempt_sink: Any,
) -> dict[str, Any]:
    """Run every warm-up and measured lane inside the suite-wide host lock."""

    measurement_lock: Mapping[str, Any] | None = None
    binding: dict[str, Any] | None = None
    try:
        with suite_contract.primary_measurement_lock(
            runner=SCRIPT_PATH.name,
            raw_root=output_dir,
            phase="warmup-and-measured",
        ) as measurement_lock:
            with ThreadPoolExecutor(
                max_workers=jobs, thread_name_prefix="std-bench-lane"
            ) as pool:
                futures = []
                try:
                    for lane in lanes:
                        futures.append(pool.submit(run_lane, lane))
                    for future in as_completed(futures):
                        future.result()
                except BaseException:
                    stop_event.set()
                    for pending in futures:
                        pending.cancel()
                    raise
    finally:
        if measurement_lock is not None:
            evidence = validate_measurement_lock_evidence(
                measurement_lock, output_dir=output_dir
            )
            binding = attempt_sink(evidence)
    if binding is None:
        raise RuntimeError("measurement lock completed without persisted evidence")
    return binding


def run_or_reuse_timed_lane_pool(
    *,
    has_pending: bool,
    existing_binding: Mapping[str, Any],
    output_dir: Path,
    lanes: Sequence[Lane],
    jobs: int,
    run_lane: Any,
    stop_event: threading.Event,
    attempt_sink: Any,
) -> dict[str, Any]:
    """Skip an empty lock scope when a complete campaign already has evidence."""

    history = validate_preexisting_measurement_lock_bindings(output_dir)
    validated_binding = measurement_lock_binding(output_dir, history)
    if dict(existing_binding) != validated_binding:
        raise RuntimeError(
            "selected measurement lock binding differs from the attempt log"
        )
    if not has_pending:
        if validated_binding.get("attempt_count") in (None, 0):
            raise RuntimeError(
                "complete campaign has no canonical measurement lock attempt"
            )
        return validated_binding
    return run_timed_lane_pool(
        output_dir=output_dir,
        lanes=lanes,
        jobs=jobs,
        run_lane=run_lane,
        stop_event=stop_event,
        attempt_sink=attempt_sink,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("evaluation/raw/std-bench-allocator-full-20260714"),
    )
    parser.add_argument("--jobs", type=int, required=True)
    parser.add_argument("--cpus", type=parse_cpu_list, required=True)
    parser.add_argument("--numa-node", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument(
        "--variants",
        type=parse_variant_ids,
        default=tuple(variant.allocator for variant in VARIANTS),
        help=(
            "comma-separated ordered allocator ids; publication set: "
            + ",".join(PUBLICATION_VARIANT_IDS)
        ),
    )
    parser.add_argument(
        "--tcmalloc-lib-dir", type=Path, default=base.default_tcmalloc_dir()
    )
    parser.add_argument("--scudo-runtime-library", type=Path)
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="build and validate every selected allocator without benchmark processes",
    )
    args = parser.parse_args(argv)
    if args.jobs <= 0:
        parser.error("--jobs must be positive")
    if len(args.cpus) != args.jobs:
        parser.error("--jobs must equal the number of entries in --cpus")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    return args


def run_campaign(args: argparse.Namespace) -> dict[str, Any]:
    source_root = args.source_root.expanduser().resolve(strict=True)
    output_dir = args.output_dir.expanduser()
    if not output_dir.is_absolute():
        output_dir = (source_root / output_dir).resolve()
    variant_ids = getattr(
        args, "variants", tuple(variant.allocator for variant in VARIANTS)
    )
    if isinstance(variant_ids, str):
        variant_ids = parse_variant_ids(variant_ids)
    variants = selected_variants(variant_ids)
    with exclusive_campaign_lock(output_dir):
        selection = bind_campaign_selection(output_dir, variants)
        validate_preexisting_measurement_lock_bindings(output_dir)
        validate_existing_campaign_request(
            output_dir,
            source_root=source_root,
            args=args,
            variants=variants,
            selection=selection,
        )
        base.ensure_host_tools()
        return _run_campaign_locked(
            args, source_root, output_dir, variants=variants, selection=selection
        )


def _run_campaign_locked(
    args: argparse.Namespace,
    source_root: Path,
    output_dir: Path,
    *,
    variants: Sequence[Any] = VARIANTS,
    selection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected = tuple(variants)
    if selection is None:
        selection = bind_campaign_selection(output_dir, selected)
    head = base.validate_clean_source(source_root)
    needs_tcmalloc = any(base.is_google_tcmalloc_variant(item) for item in selected)
    tcmalloc_lib_dir = (
        base.validate_tcmalloc_dir(args.tcmalloc_lib_dir) if needs_tcmalloc else None
    )
    tcmalloc_identity = (
        base.tcmalloc_runtime_identity(tcmalloc_lib_dir)
        if tcmalloc_lib_dir is not None
        else None
    )
    needs_scudo = any(item.allocator == "scudo" for item in selected)
    if needs_scudo:
        scudo_runtime, scudo_authenticity = base.authenticate_scudo(
            source_root, args.scudo_runtime_library
        )
        scudo_identity = scudo_authenticity.get("runtime_library_identity")
        if not isinstance(scudo_identity, dict):
            raise RuntimeError("Scudo authenticity evidence omitted runtime identity")
    else:
        scudo_runtime = None
        scudo_authenticity = None
        scudo_identity = None

    needs_mimalloc_runtime = any(
        base.mimalloc_thp_expected_state(item) is not None for item in selected
    )
    mimalloc_thp_runtime = (
        ensure_mimalloc_thp_runtime(output_dir) if needs_mimalloc_runtime else None
    )
    mimalloc_thp_runtime_path = (
        Path(str(mimalloc_thp_runtime["binary"]))
        if mimalloc_thp_runtime is not None
        else None
    )
    mimalloc_thp_runtime_sha256 = (
        str(mimalloc_thp_runtime["binary_sha256"])
        if mimalloc_thp_runtime is not None
        else None
    )

    builds: dict[str, dict[str, Any]] = {}
    builds_by_feature: dict[str, dict[str, Any]] = {}
    for variant in selected:
        if variant.feature in builds_by_feature:
            original = builds_by_feature[variant.feature]
            build = {
                **original,
                "allocator": variant.allocator,
                "feature": variant.feature,
                "label": variant.label,
                "base_build_allocator": original["allocator"],
                "binary_reused": True,
            }
            alias_dir = output_dir / "builds" / variant.allocator
            alias_dir.mkdir(parents=True, exist_ok=True)
            base.write_json(alias_dir / "base-build-alias.json", build)
        else:
            build = base.build_variant(
                source_root, output_dir, variant, tcmalloc_lib_dir
            )
            build["base_build_allocator"] = variant.allocator
            build["binary_reused"] = False
            builds_by_feature[variant.feature] = build
        builds[variant.allocator] = build

    inventories: dict[str, dict[str, Any]] = {}
    canonical: tuple[str, ...] | None = None
    canonical_sha: str | None = None
    for variant in selected:
        inventory, evidence = base.inventory_variant(
            source_root,
            output_dir,
            variant,
            Path(str(builds[variant.allocator]["binary"])),
            tcmalloc_lib_dir=tcmalloc_lib_dir,
            scudo_runtime=scudo_runtime,
            mimalloc_thp_runtime=mimalloc_thp_runtime_path,
        )
        validated = validate_canonical_inventory(inventory)
        if canonical is None:
            canonical = validated
            canonical_sha = evidence["list_sha256"]
        elif validated != canonical or evidence["list_sha256"] != canonical_sha:
            raise RuntimeError(
                f"{variant.allocator}: canonical benchmark surface differs byte-for-byte"
            )
        evidence["selected_count"] = len(validated)
        evidence["selected_missing"] = []
        inventories[variant.allocator] = {**builds[variant.allocator], **evidence}
    assert canonical is not None and canonical_sha is not None
    benchmarks = canonical
    (output_dir / "benchmark-inventory.txt").write_text(
        "\n".join(benchmarks) + "\n", encoding="utf-8"
    )
    lanes = assign_benchmark_lanes(benchmarks, args.cpus)

    runner_sha = base.sha256_file(SCRIPT_PATH)
    config = {
        "schema_version": 2,
        "source_root": str(source_root),
        "repo_head": head,
        "runner_sha256": runner_sha,
        "base_runner_sha256": base.sha256_file(BASE_SCRIPT),
        "benchmark_inventory": list(benchmarks),
        "benchmark_inventory_sha256": base.sha256_file(
            output_dir / "benchmark-inventory.txt"
        ),
        "variant_ids": [variant.allocator for variant in selected],
        "variants": [variant.__dict__ for variant in selected],
        "selection_sha256": selection["selection_sha256"],
        "warmups": 1,
        "measured_rounds": MEASURED_ROUNDS,
        "timeout_seconds": args.timeout_seconds,
        "jobs": args.jobs,
        "cpus": list(args.cpus),
        "lane_rule": "canonical benchmark index modulo jobs",
        "numa_node": args.numa_node,
        "ratio_floor_ns_per_iter": RATIO_FLOOR_NS,
        "tcmalloc_runtime_sha256": (
            tcmalloc_identity["sha256"] if tcmalloc_identity is not None else None
        ),
        "scudo_runtime": str(scudo_runtime) if scudo_runtime is not None else None,
        "scudo_runtime_sha256": (
            scudo_identity["sha256"] if scudo_identity is not None else None
        ),
        "mimalloc_thp_runtime_sha256": mimalloc_thp_runtime_sha256,
    }
    config_path = output_dir / "campaign-config.json"
    write_or_validate_campaign_config(config_path, config)

    provenance = {
        "schema_version": 2,
        "generated_utc": base.utc_now(),
        "diagnostic_label": DIAGNOSTIC_LABEL,
        "repo_head": head,
        "repo_status": "",
        "source_unialloc_tree": base.command_output(
            ["git", "rev-parse", "HEAD:unialloc"], cwd=source_root
        ).strip(),
        "runner_path": str(SCRIPT_PATH),
        "runner_sha256": runner_sha,
        "base_runner_path": str(BASE_SCRIPT),
        "base_runner_sha256": base.sha256_file(BASE_SCRIPT),
        "rustc": base.command_output(["rustc", "-Vv"], cwd=source_root),
        "cargo": base.command_output(["cargo", "-Vv"], cwd=source_root),
        "uname": base.command_output(["uname", "-a"], cwd=source_root).strip(),
        "lscpu": base.command_output(["lscpu"], cwd=source_root),
        "numactl_hardware": base.command_output(
            ["numactl", "--hardware"], cwd=source_root
        ),
        "jobs": args.jobs,
        "cpus": list(args.cpus),
        "numa_node": args.numa_node,
        "variant_selection": selection,
        "lane_assignment": [
            {
                "lane": lane.index,
                "cpu": lane.cpu,
                "benchmark_count": len(lane.benchmarks),
                "benchmark_list_sha256": sha256_bytes(
                    ("\n".join(lane.benchmarks) + "\n").encode()
                ),
            }
            for lane in lanes
        ],
        "canonical_benchmark_count": len(benchmarks),
        "canonical_benchmark_list_sha256": canonical_sha,
        "inventories": inventories,
        "scudo_runtime_authenticity": scudo_authenticity,
        "tcmalloc_runtime": tcmalloc_identity,
        "mimalloc_thp_runtime": mimalloc_thp_runtime,
    }
    existing_lock_attempts = load_measurement_lock_attempts(output_dir)
    lock_binding = bind_measurement_lock_attempts(
        output_dir,
        config=config,
        provenance=provenance,
        attempts=existing_lock_attempts,
    )
    if args.build_only:
        result = {
            "schema_version": 1,
            "mode": "build_only",
            "variant_ids": [variant.allocator for variant in selected],
            "canonical_benchmark_count": len(benchmarks),
            "builds": {
                variant.allocator: {
                    "binary": builds[variant.allocator]["binary"],
                    "binary_sha256": builds[variant.allocator]["binary_sha256"],
                    "base_build_allocator": builds[variant.allocator][
                        "base_build_allocator"
                    ],
                    "binary_reused": builds[variant.allocator]["binary_reused"],
                }
                for variant in selected
            },
            "measurement_processes_started": 0,
            "measurement_lock": lock_binding,
        }
        base.write_json(output_dir / "build-validation.json", result)
        return result

    completed = load_completed_records(output_dir)
    benchmark_indexes = {benchmark: index for index, benchmark in enumerate(benchmarks)}
    lane_cpu = {benchmark: lane.cpu for lane in lanes for benchmark in lane.benchmarks}
    variants_by_allocator = {variant.allocator: variant for variant in selected}
    expected_slots = expected_process_slots(
        benchmarks, selected, measured_rounds=MEASURED_ROUNDS
    )
    unexpected = set(completed) - expected_slots
    if unexpected:
        raise RuntimeError(
            f"records.jsonl contains unexpected process keys: {sorted(unexpected)}"
        )
    for key, record in completed.items():
        _, _, allocator, benchmark = key
        variant = variants_by_allocator[allocator]
        validate_resumed_record_artifacts(record, output_dir=output_dir)
        validate_process_record_identity(
            record,
            variant=variant,
            binary_sha256=str(builds[allocator]["binary_sha256"]),
            cpu=lane_cpu[benchmark],
            numa_node=args.numa_node,
            timeout_seconds=args.timeout_seconds,
            scudo_runtime=scudo_runtime,
            mimalloc_thp_runtime_sha256=mimalloc_thp_runtime_sha256,
        )
    has_pending = False
    for benchmark in benchmarks:
        for variant in selected:
            state = classify_cell_history(
                _cell_records(list(completed.values()), variant.allocator, benchmark),
                measured_rounds=MEASURED_ROUNDS,
            )
            has_pending = has_pending or state.status == "pending"

    append_lock = threading.Lock()
    stop_event = threading.Event()
    maximum_processes = len(expected_slots)

    def persist(record: dict[str, Any]) -> None:
        key = record_key(record)
        with append_lock:
            if key in completed:
                raise RuntimeError(f"duplicate process key during execution: {key}")
            append_record(output_dir, record)
            completed[key] = record
            if stop_event.is_set():
                return
            base.write_json(
                output_dir / "campaign-state.json",
                {
                    "status": "running",
                    "completed_terminal_processes": len(completed),
                    "maximum_processes": maximum_processes,
                    "timeout_censored_processes": sum(
                        record_kind(item) == "timeout" for item in completed.values()
                    ),
                    "last_key": key,
                    "updated_utc": base.utc_now(),
                },
            )

    def execute_one(
        lane: Lane,
        benchmark: str,
        phase: str,
        round_index: int,
        variant: Any,
    ) -> None:
        if stop_event.is_set():
            return
        key = (phase, round_index, variant.allocator, benchmark)
        with append_lock:
            if key in completed:
                return
        record = base.run_one(
            source_root=source_root,
            output_dir=output_dir,
            variant=variant,
            binary=Path(str(builds[variant.allocator]["binary"])),
            benchmark=benchmark,
            phase=phase,
            round_index=round_index,
            cpu=lane.cpu,
            numa_node=args.numa_node,
            timeout_seconds=args.timeout_seconds,
            tcmalloc_lib_dir=tcmalloc_lib_dir,
            scudo_runtime=scudo_runtime,
            mimalloc_thp_runtime=mimalloc_thp_runtime_path,
        )
        annotate_process_record(
            record, allocator=variant.allocator, output_dir=output_dir
        )
        if record["status"] == "invalid":
            stop_event.set()
            base.write_json(
                output_dir / "campaign-state.json",
                {
                    "status": "failed_closed",
                    "failed_key": key,
                    "record": record,
                    "updated_utc": base.utc_now(),
                },
            )
            raise RuntimeError(f"full campaign failed closed at {key}")
        validate_process_record_identity(
            record,
            variant=variant,
            binary_sha256=str(builds[variant.allocator]["binary_sha256"]),
            cpu=lane.cpu,
            numa_node=args.numa_node,
            timeout_seconds=args.timeout_seconds,
            scudo_runtime=scudo_runtime,
            mimalloc_thp_runtime_sha256=mimalloc_thp_runtime_sha256,
        )
        persist(record)

    def current_state(allocator: str, benchmark: str) -> CellState:
        with append_lock:
            history = _cell_records(list(completed.values()), allocator, benchmark)
        return classify_cell_history(history, measured_rounds=MEASURED_ROUNDS)

    def run_lane(lane: Lane) -> None:
        for benchmark in lane.benchmarks:
            if stop_event.is_set():
                return
            benchmark_index = benchmark_indexes[benchmark]
            for variant in variant_order(benchmark_index, 0, selected):
                state = current_state(variant.allocator, benchmark)
                if state.status == "pending" and not state.valid_measured_rounds:
                    execute_one(lane, benchmark, "warmup", 0, variant)
            for round_index in range(1, MEASURED_ROUNDS + 1):
                for variant in variant_order(benchmark_index, round_index, selected):
                    state = current_state(variant.allocator, benchmark)
                    if state.status in ("complete", "censored"):
                        continue
                    expected_round = len(state.valid_measured_rounds) + 1
                    if expected_round == round_index:
                        execute_one(lane, benchmark, "measured", round_index, variant)

    measurement_lock = run_or_reuse_timed_lane_pool(
        has_pending=has_pending,
        existing_binding=lock_binding,
        output_dir=output_dir,
        lanes=lanes,
        jobs=args.jobs,
        run_lane=run_lane,
        stop_event=stop_event,
        attempt_sink=lambda evidence: persist_measurement_lock_attempt(
            output_dir,
            config=config,
            provenance=provenance,
            evidence=evidence,
        ),
    )

    summary = summarize(
        list(completed.values()),
        benchmarks=benchmarks,
        measured_rounds=MEASURED_ROUNDS,
        output_dir=output_dir,
        require_terminal=True,
        timeout_seconds=args.timeout_seconds,
        jobs=args.jobs,
        cpus=args.cpus,
        variants=selected,
        measurement_lock=measurement_lock,
    )
    base.write_json(
        output_dir / "campaign-state.json",
        {
            "status": (
                "complete_with_timeout_censoring"
                if summary["censored_cells"]
                else "complete"
            ),
            "completed_terminal_processes": len(completed),
            "maximum_processes": maximum_processes,
            "all_warmups_attempted": summary["record_counts"]["all_warmups_attempted"],
            "censored_cells": len(summary["censored_cells"]),
            "updated_utc": base.utc_now(),
            "summary_sha256": base.sha256_file(output_dir / "summary.json"),
            "records_sha256": base.sha256_file(output_dir / "records.jsonl"),
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
    except Exception as error:
        print(f"fatal: {error}", file=sys.stderr)
        raise
