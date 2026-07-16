#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib==3.11.0"]
# ///
"""Publish the current allocator evaluation as four provenance-bound figures.

The exporter keeps microbenchmarks and real-world programs as separate
populations.  It also keeps allocator baselines and UniAlloc feature variants
as separate comparisons.  Every input is explicit and every measurement
artifact is validated before an output directory is replaced.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("SOURCE_DATE_EPOCH", "1784246400")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evaluation.scripts import immutable_evidence  # noqa: E402
from evaluation.scripts import (
    import_macro_baseline_builds as build_importer,
)  # noqa: E402
from evaluation.scripts import (  # noqa: E402
    run_primary_macro_allocator_baselines as macro_runner,
)
from evaluation.scripts import (
    type_isolation_suite_contract as suite_contract,
)  # noqa: E402

matplotlib.rcParams["svg.hashsalt"] = "unialloc-current-evaluation-v1"


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CURRENT_SUITE_PATH = suite_contract.CURRENT_SUITE_PATH.resolve()
CURRENT_SUITE_SHA256 = (
    "688b65afa2bf61f20c44192faf96afd6f4205e386ca2aea502dc101b7cd4484d"
)
REFERENCE = "unialloc"
FEATURE_VARIANTS = ("unialloc", "typed_plain", "typeiso_perf")
ALLOCATOR_BASELINE_VARIANTS = (
    "unialloc",
    "jemalloc",
    "mimalloc",
    "mimalloc_no_thp",
    "google_tcmalloc",
)
CANONICAL_STD_BENCH_COUNT = 468
ROBUST_FLOOR_NS = 100.0
FIXED_WORK = "fixed_work"
PROTOCOL_REVISION = "full-std-bench-feature-process-v6"
MICRO_FEATURE_RAW_SCHEMA_VERSION = 4
OUTPUT_SCHEMA_VERSION = 2
PNG_DPI = 300
FIGURE_SIZE = (16.0, 8.7)
MAX_DISPLAY_LOG2_RATIO = 3.0
FIXED_DATE = "2026-07-16"
FIXED_DATETIME = dt.datetime(2026, 7, 16, tzinfo=dt.timezone.utc)
MACRO_RUNNER_PATH = (
    Path(__file__).with_name("run_primary_macro_allocator_baselines.py").resolve()
)
MACRO_IMPORTER_PATH = (
    Path(__file__).with_name("import_macro_baseline_builds.py").resolve()
)
SWC_JEMALLOC_DIRECT_LOAD_ROUTE = macro_runner.SWC_JEMALLOC_DIRECT_LOAD_ROUTE
SWC_JEMALLOC_VERSION = macro_runner.SWC_JEMALLOC_VERSION
SWC_JEMALLOC_SYS_VERSION = macro_runner.SWC_JEMALLOC_SYS_VERSION
SWC_UPSTREAM_JEMALLOC_VERSION = "0.5.4"

LABELS = {
    "unialloc": "UniAlloc",
    "typed_plain": "Typed control",
    "typeiso_perf": "Type Isolation",
    "ptmalloc": "ptmalloc",
    "jemalloc": "jemalloc",
    "mimalloc": "mimalloc",
    "mimalloc_no_thp": "mimalloc (THP off)",
    "tcmalloc": "TCMalloc",
    "google_tcmalloc": "Google TCMalloc",
    "snmalloc": "snmalloc",
    "scudo": "Scudo",
    "compiler_route": "Compiler route",
    "policy_increment": "Isolation policy",
    "end_to_end": "End to end",
}

COLORS = {
    "typed_plain": "#56B4E9",
    "typeiso_perf": "#0072B2",
    "ptmalloc": "#8C8C8C",
    "jemalloc": "#009E73",
    "mimalloc": "#E69F00",
    "mimalloc_no_thp": "#D55E00",
    "tcmalloc": "#CC79A7",
    "google_tcmalloc": "#CC79A7",
    "snmalloc": "#F0E442",
    "scudo": "#6A3D9A",
    "compiler_route": "#56B4E9",
    "policy_increment": "#D55E00",
    "end_to_end": "#0072B2",
}

CSV_FIELDS = (
    "comparison_group",
    "comparison_family",
    "population",
    "metric",
    "aggregation_level",
    "variant_id",
    "variant_label",
    "reference_variant_id",
    "target_id",
    "unit_id",
    "family",
    "round",
    "ratio",
    "eligible_for_headline",
    "diagnostic_only",
    "source_artifact",
)


class EvidenceError(RuntimeError):
    """Raised when an input cannot support a current-suite figure."""


@dataclass(frozen=True)
class SuiteHarness:
    target_id: str
    target_label: str
    harness_id: str
    direction: str
    rss_work_model: str
    source_commit: str
    performance_unit: str
    performance_source: str


@dataclass(frozen=True)
class ComparisonFamily:
    family_id: str
    reference: str
    subject: str


@dataclass(frozen=True)
class SuiteContract:
    path: Path
    digest: str
    suite_id: str
    implementation_revision: str
    implementation_sha256: str
    measured_rounds: int
    warmup_rounds: int
    comparison_families: tuple[ComparisonFamily, ...]
    harnesses: tuple[SuiteHarness, ...]

    @property
    def target_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(row.target_id for row in self.harnesses))

    @property
    def fixed_work_targets(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                row.target_id
                for row in self.harnesses
                if row.rss_work_model == FIXED_WORK
            )
        )


def fail(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_object(path: Path, context: str) -> dict[str, Any]:
    fail(path.is_file(), f"{context} is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"{context} is invalid: {path}: {error}") from error
    fail(isinstance(value, dict), f"{context} must be a JSON object: {path}")
    return value


def load_jsonl(path: Path, context: str) -> list[dict[str, Any]]:
    fail(path.is_file(), f"{context} is missing: {path}")
    rows: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            value = json.loads(line)
            fail(
                isinstance(value, dict),
                f"{context} row {line_number} must be an object",
            )
            rows.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"{context} is invalid: {path}: {error}") from error
    fail(rows, f"{context} is empty: {path}")
    return rows


def positive(value: Any, context: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise EvidenceError(f"{context} must be numeric") from error
    fail(math.isfinite(result) and result > 0.0, f"{context} must be positive")
    return result


def exact_int(value: Any, context: str) -> int:
    fail(type(value) is int, f"{context} must be an integer")
    return int(value)


def geometric_mean(values: Iterable[float], context: str) -> float:
    materialized = list(values)
    fail(materialized, f"{context} has no eligible observations")
    for value in materialized:
        positive(value, context)
    return math.exp(
        math.fsum(math.log(value) for value in materialized) / len(materialized)
    )


def log_median(values: Iterable[float], context: str) -> float:
    materialized = list(values)
    fail(materialized, f"{context} has no eligible observations")
    return math.exp(
        statistics.median(math.log(positive(v, context)) for v in materialized)
    )


def resolve_artifact(base: Path, raw_path: Any, digest: Any, context: str) -> Path:
    fail(isinstance(raw_path, str) and raw_path, f"{context} path is missing")
    fail(
        isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest),
        f"{context} digest is invalid",
    )
    supplied = Path(raw_path)
    candidates = (
        [supplied]
        if supplied.is_absolute()
        else [base / supplied, REPOSITORY_ROOT / supplied]
    )
    path = next(
        (candidate.resolve() for candidate in candidates if candidate.is_file()), None
    )
    fail(path is not None, f"{context} artifact is missing: {raw_path}")
    fail(sha256_file(path) == digest, f"{context} artifact digest mismatch: {path}")
    return path


def validate_artifact_identity(reference: Any, context: str) -> dict[str, Any]:
    fail(isinstance(reference, dict), f"{context} reference must be an object")
    path_value = reference.get("path")
    digest = reference.get("sha256")
    byte_count = reference.get("bytes", reference.get("size_bytes"))
    fail(isinstance(path_value, str) and path_value, f"{context} path is invalid")
    fail(
        isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest),
        f"{context} digest is invalid",
    )
    fail(
        isinstance(byte_count, int)
        and not isinstance(byte_count, bool)
        and byte_count >= 0,
        f"{context} byte count is invalid",
    )
    path = Path(path_value).resolve()
    fail(
        path.is_file()
        and path.stat().st_size == byte_count
        and sha256_file(path) == digest,
        f"{context} content differs from its attestation",
    )
    return {"path": str(path), "sha256": digest, "bytes": byte_count}


def validate_path_digest(path_value: Any, digest: Any, context: str) -> dict[str, Any]:
    fail(isinstance(path_value, str) and path_value, f"{context} path is invalid")
    fail(
        isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest),
        f"{context} digest is invalid",
    )
    path = Path(path_value).resolve()
    fail(
        path.is_file() and sha256_file(path) == digest,
        f"{context} content differs from its attestation",
    )
    return {"path": str(path), "sha256": digest, "bytes": path.stat().st_size}


def locked_cargo_package(path: Path, name: str, context: str) -> dict[str, Any]:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise EvidenceError(
            f"{context} Cargo.lock is invalid: {path}: {error}"
        ) from error
    packages = payload.get("package")
    fail(isinstance(packages, list), f"{context} Cargo.lock has no packages")
    matches = [
        package
        for package in packages
        if isinstance(package, dict) and package.get("name") == name
    ]
    fail(
        len(matches) == 1,
        f"{context} Cargo.lock must contain one {name} package",
    )
    return dict(matches[0])


def validate_swc_jemalloc_direct_load(
    build_record: Mapping[str, Any], context: str
) -> list[tuple[dict[str, Any], str]]:
    route = build_record.get("jemalloc_direct_load_route")
    runner_sha256 = sha256_file(MACRO_RUNNER_PATH)
    fail(
        isinstance(route, dict)
        and route.get("id") == SWC_JEMALLOC_DIRECT_LOAD_ROUTE
        and route.get("adapter_source_sha256") == runner_sha256
        and route.get("target_crate") == "typescript"
        and route.get("wrapper_package") == "tikv-jemallocator"
        and route.get("wrapper_version") == SWC_JEMALLOC_VERSION
        and route.get("sys_package") == "tikv-jemalloc-sys"
        and route.get("sys_version") == SWC_JEMALLOC_SYS_VERSION,
        f"{context} SWC jemalloc direct-load route identity mismatch",
    )
    worktree = Path(str(build_record.get("worktree", ""))).resolve()
    upstream_lock = worktree / "Cargo.lock"
    upstream_lock_identity = validate_path_digest(
        str(upstream_lock),
        build_record.get("derived_cargo_lock_sha256"),
        f"{context} upstream SWC lock",
    )
    upstream_package = locked_cargo_package(
        upstream_lock, "tikv-jemallocator", f"{context} upstream SWC"
    )
    fail(
        upstream_package.get("version") == SWC_UPSTREAM_JEMALLOC_VERSION
        and route.get("upstream_inactive_lock_package") == upstream_package
        and route.get("cargo_lock_before_sha256") == upstream_lock_identity["sha256"]
        and route.get("cargo_lock_after_sha256") == upstream_lock_identity["sha256"],
        f"{context} upstream SWC jemalloc lock changed",
    )
    record_identity = validate_path_digest(
        route.get("record"),
        route.get("record_sha256"),
        f"{context} SWC jemalloc direct-load record",
    )
    direct_record_path = Path(record_identity["path"])
    direct_record = load_object(
        direct_record_path, f"{context} SWC jemalloc direct-load record"
    )
    fail(
        direct_record.get("schema_version") == 1
        and direct_record.get("success") is True
        and direct_record.get("route") == SWC_JEMALLOC_DIRECT_LOAD_ROUTE
        and direct_record.get("wrapper_package") == "tikv-jemallocator"
        and direct_record.get("wrapper_version") == SWC_JEMALLOC_VERSION
        and direct_record.get("sys_package") == "tikv-jemalloc-sys"
        and direct_record.get("sys_version") == SWC_JEMALLOC_SYS_VERSION
        and direct_record.get("adapter_source_sha256") == runner_sha256
        and direct_record.get("toolchain") == build_record.get("toolchain"),
        f"{context} SWC jemalloc direct-load record identity mismatch",
    )
    artifacts = direct_record.get("artifacts")
    required_artifacts = {"manifest", "source", "lockfile", "rlib", "wrapper"}
    fail(
        isinstance(artifacts, dict) and required_artifacts.issubset(artifacts),
        f"{context} SWC jemalloc direct-load artifacts are incomplete",
    )
    retained: list[tuple[dict[str, Any], str]] = [
        (upstream_lock_identity, "swc_upstream_lock"),
        (record_identity, "swc_jemalloc_direct_load_record"),
    ]
    artifact_identities: dict[str, dict[str, Any]] = {}
    for artifact_name in sorted(required_artifacts):
        identity = validate_artifact_identity(
            artifacts[artifact_name],
            f"{context} SWC jemalloc direct-load {artifact_name}",
        )
        artifact_identities[artifact_name] = identity
        retained.append((identity, "swc_jemalloc_direct_load_artifact"))
    direct_lock = Path(artifact_identities["lockfile"]["path"])
    try:
        manifest_text = Path(artifact_identities["manifest"]["path"]).read_text(
            encoding="utf-8"
        )
        source_text = Path(artifact_identities["source"]["path"]).read_text(
            encoding="utf-8"
        )
        wrapper_text = Path(artifact_identities["wrapper"]["path"]).read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as error:
        raise EvidenceError(f"{context} SWC direct-load source is invalid") from error
    fail(
        manifest_text == macro_runner.SWC_JEMALLOC_DIRECT_LOAD_MANIFEST
        and source_text == "pub use jemallocator::Jemalloc;\n"
        and wrapper_text == macro_runner.SWC_JEMALLOC_DIRECT_LOAD_WRAPPER,
        f"{context} SWC jemalloc direct-load source differs from the runner",
    )
    locked_wrapper = locked_cargo_package(
        direct_lock, "tikv-jemallocator", f"{context} direct-load"
    )
    locked_sys = locked_cargo_package(
        direct_lock, "tikv-jemalloc-sys", f"{context} direct-load"
    )
    fail(
        locked_wrapper.get("version") == SWC_JEMALLOC_VERSION
        and locked_sys.get("version") == SWC_JEMALLOC_SYS_VERSION
        and direct_record.get("locked_wrapper_package") == locked_wrapper
        and direct_record.get("locked_sys_package") == locked_sys,
        f"{context} SWC jemalloc direct-load dependency pins mismatch",
    )
    fail(
        validate_artifact_identity(
            route.get("rlib"), f"{context} routed SWC jemalloc rlib"
        )
        == artifact_identities["rlib"]
        and validate_artifact_identity(
            route.get("wrapper"), f"{context} routed SWC jemalloc wrapper"
        )
        == artifact_identities["wrapper"],
        f"{context} SWC jemalloc routed artifacts mismatch",
    )
    commands = direct_record.get("commands")
    fail(
        isinstance(commands, list) and len(commands) == 2,
        f"{context} SWC jemalloc direct-load commands are incomplete",
    )
    for index, command in enumerate(commands):
        fail(
            isinstance(command, dict)
            and command.get("exit_code") == 0
            and command.get("timed_out") is False,
            f"{context} SWC jemalloc direct-load command failed: {index}",
        )
        for stream in ("stdout", "stderr"):
            retained.append(
                (
                    validate_path_digest(
                        command.get(stream),
                        command.get(f"{stream}_sha256"),
                        f"{context} SWC jemalloc direct-load {stream} {index}",
                    ),
                    "swc_jemalloc_direct_load_command",
                )
            )
    return retained


def validate_macro_measurement_record(
    path: Path,
    *,
    expected_identity: Mapping[str, Any],
    expected_metrics: Mapping[str, Any],
    context: str,
    require_baseline_contract: bool = False,
) -> Mapping[str, Any]:
    record = load_object(path, context)
    try:
        immutable_evidence.validate_measurement_record(
            record,
            expected_identity=expected_identity,
            expected_metrics=expected_metrics,
        )
    except immutable_evidence.ImmutableEvidenceError as error:
        raise EvidenceError(
            f"{context} immutable evidence is invalid: {error}"
        ) from error
    for field, expected in expected_identity.items():
        fail(
            record.get(field) == expected,
            f"{context} flattened identity mismatch: {field}",
        )
    for field, expected in expected_metrics.items():
        actual = record.get(field)
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            fail(
                isinstance(actual, (int, float))
                and not isinstance(actual, bool)
                and float(actual) == float(expected),
                f"{context} flattened metric mismatch: {field}",
            )
        else:
            fail(actual == expected, f"{context} flattened metric mismatch: {field}")
    correctness = record.get("correctness")
    process_succeeded = (
        record.get("success") is True
        or (isinstance(correctness, dict) and bool(correctness))
        or (
            record.get("exit_code") == 0
            and record.get("timed_out") is False
            and record.get("gnu_time_exit_status", record.get("time_exit_code", 0)) == 0
        )
    )
    fail(process_succeeded, f"{context} process success proof is missing")
    if require_baseline_contract:
        fail(
            record.get("success") is True
            and isinstance(correctness, dict)
            and correctness.get("passed") is True,
            f"{context} baseline correctness proof is missing",
        )
    artifacts = record["artifacts"]
    binary = artifacts.get("binary")
    suite_manifest = artifacts.get("suite_manifest") or record.get("suite_manifest")
    fail(
        isinstance(binary, dict)
        and binary.get("sha256") == record["identity"].get("binary_sha256"),
        f"{context} binary artifact identity mismatch",
    )
    fail(
        isinstance(suite_manifest, dict)
        and suite_manifest.get("sha256")
        == expected_identity.get("suite_manifest_sha256"),
        f"{context} suite artifact identity mismatch",
    )
    if "suite_manifest" not in artifacts:
        try:
            immutable_evidence.validate_artifact_ref(
                suite_manifest, context=f"{context} suite manifest"
            )
        except immutable_evidence.ImmutableEvidenceError as error:
            raise EvidenceError(str(error)) from error
    embedded_runtime = record.get("runtime_environment")
    environment_record = record.get("environment")
    if not isinstance(embedded_runtime, dict) and isinstance(environment_record, dict):
        embedded_runtime = environment_record.get("runtime_environment")
    if require_baseline_contract:
        fail(
            isinstance(artifacts.get("runtime_environment"), dict),
            f"{context} runtime environment artifact is missing",
        )
    else:
        fail(
            isinstance(embedded_runtime, dict)
            and embedded_runtime.get("policy")
            == suite_contract.RUNTIME_ENVIRONMENT_POLICY
            and embedded_runtime.get("rseq_policy")
            == suite_contract.PRODUCTION_RSEQ_POLICY
            and embedded_runtime.get("glibc_tunables_present") is False
            and isinstance(embedded_runtime.get("environment"), dict)
            and canonical_sha256(embedded_runtime["environment"])
            == embedded_runtime.get("environment_sha256"),
            f"{context} runtime environment evidence is invalid",
        )
    command_contract = record.get("command_contract")
    if require_baseline_contract or command_contract is not None:
        fail(
            isinstance(command_contract, dict)
            and canonical_sha256(command_contract)
            == record["identity"].get("command_contract_sha256"),
            f"{context} command contract identity mismatch",
        )
    return record


def read_suite(path: Path) -> SuiteContract:
    path = path.resolve()
    fail(
        path == CURRENT_SUITE_PATH,
        f"suite must be the official current suite: {CURRENT_SUITE_PATH}",
    )
    fail(
        sha256_file(path) == CURRENT_SUITE_SHA256,
        "official current suite digest mismatch",
    )
    try:
        official = suite_contract.load_suite_contract(path)
        suite_contract.verify_protocol_files(official)
    except suite_contract.SuiteContractError as error:
        raise EvidenceError(
            f"official current suite contract is invalid: {error}"
        ) from error
    value = load_object(path, "suite manifest")
    fail(value.get("schema_version") == 2, "suite schema version must be 2")
    suite_id = value.get("suite_id")
    fail(isinstance(suite_id, str) and suite_id, "suite id is missing")
    implementation = value.get("implementation")
    fail(isinstance(implementation, dict), "suite implementation is missing")
    revision = implementation.get("git_revision")
    implementation_digest = implementation.get("canonical_sha256")
    fail(isinstance(revision, str) and len(revision) == 40, "suite revision is invalid")
    fail(
        isinstance(implementation_digest, str) and len(implementation_digest) == 64,
        "suite implementation digest is invalid",
    )
    fail(
        official.manifest_sha256 == CURRENT_SUITE_SHA256
        and official.suite_id == suite_id
        and official.implementation_revision == revision
        and official.implementation_sha256 == implementation_digest,
        "official suite loader disagrees with the presentation contract",
    )
    measurement = value.get("measurement_contract")
    fail(isinstance(measurement, dict), "suite measurement contract is missing")
    measured = exact_int(measurement.get("measured_rounds"), "measured rounds")
    warmups = exact_int(measurement.get("warmup_rounds"), "warmup rounds")
    fail(measured > 0 and warmups == 1, "suite requires one warmup and measured rounds")
    raw_families = value.get("comparison_families")
    fail(
        isinstance(raw_families, list) and raw_families,
        "suite comparison families are missing",
    )
    comparison_families: list[ComparisonFamily] = []
    seen_families: set[str] = set()
    for raw_family in raw_families:
        fail(isinstance(raw_family, dict), "suite comparison family must be an object")
        family_id = raw_family.get("id")
        reference = raw_family.get("reference")
        subject = raw_family.get("subject")
        fail(
            isinstance(family_id, str)
            and family_id not in seen_families
            and reference in FEATURE_VARIANTS
            and subject in FEATURE_VARIANTS
            and reference != subject,
            "suite comparison family is invalid",
        )
        seen_families.add(family_id)
        comparison_families.append(
            ComparisonFamily(str(family_id), str(reference), str(subject))
        )
    fail(
        tuple(family.family_id for family in comparison_families)
        == ("compiler_route", "policy_increment", "end_to_end"),
        "suite comparison family order is invalid",
    )
    fail(
        tuple((family.reference, family.subject) for family in comparison_families)
        == (
            ("unialloc", "typed_plain"),
            ("typed_plain", "typeiso_perf"),
            ("unialloc", "typeiso_perf"),
        ),
        "suite comparison family endpoints are invalid",
    )
    raw_targets = value.get("targets")
    fail(isinstance(raw_targets, list) and raw_targets, "suite targets are missing")
    harnesses: list[SuiteHarness] = []
    seen_targets: set[str] = set()
    seen_workloads: set[tuple[str, str]] = set()
    for raw_target in raw_targets:
        fail(isinstance(raw_target, dict), "suite target must be an object")
        target_id = raw_target.get("id")
        label = raw_target.get("label")
        rss_model = raw_target.get("rss_work_model")
        source = raw_target.get("source")
        fail(
            isinstance(target_id, str) and target_id not in seen_targets,
            "suite target id is invalid",
        )
        fail(isinstance(label, str) and label, f"{target_id} label is missing")
        fail(
            isinstance(rss_model, str) and rss_model,
            f"{target_id} RSS model is missing",
        )
        fail(isinstance(source, dict), f"{target_id} source contract is missing")
        source_commit = source.get("commit")
        fail(
            isinstance(source_commit, str) and len(source_commit) == 40,
            f"{target_id} source commit is invalid",
        )
        seen_targets.add(target_id)
        raw_harnesses = raw_target.get("harnesses")
        fail(
            isinstance(raw_harnesses, list) and raw_harnesses,
            f"{target_id} harnesses are missing",
        )
        for raw_harness in raw_harnesses:
            fail(
                isinstance(raw_harness, dict), f"{target_id} harness must be an object"
            )
            harness_id = raw_harness.get("id")
            direction = raw_harness.get("metric_direction")
            performance_unit = raw_harness.get("performance_unit")
            performance_source = raw_harness.get("performance_source")
            key = (target_id, harness_id)
            fail(
                isinstance(harness_id, str) and key not in seen_workloads,
                f"{target_id} harness id is invalid",
            )
            fail(
                direction in {"lower_is_better", "higher_is_better"},
                f"{target_id}/{harness_id} direction is invalid",
            )
            fail(
                isinstance(performance_unit, str) and performance_unit,
                f"{target_id}/{harness_id} performance unit is invalid",
            )
            fail(
                isinstance(performance_source, str) and performance_source,
                f"{target_id}/{harness_id} performance source is invalid",
            )
            seen_workloads.add(key)
            harnesses.append(
                SuiteHarness(
                    target_id,
                    label,
                    harness_id,
                    direction,
                    rss_model,
                    source_commit,
                    performance_unit,
                    performance_source,
                )
            )
    return SuiteContract(
        path=path,
        digest=sha256_file(path),
        suite_id=suite_id,
        implementation_revision=revision,
        implementation_sha256=implementation_digest,
        measured_rounds=measured,
        warmup_rounds=warmups,
        comparison_families=tuple(comparison_families),
        harnesses=tuple(harnesses),
    )


def normalized_row(
    *,
    group: str,
    comparison_family: str,
    population: str,
    metric: str,
    level: str,
    variant: str,
    reference: str,
    target: str = "",
    unit: str = "",
    family: str = "",
    round_number: int | str = "",
    ratio: float,
    eligible: bool,
    diagnostic: bool,
    source: Path,
) -> dict[str, Any]:
    return {
        "comparison_group": group,
        "comparison_family": comparison_family,
        "population": population,
        "metric": metric,
        "aggregation_level": level,
        "variant_id": variant,
        "variant_label": LABELS.get(
            comparison_family if group == "feature" else variant,
            variant.replace("_", " ").title(),
        ),
        "reference_variant_id": reference,
        "target_id": target,
        "unit_id": unit,
        "family": family,
        "round": round_number,
        "ratio": float(ratio),
        "eligible_for_headline": bool(eligible),
        "diagnostic_only": bool(diagnostic),
        "source_artifact": str(source.resolve()),
    }


def micro_aggregate(
    *,
    group: str,
    comparison_family: str,
    metric: str,
    variant: str,
    reference: str,
    leaves: Sequence[tuple[str, str, float, bool]],
    source: Path,
    diagnostic: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    families: dict[str, list[float]] = defaultdict(list)
    for benchmark, family, ratio, eligible in leaves:
        rows.append(
            normalized_row(
                group=group,
                comparison_family=comparison_family,
                population="micro",
                metric=metric,
                level="benchmark_case",
                variant=variant,
                reference=reference,
                unit=benchmark,
                family=family,
                ratio=ratio,
                eligible=eligible and not diagnostic,
                diagnostic=diagnostic,
                source=source,
            )
        )
        if eligible:
            families[family].append(ratio)
    fail(families, f"{group}/{metric}/{variant} has no eligible micro families")
    family_values: list[float] = []
    for family in sorted(families):
        ratio = log_median(families[family], f"{group}/{metric}/{variant}/{family}")
        family_values.append(ratio)
        rows.append(
            normalized_row(
                group=group,
                comparison_family=comparison_family,
                population="micro",
                metric=metric,
                level="family_median",
                variant=variant,
                reference=reference,
                unit=family,
                family=family,
                ratio=ratio,
                eligible=not diagnostic,
                diagnostic=diagnostic,
                source=source,
            )
        )
    rows.append(
        normalized_row(
            group=group,
            comparison_family=comparison_family,
            population="micro",
            metric=metric,
            level="population_geomean",
            variant=variant,
            reference=reference,
            unit="all_families",
            ratio=geometric_mean(family_values, f"{group}/{metric}/{variant}"),
            eligible=not diagnostic,
            diagnostic=diagnostic,
            source=source,
        )
    )
    return rows


def _discover_feature_cohort(root: Path) -> str:
    session_root = root / "sessions"
    fail(session_root.is_dir(), f"micro feature sessions are missing: {session_root}")
    cohort_ids = sorted(path.name for path in session_root.iterdir() if path.is_dir())
    fail(len(cohort_ids) == 1, "micro feature input must contain exactly one cohort")
    return cohort_ids[0]


def read_micro_feature(
    root: Path, suite: SuiteContract
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = root.resolve()
    fail(root.is_dir(), f"micro feature root is missing: {root}")
    protocol_path = root / "campaign-protocol.json"
    protocol = load_object(protocol_path, "micro feature protocol")
    fail(
        protocol.get("schema_version") == 1, "micro feature protocol schema is invalid"
    )
    compatibility = protocol.get("compatibility")
    provenance = protocol.get("provenance")
    fail(isinstance(compatibility, dict), "micro feature compatibility is missing")
    fail(
        isinstance(provenance, dict) and provenance,
        "micro feature provenance is missing",
    )
    protocol_digest = canonical_sha256(
        {
            "schema_version": protocol.get("schema_version"),
            "compatibility": compatibility,
        }
    )
    fail(
        protocol.get("protocol_sha256") == protocol_digest,
        "micro feature protocol digest mismatch",
    )
    fail(
        compatibility.get("protocol_revision") == PROTOCOL_REVISION,
        "micro feature protocol revision is not current",
    )
    timeout_seconds = exact_int(
        compatibility.get("timeout_seconds"), "micro feature process timeout"
    )
    fail(
        compatibility.get("raw_record_schema_version")
        == MICRO_FEATURE_RAW_SCHEMA_VERSION
        and timeout_seconds > 0
        and compatibility.get("timeout_censoring_contract")
        == (
            "a process timeout is a terminal right-censored cell; an incomplete "
            "GNU time footer is retained and attested without metric imputation"
        )
        and compatibility.get("common_complete_selection_contract")
        == (
            "aggregate only canonical leaves with one valid warmup and every "
            "measured round for every selected variant"
        )
        and compatibility.get("peer_timeout_contract")
        == (
            "after any variant timeout, remaining processes for that canonical "
            "leaf are not launched and are terminally accounted as peer blocked"
        ),
        "micro feature timeout and common-completion contract is invalid",
    )
    fail(
        tuple(compatibility.get("variant_ids", ())) == FEATURE_VARIANTS,
        "micro feature variants are invalid",
    )
    fail(
        compatibility.get("source_head") == suite.implementation_revision,
        "micro feature source revision differs from the suite",
    )
    fail(
        isinstance(compatibility.get("source_git_objects"), dict)
        and compatibility["source_git_objects"],
        "micro feature source object provenance is missing",
    )
    fail(
        exact_int(compatibility.get("measured_rounds"), "micro feature measured rounds")
        == suite.measured_rounds,
        "micro feature round count differs from the suite",
    )
    inventory_count = exact_int(
        compatibility.get("canonical_inventory_count"), "micro feature inventory count"
    )
    fail(
        inventory_count == CANONICAL_STD_BENCH_COUNT,
        "micro feature inventory differs from the canonical 468-case contract",
    )
    cohort = _discover_feature_cohort(root)
    session_path = root / "sessions" / cohort / "measurement-session.json"
    selection_path = root / "views" / cohort / "selection.json"
    index_path = root / "derived" / cohort / "absolute-process-index.jsonl"
    terminal_path = root / "derived" / cohort / "terminal-accounting.json"
    session = load_object(session_path, "micro feature measurement session")
    selection = load_object(selection_path, "micro feature selection")
    fail(session.get("schema_version") == 2, "micro feature session schema is invalid")
    fail(session.get("cohort_id") == cohort, "micro feature session cohort mismatch")
    fail(
        tuple(session.get("variant_ids", ())) == FEATURE_VARIANTS,
        "micro feature session variants are invalid",
    )
    fail(
        session.get("protocol_sha256") == protocol_digest,
        "micro feature session protocol binding mismatch",
    )
    session_id = session.get("measurement_session_id")
    fail(
        isinstance(session_id, str) and session_id,
        "micro feature session id is missing",
    )
    fail(
        selection.get("schema_version") == 1 and selection.get("cohort_id") == cohort,
        "micro feature selection is invalid",
    )
    fail(
        selection.get("raw_data_mutated") is False,
        "micro feature selection reports mutated raw data",
    )
    fail(
        tuple(selection.get("variant_ids", ())) == FEATURE_VARIANTS,
        "micro feature selection variants are invalid",
    )
    raw_rows = load_jsonl(index_path, "micro feature process index")
    indexed: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    indexed_benchmarks: set[str] = set()
    for row in raw_rows:
        benchmark = row.get("benchmark")
        variant = row.get("variant_id")
        phase = row.get("phase")
        round_number = exact_int(row.get("round"), "micro feature row round")
        fail(
            isinstance(benchmark, str) and benchmark,
            "micro feature benchmark is invalid",
        )
        fail(
            variant in FEATURE_VARIANTS, f"micro feature variant is invalid: {variant}"
        )
        fail(phase in {"warmup", "measured"}, "micro feature phase is invalid")
        fail(
            (phase == "warmup" and round_number == 0)
            or (phase == "measured" and 1 <= round_number <= suite.measured_rounds),
            "micro feature process slot is invalid",
        )
        status = row.get("status")
        fail(
            status in {"valid", "timeout_censored"},
            "micro feature process status is invalid",
        )
        fail(
            row.get("cohort_id") == cohort
            and row.get("measurement_session_id") == session_id,
            "micro feature row is outside the selected session",
        )
        fail(
            row.get("measurement_anchor") is (variant == REFERENCE),
            "micro feature anchor ordering evidence is invalid",
        )
        fail(
            type(row.get("performance_claim_eligible")) is bool,
            "micro feature performance eligibility is missing",
        )
        fail(
            type(row.get("peak_rss_claim_eligible")) is bool,
            "micro feature RSS eligibility is missing",
        )
        fail(
            row.get("performance_claim_eligible") is True
            and row.get("peak_rss_claim_eligible") is False
            and row.get("fixed_work_contract") is False
            and row.get("rss_work_model") == "workload_native_adaptive_iterations",
            "micro feature claim eligibility differs from the adaptive process contract",
        )
        valid = status == "valid"
        if valid:
            fail(
                row.get("timed_out") is False
                and row.get("terminal_reason") is None
                and row.get("time_parse_status") == "complete",
                "micro feature valid process terminal identity is invalid",
            )
            positive(row.get("ns_per_iter"), "micro feature ns_per_iter")
            positive(row.get("peak_rss_kib"), "micro feature peak_rss_kib")
        else:
            fail(
                row.get("timed_out") is True
                and row.get("terminal_reason") == "process_timeout"
                and row.get("time_parse_status")
                in {"complete", "incomplete_after_timeout"},
                "micro feature timeout process terminal identity is invalid",
            )
            if row.get("time_parse_status") == "incomplete_after_timeout":
                fail(
                    row.get("ns_per_iter") is None and row.get("peak_rss_kib") is None,
                    "micro feature incomplete timeout exposes imputed metrics",
                )
            else:
                if row.get("ns_per_iter") is not None:
                    positive(
                        row.get("ns_per_iter"), "micro feature timeout ns_per_iter"
                    )
                if row.get("peak_rss_kib") is not None:
                    positive(
                        row.get("peak_rss_kib"),
                        "micro feature timeout peak_rss_kib",
                    )
        record_path = resolve_artifact(
            root,
            row.get("record_path"),
            row.get("record_sha256"),
            "micro feature process",
        )
        raw_record = load_object(record_path, "micro feature raw process")
        fail(
            raw_record.get("schema_version") == MICRO_FEATURE_RAW_SCHEMA_VERSION
            and raw_record.get("protocol_sha256") == protocol_digest
            and raw_record.get("cohort_id") == cohort
            and raw_record.get("measurement_session_id") == session_id
            and raw_record.get("measurement_anchor") is (variant == REFERENCE)
            and raw_record.get("variant_id") == variant
            and raw_record.get("allocator") == variant
            and raw_record.get("benchmark") == benchmark
            and raw_record.get("phase") == phase
            and raw_record.get("round") == round_number
            and raw_record.get("status") == status
            and raw_record.get("valid") is valid
            and raw_record.get("timed_out") is (not valid)
            and raw_record.get("terminal_reason") == row.get("terminal_reason")
            and raw_record.get("time_parse_status") == row.get("time_parse_status")
            and raw_record.get("timeout_seconds") == timeout_seconds,
            "micro feature raw process identity differs from its index row",
        )
        for claim_field in (
            "fixed_work_contract",
            "performance_claim_eligible",
            "peak_rss_claim_eligible",
            "rss_work_model",
        ):
            fail(
                raw_record.get(claim_field) == row.get(claim_field),
                f"micro feature raw process {claim_field} differs from its index row",
            )
        for metric_field in ("ns_per_iter", "peak_rss_kib"):
            fail(
                raw_record.get(metric_field) == row.get(metric_field),
                f"micro feature raw process {metric_field} differs from its index row",
            )
        key = (benchmark, str(variant), str(phase), round_number)
        fail(key not in indexed, f"duplicate micro feature process: {key}")
        indexed[key] = row
        indexed_benchmarks.add(benchmark)

    terminal = load_object(terminal_path, "micro feature terminal accounting")
    fail(
        set(terminal)
        == {
            "schema_version",
            "cohort_id",
            "measurement_session_id",
            "protocol_sha256",
            "selected_variant_ids",
            "canonical_inventory_count",
            "measured_rounds",
            "terminal_benchmark_count",
            "terminal_accounting_complete",
            "common_complete_rule",
            "common_complete_benchmark_count",
            "excluded_benchmark_count",
            "common_complete_benchmarks",
            "excluded_benchmarks",
            "cell_status_counts",
            "raw_process_record_count",
            "benchmarks",
        },
        "micro feature terminal accounting fields are invalid",
    )
    terminal_rows = terminal.get("benchmarks")
    fail(
        terminal.get("schema_version") == 1
        and terminal.get("cohort_id") == cohort
        and terminal.get("measurement_session_id") == session_id
        and terminal.get("protocol_sha256") == protocol_digest
        and tuple(terminal.get("selected_variant_ids", ())) == FEATURE_VARIANTS
        and terminal.get("canonical_inventory_count") == inventory_count
        and terminal.get("measured_rounds") == suite.measured_rounds
        and terminal.get("terminal_benchmark_count") == inventory_count
        and terminal.get("terminal_accounting_complete") is True
        and terminal.get("common_complete_rule")
        == (
            "every selected variant has one valid warmup and every measured round; "
            "a timeout excludes the leaf symmetrically from all comparisons"
        )
        and terminal.get("raw_process_record_count") == len(raw_rows)
        and isinstance(terminal_rows, list)
        and len(terminal_rows) == inventory_count,
        "micro feature terminal accounting identity is invalid",
    )
    terminal_benchmarks: list[str] = []
    derived_common: list[str] = []
    derived_excluded: list[str] = []
    derived_cell_counts = {
        "complete": 0,
        "timeout_censored": 0,
        "peer_timeout_blocked": 0,
    }
    for benchmark_row in terminal_rows:
        fail(
            isinstance(benchmark_row, dict)
            and set(benchmark_row)
            == {"benchmark", "family", "status", "timeout_variants", "cells"},
            "micro feature terminal benchmark row is invalid",
        )
        benchmark = benchmark_row.get("benchmark")
        fail(
            isinstance(benchmark, str)
            and benchmark
            and benchmark not in terminal_benchmarks,
            "micro feature terminal benchmark identity is invalid",
        )
        terminal_benchmarks.append(benchmark)
        fail(
            benchmark_row.get("family") == benchmark.split("::", 1)[0],
            "micro feature terminal benchmark family is invalid",
        )
        cells = benchmark_row.get("cells")
        fail(
            isinstance(cells, list)
            and len(cells) == len(FEATURE_VARIANTS)
            and all(isinstance(cell, dict) for cell in cells)
            and [cell.get("variant_id") for cell in cells] == list(FEATURE_VARIANTS),
            "micro feature terminal benchmark cells are invalid",
        )
        derived_timeout_variants: list[str] = []
        cell_states: dict[str, tuple[str, list[int], dict[str, Any] | None]] = {}
        for variant in FEATURE_VARIANTS:
            history = sorted(
                (
                    row
                    for (
                        row_benchmark,
                        row_variant,
                        _phase,
                        _round,
                    ), row in indexed.items()
                    if row_benchmark == benchmark and row_variant == variant
                ),
                key=lambda row: (str(row["phase"]), int(row["round"])),
            )
            by_slot = {(str(row["phase"]), int(row["round"])): row for row in history}
            warmup = by_slot.get(("warmup", 0))
            measured = {
                round_number: row
                for (phase, round_number), row in by_slot.items()
                if phase == "measured"
            }
            fail(
                warmup is not None or not measured,
                "micro feature measured process exists before its warmup",
            )
            if measured:
                observed_rounds = sorted(measured)
                fail(
                    observed_rounds == list(range(1, max(observed_rounds) + 1)),
                    "micro feature measured rounds are not a contiguous prefix",
                )
            timeout_row: dict[str, Any] | None = None
            valid_rounds: list[int] = []
            if warmup is None:
                state = "pending"
            elif warmup.get("status") == "timeout_censored":
                fail(
                    not measured,
                    "micro feature measured process exists after a warmup timeout",
                )
                state = "censored"
                timeout_row = warmup
            else:
                for round_number in sorted(measured):
                    measured_row = measured[round_number]
                    fail(
                        timeout_row is None,
                        "micro feature measured process exists after a timeout",
                    )
                    if measured_row.get("status") == "timeout_censored":
                        timeout_row = measured_row
                    else:
                        valid_rounds.append(round_number)
                if timeout_row is not None:
                    state = "censored"
                elif valid_rounds == list(range(1, suite.measured_rounds + 1)):
                    state = "complete"
                else:
                    state = "pending"
            cell_states[variant] = (state, valid_rounds, timeout_row)
            if state == "censored":
                derived_timeout_variants.append(variant)
        complete = all(
            cell_states[variant][0] == "complete" for variant in FEATURE_VARIANTS
        )
        fail(
            complete or derived_timeout_variants,
            "micro feature terminal accounting has an unexplained pending cell",
        )
        expected_benchmark_status = (
            "common_complete" if complete else "excluded_timeout_censored"
        )
        fail(
            benchmark_row.get("status") == expected_benchmark_status
            and benchmark_row.get("timeout_variants") == derived_timeout_variants,
            "micro feature terminal benchmark status is invalid",
        )
        (derived_common if complete else derived_excluded).append(benchmark)
        for variant, cell in zip(FEATURE_VARIANTS, cells, strict=True):
            fail(
                set(cell)
                == {
                    "variant_id",
                    "status",
                    "valid_measured_rounds",
                    "observed_process_slots",
                    "timeout_phase",
                    "timeout_round",
                    "timeout_record_path",
                    "timeout_record_sha256",
                    "blocked_by_timeout_variants",
                },
                "micro feature terminal cell fields are invalid",
            )
            state, valid_rounds, timeout_row = cell_states[variant]
            if state == "complete":
                expected_cell_status = "complete"
            elif state == "censored":
                expected_cell_status = "timeout_censored"
            else:
                expected_cell_status = "peer_timeout_blocked"
            history = sorted(
                (
                    row
                    for (
                        row_benchmark,
                        row_variant,
                        _phase,
                        _round,
                    ), row in indexed.items()
                    if row_benchmark == benchmark and row_variant == variant
                ),
                key=lambda row: (str(row["phase"]), int(row["round"])),
            )
            expected_slots = [
                {
                    "phase": row["phase"],
                    "round": row["round"],
                    "status": row["status"],
                    "record_path": row["record_path"],
                    "record_sha256": row["record_sha256"],
                }
                for row in history
            ]
            fail(
                cell.get("status") == expected_cell_status
                and cell.get("valid_measured_rounds") == valid_rounds
                and cell.get("observed_process_slots") == expected_slots
                and cell.get("timeout_phase")
                == (timeout_row.get("phase") if timeout_row else None)
                and cell.get("timeout_round")
                == (timeout_row.get("round") if timeout_row else None)
                and cell.get("timeout_record_path")
                == (timeout_row.get("record_path") if timeout_row else None)
                and cell.get("timeout_record_sha256")
                == (timeout_row.get("record_sha256") if timeout_row else None)
                and cell.get("blocked_by_timeout_variants")
                == (
                    derived_timeout_variants
                    if expected_cell_status == "peer_timeout_blocked"
                    else []
                ),
                "micro feature terminal cell accounting is invalid",
            )
            derived_cell_counts[expected_cell_status] += 1
    fail(
        set(indexed_benchmarks) == set(terminal_benchmarks),
        "micro feature process index differs from the terminal inventory",
    )
    fail(
        derived_common
        and terminal.get("common_complete_benchmarks") == derived_common
        and terminal.get("excluded_benchmarks") == derived_excluded
        and terminal.get("common_complete_benchmark_count") == len(derived_common)
        and terminal.get("excluded_benchmark_count") == len(derived_excluded)
        and terminal.get("cell_status_counts") == derived_cell_counts,
        "micro feature common-complete selection is invalid",
    )
    benchmarks = tuple(terminal_benchmarks)
    common_complete_benchmarks = set(derived_common)
    rows: list[dict[str, Any]] = []
    medians: dict[tuple[str, str, str], float] = {}
    for benchmark in derived_common:
        for variant in FEATURE_VARIANTS:
            measured_rows = [
                indexed[(benchmark, variant, "measured", round_number)]
                for round_number in range(1, suite.measured_rounds + 1)
            ]
            medians[(benchmark, variant, "performance")] = statistics.median(
                float(row["ns_per_iter"]) for row in measured_rows
            )
            medians[(benchmark, variant, "rss")] = statistics.median(
                float(row["peak_rss_kib"]) for row in measured_rows
            )
    robust_benchmarks = {
        benchmark
        for benchmark in derived_common
        if min(
            medians[(benchmark, variant, "performance")] for variant in FEATURE_VARIANTS
        )
        >= ROBUST_FLOOR_NS
    }
    fail(
        robust_benchmarks,
        "micro feature has no common all-variant robust benchmark cases",
    )
    for comparison in suite.comparison_families:
        variant = comparison.subject
        reference = comparison.reference
        performance_leaves: list[tuple[str, str, float, bool]] = []
        rss_leaves: list[tuple[str, str, float, bool]] = []
        for benchmark in sorted(common_complete_benchmarks):
            family = benchmark.split("::", 1)[0]
            reference_ns = medians[(benchmark, reference, "performance")]
            subject_ns = medians[(benchmark, variant, "performance")]
            performance_leaves.append(
                (
                    benchmark,
                    family,
                    subject_ns / reference_ns,
                    benchmark in robust_benchmarks,
                )
            )
            rss_leaves.append(
                (
                    benchmark,
                    family,
                    medians[(benchmark, variant, "rss")]
                    / medians[(benchmark, reference, "rss")],
                    benchmark in robust_benchmarks,
                )
            )
        rows.extend(
            micro_aggregate(
                group="feature",
                comparison_family=comparison.family_id,
                metric="performance",
                variant=variant,
                reference=reference,
                leaves=performance_leaves,
                source=index_path,
                diagnostic=False,
            )
        )
        rows.extend(
            micro_aggregate(
                group="feature",
                comparison_family=comparison.family_id,
                metric="rss",
                variant=variant,
                reference=reference,
                leaves=rss_leaves,
                source=index_path,
                diagnostic=True,
            )
        )
    identity = {
        "root": str(root),
        "protocol_sha256": sha256_file(protocol_path),
        "session_sha256": sha256_file(session_path),
        "selection_sha256": sha256_file(selection_path),
        "index_sha256": sha256_file(index_path),
        "terminal_accounting_sha256": sha256_file(terminal_path),
        "cohort_id": cohort,
        "measurement_session_id": session_id,
        "benchmark_count": len(benchmarks),
        "terminal_benchmark_count": len(benchmarks),
        "common_completed_benchmark_count": len(common_complete_benchmarks),
        "common_completed_excluded_benchmark_count": len(derived_excluded),
        "common_completed_excluded_benchmarks": derived_excluded,
        "timeout_censored_cell_count": derived_cell_counts["timeout_censored"],
        "peer_timeout_blocked_cell_count": derived_cell_counts["peer_timeout_blocked"],
        "timeout_seconds": timeout_seconds,
        "common_robust_benchmark_count": len(robust_benchmarks),
        "common_robust_excluded_benchmark_count": len(common_complete_benchmarks)
        - len(robust_benchmarks),
        "common_robust_rule": (
            "minimum three-round median ns_per_iter across every feature variant "
            f"is at least {ROBUST_FLOOR_NS:g} ns"
        ),
    }
    return rows, identity


def read_micro_baseline(
    root: Path, suite: SuiteContract
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = root.resolve()
    fail(root.is_dir(), f"micro baseline root is missing: {root}")
    paths = {
        name: root / name
        for name in (
            "campaign-config.json",
            "campaign-selection.json",
            "campaign-state.json",
            "provenance.json",
            "records.jsonl",
            "summary.json",
        )
    }
    config = load_object(
        paths["campaign-config.json"], "micro baseline campaign config"
    )
    selection = load_object(
        paths["campaign-selection.json"], "micro baseline campaign selection"
    )
    state = load_object(paths["campaign-state.json"], "micro baseline campaign state")
    provenance = load_object(paths["provenance.json"], "micro baseline provenance")
    summary = load_object(paths["summary.json"], "micro baseline summary")
    fail(
        config.get("schema_version") == 2
        and selection.get("schema_version") == 1
        and provenance.get("schema_version") == 2
        and summary.get("schema_version") == 2,
        "micro baseline schema version is invalid",
    )
    fail(
        state.get("status") in {"complete", "complete_with_timeout_censoring"},
        "micro baseline terminal state is invalid",
    )
    fail(
        state.get("all_warmups_attempted") is True,
        "micro baseline warmups are incomplete",
    )
    fail(
        state.get("records_sha256") == sha256_file(paths["records.jsonl"]),
        "micro baseline records digest mismatch",
    )
    fail(
        state.get("summary_sha256") == sha256_file(paths["summary.json"]),
        "micro baseline summary digest mismatch",
    )
    fail(
        config.get("repo_head") == suite.implementation_revision
        and provenance.get("repo_head") == suite.implementation_revision,
        "micro baseline source revision differs from the suite",
    )
    fail(
        exact_int(config.get("measured_rounds"), "micro baseline measured rounds")
        == suite.measured_rounds,
        "micro baseline round count differs from the suite",
    )
    fail(
        exact_int(config.get("warmups"), "micro baseline warmups") == 1,
        "micro baseline warmup count is invalid",
    )
    timeout_seconds = exact_int(
        config.get("timeout_seconds"), "micro baseline process timeout"
    )
    fail(timeout_seconds > 0, "micro baseline process timeout must be positive")
    raw_variants = config.get("variants")
    inventory = config.get("benchmark_inventory")
    fail(
        isinstance(raw_variants, list) and len(raw_variants) >= 2,
        "micro baseline variants are missing",
    )
    fail(
        isinstance(inventory, list)
        and inventory
        and len(set(inventory)) == len(inventory),
        "micro baseline inventory is invalid",
    )
    variants: list[str] = []
    for item in raw_variants:
        fail(isinstance(item, dict), "micro baseline variant is invalid")
        variant = item.get("allocator")
        fail(
            isinstance(variant, str) and variant not in variants,
            "micro baseline allocator id is invalid",
        )
        variants.append(variant)
    expected_selection_digest = canonical_sha256(
        {
            "variant_ids": variants,
            "variants": raw_variants,
        }
    )
    fail(
        selection.get("variant_ids") == variants
        and selection.get("variants") == raw_variants
        and selection.get("selection_sha256") == expected_selection_digest
        and config.get("variant_ids") == variants
        and config.get("selection_sha256") == expected_selection_digest
        and provenance.get("variant_selection") == selection,
        "micro baseline ordered variant selection binding mismatch",
    )
    fail(
        tuple(variants) == ALLOCATOR_BASELINE_VARIANTS,
        "micro baseline variants differ from the formal five-allocator contract",
    )
    fail(
        len(inventory) == CANONICAL_STD_BENCH_COUNT,
        "micro baseline inventory differs from the canonical 468-case contract",
    )
    fail(
        isinstance(provenance.get("inventories"), dict)
        and set(provenance["inventories"]) == set(variants),
        "micro baseline build provenance is incomplete",
    )
    fail(
        provenance.get("canonical_benchmark_count") == len(inventory),
        "micro baseline provenance inventory count mismatch",
    )
    fail(
        isinstance(config.get("benchmark_inventory_sha256"), str)
        and len(config["benchmark_inventory_sha256"]) == 64
        and isinstance(provenance.get("canonical_benchmark_list_sha256"), str)
        and len(provenance["canonical_benchmark_list_sha256"]) == 64,
        "micro baseline inventory provenance is missing",
    )
    for variant in variants:
        build = provenance["inventories"][variant]
        fail(
            isinstance(build, dict)
            and isinstance(build.get("binary_sha256"), str)
            and len(build["binary_sha256"]) == 64
            and build.get("selected_count") == len(inventory),
            f"micro baseline build provenance is incomplete: {variant}",
        )
    maximum_processes = len(inventory) * len(variants) * (suite.measured_rounds + 1)
    fail(
        state.get("maximum_processes") == maximum_processes,
        "micro baseline maximum process count mismatch",
    )
    terminal_processes = state.get("completed_terminal_processes")
    fail(
        type(terminal_processes) is int and 0 < terminal_processes <= maximum_processes,
        "micro baseline terminal process count is invalid",
    )
    raw_cells = summary.get("cells")
    fail(isinstance(raw_cells, list), "micro baseline summary cells are missing")
    cells: dict[tuple[str, str], Mapping[str, Any]] = {}
    for cell in raw_cells:
        fail(isinstance(cell, dict), "micro baseline summary cell is invalid")
        key = (cell.get("benchmark"), cell.get("allocator"))
        fail(
            key[0] in inventory and key[1] in variants and key not in cells,
            "micro baseline summary cell identity is invalid",
        )
        fail(
            cell.get("status") in {"complete", "censored"},
            "micro baseline cell has a non-terminal status",
        )
        cells[(str(key[0]), str(key[1]))] = cell
    fail(
        set(cells)
        == {(benchmark, variant) for benchmark in inventory for variant in variants},
        "micro baseline summary matrix is incomplete",
    )
    raw_rows = load_jsonl(paths["records.jsonl"], "micro baseline records")
    records: dict[tuple[str, str, str, int], Mapping[str, Any]] = {}
    for row in raw_rows:
        benchmark = row.get("benchmark")
        variant = row.get("allocator")
        phase = row.get("phase")
        round_number = exact_int(row.get("round"), "micro baseline record round")
        fail(
            benchmark in inventory and variant in variants,
            "micro baseline record identity is invalid",
        )
        fail(phase in {"warmup", "measured"}, "micro baseline record phase is invalid")
        key = (str(benchmark), str(variant), str(phase), round_number)
        fail(key not in records, f"duplicate micro baseline record: {key}")
        records[key] = row
    expected_records: set[tuple[str, str, str, int]] = set()
    derived_censored_cells: list[Mapping[str, Any]] = []
    for benchmark in inventory:
        for variant in variants:
            cell = cells[(benchmark, variant)]
            if cell.get("status") == "complete":
                expected_records.add((benchmark, variant, "warmup", 0))
                expected_records.update(
                    (benchmark, variant, "measured", round_number)
                    for round_number in range(1, suite.measured_rounds + 1)
                )
                fail(
                    cell.get("valid_measured_rounds")
                    in (None, list(range(1, suite.measured_rounds + 1))),
                    f"micro baseline complete cell round accounting mismatch: {benchmark}/{variant}",
                )
                continue
            censoring = cell.get("censoring")
            fail(
                isinstance(censoring, dict)
                and censoring.get("benchmark") == benchmark
                and censoring.get("allocator") == variant
                and censoring.get("timeout_seconds") == timeout_seconds
                and censoring.get("phase") in {"warmup", "measured"},
                f"micro baseline censoring identity mismatch: {benchmark}/{variant}",
            )
            phase = str(censoring["phase"])
            round_number = exact_int(
                censoring.get("round"), "micro baseline censored round"
            )
            valid_rounds = censoring.get("valid_measured_rounds_before_timeout")
            expected_valid_rounds = (
                list(range(1, round_number)) if phase == "measured" else []
            )
            fail(
                isinstance(valid_rounds, list)
                and valid_rounds == expected_valid_rounds,
                f"micro baseline censored round prefix mismatch: {benchmark}/{variant}",
            )
            if phase == "warmup":
                fail(
                    round_number == 0,
                    f"micro baseline warmup censor round is invalid: {benchmark}/{variant}",
                )
            else:
                fail(
                    1 <= round_number <= suite.measured_rounds,
                    f"micro baseline measured censor round is invalid: {benchmark}/{variant}",
                )
                expected_records.add((benchmark, variant, "warmup", 0))
                expected_records.update(
                    (benchmark, variant, "measured", completed_round)
                    for completed_round in range(1, round_number)
                )
            expected_records.add((benchmark, variant, phase, round_number))
            fail(
                cell.get("valid_measured_rounds") in (None, valid_rounds),
                f"micro baseline censored cell round accounting mismatch: {benchmark}/{variant}",
            )
            derived_censored_cells.append(censoring)
    fail(
        set(records) == expected_records and terminal_processes == len(records),
        "micro baseline terminal process accounting is incomplete or contains extra rows",
    )
    for key, row in records.items():
        if row.get("status") == "valid":
            fail(
                row.get("valid") is True
                and row.get("timed_out") is False
                and row.get("time_parse_error") in (None, "")
                and row.get("exit_code", 0) == 0
                and row.get("time_exit_status", 0) == 0,
                f"micro baseline valid process evidence is invalid: {key}",
            )
            positive(row.get("ns_per_iter"), "micro baseline ns_per_iter")
            positive(row.get("peak_rss_kib"), "micro baseline peak_rss_kib")
        else:
            fail(
                row.get("status") == "timeout_censored"
                and row.get("valid") is not True
                and row.get("timed_out") is True
                and row.get("timeout_seconds") == timeout_seconds,
                f"micro baseline timeout evidence is invalid: {key}",
            )
    complete_benchmarks = [
        benchmark
        for benchmark in inventory
        if all(
            cells[(benchmark, variant)].get("status") == "complete"
            for variant in variants
        )
    ]
    excluded_benchmarks = [
        benchmark
        for benchmark in inventory
        if benchmark not in set(complete_benchmarks)
    ]
    fail(
        complete_benchmarks,
        "micro baseline has no common all-allocator completed benchmark cases",
    )
    medians: dict[tuple[str, str, str], float] = {}
    for benchmark in complete_benchmarks:
        for variant in variants:
            measured = [
                records[(benchmark, variant, "measured", round_number)]
                for round_number in range(1, suite.measured_rounds + 1)
            ]
            medians[(benchmark, variant, "performance")] = statistics.median(
                positive(row.get("ns_per_iter"), "micro baseline ns_per_iter")
                for row in measured
            )
            medians[(benchmark, variant, "rss")] = statistics.median(
                positive(row.get("peak_rss_kib"), "micro baseline peak_rss_kib")
                for row in measured
            )
    robust_benchmarks = {
        benchmark
        for benchmark in complete_benchmarks
        if min(medians[(benchmark, variant, "performance")] for variant in variants)
        >= ROBUST_FLOOR_NS
    }
    fail(
        robust_benchmarks,
        "micro baseline has no common all-variant robust benchmark cases",
    )
    methodology = summary.get("methodology")
    coverage = summary.get("coverage")
    comparison_selection = summary.get("comparison_selection")
    robustness_selection = summary.get("robustness_selection")
    record_counts = summary.get("record_counts")
    stored_censored_cells = summary.get("censored_cells")
    complete_cell_count = sum(
        cell.get("status") == "complete" for cell in cells.values()
    )
    censored_cell_count = len(derived_censored_cells)
    fail(
        isinstance(methodology, dict)
        and methodology.get("allocators") == variants
        and methodology.get("timeout_seconds_per_process") == timeout_seconds
        and methodology.get("timeout_censoring")
        == (
            "a warmup timeout terminates the cell; a measured timeout retains "
            "prior raw observations and suppresses the cell median"
        )
        and config.get("ratio_floor_ns_per_iter") == ROBUST_FLOOR_NS
        and isinstance(coverage, dict)
        and coverage.get("canonical_inventory_count") == len(inventory)
        and coverage.get("allocator_cells") == len(inventory) * len(variants)
        and coverage.get("complete_cells") == complete_cell_count
        and coverage.get("censored_cells") == censored_cell_count
        and coverage.get("pending_cells", 0) == 0
        and coverage.get("all_allocator_complete_benchmarks")
        in (None, len(complete_benchmarks))
        and isinstance(record_counts, dict)
        and record_counts.get("all") == len(records)
        and record_counts.get("valid")
        == sum(row.get("status") == "valid" for row in records.values())
        and record_counts.get("timeout_censored") == censored_cell_count
        and record_counts.get("all_warmups_attempted") is True
        and stored_censored_cells == derived_censored_cells
        and state.get("censored_cells", censored_cell_count) == censored_cell_count
        and state.get("status")
        == ("complete_with_timeout_censoring" if censored_cell_count else "complete")
        and isinstance(comparison_selection, dict)
        and comparison_selection.get("complete_all_allocator_benchmarks")
        == complete_benchmarks
        and comparison_selection.get("comparable_benchmarks") == complete_benchmarks
        and comparison_selection.get("comparable_count", len(complete_benchmarks))
        == len(complete_benchmarks)
        and comparison_selection.get("excluded_count", len(excluded_benchmarks))
        == len(excluded_benchmarks)
        and isinstance(robustness_selection, dict)
        and robustness_selection.get("threshold_ns_per_iter") == ROBUST_FLOOR_NS
        and robustness_selection.get("selected_benchmarks")
        == [benchmark for benchmark in inventory if benchmark in robust_benchmarks],
        "micro baseline stored all-variant comparison selection mismatch",
    )
    rows: list[dict[str, Any]] = []
    for variant in variants[1:]:
        performance_leaves: list[tuple[str, str, float, bool]] = []
        rss_leaves: list[tuple[str, str, float, bool]] = []
        for benchmark in complete_benchmarks:
            family = benchmark.split("::", 1)[0]
            reference_ns = medians[(benchmark, REFERENCE, "performance")]
            subject_ns = medians[(benchmark, variant, "performance")]
            performance_leaves.append(
                (
                    benchmark,
                    family,
                    subject_ns / reference_ns,
                    benchmark in robust_benchmarks,
                )
            )
            rss_leaves.append(
                (
                    benchmark,
                    family,
                    medians[(benchmark, variant, "rss")]
                    / medians[(benchmark, REFERENCE, "rss")],
                    benchmark in robust_benchmarks,
                )
            )
        rows.extend(
            micro_aggregate(
                group="baseline",
                comparison_family="allocator_baseline",
                metric="performance",
                variant=variant,
                reference=REFERENCE,
                leaves=performance_leaves,
                source=paths["records.jsonl"],
                diagnostic=False,
            )
        )
        rows.extend(
            micro_aggregate(
                group="baseline",
                comparison_family="allocator_baseline",
                metric="rss",
                variant=variant,
                reference=REFERENCE,
                leaves=rss_leaves,
                source=paths["records.jsonl"],
                diagnostic=True,
            )
        )
    identity = {
        "root": str(root),
        **{
            name.replace(".", "_") + "_sha256": sha256_file(path)
            for name, path in paths.items()
        },
        "variant_ids": variants,
        "benchmark_count": len(inventory),
        "complete_comparable_benchmark_count": len(complete_benchmarks),
        "terminal_benchmark_count": len(inventory),
        "common_completed_benchmark_count": len(complete_benchmarks),
        "common_completed_excluded_benchmark_count": len(excluded_benchmarks),
        "common_completed_excluded_benchmarks": excluded_benchmarks,
        "common_robust_benchmark_count": len(robust_benchmarks),
        "common_robust_excluded_benchmark_count": len(complete_benchmarks)
        - len(robust_benchmarks),
        "censored_cell_count": censored_cell_count,
        "censored_benchmark_count": len(excluded_benchmarks),
        "timeout_seconds_per_process": timeout_seconds,
    }
    return rows, identity


def cost_ratio(subject: float, reference: float, direction: str) -> float:
    if direction == "lower_is_better":
        return subject / reference
    if direction == "higher_is_better":
        return reference / subject
    raise EvidenceError(f"invalid performance direction: {direction}")


def macro_aggregate(
    *,
    group: str,
    comparison_family: str,
    metric: str,
    variant: str,
    reference: str,
    observations: Mapping[tuple[str, str], Sequence[tuple[int, float]]],
    suite: SuiteContract,
    source: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    harness_map = {(row.target_id, row.harness_id): row for row in suite.harnesses}
    target_values: dict[str, list[float]] = defaultdict(list)
    target_headline: dict[str, bool] = {}
    for key in sorted(observations):
        contract = harness_map[key]
        eligible = metric == "performance" or contract.rss_work_model == FIXED_WORK
        diagnostic = metric == "rss" and not eligible
        ratios = []
        for round_number, ratio in observations[key]:
            ratios.append(ratio)
            rows.append(
                normalized_row(
                    group=group,
                    comparison_family=comparison_family,
                    population="macro",
                    metric=metric,
                    level="paired_round",
                    variant=variant,
                    reference=reference,
                    target=contract.target_id,
                    unit=contract.harness_id,
                    round_number=round_number,
                    ratio=ratio,
                    eligible=eligible,
                    diagnostic=diagnostic,
                    source=source,
                )
            )
        median = statistics.median(ratios)
        rows.append(
            normalized_row(
                group=group,
                comparison_family=comparison_family,
                population="macro",
                metric=metric,
                level="workload_median",
                variant=variant,
                reference=reference,
                target=contract.target_id,
                unit=contract.harness_id,
                ratio=median,
                eligible=eligible,
                diagnostic=diagnostic,
                source=source,
            )
        )
        target_values[contract.target_id].append(median)
        target_headline[contract.target_id] = eligible
    fail(
        set(target_values) == set(suite.target_ids),
        f"{group}/{metric}/{variant} target coverage is incomplete",
    )
    eligible_targets: list[float] = []
    for target_id in suite.target_ids:
        ratio = geometric_mean(
            target_values[target_id], f"{group}/{metric}/{variant}/{target_id}"
        )
        eligible = target_headline[target_id]
        rows.append(
            normalized_row(
                group=group,
                comparison_family=comparison_family,
                population="macro",
                metric=metric,
                level="target_geomean",
                variant=variant,
                reference=reference,
                target=target_id,
                unit=target_id,
                ratio=ratio,
                eligible=eligible,
                diagnostic=not eligible,
                source=source,
            )
        )
        if eligible:
            eligible_targets.append(ratio)
    rows.append(
        normalized_row(
            group=group,
            comparison_family=comparison_family,
            population="macro",
            metric=metric,
            level="population_geomean",
            variant=variant,
            reference=reference,
            unit="eligible_targets",
            ratio=geometric_mean(eligible_targets, f"{group}/{metric}/{variant}"),
            eligible=True,
            diagnostic=False,
            source=source,
        )
    )
    return rows


def read_macro_feature(
    path: Path, suite: SuiteContract
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = path.resolve()
    result = load_object(path, "macro feature result")
    fail(result.get("schema_version") == 1, "macro feature schema version is invalid")
    fail(
        result.get("suite_id") == suite.suite_id
        and result.get("suite_manifest_sha256") == suite.digest,
        "macro feature suite binding mismatch",
    )
    fail(
        result.get("implementation_revision") == suite.implementation_revision
        and result.get("implementation_sha256") == suite.implementation_sha256,
        "macro feature implementation binding mismatch",
    )
    fail(
        result.get("status") in {"complete", "complete_with_attribution_limits"},
        "macro feature result is incomplete",
    )
    declared_families = {
        family.family_id: family for family in suite.comparison_families
    }
    stored_population_families = result.get("comparison_families")
    fail(
        isinstance(stored_population_families, dict)
        and set(stored_population_families) == set(declared_families),
        "macro feature comparison families differ from the suite",
    )
    for family_id, comparison in declared_families.items():
        stored = stored_population_families[family_id]
        fail(
            isinstance(stored, dict)
            and stored.get("reference") == comparison.reference
            and stored.get("subject") == comparison.subject,
            f"macro feature comparison family endpoints mismatch: {family_id}",
        )
    raw_targets = result.get("targets")
    fail(isinstance(raw_targets, list), "macro feature targets are missing")
    target_map = {row.target_id: row for row in suite.harnesses}
    result_targets: dict[str, Mapping[str, Any]] = {}
    for raw_target in raw_targets:
        fail(isinstance(raw_target, dict), "macro feature target is invalid")
        target_id = raw_target.get("id")
        fail(
            target_id in suite.target_ids and target_id not in result_targets,
            "macro feature target identity is invalid",
        )
        result_targets[str(target_id)] = raw_target
    fail(
        tuple(result_targets) == suite.target_ids,
        "macro feature target order or coverage differs from the suite",
    )
    observations: dict[
        str, dict[str, dict[tuple[str, str], list[tuple[int, float]]]]
    ] = {
        family_id: {"performance": defaultdict(list), "rss": defaultdict(list)}
        for family_id in declared_families
    }
    required_gates = {
        "correctness",
        "build_success",
        "allocator_activation",
        "actual_mir_provenance",
        "stats_disabled",
        "source_audit_retained",
    }
    for target_id, raw_target in result_targets.items():
        contract_target = target_map[target_id]
        fail(
            raw_target.get("source_commit") == contract_target.source_commit,
            f"macro feature {target_id} source commit mismatch",
        )
        fail(
            raw_target.get("implementation_revision") == suite.implementation_revision
            and raw_target.get("implementation_sha256") == suite.implementation_sha256,
            f"macro feature {target_id} implementation mismatch",
        )
        stored_target_families = raw_target.get("comparison_families")
        fail(
            isinstance(stored_target_families, dict)
            and set(stored_target_families) == set(declared_families),
            f"macro feature {target_id} comparison families are incomplete",
        )
        for family_id, comparison in declared_families.items():
            stored = stored_target_families[family_id]
            fail(
                isinstance(stored, dict)
                and stored.get("reference") == comparison.reference
                and stored.get("subject") == comparison.subject,
                f"macro feature {target_id}/{family_id} endpoints mismatch",
            )
        provenance = raw_target.get("compact_provenance")
        fail(
            isinstance(provenance, dict),
            f"macro feature {target_id} provenance is missing",
        )
        source_audit = provenance.get("source_audit")
        builds = provenance.get("builds")
        fail(
            isinstance(source_audit, dict),
            f"macro feature {target_id} source audit is missing",
        )
        resolve_artifact(
            REPOSITORY_ROOT,
            source_audit.get("raw_record_path"),
            source_audit.get("sha256"),
            f"macro feature {target_id} source audit",
        )
        fail(
            isinstance(builds, dict) and set(builds) == set(FEATURE_VARIANTS),
            f"macro feature {target_id} build provenance is incomplete",
        )
        for variant, build in builds.items():
            fail(
                isinstance(build, dict) and build.get("success") is True,
                f"macro feature {target_id}/{variant} build failed",
            )
            fail(
                build.get("source_commit") == contract_target.source_commit
                and build.get("implementation_sha256") == suite.implementation_sha256,
                f"macro feature {target_id}/{variant} build provenance mismatch",
            )
            fail(
                build.get("allocator_activation") is True
                and type(build.get("actual_mir_provenance")) is bool
                and (variant == REFERENCE or build["actual_mir_provenance"] is True)
                and build.get("stats_disabled") is True,
                f"macro feature {target_id}/{variant} build gates failed",
            )
            build_record_path = resolve_artifact(
                REPOSITORY_ROOT,
                build.get("raw_record_path"),
                build.get("record_sha256"),
                f"macro feature {target_id}/{variant} build",
            )
            build_record = load_object(
                build_record_path, f"macro feature {target_id}/{variant} build"
            )
            fail(
                build_record.get("source_commit") == contract_target.source_commit
                and (
                    build_record.get("implementation_sha256")
                    or build_record.get("unialloc_implementation_sha256")
                    or build_record.get("frozen_implementation_sha256")
                )
                == suite.implementation_sha256,
                f"macro feature {target_id}/{variant} raw build identity mismatch",
            )
        raw_harnesses = raw_target.get("harnesses")
        fail(
            isinstance(raw_harnesses, list),
            f"macro feature {target_id} harnesses are missing",
        )
        expected_harnesses = [
            row for row in suite.harnesses if row.target_id == target_id
        ]
        harness_results = {
            row.get("id"): row for row in raw_harnesses if isinstance(row, dict)
        }
        fail(
            tuple(harness_results)
            == tuple(row.harness_id for row in expected_harnesses),
            f"macro feature {target_id} harness order or coverage differs from the suite",
        )
        for contract in expected_harnesses:
            harness = harness_results[contract.harness_id]
            fail(
                harness.get("metric_direction") == contract.direction
                and harness.get("rss_work_model") == contract.rss_work_model,
                f"macro feature {target_id}/{contract.harness_id} contract mismatch",
            )
            gates = harness.get("gates")
            fail(
                isinstance(gates, dict)
                and all(gates.get(gate) is True for gate in required_gates),
                f"macro feature {target_id}/{contract.harness_id} gates failed",
            )
            warmups = harness.get("warmup_attestations")
            fail(
                isinstance(warmups, dict) and set(warmups) == set(FEATURE_VARIANTS),
                f"macro feature {target_id}/{contract.harness_id} warmups are incomplete",
            )
            for variant, warmup in warmups.items():
                fail(
                    isinstance(warmup, dict)
                    and warmup.get("target_id") == target_id
                    and warmup.get("harness_id") == contract.harness_id
                    and warmup.get("phase") == "warmup"
                    and warmup.get("variant") == variant,
                    f"macro feature {target_id}/{contract.harness_id}/{variant} warmup is invalid",
                )
                fail(
                    warmup.get("round") == 0 and warmup.get("correctness") is True,
                    f"macro feature {target_id}/{contract.harness_id}/{variant} warmup attestation is invalid",
                )
                warmup_performance = positive(
                    warmup.get("performance"), "macro feature warmup performance"
                )
                warmup_rss = positive(
                    warmup.get("peak_rss_mib"), "macro feature warmup peak RSS"
                )
                warmup_path = resolve_artifact(
                    REPOSITORY_ROOT,
                    warmup.get("raw_record_path"),
                    warmup.get("record_sha256"),
                    f"macro feature {target_id}/{contract.harness_id}/{variant} warmup",
                )
                validate_macro_measurement_record(
                    warmup_path,
                    expected_identity={
                        "suite_id": suite.suite_id,
                        "suite_manifest_sha256": suite.digest,
                        "target_id": target_id,
                        "harness_id": contract.harness_id,
                        "source_commit": contract.source_commit,
                        "implementation_revision": suite.implementation_revision,
                        "implementation_sha256": suite.implementation_sha256,
                        "variant": variant,
                        "phase": "warmup",
                        "round": 0,
                    },
                    expected_metrics={
                        "performance": warmup_performance,
                        "performance_unit": contract.performance_unit,
                        "peak_rss_mib": warmup_rss,
                    },
                    context=(
                        f"macro feature {target_id}/{contract.harness_id}/"
                        f"{variant} warmup"
                    ),
                )
            measurements = harness.get("measurements")
            fail(
                isinstance(measurements, list),
                f"macro feature {target_id}/{contract.harness_id} measurements are missing",
            )
            indexed: dict[tuple[int, str], Mapping[str, Any]] = {}
            for measurement in measurements:
                fail(
                    isinstance(measurement, dict),
                    "macro feature measurement is invalid",
                )
                round_number = exact_int(
                    measurement.get("round"), "macro feature measurement round"
                )
                variant = measurement.get("variant")
                key = (round_number, variant)
                fail(
                    round_number in range(1, suite.measured_rounds + 1)
                    and variant in FEATURE_VARIANTS
                    and key not in indexed,
                    "macro feature measurement identity is invalid",
                )
                positive(measurement.get("performance"), "macro feature performance")
                positive(measurement.get("peak_rss_mib"), "macro feature peak RSS")
                measurement_path = resolve_artifact(
                    REPOSITORY_ROOT,
                    measurement.get("raw_record_path"),
                    measurement.get("record_sha256"),
                    f"macro feature {target_id}/{contract.harness_id}/{variant}/round-{round_number}",
                )
                validate_macro_measurement_record(
                    measurement_path,
                    expected_identity={
                        "suite_id": suite.suite_id,
                        "suite_manifest_sha256": suite.digest,
                        "target_id": target_id,
                        "harness_id": contract.harness_id,
                        "source_commit": contract.source_commit,
                        "implementation_revision": suite.implementation_revision,
                        "implementation_sha256": suite.implementation_sha256,
                        "variant": variant,
                        "phase": "measurement",
                        "round": round_number,
                    },
                    expected_metrics={
                        "performance": float(measurement["performance"]),
                        "performance_unit": contract.performance_unit,
                        "peak_rss_mib": float(measurement["peak_rss_mib"]),
                    },
                    context=(
                        f"macro feature {target_id}/{contract.harness_id}/"
                        f"{variant}/round-{round_number}"
                    ),
                )
                indexed[(round_number, str(variant))] = measurement
            expected = {
                (round_number, variant)
                for round_number in range(1, suite.measured_rounds + 1)
                for variant in FEATURE_VARIANTS
            }
            fail(
                set(indexed) == expected,
                f"macro feature {target_id}/{contract.harness_id} measurement matrix is incomplete",
            )
            stored_harness_families = harness.get("comparison_families")
            fail(
                isinstance(stored_harness_families, dict)
                and set(stored_harness_families) == set(declared_families),
                f"macro feature {target_id}/{contract.harness_id} comparison families are incomplete",
            )
            for family_id, comparison in declared_families.items():
                performance_values: list[float] = []
                rss_values: list[float] = []
                for round_number in range(1, suite.measured_rounds + 1):
                    reference = indexed[(round_number, comparison.reference)]
                    subject = indexed[(round_number, comparison.subject)]
                    performance_ratio = cost_ratio(
                        float(subject["performance"]),
                        float(reference["performance"]),
                        contract.direction,
                    )
                    rss_ratio = float(subject["peak_rss_mib"]) / float(
                        reference["peak_rss_mib"]
                    )
                    performance_values.append(performance_ratio)
                    rss_values.append(rss_ratio)
                    observations[family_id]["performance"][
                        (target_id, contract.harness_id)
                    ].append((round_number, performance_ratio))
                    observations[family_id]["rss"][
                        (target_id, contract.harness_id)
                    ].append((round_number, rss_ratio))
                stored_comparison = stored_harness_families[family_id]
                fail(
                    isinstance(stored_comparison, dict)
                    and stored_comparison.get("reference") == comparison.reference
                    and stored_comparison.get("subject") == comparison.subject,
                    f"macro feature {target_id}/{contract.harness_id}/{family_id} endpoints mismatch",
                )
                fail(
                    math.isclose(
                        positive(
                            stored_comparison.get("execution_cost_ratio_median"),
                            "macro feature stored performance median",
                        ),
                        statistics.median(performance_values),
                        rel_tol=1e-12,
                    )
                    and math.isclose(
                        positive(
                            stored_comparison.get("peak_rss_ratio_median"),
                            "macro feature stored RSS median",
                        ),
                        statistics.median(rss_values),
                        rel_tol=1e-12,
                    ),
                    f"macro feature stored harness aggregate mismatch: {target_id}/{contract.harness_id}/{family_id}",
                )
    rows: list[dict[str, Any]] = []
    for comparison in suite.comparison_families:
        family_rows: dict[str, list[dict[str, Any]]] = {}
        for metric in ("performance", "rss"):
            metric_rows = macro_aggregate(
                group="feature",
                comparison_family=comparison.family_id,
                metric=metric,
                variant=comparison.subject,
                reference=comparison.reference,
                observations=observations[comparison.family_id][metric],
                suite=suite,
                source=path,
            )
            family_rows[metric] = metric_rows
            rows.extend(metric_rows)
        for target_id in suite.target_ids:
            stored = result_targets[target_id]["comparison_families"][
                comparison.family_id
            ]
            calculated_performance = next(
                row["ratio"]
                for row in family_rows["performance"]
                if row["aggregation_level"] == "target_geomean"
                and row["target_id"] == target_id
            )
            calculated_rss = next(
                row["ratio"]
                for row in family_rows["rss"]
                if row["aggregation_level"] == "target_geomean"
                and row["target_id"] == target_id
            )
            fail(
                math.isclose(
                    positive(
                        stored.get("execution_cost_ratio_geometric_mean"),
                        "macro feature stored target performance aggregate",
                    ),
                    calculated_performance,
                    rel_tol=1e-12,
                )
                and math.isclose(
                    positive(
                        stored.get("peak_rss_process_observed_ratio_geometric_mean"),
                        "macro feature stored target RSS aggregate",
                    ),
                    calculated_rss,
                    rel_tol=1e-12,
                ),
                f"macro feature stored target aggregate mismatch: {target_id}/{comparison.family_id}",
            )
        stored_population = stored_population_families[comparison.family_id]
        calculated_population_performance = next(
            row["ratio"]
            for row in family_rows["performance"]
            if row["aggregation_level"] == "population_geomean"
        )
        calculated_population_rss = next(
            row["ratio"]
            for row in family_rows["rss"]
            if row["aggregation_level"] == "population_geomean"
        )
        fail(
            math.isclose(
                positive(
                    stored_population.get("execution_cost_ratio_geometric_mean"),
                    "macro feature stored population performance aggregate",
                ),
                calculated_population_performance,
                rel_tol=1e-12,
            )
            and math.isclose(
                positive(
                    stored_population.get("fixed_work_peak_rss_ratio_geometric_mean"),
                    "macro feature stored population fixed-work RSS aggregate",
                ),
                calculated_population_rss,
                rel_tol=1e-12,
            ),
            f"macro feature stored population aggregate mismatch: {comparison.family_id}",
        )
    return rows, {
        "path": str(path),
        "sha256": sha256_file(path),
        "target_count": len(result_targets),
        "workload_count": len(suite.harnesses),
    }


def _resolve_selector(path: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    supplied = path.resolve()
    selector_path = (
        supplied / "latest-selection.json" if supplied.is_dir() else supplied
    )
    selector = load_object(selector_path, "macro baseline selection")
    root = selector_path.parent

    def selected_path(key: str) -> Path:
        raw = selector.get(key)
        fail(isinstance(raw, str) and raw, f"macro baseline selection {key} is missing")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        fail(
            candidate.is_file(),
            f"macro baseline selected {key} is missing: {candidate}",
        )
        return candidate.resolve()

    return root, selected_path("plan"), selected_path("summary"), selector


def macro_target_fingerprint(
    protocol_payload: Mapping[str, Any], target_id: str
) -> str:
    targets = {
        str(target["id"]): target
        for target in protocol_payload.get("targets", [])
        if isinstance(target, dict) and isinstance(target.get("id"), str)
    }
    fail(
        target_id in targets, f"macro baseline protocol target is missing: {target_id}"
    )
    return canonical_sha256(
        {
            "protocol_id": "primary-macro-allocator-baselines-v1",
            "runner": protocol_payload.get("runner"),
            "suite": protocol_payload.get("suite"),
            "implementation": protocol_payload.get("implementation"),
            "measurement": protocol_payload.get("measurement"),
            "target": targets[target_id],
        }
    )


def macro_build_contract_fingerprint(
    protocol_payload: Mapping[str, Any], target_id: str, build_variant: str
) -> str:
    pins = {
        "unialloc": dict(protocol_payload["implementation"]),
        "mimalloc": {"mimalloc": "0.1.52", "libmimalloc-sys": "0.1.49"},
        "jemalloc": {"tikv-jemallocator": "0.7.0"},
        "system": {},
    }.get(build_variant)
    fail(pins is not None, f"macro baseline build variant is invalid: {build_variant}")
    if target_id in {"swc", "rustpython", "actix_web"} and build_variant == "unialloc":
        pins = {
            **pins,
            "injection_route": "rustc-workspace-wrapper-direct-load-rlib-v1",
        }
    if target_id == "swc" and build_variant == "jemalloc":
        pins = {
            **pins,
            "injection_route": SWC_JEMALLOC_DIRECT_LOAD_ROUTE,
            "tikv-jemalloc-sys": SWC_JEMALLOC_SYS_VERSION,
        }
    return canonical_sha256(
        {
            "schema_version": 1,
            "build_adapter_version": protocol_payload.get("build_adapter_version"),
            "target_fingerprint": macro_target_fingerprint(protocol_payload, target_id),
            "build_variant": build_variant,
            "dependency_pins": pins,
        }
    )


def macro_variant_contract_fingerprint(
    protocol_payload: Mapping[str, Any],
    target_id: str,
    variant: str,
    google_tcmalloc_identity: Mapping[str, Any] | None = None,
) -> str:
    definitions = {
        "unialloc": {
            "label": "UniAlloc Default",
            "build_variant": "unialloc",
            "allocator_route": "injected-rust-global-allocator",
            "thp_mode": "allocator-default",
        },
        "mimalloc": {
            "label": "mimalloc (THP allowed)",
            "build_variant": "mimalloc",
            "allocator_route": "injected-rust-global-allocator",
            "thp_mode": "mimalloc-thp-explicitly-allowed",
        },
        "mimalloc_no_thp": {
            "label": "mimalloc (THP off)",
            "build_variant": "mimalloc",
            "allocator_route": "injected-rust-global-allocator",
            "thp_mode": "process-wide-pr-set-thp-disable",
        },
        "google_tcmalloc": {
            "label": "Google TCMalloc",
            "build_variant": "system",
            "allocator_route": "rust-system-api-with-authenticated-ld-preload",
            "thp_mode": "modern-hpaa-adaptive-subrelease",
        },
        "jemalloc": {
            "label": "jemalloc",
            "build_variant": "jemalloc",
            "allocator_route": "injected-rust-global-allocator",
            "thp_mode": "allocator-default",
        },
    }
    fail(variant in definitions, f"macro baseline variant is invalid: {variant}")
    runtime_overrides = (
        {
            "MIMALLOC_ALLOW_LARGE_OS_PAGES": "0",
            "MIMALLOC_RESERVE_HUGE_OS_PAGES": "0",
            "MIMALLOC_ALLOW_THP": "0" if variant == "mimalloc_no_thp" else "1",
        }
        if variant in {"mimalloc", "mimalloc_no_thp"}
        else {}
    )
    dependency_pins = (
        {"mimalloc": "0.1.52", "libmimalloc-sys": "0.1.49"}
        if variant in {"mimalloc", "mimalloc_no_thp"}
        else ({"tikv-jemallocator": "0.7.0"} if variant == "jemalloc" else {})
    )
    return canonical_sha256(
        {
            "schema_version": 1,
            "target_fingerprint": macro_target_fingerprint(protocol_payload, target_id),
            "variant": variant,
            **definitions[variant],
            "runtime_environment_overrides": runtime_overrides,
            "dependency_pins": dependency_pins,
            "google_tcmalloc_identity": (
                dict(google_tcmalloc_identity)
                if google_tcmalloc_identity is not None
                else None
            ),
        }
    )


def validate_macro_build_import(
    root: Path,
    *,
    protocol_payload: Mapping[str, Any],
    protocol_fingerprint: str,
    suite: SuiteContract,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Mapping[str, Any]],
    list[tuple[dict[str, Any], str]],
]:
    pointer_path = root / "latest-compatibility-import.json"
    if not pointer_path.exists():
        return None, {}, []
    pointer = load_object(pointer_path, "macro baseline compatibility import pointer")
    fail(
        set(pointer) == {"import_id", "manifest", "manifest_payload_sha256"},
        "macro baseline compatibility import pointer is invalid",
    )
    import_id = pointer.get("import_id")
    payload_sha256 = pointer.get("manifest_payload_sha256")
    fail(
        isinstance(import_id, str)
        and len(import_id) == 64
        and isinstance(payload_sha256, str)
        and len(payload_sha256) == 64,
        "macro baseline compatibility import identity is invalid",
    )
    manifest_identity = validate_artifact_identity(
        pointer.get("manifest"), "macro baseline compatibility import manifest"
    )
    manifest_path = Path(manifest_identity["path"])
    expected_manifest_path = (
        root / "compatibility-imports" / payload_sha256 / "manifest.json"
    ).resolve()
    fail(
        manifest_path == expected_manifest_path,
        "macro baseline compatibility import manifest path is invalid",
    )
    manifest = load_object(
        manifest_path, "macro baseline compatibility import manifest"
    )
    fail(
        canonical_sha256(manifest) == payload_sha256,
        "macro baseline compatibility import payload digest mismatch",
    )
    base_mutations = tuple(sorted(build_importer.BASE_MUTATIONS))
    alias_mutations = tuple(sorted(build_importer.ALIAS_MUTATIONS))
    base_variants = tuple(build_importer.BASE_VARIANTS)
    alias_variants = tuple(build_importer.ALIAS_VARIANTS)
    all_variants = (*base_variants, *alias_variants)
    destination_protocol = manifest.get("destination_protocol")
    source_protocol = manifest.get("source_protocol")
    current_runner = {
        "path": str(MACRO_RUNNER_PATH.relative_to(REPOSITORY_ROOT)),
        "sha256": sha256_file(MACRO_RUNNER_PATH),
    }
    fail(
        manifest.get("schema_version") == build_importer.SCHEMA_VERSION
        and manifest.get("import_id") == import_id
        and manifest.get("suite_manifest_sha256") == suite.digest
        and Path(str(manifest.get("destination_raw", ""))).resolve() == root.resolve()
        and isinstance(destination_protocol, dict)
        and destination_protocol
        == {"fingerprint": protocol_fingerprint, "runner": current_runner}
        and isinstance(source_protocol, dict)
        and source_protocol.get("fingerprint") != protocol_fingerprint
        and manifest.get("permitted_base_mutations") == list(base_mutations)
        and manifest.get("permitted_alias_mutations") == list(alias_mutations)
        and manifest.get("import_execution")
        == {
            "processes_started": 0,
            "policy": "pure-python-file-derivation-with-no-build-or-command-execution",
        }
        and manifest.get("artifact_path_policy")
        == (
            "compiled artifacts, source worktrees, and command logs retain their "
            "absolute immutable v3 paths"
        ),
        "macro baseline compatibility import contract mismatch",
    )
    importer_identity = validate_artifact_identity(
        manifest.get("importer"), "macro baseline compatibility importer"
    )
    fail(
        Path(importer_identity["path"]) == MACRO_IMPORTER_PATH
        and importer_identity["sha256"] == sha256_file(MACRO_IMPORTER_PATH),
        "macro baseline compatibility importer differs from the current importer",
    )
    source_raw = Path(str(manifest.get("source_raw", ""))).resolve()
    fail(
        source_raw.is_dir() and source_raw != root.resolve(),
        "macro baseline compatibility source root is invalid",
    )
    source_manifest_identity = validate_artifact_identity(
        source_protocol.get("manifest"),
        "macro baseline source protocol manifest",
    )
    source_manifest = load_object(
        Path(source_manifest_identity["path"]),
        "macro baseline source protocol manifest",
    )
    source_payload = source_manifest.get("protocol")
    source_fingerprint = source_protocol.get("fingerprint")
    fail(
        isinstance(source_payload, dict)
        and source_manifest.get("protocol_id") == "primary-macro-allocator-baselines-v1"
        and source_manifest.get("protocol_fingerprint") == source_fingerprint
        and canonical_sha256(source_payload) == source_fingerprint
        and source_protocol.get("runner") == source_payload.get("runner"),
        "macro baseline source protocol identity mismatch",
    )
    snapshot = manifest.get("implementation_snapshot_copy")
    fail(
        isinstance(snapshot, dict),
        "macro baseline imported implementation snapshot is missing",
    )
    source_snapshot_manifest = validate_artifact_identity(
        snapshot.get("source_manifest"),
        "macro baseline source implementation snapshot manifest",
    )
    destination_snapshot_manifest = validate_artifact_identity(
        snapshot.get("destination_manifest"),
        "macro baseline destination implementation snapshot manifest",
    )
    source_snapshot_path = Path(str(snapshot.get("source", ""))).resolve()
    destination_snapshot_path = Path(str(snapshot.get("destination", ""))).resolve()
    fail(
        source_snapshot_path.is_dir()
        and destination_snapshot_path.is_dir()
        and not destination_snapshot_path.is_symlink()
        and destination_snapshot_path.is_relative_to(root.resolve())
        and Path(source_snapshot_manifest["path"]).parent == source_snapshot_path
        and Path(destination_snapshot_manifest["path"]).parent
        == destination_snapshot_path
        and source_snapshot_manifest["sha256"]
        == destination_snapshot_manifest["sha256"]
        and snapshot.get("canonical_sha256") == suite.implementation_sha256
        and snapshot.get("canonical_sha256")
        == protocol_payload.get("implementation", {}).get("canonical_sha256")
        and snapshot.get("canonical_file_count")
        == protocol_payload.get("implementation", {}).get("canonical_file_count")
        and snapshot.get("canonical_size_bytes")
        == protocol_payload.get("implementation", {}).get("canonical_size_bytes"),
        "macro baseline imported implementation snapshot identity mismatch",
    )
    import_index_identity = validate_artifact_identity(
        manifest.get("import_build_index"),
        "macro baseline compatibility import build index",
    )
    import_index = load_object(
        Path(import_index_identity["path"]),
        "macro baseline compatibility import build index",
    )
    manifest_rows = manifest.get("records")
    expected_pairs = {
        (target_id, variant)
        for target_id in suite.target_ids
        for variant in all_variants
    }
    fail(
        isinstance(manifest_rows, list)
        and manifest.get("record_count") == 42
        and len(manifest_rows) == 42
        and len(expected_pairs) == 42,
        "macro baseline compatibility import must contain exactly 42 records",
    )
    retained: list[tuple[dict[str, Any], str]] = [
        (manifest_identity, "compatibility_import_manifest"),
        (importer_identity, "compatibility_importer"),
        (source_manifest_identity, "source_protocol_manifest"),
        (source_snapshot_manifest, "source_implementation_snapshot_manifest"),
        (
            destination_snapshot_manifest,
            "destination_implementation_snapshot_manifest",
        ),
        (import_index_identity, "compatibility_import_build_index"),
    ]
    destination_records: dict[str, Mapping[str, Any]] = {}
    source_records: dict[tuple[str, str], Mapping[str, Any]] = {}
    destination_by_pair: dict[tuple[str, str], Mapping[str, Any]] = {}
    seen_pairs: set[tuple[str, str]] = set()
    source_refs: list[Mapping[str, Any]] = []
    destination_refs: list[Mapping[str, Any]] = []
    for row in manifest_rows:
        fail(isinstance(row, dict), "macro baseline import record row is invalid")
        pair = (str(row.get("target_id")), str(row.get("variant")))
        fail(
            pair in expected_pairs and pair not in seen_pairs,
            f"macro baseline import record identity is invalid: {pair}",
        )
        seen_pairs.add(pair)
        expected_mutations = (
            alias_mutations if pair[1] in alias_variants else base_mutations
        )
        fail(
            row.get("mutated_fields") == list(expected_mutations),
            f"macro baseline import mutation declaration mismatch: {pair}",
        )
        source_identity = validate_artifact_identity(
            row.get("source"), f"macro baseline imported source record {pair}"
        )
        destination_identity = validate_artifact_identity(
            row.get("destination"),
            f"macro baseline imported destination record {pair}",
        )
        source_path = Path(source_identity["path"])
        destination_path = Path(destination_identity["path"])
        fail(
            source_path.is_relative_to(source_raw)
            and destination_path.is_relative_to(root.resolve()),
            f"macro baseline imported record path escapes its campaign: {pair}",
        )
        source_record = load_object(
            source_path, f"macro baseline imported source record {pair}"
        )
        destination_record = load_object(
            destination_path, f"macro baseline imported destination record {pair}"
        )
        changed_fields = tuple(
            sorted(
                field
                for field in source_record.keys() | destination_record.keys()
                if source_record.get(field) != destination_record.get(field)
            )
        )
        compatibility_import = destination_record.get("compatibility_import")
        expected_compatibility_import = {
            "schema_version": build_importer.SCHEMA_VERSION,
            "import_id": import_id,
            "source_protocol_fingerprint": source_fingerprint,
            "destination_protocol_fingerprint": protocol_fingerprint,
            "source_record": row.get("source"),
            "permitted_mutations": list(expected_mutations),
        }
        fail(
            changed_fields == expected_mutations
            and source_record.get("target_id") == pair[0]
            and source_record.get("variant") == pair[1]
            and destination_record.get("target_id") == pair[0]
            and destination_record.get("variant") == pair[1]
            and source_record.get("protocol_fingerprint") == source_fingerprint
            and destination_record.get("protocol_fingerprint") == protocol_fingerprint
            and destination_record.get("target_fingerprint")
            == macro_target_fingerprint(protocol_payload, pair[0])
            and destination_record.get("implementation_snapshot")
            == str(destination_snapshot_path)
            and destination_record.get("build_path") == str(destination_path)
            and compatibility_import == expected_compatibility_import
            and row.get("source_build_id") == source_record.get("build_id")
            and row.get("destination_build_id") == destination_record.get("build_id"),
            f"macro baseline imported record identity mismatch: {pair}",
        )
        source_build_id = canonical_sha256(
            {
                "build_fingerprint": source_record.get("build_fingerprint"),
                "target_id": pair[0],
                "variant": pair[1],
                "source_commit": source_record.get("source_commit"),
                "harness_binaries": source_record.get("harness_binaries"),
            }
        )
        destination_build_id = canonical_sha256(
            {
                "build_fingerprint": destination_record.get("build_fingerprint"),
                "target_id": pair[0],
                "variant": pair[1],
                "source_commit": destination_record.get("source_commit"),
                "harness_binaries": destination_record.get("harness_binaries"),
            }
        )
        if pair[1] in alias_variants:
            source_expected_fingerprint = macro_variant_contract_fingerprint(
                source_payload,
                pair[0],
                pair[1],
                (
                    source_record.get("tcmalloc_identity")
                    if pair[1] == "google_tcmalloc"
                    else None
                ),
            )
            destination_expected_fingerprint = macro_variant_contract_fingerprint(
                protocol_payload,
                pair[0],
                pair[1],
                (
                    destination_record.get("tcmalloc_identity")
                    if pair[1] == "google_tcmalloc"
                    else None
                ),
            )
        else:
            source_expected_fingerprint = macro_build_contract_fingerprint(
                source_payload, pair[0], pair[1]
            )
            destination_expected_fingerprint = macro_build_contract_fingerprint(
                protocol_payload, pair[0], pair[1]
            )
        fail(
            source_record.get("target_fingerprint")
            == macro_target_fingerprint(source_payload, pair[0])
            and source_record.get("build_fingerprint") == source_expected_fingerprint
            and destination_record.get("build_fingerprint")
            == destination_expected_fingerprint
            and source_record.get("build_id") == source_build_id
            and destination_record.get("build_id") == destination_build_id,
            f"macro baseline imported build fingerprint mismatch: {pair}",
        )
        retained.extend(
            [
                (source_identity, "compatibility_import_source_record"),
                (destination_identity, "compatibility_import_destination_record"),
            ]
        )
        source_refs.append(row["source"])
        destination_refs.append(row["destination"])
        source_records[pair] = source_record
        destination_by_pair[pair] = destination_record
        destination_records[str(destination_path)] = destination_record
    fail(
        seen_pairs == expected_pairs
        and manifest.get("source_record_set_sha256") == canonical_sha256(source_refs)
        and manifest.get("destination_record_set_sha256")
        == canonical_sha256(destination_refs),
        "macro baseline compatibility import record set mismatch",
    )
    for target_id in suite.target_ids:
        for alias, base in (
            ("mimalloc_no_thp", "mimalloc"),
            ("google_tcmalloc", "system"),
        ):
            source_alias = source_records[(target_id, alias)]
            source_base = source_records[(target_id, base)]
            destination_alias = destination_by_pair[(target_id, alias)]
            destination_base = destination_by_pair[(target_id, base)]
            fail(
                source_alias.get("base_build_path") == source_base.get("build_path")
                and source_alias.get("base_build_id") == source_base.get("build_id")
                and destination_alias.get("base_build_path")
                == destination_base.get("build_path")
                and destination_alias.get("base_build_id")
                == destination_base.get("build_id"),
                f"macro baseline imported alias base mismatch: {target_id}/{alias}",
            )
    index_rows = import_index.get("builds")
    expected_public_pairs = {
        (target_id, variant)
        for target_id in suite.target_ids
        for variant in build_importer.PUBLIC_VARIANTS
    }
    fail(
        import_index.get("schema_version") == 1
        and isinstance(index_rows, list)
        and len(index_rows) == len(expected_public_pairs),
        "macro baseline compatibility import build index is invalid",
    )
    indexed_pairs: set[tuple[str, str]] = set()
    for row in index_rows:
        fail(
            isinstance(row, dict), "macro baseline imported build index row is invalid"
        )
        pair = (str(row.get("target_id")), str(row.get("variant")))
        destination_record = destination_by_pair.get(pair)
        fail(
            pair in expected_public_pairs
            and pair not in indexed_pairs
            and destination_record is not None
            and row.get("build_id") == destination_record.get("build_id")
            and row.get("build_path") == destination_record.get("build_path")
            and row.get("harness_binaries")
            == destination_record.get("harness_binaries")
            and row.get("compatibility_import")
            == destination_record.get("compatibility_import"),
            f"macro baseline compatibility import build index mismatch: {pair}",
        )
        indexed_pairs.add(pair)
    fail(
        indexed_pairs == expected_public_pairs,
        "macro baseline compatibility import public build set is incomplete",
    )
    identity = {
        "pointer_path": str(pointer_path.resolve()),
        "pointer_sha256": sha256_file(pointer_path),
        "manifest": manifest_identity,
        "manifest_payload_sha256": payload_sha256,
        "import_id": import_id,
        "source_protocol_fingerprint": source_fingerprint,
        "destination_protocol_fingerprint": protocol_fingerprint,
        "record_count": len(manifest_rows),
        "source_record_set_sha256": manifest.get("source_record_set_sha256"),
        "destination_record_set_sha256": manifest.get("destination_record_set_sha256"),
        "import_build_index": import_index_identity,
    }
    return identity, destination_records, retained


def read_macro_baseline(
    path: Path, suite: SuiteContract
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root, plan_path, summary_path, selector = _resolve_selector(path)
    plan = load_object(plan_path, "macro baseline plan")
    summary = load_object(summary_path, "macro baseline summary")
    fail(
        plan.get("schema_version") == 1 and summary.get("schema_version") == 1,
        "macro baseline schema version is invalid",
    )
    fail(
        plan.get("protocol_id") == "primary-macro-allocator-baselines-v1"
        and summary.get("protocol_id") == plan.get("protocol_id"),
        "macro baseline protocol id is invalid",
    )
    selection_fingerprint = selector.get("selection_fingerprint")
    expected_selection = canonical_sha256(
        {
            "protocol_fingerprint": plan.get("protocol_fingerprint"),
            "targets": plan.get("requested_targets"),
            "requested_variants": plan.get("requested_variants"),
            "execution_variants": plan.get("execution_variants"),
        }
    )
    fail(
        selection_fingerprint == expected_selection
        and plan.get("selection_fingerprint") == expected_selection
        and summary.get("selection_fingerprint") == expected_selection,
        "macro baseline selection fingerprint mismatch",
    )
    fail(
        summary.get("protocol_fingerprint") == plan.get("protocol_fingerprint"),
        "macro baseline protocol fingerprint mismatch",
    )
    fail(
        tuple(plan.get("requested_targets", ())) == suite.target_ids
        and tuple(summary.get("requested_targets", ())) == suite.target_ids,
        "macro baseline targets differ from the suite",
    )
    requested_variants = tuple(plan.get("requested_variants", ()))
    execution_variants = tuple(plan.get("execution_variants", ()))
    fail(
        requested_variants and REFERENCE not in requested_variants,
        "macro baseline requested variants are invalid",
    )
    fail(
        execution_variants == (REFERENCE, *requested_variants),
        "macro baseline execution variants are invalid",
    )
    fail(
        tuple(summary.get("requested_variants", ())) == requested_variants
        and tuple(summary.get("execution_variants", ())) == execution_variants,
        "macro baseline summary variants differ from the plan",
    )
    fail(
        exact_int(plan.get("warmup_rounds"), "macro baseline warmups") == 1,
        "macro baseline warmup count is invalid",
    )
    fail(
        exact_int(plan.get("measured_rounds"), "macro baseline measured rounds")
        == suite.measured_rounds
        and summary.get("measured_rounds") == suite.measured_rounds,
        "macro baseline round count differs from the suite",
    )
    suite_copy = root / "suite-manifest.json"
    fail(
        suite_copy.is_file() and sha256_file(suite_copy) == suite.digest,
        "macro baseline suite binding mismatch",
    )
    campaign_path = (
        root / "campaigns" / str(plan.get("protocol_fingerprint")) / "campaign.json"
    )
    campaign = load_object(campaign_path, "macro baseline campaign manifest")
    binding = campaign.get("suite_manifest")
    protocol_payload = campaign.get("protocol")
    fail(
        campaign.get("protocol_fingerprint") == plan.get("protocol_fingerprint")
        and isinstance(protocol_payload, dict)
        and canonical_sha256(protocol_payload) == plan.get("protocol_fingerprint")
        and isinstance(binding, dict)
        and binding.get("sha256") == suite.digest,
        "macro baseline campaign provenance mismatch",
    )
    protocol_suite = protocol_payload.get("suite")
    protocol_implementation = protocol_payload.get("implementation")
    protocol_measurement = protocol_payload.get("measurement")
    protocol_targets = protocol_payload.get("targets")
    protocol_runner = protocol_payload.get("runner")
    fail(
        protocol_payload.get("build_adapter_version")
        == macro_runner.BUILD_ADAPTER_VERSION
        and protocol_runner
        == {
            "path": str(MACRO_RUNNER_PATH.relative_to(REPOSITORY_ROOT)),
            "sha256": sha256_file(MACRO_RUNNER_PATH),
        }
        and isinstance(protocol_suite, dict)
        and protocol_suite.get("id") == suite.suite_id
        and protocol_suite.get("manifest_sha256") == suite.digest
        and isinstance(protocol_implementation, dict)
        and protocol_implementation.get("git_revision") == suite.implementation_revision
        and protocol_implementation.get("canonical_sha256")
        == suite.implementation_sha256
        and isinstance(protocol_measurement, dict)
        and protocol_measurement.get("warmup_rounds") == suite.warmup_rounds
        and protocol_measurement.get("measured_rounds") == suite.measured_rounds,
        "macro baseline protocol differs from the suite",
    )
    expected_target_protocol = {
        row.target_id: (row.source_commit, row.rss_work_model)
        for row in suite.harnesses
    }
    fail(
        isinstance(protocol_targets, list), "macro baseline target protocol is missing"
    )
    observed_target_protocol: dict[str, tuple[Any, Any]] = {}
    for target in protocol_targets:
        fail(isinstance(target, dict), "macro baseline target protocol is invalid")
        observed_target_protocol[str(target.get("id"))] = (
            target.get("source_commit"),
            target.get("rss_work_model"),
        )
    fail(
        observed_target_protocol == expected_target_protocol,
        "macro baseline target protocol differs from the suite",
    )
    target_protocol_by_id = {str(target["id"]): target for target in protocol_targets}
    fail(
        len(target_protocol_by_id) == len(protocol_targets),
        "macro baseline target protocol contains duplicate targets",
    )
    referenced_artifacts: dict[tuple[str, str, int], dict[str, Any]] = {}

    def retain_identity(identity: Mapping[str, Any], role: str) -> dict[str, Any]:
        key = (identity["path"], identity["sha256"], identity["bytes"])
        retained = referenced_artifacts.setdefault(
            key,
            {**identity, "roles": set()},
        )
        retained["roles"].add(role)
        return dict(identity)

    def retain_artifact(reference: Any, role: str, context: str) -> dict[str, Any]:
        return retain_identity(validate_artifact_identity(reference, context), role)

    retain_artifact(
        binding,
        "suite_manifest",
        "macro baseline campaign suite manifest",
    )
    (
        compatibility_import_identity,
        imported_build_records,
        imported_artifacts,
    ) = validate_macro_build_import(
        root,
        protocol_payload=protocol_payload,
        protocol_fingerprint=str(plan.get("protocol_fingerprint")),
        suite=suite,
    )
    for artifact_identity, role in imported_artifacts:
        retain_identity(artifact_identity, role)
    raw_cells = plan.get("cells")
    fail(
        isinstance(raw_cells, list) and plan.get("cell_count") == len(raw_cells),
        "macro baseline plan cells are invalid",
    )
    plan_identities: set[tuple[str, str, str, str, int, str]] = set()
    cell_records: dict[tuple[str, str, str, str, int, str], Mapping[str, Any]] = {}
    harness_contracts = {
        (harness.target_id, harness.harness_id): harness for harness in suite.harnesses
    }
    for raw_cell in raw_cells:
        fail(isinstance(raw_cell, dict), "macro baseline planned cell is invalid")
        identity = (
            raw_cell.get("target_id"),
            raw_cell.get("harness_id"),
            raw_cell.get("cohort_id"),
            raw_cell.get("phase"),
            raw_cell.get("round"),
            raw_cell.get("variant"),
        )
        fail(
            identity not in plan_identities, "macro baseline plan has a duplicate cell"
        )
        plan_identities.add(identity)
        relative = raw_cell.get("relative_path")
        fail(
            isinstance(relative, str) and relative,
            "macro baseline planned cell path is missing",
        )
        fail(
            all(
                isinstance(raw_cell.get(field), str)
                and len(raw_cell[field]) == 64
                and all(
                    character in "0123456789abcdef" for character in raw_cell[field]
                )
                for field in (
                    "target_fingerprint",
                    "variant_fingerprint",
                    "cell_fingerprint",
                )
            ),
            "macro baseline planned cell fingerprints are invalid",
        )
        cell_path = (root / relative).resolve()
        fail(
            cell_path.is_relative_to(root.resolve()),
            "macro baseline planned cell path escapes the campaign root",
        )
        cell = load_object(cell_path, "macro baseline measurement cell")
        for key, expected in (
            ("target_id", raw_cell.get("target_id")),
            ("harness_id", raw_cell.get("harness_id")),
            ("cohort_id", raw_cell.get("cohort_id")),
            ("variant", raw_cell.get("variant")),
            ("phase", raw_cell.get("phase")),
            ("round", raw_cell.get("round")),
            ("protocol_fingerprint", plan.get("protocol_fingerprint")),
            ("cell_fingerprint", raw_cell.get("cell_fingerprint")),
            ("target_fingerprint", raw_cell.get("target_fingerprint")),
            ("variant_fingerprint", raw_cell.get("variant_fingerprint")),
            ("implementation_revision", suite.implementation_revision),
            ("implementation_sha256", suite.implementation_sha256),
            ("suite_id", suite.suite_id),
            ("suite_manifest_sha256", suite.digest),
        ):
            fail(
                cell.get(key) == expected,
                f"macro baseline cell identity mismatch: {cell_path}: {key}",
            )
        fail(
            cell.get("success") is True
            and isinstance(cell.get("correctness"), dict)
            and cell["correctness"].get("passed") is True,
            f"macro baseline cell failed: {cell_path}",
        )
        performance = positive(
            cell.get("performance"), f"macro baseline performance: {cell_path}"
        )
        peak_rss = positive(
            cell.get("peak_rss_mib"), f"macro baseline peak RSS: {cell_path}"
        )
        harness_contract = harness_contracts.get(
            (str(raw_cell.get("target_id")), str(raw_cell.get("harness_id")))
        )
        fail(
            harness_contract is not None,
            f"macro baseline planned cell harness is outside the suite: {cell_path}",
        )
        validate_macro_measurement_record(
            cell_path,
            expected_identity={
                "suite_id": suite.suite_id,
                "suite_manifest_sha256": suite.digest,
                "protocol_fingerprint": plan.get("protocol_fingerprint"),
                "target_fingerprint": raw_cell.get("target_fingerprint"),
                "variant_fingerprint": raw_cell.get("variant_fingerprint"),
                "cell_fingerprint": raw_cell.get("cell_fingerprint"),
                "target_id": raw_cell.get("target_id"),
                "harness_id": raw_cell.get("harness_id"),
                "cohort_id": raw_cell.get("cohort_id"),
                "variant": raw_cell.get("variant"),
                "phase": raw_cell.get("phase"),
                "round": raw_cell.get("round"),
                "source_commit": harness_contract.source_commit,
                "implementation_revision": suite.implementation_revision,
                "implementation_sha256": suite.implementation_sha256,
            },
            expected_metrics={
                "performance": performance,
                "performance_unit": harness_contract.performance_unit,
                "peak_rss_mib": peak_rss,
            },
            context=f"macro baseline measurement cell {cell_path}",
            require_baseline_contract=True,
        )
        artifacts = cell.get("artifacts")
        fail(
            isinstance(artifacts, dict),
            f"macro baseline measurement artifacts are missing: {cell_path}",
        )
        retain_artifact(
            artifacts.get("binary"),
            "measurement_binary",
            f"macro baseline measurement binary {cell_path}",
        )
        retain_artifact(
            artifacts.get("runtime_environment"),
            "runtime_environment",
            f"macro baseline runtime environment {cell_path}",
        )
        suite_artifact = retain_artifact(
            artifacts.get("suite_manifest"),
            "suite_manifest",
            f"macro baseline suite manifest {cell_path}",
        )
        fail(
            suite_artifact["sha256"] == suite.digest,
            f"macro baseline cell suite artifact differs from the suite: {cell_path}",
        )
        cell_records[identity] = cell
    cell_index_path = root / "cells.jsonl"
    cell_index_rows = load_jsonl(cell_index_path, "macro baseline cell index")
    indexed_cell_paths: dict[str, Mapping[str, Any]] = {}
    for row in cell_index_rows:
        relative = row.get("path")
        fail(
            isinstance(relative, str) and relative not in indexed_cell_paths,
            "macro baseline cell index path is invalid or repeated",
        )
        cell_path = (root / relative).resolve()
        fail(
            cell_path.is_relative_to(root.resolve())
            and cell_path.is_file()
            and row.get("sha256") == sha256_file(cell_path),
            f"macro baseline cell index digest mismatch: {relative}",
        )
        indexed_cell_paths[relative] = row
    actual_cell_paths = {
        path.relative_to(root).as_posix()
        for path in (root / "cells").rglob("*.json")
        if path.is_file() and "attempts" not in path.relative_to(root / "cells").parts
    }
    fail(
        set(indexed_cell_paths) == actual_cell_paths,
        "macro baseline cell index does not exactly cover committed cells",
    )
    for raw_cell in raw_cells:
        relative = str(raw_cell["relative_path"])
        indexed = indexed_cell_paths.get(relative)
        fail(
            isinstance(indexed, dict)
            and all(
                indexed.get(field) == raw_cell.get(field)
                for field in (
                    "target_id",
                    "harness_id",
                    "cohort_id",
                    "variant",
                    "phase",
                    "round",
                )
            )
            and indexed.get("protocol_fingerprint") == plan.get("protocol_fingerprint"),
            f"macro baseline selected cell is absent from the cell index: {relative}",
        )
    build_index_path = root / "build-index.json"
    build_index = load_object(build_index_path, "macro baseline build index")
    raw_builds = build_index.get("builds")
    fail(
        build_index.get("schema_version") == 1 and isinstance(raw_builds, list),
        "macro baseline build index is invalid",
    )
    build_records: dict[tuple[str, str], Mapping[str, Any]] = {}
    selected_build_records: list[dict[str, Any]] = []
    expected_harness_ids = {
        target_id: {
            harness.harness_id
            for harness in suite.harnesses
            if harness.target_id == target_id
        }
        for target_id in suite.target_ids
    }
    for raw_build in raw_builds:
        fail(isinstance(raw_build, dict), "macro baseline build index row is invalid")
        key = (raw_build.get("target_id"), raw_build.get("variant"))
        fail(
            key[0] in suite.target_ids
            and key[1] in execution_variants
            and key not in build_records,
            "macro baseline build index identity is invalid",
        )
        binaries = raw_build.get("harness_binaries")
        fail(
            isinstance(binaries, dict)
            and set(binaries) == expected_harness_ids[str(key[0])],
            f"macro baseline build binary coverage is incomplete: {key}",
        )
        for harness_id, artifact in binaries.items():
            retain_artifact(
                artifact,
                "build_binary",
                f"macro baseline build binary {key}/{harness_id}",
            )
        build_path = Path(str(raw_build.get("build_path", ""))).resolve()
        fail(
            build_path.is_file() and build_path.is_relative_to(root.resolve()),
            f"macro baseline build record is missing or outside the campaign: {key}",
        )
        build_record = load_object(build_path, f"macro baseline build record {key}")
        if compatibility_import_identity is None:
            fail(
                build_record.get("compatibility_import") is None,
                f"macro baseline build has an unbound compatibility import: {key}",
            )
        else:
            fail(
                imported_build_records.get(str(build_path)) == build_record,
                f"macro baseline build is absent from the compatibility import: {key}",
            )
        target_id, variant = str(key[0]), str(key[1])
        target_protocol = target_protocol_by_id[target_id]
        expected_target_fingerprint = macro_target_fingerprint(
            protocol_payload, target_id
        )
        expected_build_identity = {
            "schema_version": 1,
            "protocol_id": "primary-macro-allocator-baselines-v1",
            "protocol_fingerprint": plan.get("protocol_fingerprint"),
            "target_fingerprint": expected_target_fingerprint,
            "target_id": target_id,
            "variant": variant,
            "source_ref": target_protocol.get("source_ref"),
            "source_commit": target_protocol.get("source_commit"),
            "implementation_revision": suite.implementation_revision,
            "implementation_sha256": suite.implementation_sha256,
            "stats_enabled": False,
            "success": True,
        }
        fail(
            all(
                build_record.get(field) == expected
                for field, expected in expected_build_identity.items()
            ),
            f"macro baseline build identity mismatch: {key}",
        )
        fail(
            build_record.get("build_path") == str(build_path)
            and build_record.get("harness_binaries") == binaries,
            f"macro baseline build artifacts differ from the build index: {key}",
        )
        source_record = build_record.get("source_record")
        fail(
            isinstance(source_record, dict)
            and source_record.get("source_commit", source_record.get("head"))
            == target_protocol.get("source_commit"),
            f"macro baseline build source record mismatch: {key}",
        )
        build_fingerprint = build_record.get("build_fingerprint")
        fail(
            isinstance(build_fingerprint, str)
            and len(build_fingerprint) == 64
            and all(character in "0123456789abcdef" for character in build_fingerprint),
            f"macro baseline build fingerprint is invalid: {key}",
        )
        base_variant = build_record.get("base_build_variant")
        if variant in {"mimalloc_no_thp", "google_tcmalloc"}:
            expected_base = {
                "mimalloc_no_thp": "mimalloc",
                "google_tcmalloc": "system",
            }[variant]
            google_identity = (
                build_record.get("tcmalloc_identity")
                if variant == "google_tcmalloc"
                else None
            )
            fail(
                base_variant == expected_base
                and (variant != "google_tcmalloc" or isinstance(google_identity, dict))
                and build_fingerprint
                == macro_variant_contract_fingerprint(
                    protocol_payload,
                    target_id,
                    variant,
                    google_identity,
                ),
                f"macro baseline alias build base is invalid: {key}",
            )
            base_path = Path(str(build_record.get("base_build_path", ""))).resolve()
            fail(
                base_path.is_file() and base_path.is_relative_to(root.resolve()),
                f"macro baseline alias base build is missing: {key}",
            )
            base_record = load_object(
                base_path, f"macro baseline alias base build record {key}"
            )
            if compatibility_import_identity is None:
                fail(
                    base_record.get("compatibility_import") is None,
                    f"macro baseline alias base has an unbound import: {key}",
                )
            else:
                fail(
                    imported_build_records.get(str(base_path)) == base_record,
                    f"macro baseline alias base is absent from the import: {key}",
                )
            base_binaries = base_record.get("harness_binaries")
            expected_base_id = canonical_sha256(
                {
                    "build_fingerprint": base_record.get("build_fingerprint"),
                    "target_id": target_id,
                    "variant": expected_base,
                    "source_commit": target_protocol.get("source_commit"),
                    "harness_binaries": base_binaries,
                }
            )
            fail(
                base_record.get("build_id") == expected_base_id
                and build_record.get("base_build_id") == expected_base_id
                and base_record.get("schema_version") == 1
                and base_record.get("protocol_id")
                == "primary-macro-allocator-baselines-v1"
                and base_record.get("protocol_fingerprint")
                == plan.get("protocol_fingerprint")
                and base_record.get("target_fingerprint") == expected_target_fingerprint
                and base_record.get("target_id") == target_id
                and base_record.get("variant") == expected_base
                and base_record.get("build_fingerprint")
                == macro_build_contract_fingerprint(
                    protocol_payload, target_id, expected_base
                )
                and base_record.get("source_commit")
                == target_protocol.get("source_commit")
                and base_record.get("implementation_revision")
                == suite.implementation_revision
                and base_record.get("implementation_sha256")
                == suite.implementation_sha256
                and base_record.get("success") is True,
                f"macro baseline alias base build identity mismatch: {key}",
            )
            retain_identity(
                validate_path_digest(
                    str(base_path),
                    sha256_file(base_path),
                    f"macro baseline alias base build record {key}",
                ),
                "base_build_record",
            )
        else:
            fail(
                base_variant is None
                and build_fingerprint
                == macro_build_contract_fingerprint(
                    protocol_payload, target_id, variant
                ),
                f"macro baseline base build contract mismatch: {key}",
            )
        record_binaries = build_record.get("harness_binaries")
        fail(
            isinstance(record_binaries, dict)
            and set(record_binaries) == expected_harness_ids[target_id],
            f"macro baseline build record binary coverage is incomplete: {key}",
        )
        for harness_id, artifact in record_binaries.items():
            indexed_identity = validate_artifact_identity(
                binaries[harness_id],
                f"macro baseline indexed build binary {key}/{harness_id}",
            )
            record_identity = retain_artifact(
                artifact,
                "build_binary",
                f"macro baseline recorded build binary {key}/{harness_id}",
            )
            fail(
                record_identity == indexed_identity,
                f"macro baseline build binary identity mismatch: {key}/{harness_id}",
            )
        commands = build_record.get("commands")
        fail(
            isinstance(commands, list) and commands,
            f"macro baseline build command evidence is missing: {key}",
        )
        for command_index, command in enumerate(commands):
            fail(
                isinstance(command, dict)
                and command.get("exit_code") == 0
                and command.get("timed_out") is False,
                f"macro baseline build command failed: {key}/{command_index}",
            )
            for stream in ("stdout", "stderr"):
                retain_identity(
                    validate_path_digest(
                        command.get(stream),
                        command.get(f"{stream}_sha256"),
                        f"macro baseline build {stream} {key}/{command_index}",
                    ),
                    "build_command_output",
                )
        direct_load = build_record.get("direct_load_route")
        if direct_load is not None:
            fail(
                isinstance(direct_load, dict)
                and direct_load.get("id")
                == "rustc-workspace-wrapper-direct-load-rlib-v1"
                and direct_load.get("implementation_revision")
                == suite.implementation_revision
                and direct_load.get("implementation_sha256")
                == suite.implementation_sha256,
                f"macro baseline direct-load identity mismatch: {key}",
            )
            for artifact_name in (
                "rlib",
                "wrapper",
                "build_stdout",
                "build_stderr",
            ):
                retain_artifact(
                    direct_load.get(artifact_name),
                    "build_support",
                    f"macro baseline direct-load {artifact_name} {key}",
                )
        if target_id == "swc" and variant == "jemalloc":
            for artifact_identity, role in validate_swc_jemalloc_direct_load(
                build_record, f"macro baseline build {key}"
            ):
                retain_identity(artifact_identity, role)
        else:
            fail(
                build_record.get("jemalloc_direct_load_route") is None,
                f"macro baseline build has an unexpected SWC jemalloc route: {key}",
            )
        allocator_provenance = build_record.get("allocator_provenance")
        if allocator_provenance is not None:
            fail(
                isinstance(allocator_provenance, dict),
                f"macro baseline allocator provenance is invalid: {key}",
            )
            retain_artifact(
                allocator_provenance.get("core_header"),
                "allocator_provenance",
                f"macro baseline allocator header {key}",
            )
            archives = allocator_provenance.get("static_archives")
            fail(
                isinstance(archives, list) and archives,
                f"macro baseline allocator archives are missing: {key}",
            )
            for archive_index, archive in enumerate(archives):
                retain_artifact(
                    archive,
                    "allocator_provenance",
                    f"macro baseline allocator archive {key}/{archive_index}",
                )
        if variant == "google_tcmalloc":
            runtime = build_record.get("tcmalloc_runtime")
            fail(
                isinstance(runtime, dict),
                f"macro baseline Google TCMalloc runtime is missing: {key}",
            )
            library_identity = validate_path_digest(
                runtime.get("library"),
                runtime.get("library_sha256"),
                f"macro baseline Google TCMalloc library {key}",
            )
            retain_identity(
                library_identity,
                "allocator_runtime",
            )
            requirements = runtime.get("runtime_requirements")
            expected_google_identity = {
                "revision": (
                    requirements.get("revision")
                    if isinstance(requirements, dict)
                    else None
                ),
                "library_sha256": library_identity["sha256"],
                "hpaa_active": (
                    requirements.get("hpaa_active")
                    if isinstance(requirements, dict)
                    else None
                ),
                "malloc_provider_is_self": (
                    requirements.get("malloc_provider_is_self")
                    if isinstance(requirements, dict)
                    else None
                ),
                "label": runtime.get("label"),
            }
            fail(
                isinstance(requirements, dict)
                and isinstance(expected_google_identity["revision"], str)
                and expected_google_identity["revision"]
                and isinstance(expected_google_identity["label"], str)
                and expected_google_identity["label"]
                and expected_google_identity["hpaa_active"] == 1
                and expected_google_identity["malloc_provider_is_self"] == 1
                and build_record.get("tcmalloc_identity") == expected_google_identity,
                f"macro baseline Google TCMalloc identity mismatch: {key}",
            )
        expected_build_id = canonical_sha256(
            {
                "build_fingerprint": build_fingerprint,
                "target_id": target_id,
                "variant": variant,
                "source_commit": target_protocol.get("source_commit"),
                "harness_binaries": record_binaries,
            }
        )
        fail(
            build_record.get("build_id") == expected_build_id
            and raw_build.get("build_id") == expected_build_id,
            f"macro baseline build id mismatch: {key}",
        )
        build_records[(target_id, variant)] = build_record
        selected_build_records.append(
            {
                "target_id": target_id,
                "variant": variant,
                "path": str(build_path),
                "sha256": sha256_file(build_path),
                "build_id": expected_build_id,
            }
        )
    fail(
        set(build_records)
        == {
            (target_id, variant)
            for target_id in suite.target_ids
            for variant in execution_variants
        },
        "macro baseline build index does not exactly cover selected variants",
    )
    for identity, cell in cell_records.items():
        target_id, harness_id, _cohort_id, _phase, _round_number, variant = identity
        build = build_records[(target_id, variant)]
        binary = build["harness_binaries"][harness_id]
        cell_identity = cell.get("identity")
        cell_binary = cell.get("artifacts", {}).get("binary")
        fail(
            isinstance(cell_identity, dict)
            and cell.get("build_id") == build.get("build_id")
            and cell_identity.get("build_id") == build.get("build_id")
            and cell.get("build_fingerprint") == build.get("build_fingerprint")
            and cell_identity.get("build_fingerprint") == build.get("build_fingerprint")
            and cell_identity.get("binary_sha256") == binary.get("sha256")
            and validate_artifact_identity(
                cell_binary,
                f"macro baseline cell binary {identity}",
            )
            == validate_artifact_identity(
                binary,
                f"macro baseline selected build binary {identity}",
            ),
            f"macro baseline cell differs from selected build: {identity}",
        )
    workloads = summary.get("workloads")
    targets = summary.get("targets")
    fail(
        isinstance(workloads, dict) and isinstance(targets, dict),
        "macro baseline summary aggregates are missing",
    )
    observations: dict[
        str, dict[str, dict[tuple[str, str], list[tuple[int, float]]]]
    ] = {
        variant: {"performance": defaultdict(list), "rss": defaultdict(list)}
        for variant in requested_variants
    }
    selected_cohorts: dict[tuple[str, str, str], str] = {}
    for contract in suite.harnesses:
        workload_id = f"{contract.target_id}/{contract.harness_id}"
        raw_workload = workloads.get(workload_id)
        fail(
            isinstance(raw_workload, dict)
            and set(raw_workload) == set(requested_variants),
            f"macro baseline workload is incomplete: {workload_id}",
        )
        for variant in requested_variants:
            record = raw_workload[variant]
            fail(
                isinstance(record, dict),
                f"macro baseline workload record is invalid: {workload_id}/{variant}",
            )
            raw_observations = record.get("same_round_observations")
            fail(
                isinstance(raw_observations, list)
                and len(raw_observations) == suite.measured_rounds,
                f"macro baseline paired observations are incomplete: {workload_id}/{variant}",
            )
            seen_rounds: set[int] = set()
            performance_values: list[float] = []
            rss_values: list[float] = []
            cohort_id = record.get("cohort_id")
            fail(
                isinstance(cohort_id, str) and cohort_id,
                f"macro baseline cohort id is missing: {workload_id}/{variant}",
            )
            selected_cohorts[(contract.target_id, contract.harness_id, variant)] = (
                cohort_id
            )
            for observation in raw_observations:
                fail(
                    isinstance(observation, dict),
                    "macro baseline paired observation is invalid",
                )
                round_number = exact_int(
                    observation.get("round"), "macro baseline paired round"
                )
                fail(
                    round_number in range(1, suite.measured_rounds + 1)
                    and round_number not in seen_rounds,
                    "macro baseline paired round is invalid",
                )
                seen_rounds.add(round_number)
                performance_ratio = positive(
                    observation.get("performance_cost_ratio"),
                    "macro baseline performance ratio",
                )
                rss_ratio = positive(
                    observation.get("peak_rss_ratio"), "macro baseline RSS ratio"
                )
                reference_cell = cell_records.get(
                    (
                        contract.target_id,
                        contract.harness_id,
                        cohort_id,
                        "measurement",
                        round_number,
                        REFERENCE,
                    )
                )
                subject_cell = cell_records.get(
                    (
                        contract.target_id,
                        contract.harness_id,
                        cohort_id,
                        "measurement",
                        round_number,
                        variant,
                    )
                )
                fail(
                    reference_cell is not None and subject_cell is not None,
                    f"macro baseline paired cells are missing: {workload_id}/{variant}/round-{round_number}",
                )
                derived_performance = cost_ratio(
                    positive(
                        subject_cell.get("performance"),
                        "macro baseline subject performance",
                    ),
                    positive(
                        reference_cell.get("performance"),
                        "macro baseline reference performance",
                    ),
                    contract.direction,
                )
                derived_rss = positive(
                    subject_cell.get("peak_rss_mib"), "macro baseline subject RSS"
                ) / positive(
                    reference_cell.get("peak_rss_mib"), "macro baseline reference RSS"
                )
                fail(
                    math.isclose(performance_ratio, derived_performance, rel_tol=1e-12)
                    and math.isclose(rss_ratio, derived_rss, rel_tol=1e-12),
                    f"macro baseline summary differs from raw cells: {workload_id}/{variant}/round-{round_number}",
                )
                performance_values.append(performance_ratio)
                rss_values.append(rss_ratio)
                observations[variant]["performance"][
                    (contract.target_id, contract.harness_id)
                ].append((round_number, performance_ratio))
                observations[variant]["rss"][
                    (contract.target_id, contract.harness_id)
                ].append((round_number, rss_ratio))
            fail(
                math.isclose(
                    float(record.get("performance_cost_ratio_median")),
                    statistics.median(performance_values),
                    rel_tol=1e-12,
                ),
                f"macro baseline stored performance median mismatch: {workload_id}/{variant}",
            )
            fail(
                math.isclose(
                    float(record.get("peak_rss_ratio_median")),
                    statistics.median(rss_values),
                    rel_tol=1e-12,
                ),
                f"macro baseline stored RSS median mismatch: {workload_id}/{variant}",
            )
            fail(
                record.get("peak_rss_headline_eligible")
                is (contract.rss_work_model == FIXED_WORK),
                f"macro baseline RSS eligibility mismatch: {workload_id}/{variant}",
            )
    expected_plan_identities = {
        (
            contract.target_id,
            contract.harness_id,
            selected_cohorts[(contract.target_id, contract.harness_id, subject)],
            phase,
            round_number,
            variant,
        )
        for contract in suite.harnesses
        for subject in requested_variants
        for variant in (REFERENCE, subject)
        for phase, rounds in (
            ("warmup", range(0, suite.warmup_rounds)),
            ("measurement", range(1, suite.measured_rounds + 1)),
        )
        for round_number in rounds
    }
    fail(
        plan_identities == expected_plan_identities,
        "macro baseline warmup and measurement cell matrix is incomplete or contains extra cells",
    )
    fail(
        set(targets) == set(suite.target_ids),
        "macro baseline target aggregates are incomplete",
    )
    rows: list[dict[str, Any]] = []
    for variant in requested_variants:
        for metric in ("performance", "rss"):
            variant_rows = macro_aggregate(
                group="baseline",
                comparison_family="allocator_baseline",
                metric=metric,
                variant=variant,
                reference=REFERENCE,
                observations=observations[variant][metric],
                suite=suite,
                source=summary_path,
            )
            for target_id in suite.target_ids:
                calculated = next(
                    row["ratio"]
                    for row in variant_rows
                    if row["aggregation_level"] == "target_geomean"
                    and row["target_id"] == target_id
                )
                stored = targets[target_id][variant][
                    (
                        "equal_weight_harness_geomean_performance_cost_ratio"
                        if metric == "performance"
                        else "equal_weight_harness_geomean_peak_rss_ratio"
                    )
                ]
                if metric == "rss" and target_id not in suite.fixed_work_targets:
                    fail(
                        stored is None,
                        f"macro baseline adaptive RSS aggregate must be null: {target_id}/{variant}",
                    )
                else:
                    fail(
                        math.isclose(float(stored), calculated, rel_tol=1e-12),
                        f"macro baseline stored target aggregate mismatch: {target_id}/{variant}/{metric}",
                    )
            rows.extend(variant_rows)
    identity = {
        "root": str(root),
        "selector_sha256": sha256_file(
            (path / "latest-selection.json") if path.is_dir() else path
        ),
        "plan_sha256": sha256_file(plan_path),
        "summary_sha256": sha256_file(summary_path),
        "campaign_sha256": sha256_file(campaign_path),
        "suite_copy_sha256": sha256_file(suite_copy),
        "cell_index_sha256": sha256_file(cell_index_path),
        "build_index_sha256": sha256_file(build_index_path),
        "selected_build_records": sorted(
            selected_build_records,
            key=lambda row: (row["target_id"], row["variant"]),
        ),
        "compatibility_import": compatibility_import_identity,
        "referenced_artifacts": [
            {
                "path": retained["path"],
                "sha256": retained["sha256"],
                "bytes": retained["bytes"],
                "roles": sorted(retained["roles"]),
            }
            for retained in sorted(
                referenced_artifacts.values(),
                key=lambda row: (row["path"], row["sha256"], row["bytes"]),
            )
        ],
        "variant_ids": list(requested_variants),
        "planned_cell_count": len(raw_cells),
    }
    return rows, identity


def select_summaries(
    rows: Sequence[Mapping[str, Any]], group: str, population: str, metric: str
) -> list[dict[str, Any]]:
    selected = [
        row
        for row in rows
        if row["comparison_group"] == group
        and row["population"] == population
        and row["metric"] == metric
    ]
    series = list(
        dict.fromkeys(
            (
                str(row["comparison_family"]),
                str(row["variant_id"]),
                str(row["reference_variant_id"]),
            )
            for row in selected
        )
    )
    if group == "feature":
        order = {
            family_id: position
            for position, family_id in enumerate(
                ("compiler_route", "policy_increment", "end_to_end")
            )
        }
        series.sort(key=lambda item: order[item[0]])
    result: list[dict[str, Any]] = []
    detail_level = "family_median" if population == "micro" else "target_geomean"
    for comparison_family, variant, reference in series:
        variant_rows = [
            row
            for row in selected
            if row["comparison_family"] == comparison_family
            and row["variant_id"] == variant
            and row["reference_variant_id"] == reference
        ]
        aggregate = [
            row
            for row in variant_rows
            if row["aggregation_level"] == "population_geomean"
        ]
        details = [
            row for row in variant_rows if row["aggregation_level"] == detail_level
        ]
        fail(
            len(aggregate) == 1 and details,
            f"plot summary is incomplete: {group}/{population}/{metric}/{variant}",
        )
        result.append(
            {
                "variant_id": variant,
                "reference_variant_id": reference,
                "comparison_family": comparison_family,
                "series_id": (comparison_family if group == "feature" else variant),
                "label": aggregate[0]["variant_label"],
                "aggregate": float(aggregate[0]["ratio"]),
                "aggregate_diagnostic": bool(aggregate[0]["diagnostic_only"]),
                "aggregate_eligible": bool(aggregate[0]["eligible_for_headline"]),
                "details": [
                    {
                        "id": (
                            row["family"] if population == "micro" else row["target_id"]
                        ),
                        "ratio": float(row["ratio"]),
                        "diagnostic": bool(row["diagnostic_only"]),
                    }
                    for row in details
                ],
            }
        )
    return result


def _ratio_formatter(value: float, _position: int) -> str:
    ratio = 2.0**value
    return f"{ratio:.2g}x"


def _display_log_ratio(ratio: float, bound: float) -> tuple[float, int]:
    value = math.log2(positive(ratio, "plotted ratio"))
    if value > bound:
        return bound * 0.965, 1
    if value < -bound:
        return -bound * 0.965, -1
    return value, 0


def draw_panel(
    ax: Any, summaries: Sequence[Mapping[str, Any]], xlabel: str, metric: str
) -> bool:
    values = [math.log2(float(row["aggregate"])) for row in summaries]
    values.extend(
        math.log2(float(detail["ratio"]))
        for row in summaries
        for detail in row["details"]
    )
    bound = min(
        MAX_DISPLAY_LOG2_RATIO,
        max(0.55, max(abs(value) for value in values) * 1.22),
    )
    any_overflow = False
    ax.axvline(0.0, color="#202124", linewidth=1.8, zorder=1)
    ax.grid(axis="x", color="#D9DDE3", linewidth=1.0, zorder=0)
    for position, row in enumerate(summaries):
        series_id = str(row["series_id"])
        color = COLORS.get(series_id, "#4C78A8")
        aggregate, aggregate_overflow = _display_log_ratio(
            float(row["aggregate"]), bound
        )
        any_overflow = any_overflow or aggregate_overflow != 0
        aggregate_diagnostic = bool(row["aggregate_diagnostic"])
        ax.barh(
            position,
            aggregate,
            left=0.0,
            height=0.48,
            color="white" if aggregate_diagnostic else color,
            alpha=0.78,
            edgecolor=color,
            linewidth=1.6 if aggregate_diagnostic else 0.8,
            zorder=2,
        )
        details = list(row["details"])
        for index, detail in enumerate(details):
            jitter = (index - (len(details) - 1) / 2.0) * min(
                0.055, 0.34 / max(1, len(details))
            )
            x, overflow = _display_log_ratio(float(detail["ratio"]), bound)
            any_overflow = any_overflow or overflow != 0
            diagnostic = bool(detail["diagnostic"])
            ax.scatter(
                x,
                position + jitter,
                s=58,
                marker=(">" if overflow > 0 else "<" if overflow < 0 else "o"),
                facecolor="white" if diagnostic else color,
                edgecolor=color,
                linewidth=1.5,
                alpha=0.95,
                zorder=3,
            )
        ax.scatter(
            aggregate,
            position,
            s=145,
            marker=(
                ">"
                if aggregate_overflow > 0
                else "<" if aggregate_overflow < 0 else "D"
            ),
            facecolor="white" if aggregate_diagnostic else "#111111",
            edgecolor=color if aggregate_diagnostic else "white",
            linewidth=1.8 if aggregate_diagnostic else 0.8,
            zorder=4,
        )
        ax.text(
            (
                aggregate - 0.055
                if aggregate_overflow > 0
                else (
                    aggregate + 0.055
                    if aggregate_overflow < 0
                    else aggregate + (0.045 if aggregate >= 0 else -0.045)
                )
            ),
            position - 0.31,
            f"{float(row['aggregate']):.3f}x",
            ha=(
                "right"
                if aggregate_overflow > 0
                else (
                    "left"
                    if aggregate_overflow < 0
                    else "left" if aggregate >= 0 else "right"
                )
            ),
            va="center",
            fontsize=15,
            color="#202124",
            clip_on=False,
        )
    ax.set_yticks(
        range(len(summaries)), [str(row["label"]) for row in summaries], fontsize=18
    )
    ax.invert_yaxis()
    ax.set_xlim(-bound * 1.08, bound * 1.08)
    ax.xaxis.set_major_formatter(FuncFormatter(_ratio_formatter))
    ax.tick_params(axis="x", labelsize=16)
    ax.set_xlabel(xlabel, fontsize=19, labelpad=10)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color("#6B7078")
    ax.tick_params(axis="y", length=0, pad=8)
    if metric == "rss":
        ax.set_facecolor("#FCFCFD")
    return any_overflow


def save_figure(
    rows: Sequence[Mapping[str, Any]], group: str, metric: str, stem: str, output: Path
) -> list[Path]:
    micro = select_summaries(rows, group, "micro", metric)
    macro = select_summaries(rows, group, "macro", metric)
    figure, axes = plt.subplots(1, 2, figsize=FIGURE_SIZE, gridspec_kw={"wspace": 0.50})
    metric_label = "cost ratio" if metric == "performance" else "peak RSS ratio"
    overflow = draw_panel(
        axes[0], micro, f"Rust std_bench {metric_label} / reference", metric
    )
    overflow = (
        draw_panel(
            axes[1], macro, f"Real-world target {metric_label} / reference", metric
        )
        or overflow
    )
    legend = [
        Line2D(
            [0],
            [0],
            marker="o",
            color="none",
            markerfacecolor="#4C78A8",
            markeredgecolor="#4C78A8",
            markersize=9,
            label="Family or target",
        ),
        Line2D(
            [0],
            [0],
            marker="D",
            color="none",
            markerfacecolor="#111111",
            markeredgecolor="#111111",
            markersize=9,
            label="Equal-weight aggregate",
        ),
    ]
    if metric == "rss":
        legend.extend(
            [
                Line2D(
                    [0],
                    [0],
                    marker="D",
                    color="none",
                    markerfacecolor="white",
                    markeredgecolor="#4C78A8",
                    markersize=9,
                    label="Diagnostic aggregate",
                ),
                Line2D(
                    [0],
                    [0],
                    marker="o",
                    color="none",
                    markerfacecolor="white",
                    markeredgecolor="#4C78A8",
                    markersize=9,
                    label="Diagnostic detail",
                ),
            ]
        )
    if overflow:
        legend.append(
            Line2D(
                [0],
                [0],
                marker=">",
                color="none",
                markerfacecolor="#4C78A8",
                markeredgecolor="#4C78A8",
                markersize=9,
                label="Off-scale value",
            )
        )
    figure.legend(
        handles=legend,
        loc="lower center",
        ncol=len(legend),
        frameon=False,
        fontsize=17,
        bbox_to_anchor=(0.5, 0.005),
    )
    figure.subplots_adjust(left=0.17, right=0.985, top=0.965, bottom=0.20)
    outputs: list[Path] = []
    metadata = {"Creator": "UniAlloc evaluation exporter", "Date": FIXED_DATE}
    for extension in ("svg", "pdf", "png"):
        destination = output / f"{stem}.{extension}"
        if extension == "png":
            figure.savefig(
                destination,
                dpi=PNG_DPI,
                facecolor="white",
                metadata={"Software": "UniAlloc evaluation exporter"},
            )
        elif extension == "pdf":
            figure.savefig(
                destination,
                facecolor="white",
                metadata={
                    "Creator": metadata["Creator"],
                    "Producer": metadata["Creator"],
                    "CreationDate": FIXED_DATETIME,
                    "ModDate": FIXED_DATETIME,
                },
            )
        else:
            figure.savefig(destination, facecolor="white", metadata=metadata)
        outputs.append(destination)
    plt.close(figure)
    return outputs


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row[field] for field in CSV_FIELDS} for row in rows)


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def publish_atomically(stage: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    backup = output.with_name(f".{output.name}.backup")
    if backup.exists():
        shutil.rmtree(backup)
    if output.exists():
        output.rename(backup)
    try:
        stage.rename(output)
    except BaseException:
        if backup.exists() and not output.exists():
            backup.rename(output)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def export(
    *,
    suite_path: Path,
    micro_feature_path: Path,
    micro_baseline_path: Path,
    macro_feature_path: Path,
    macro_baseline_path: Path,
    output: Path,
) -> Path:
    suite = read_suite(suite_path)
    micro_feature_rows, micro_feature_identity = read_micro_feature(
        micro_feature_path, suite
    )
    micro_baseline_rows, micro_baseline_identity = read_micro_baseline(
        micro_baseline_path, suite
    )
    macro_feature_rows, macro_feature_identity = read_macro_feature(
        macro_feature_path, suite
    )
    macro_baseline_rows, macro_baseline_identity = read_macro_baseline(
        macro_baseline_path, suite
    )
    rows = sorted(
        [
            *micro_baseline_rows,
            *macro_baseline_rows,
            *micro_feature_rows,
            *macro_feature_rows,
        ],
        key=lambda row: tuple(str(row[field]) for field in CSV_FIELDS),
    )
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=output.parent))
    try:
        input_identities = {
            "micro_feature": micro_feature_identity,
            "micro_baseline": micro_baseline_identity,
            "macro_feature": macro_feature_identity,
            "macro_baseline": macro_baseline_identity,
        }
        csv_path = stage / "normalized-observations.csv"
        data_path = stage / "presentation-data.json"
        write_csv(csv_path, rows)
        write_json(
            data_path,
            {
                "schema_version": OUTPUT_SCHEMA_VERSION,
                "suite": {
                    "path": str(suite.path),
                    "sha256": suite.digest,
                    "suite_id": suite.suite_id,
                    "implementation_revision": suite.implementation_revision,
                    "implementation_sha256": suite.implementation_sha256,
                    "measured_rounds": suite.measured_rounds,
                    "target_ids": list(suite.target_ids),
                    "fixed_work_rss_target_ids": list(suite.fixed_work_targets),
                    "comparison_families": [
                        {
                            "id": family.family_id,
                            "reference": family.reference,
                            "subject": family.subject,
                        }
                        for family in suite.comparison_families
                    ],
                },
                "aggregation_contract": {
                    "micro_leaf": (
                        "subject/reference ratio of three-round process medians "
                        "on one shared all-variant robust benchmark set"
                    ),
                    "micro_family": "back-transformed median log ratio",
                    "micro_population": "unweighted geometric mean across family medians",
                    "micro_performance_floor_ns": ROBUST_FLOOR_NS,
                    "micro_rss": "diagnostic because libtest iteration counts are adaptive",
                    "macro_leaf": "median of complete same-round ratios",
                    "macro_target": "unweighted geometric mean across harness medians",
                    "macro_population": "unweighted geometric mean across eligible target aggregates",
                    "macro_rss": "headline aggregate includes fixed-work targets only",
                },
                "inputs": input_identities,
                "normalized_rows": rows,
            },
        )
        figure_paths: list[Path] = []
        for group, metric, stem in (
            ("baseline", "performance", "allocator-baselines-performance"),
            ("baseline", "rss", "allocator-baselines-peak-rss"),
            ("feature", "performance", "unialloc-features-performance"),
            ("feature", "rss", "unialloc-features-peak-rss"),
        ):
            figure_paths.extend(save_figure(rows, group, metric, stem, stage))
        manifest_path = stage / "artifact-manifest.json"
        artifacts = [csv_path, data_path, *figure_paths]
        write_json(
            manifest_path,
            {
                "schema_version": OUTPUT_SCHEMA_VERSION,
                "suite_id": suite.suite_id,
                "png_dpi": PNG_DPI,
                "figure_groups": [
                    "allocator-baselines-performance",
                    "allocator-baselines-peak-rss",
                    "unialloc-features-performance",
                    "unialloc-features-peak-rss",
                ],
                "input_evidence": input_identities,
                "artifacts": [
                    {
                        "path": artifact.name,
                        "sha256": sha256_file(artifact),
                        "bytes": artifact.stat().st_size,
                    }
                    for artifact in artifacts
                ],
            },
        )
        publish_atomically(stage, output)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return output


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=CURRENT_SUITE_PATH)
    parser.add_argument("--micro-feature", type=Path, required=True)
    parser.add_argument("--micro-baseline", type=Path, required=True)
    parser.add_argument("--macro-feature", type=Path, required=True)
    parser.add_argument("--macro-baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        destination = export(
            suite_path=args.suite,
            micro_feature_path=args.micro_feature,
            micro_baseline_path=args.micro_baseline,
            macro_feature_path=args.macro_feature,
            macro_baseline_path=args.macro_baseline,
            output=args.output,
        )
    except EvidenceError as error:
        raise SystemExit(f"error: {error}") from error
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
