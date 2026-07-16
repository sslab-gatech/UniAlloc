#!/usr/bin/env python3
"""Run an incremental full-std_bench UniAlloc feature campaign.

The raw store is append-only at the process-record level.  Variant selection is
a mutable view until a cohort receives its immutable measurement session.  The
session binds the full ordered variant selection, so later selection changes
use a new cohort.  Raw records contain absolute time and diagnostic peak-RSS
measurements; ratios and aggregates belong to later derivation.  Every libtest
leaf chooses its own adaptive iteration count, so no std_bench variant has a
fixed-work RSS contract.

``typed_plain`` and ``typeiso_perf`` are built through the actual MIR wrapper
with policy flags 0 and 1.  The only Lifetime entry is deliberately named
``lifetime_transport_all_unknown``.  It reuses the Stage-A initializer and
force-tracked all-Unknown runtime arm.  The libtest harness has neither a
fixed-work contract nor an explicit epoch boundary, so this arm is diagnostic
transport evidence and cannot be presented as a Lifetime-aware performance arm.

Examples::

    # Pure plan: no build and no benchmark process.
    uv run python evaluation/scripts/run_std_bench_feature_campaign.py \
      --plan-only --jobs 1 --cpus 20

    # Build every selected variant and validate all 468 canonical leaves.
    uv run python evaluation/scripts/run_std_bench_feature_campaign.py \
      --build-only --jobs 1 --cpus 20

    # Collect one resumable smoke cell per selected variant.
    uv run python evaluation/scripts/run_std_bench_feature_campaign.py \
      --smoke-benchmark vec::bench_from_elem_1000 --jobs 1 --cpus 20

The absence of ``--plan-only``, ``--build-only``, and ``--smoke-benchmark``
selects the full 468-leaf campaign.  Full collection is intentionally explicit
and is never launched by the build-only validation path.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence


SCRIPT_PATH = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_PATH.parent
ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _load_script(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import campaign helper from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


full = _load_script(
    "unialloc_std_bench_full_campaign_helper",
    SCRIPT_DIR / "run_std_bench_allocator_full.py",
)
base = full.base

import realworld_type_isolation_matrix as matrix  # noqa: E402
from evaluation.scripts import immutable_evidence  # noqa: E402
from evaluation.scripts import type_isolation_suite_contract as suite_contract  # noqa: E402


PROTOCOL_SCHEMA_VERSION = 1
RAW_RECORD_SCHEMA_VERSION = 3
PROTOCOL_REVISION = "full-std-bench-feature-process-v4"
MEASUREMENT_SESSION_SCHEMA_VERSION = 2
EXPECTED_CANONICAL_BENCHMARK_COUNT = full.EXPECTED_CANONICAL_BENCHMARK_COUNT
DEFAULT_MEASURED_ROUNDS = full.MEASURED_ROUNDS
MEASUREMENT_LOCK = suite_contract.HOST_PRIMARY_MEASUREMENT_LOCK
CANONICAL_INVENTORY_PATH = ROOT / "evaluation/config/compiler_coverage_manifest.template.json"
LIFETIME_VARIANT_ID = "lifetime_transport_all_unknown"
LIFETIME_RUNTIME_ARM = "force-track-all-unknown-diagnostic"
RSS_WORK_MODEL = "workload_native_adaptive_iterations"
DEFAULT_VARIANT_IDS = (
    "unialloc",
    "typed_plain",
    "typeiso_perf",
    LIFETIME_VARIANT_ID,
)
BUILD_INPUT_PATHS = (
    "Cargo.toml",
    "Cargo.lock",
    "rust-toolchain",
    "unialloc",
    "alloc_macros",
    "tools/unialloc-rustc-pass",
)
SOURCE_COPY_PATHS = (
    "Cargo.toml",
    "Cargo.lock",
    "rust-toolchain",
    "unialloc",
    "alloc_macros",
)
DERIVED_METRIC_TOKENS = (
    "ratio",
    "normalized",
    "geomean",
    "geometric_mean",
    "median_",
    "confidence_interval",
)


class CampaignError(RuntimeError):
    """The campaign cannot preserve its measurement or evidence contract."""


@dataclass(frozen=True)
class VariantSpec:
    """One independently buildable and collectable campaign shard."""

    variant_id: str
    label: str
    cargo_features: tuple[str, ...]
    mir_policy_flags: int | None
    runtime_arm: str | None
    claim_scope: str
    fixed_work_contract: bool
    performance_claim_eligible: bool
    peak_rss_claim_eligible: bool
    rss_work_model: str
    epoch_contract: bool
    initializer_required: bool
    comparison_role: str

    @property
    def allocator(self) -> str:
        """Compatibility alias for the existing std_bench process helpers."""

        return self.variant_id

    @property
    def feature(self) -> str:
        """Cargo's comma-separated feature selector."""

        return ",".join(self.cargo_features)


VARIANT_REGISTRY: dict[str, VariantSpec] = {
    "unialloc": VariantSpec(
        variant_id="unialloc",
        label="UniAlloc Default",
        cargo_features=("bench_ourself",),
        mir_policy_flags=None,
        runtime_arm=None,
        claim_scope="default_allocator_reference",
        fixed_work_contract=False,
        performance_claim_eligible=True,
        peak_rss_claim_eligible=False,
        rss_work_model=RSS_WORK_MODEL,
        epoch_contract=False,
        initializer_required=False,
        comparison_role="reference",
    ),
    "typed_plain": VariantSpec(
        variant_id="typed_plain",
        label="UniAlloc + Compiler Transport (Policy Off)",
        cargo_features=("bench_ourself", "type_isolation"),
        mir_policy_flags=0,
        runtime_arm=None,
        claim_scope="compiler_transport_control",
        fixed_work_contract=False,
        performance_claim_eligible=True,
        peak_rss_claim_eligible=False,
        rss_work_model=RSS_WORK_MODEL,
        epoch_contract=False,
        initializer_required=False,
        comparison_role="compiler_route_control",
    ),
    "typeiso_perf": VariantSpec(
        variant_id="typeiso_perf",
        label="UniAlloc + Type Isolation",
        cargo_features=("bench_ourself", "type_isolation"),
        mir_policy_flags=1,
        runtime_arm=None,
        claim_scope="type_isolation_performance",
        fixed_work_contract=False,
        performance_claim_eligible=True,
        peak_rss_claim_eligible=False,
        rss_work_model=RSS_WORK_MODEL,
        epoch_contract=False,
        initializer_required=False,
        comparison_role="policy_subject",
    ),
    LIFETIME_VARIANT_ID: VariantSpec(
        variant_id=LIFETIME_VARIANT_ID,
        label="Lifetime Transport / All-Unknown Diagnostic",
        cargo_features=("bench_ourself", "lifetime_hugepage"),
        mir_policy_flags=0,
        runtime_arm=LIFETIME_RUNTIME_ARM,
        claim_scope="transport_all_unknown_diagnostic",
        fixed_work_contract=False,
        performance_claim_eligible=False,
        peak_rss_claim_eligible=False,
        rss_work_model=RSS_WORK_MODEL,
        epoch_contract=False,
        initializer_required=True,
        comparison_role="diagnostic_only",
    ),
}


_lifetime_module: ModuleType | None = None


def variant_claim_contract(variant: VariantSpec) -> dict[str, Any]:
    """Return the claim boundary persisted with every campaign artifact."""

    return {
        "fixed_work_contract": variant.fixed_work_contract,
        "performance_claim_eligible": variant.performance_claim_eligible,
        "peak_rss_claim_eligible": variant.peak_rss_claim_eligible,
        "rss_work_model": variant.rss_work_model,
    }


def lifetime_campaign() -> ModuleType:
    """Load the authoritative Stage-A initializer lazily."""

    global _lifetime_module
    if _lifetime_module is None:
        _lifetime_module = _load_script(
            "unialloc_lifetime_prior_six_program_helper",
            SCRIPT_DIR / "lifetime_prior_six_program_campaign.py",
        )
    return _lifetime_module


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return base.sha256_file(path)


def transitive_evaluator_digests(
    seed_paths: Sequence[Path], *, search_root: Path = SCRIPT_DIR
) -> dict[str, str]:
    """Digest the complete local Python import closure of the evaluator."""

    root = search_root.resolve()
    pending = [path.resolve() for path in seed_paths]
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited or not path.is_file():
            continue
        visited.add(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as error:
            raise CampaignError(f"cannot inspect evaluator dependency: {path}") from error
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").rsplit(".", 1)[-1]
                if module and module != "scripts":
                    modules.add(module)
                if node.module in {"evaluation.scripts", "scripts"}:
                    modules.update(alias.name for alias in node.names)
        for module in modules:
            candidate = root / f"{module}.py"
            if candidate.is_file() and candidate.resolve() not in visited:
                pending.append(candidate.resolve())
    result: dict[str, str] = {}
    for path in sorted(visited):
        try:
            key = str(path.relative_to(ROOT))
        except ValueError:
            key = str(path.relative_to(root))
        result[key] = immutable_evidence.sha256_file(path)
    return result


class ProcessGroupRegistry:
    """Track all active benchmark process groups across worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[Any]] = {}

    def add(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            self._processes[process.pid] = process

    def discard(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            self._processes.pop(process.pid, None)

    def terminate_all(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            terminate_and_wait_process_group(process)


ACTIVE_PROCESS_GROUPS = ProcessGroupRegistry()


def terminate_and_wait_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
    else:
        process.wait()


@contextlib.contextmanager
def cleanup_signal_handlers(
    registry: ProcessGroupRegistry = ACTIVE_PROCESS_GROUPS,
) -> Iterator[None]:
    """Terminate active worker groups before SIGTERM unwinds the campaign."""

    previous = signal.getsignal(signal.SIGTERM)

    def handler(_signum: int, _frame: Any) -> None:
        registry.terminate_all()
        raise KeyboardInterrupt("SIGTERM")

    signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def await_worker_futures(
    futures: Sequence[Any],
    *,
    stop: threading.Event,
    registry: ProcessGroupRegistry = ACTIVE_PROCESS_GROUPS,
) -> None:
    """Fail closed and reap every process group before pool shutdown waits."""

    try:
        for future in as_completed(futures):
            future.result()
    except BaseException:
        stop.set()
        for pending in futures:
            pending.cancel()
        registry.terminate_all()
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Atomically replace a mutable derived artifact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def persist_immutable_bytes(path: Path, payload: bytes, *, label: str) -> Path:
    """Create one immutable shard, accepting an exact idempotent retry."""
    try:
        return immutable_evidence.persist_immutable_bytes(path, payload).path
    except immutable_evidence.ImmutableEvidenceError as error:
        raise RuntimeError(f"{label}: {error}") from error


def persist_immutable_json(path: Path, value: Any, *, label: str) -> Path:
    return persist_immutable_bytes(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode(),
        label=label,
    )


def validate_variant_registry(
    registry: Mapping[str, VariantSpec],
) -> dict[str, VariantSpec]:
    """Validate the complete data-driven registry and scientific labels."""

    required = set(DEFAULT_VARIANT_IDS)
    missing = sorted(required - set(registry))
    if missing:
        raise RuntimeError("variant registry is incomplete: " + ",".join(missing))
    result: dict[str, VariantSpec] = {}
    for variant_id, variant in registry.items():
        if variant.variant_id != variant_id:
            raise RuntimeError(f"variant registry key mismatch for {variant_id}")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", variant_id):
            raise RuntimeError(f"unsafe variant id: {variant_id}")
        if "bench_ourself" not in variant.cargo_features:
            raise RuntimeError(f"{variant_id} does not select UniAlloc")
        if len(variant.cargo_features) != len(set(variant.cargo_features)):
            raise RuntimeError(f"{variant_id} has duplicate Cargo features")
        if variant.mir_policy_flags not in (None, 0, 1):
            raise RuntimeError(f"{variant_id} has an unsupported MIR policy flag")
        result[variant_id] = variant

    if result["unialloc"].mir_policy_flags is not None:
        raise RuntimeError("unialloc must bypass the MIR wrapper")
    if result["typed_plain"].mir_policy_flags != 0:
        raise RuntimeError("typed_plain must use MIR policy flags 0")
    if result["typeiso_perf"].mir_policy_flags != 1:
        raise RuntimeError("typeiso_perf must use MIR policy flags 1")

    performance_eligible = {"unialloc", "typed_plain", "typeiso_perf"}
    for variant_id, variant in result.items():
        if variant.fixed_work_contract:
            raise RuntimeError("std_bench variants cannot claim a fixed-work contract")
        if variant.performance_claim_eligible != (variant_id in performance_eligible):
            raise RuntimeError(
                f"{variant_id} has the wrong performance claim eligibility"
            )
        if variant.peak_rss_claim_eligible:
            raise RuntimeError("std_bench peak RSS must remain diagnostic")
        if variant.rss_work_model != RSS_WORK_MODEL:
            raise RuntimeError(f"{variant_id} has the wrong RSS work model")

    lifetime = result[LIFETIME_VARIANT_ID]
    if (
        lifetime.runtime_arm != LIFETIME_RUNTIME_ARM
        or lifetime.mir_policy_flags != 0
        or not lifetime.initializer_required
    ):
        raise RuntimeError("Lifetime diagnostic does not use the all-Unknown initializer arm")
    if lifetime.epoch_contract:
        raise RuntimeError("std_bench Lifetime arm cannot claim an epoch contract")
    if lifetime.claim_scope != "transport_all_unknown_diagnostic":
        raise RuntimeError("Lifetime std_bench arm must remain diagnostic")
    if "lifetime-aware" in lifetime.label.lower():
        raise RuntimeError("diagnostic Lifetime arm is mislabeled Lifetime-aware")
    return result


validate_variant_registry(VARIANT_REGISTRY)


def validate_variant_selection(variant_ids: Sequence[str]) -> tuple[str, ...]:
    """Return one complete, ordered variant selection or fail closed."""

    selected = tuple(variant_ids)
    if not selected or len(selected) != len(set(selected)):
        raise CampaignError("variant selection must contain unique variants")
    unknown = sorted(set(selected) - set(VARIANT_REGISTRY))
    if unknown:
        raise CampaignError(
            "variant selection contains unknown variants: " + ",".join(unknown)
        )
    return selected


def parse_variant_ids(value: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    try:
        return validate_variant_selection(values)
    except CampaignError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def validate_cohort_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise argparse.ArgumentTypeError("cohort id contains unsafe characters")
    return value


def load_canonical_inventory(path: Path = CANONICAL_INVENTORY_PATH) -> tuple[str, ...]:
    value = json.loads(path.read_text(encoding="utf-8"))
    suite = value.get("benchmark_suite") if isinstance(value, dict) else None
    names = suite.get("expected_benchmarks") if isinstance(suite, dict) else None
    if not isinstance(names, list):
        raise CampaignError("canonical std_bench manifest has no expected benchmark list")
    return full.validate_canonical_inventory(tuple(str(name) for name in names))


def protocol_digest(protocol: Mapping[str, Any]) -> str:
    payload = {
        "schema_version": protocol.get("schema_version"),
        "compatibility": protocol.get("compatibility"),
    }
    return sha256_bytes(canonical_json_bytes(payload))


def ensure_campaign_protocol(
    output_dir: Path, protocol: Mapping[str, Any]
) -> dict[str, Any]:
    """Persist or revalidate the campaign-wide compatibility contract."""

    if protocol.get("schema_version") != PROTOCOL_SCHEMA_VERSION:
        raise RuntimeError("unsupported campaign protocol schema")
    compatibility = protocol.get("compatibility")
    if not isinstance(compatibility, dict):
        raise RuntimeError("campaign protocol compatibility object is missing")
    expected = dict(protocol)
    expected["protocol_sha256"] = protocol_digest(expected)
    path = output_dir / "campaign-protocol.json"
    if path.exists():
        immutable_evidence.validate_committed_file(path)
        existing = json.loads(path.read_text(encoding="utf-8"))
        if (
            existing.get("schema_version") != expected["schema_version"]
            or existing.get("compatibility") != expected["compatibility"]
            or existing.get("protocol_sha256") != protocol_digest(existing)
        ):
            raise RuntimeError(
                "campaign protocol mismatch; select a new output directory or cohort epoch"
            )
        return existing
    expected.setdefault("created_utc", utc_now())
    persist_immutable_json(path, expected, label="campaign protocol")
    return expected


def ensure_measurement_session(
    output_dir: Path,
    *,
    protocol: Mapping[str, Any],
    cohort_id: str,
    variant_ids: Sequence[str],
) -> dict[str, Any]:
    """Create one fresh immutable session and UniAlloc anchor contract per cohort."""

    cohort_id = validate_cohort_id(cohort_id)
    selected = validate_variant_selection(variant_ids)
    path = output_dir / "sessions" / cohort_id / "measurement-session.json"
    raw_root = output_dir / "raw" / cohort_id
    existing_records = list(raw_root.glob("*/**/record.json")) if raw_root.exists() else []
    if not path.is_file() and existing_records:
        raise CampaignError(
            "raw cohort records exist without an immutable measurement session"
        )
    if path.is_file():
        immutable_evidence.validate_committed_file(path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CampaignError("measurement session record is unreadable") from error
    else:
        value = {
            "schema_version": MEASUREMENT_SESSION_SCHEMA_VERSION,
            "protocol_sha256": protocol["protocol_sha256"],
            "cohort_id": cohort_id,
            "measurement_session_id": str(uuid.uuid4()),
            "anchor_variant_id": "unialloc",
            "anchor_contract": "fresh-unialloc-process-before-each-comparison-cell",
            "variant_ids": list(selected),
        }
        immutable_evidence.persist_immutable_json(path, value)
    expected = {
        "schema_version": MEASUREMENT_SESSION_SCHEMA_VERSION,
        "protocol_sha256": protocol["protocol_sha256"],
        "cohort_id": cohort_id,
        "anchor_variant_id": "unialloc",
        "anchor_contract": "fresh-unialloc-process-before-each-comparison-cell",
        "variant_ids": list(selected),
    }
    if (
        value.get("schema_version") != MEASUREMENT_SESSION_SCHEMA_VERSION
        or value.get("variant_ids") != list(selected)
    ):
        raise CampaignError(
            "measurement session variant selection differs from the requested cohort"
        )
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise CampaignError("measurement session differs from its cohort protocol")
    try:
        parsed = uuid.UUID(str(value.get("measurement_session_id")))
    except (ValueError, AttributeError) as error:
        raise CampaignError("measurement session id is invalid") from error
    if str(parsed) != value["measurement_session_id"]:
        raise CampaignError("measurement session id is not canonical")
    return dict(value)


def validate_existing_session_selection(
    output_dir: Path, *, cohort_id: str, variant_ids: Sequence[str]
) -> None:
    """Reject cohort selection drift before launching any external process."""

    cohort_id = validate_cohort_id(cohort_id)
    selected = validate_variant_selection(variant_ids)
    path = output_dir / "sessions" / cohort_id / "measurement-session.json"
    if not path.is_file():
        return
    immutable_evidence.validate_committed_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CampaignError("measurement session record is unreadable") from error
    if (
        value.get("schema_version") != MEASUREMENT_SESSION_SCHEMA_VERSION
        or value.get("variant_ids") != list(selected)
    ):
        raise CampaignError(
            "measurement session variant selection differs from the requested cohort"
        )


def persist_selection(
    output_dir: Path, *, cohort_id: str, variant_ids: Sequence[str]
) -> Path:
    """Replace a view manifest without modifying any raw shard."""

    cohort_id = validate_cohort_id(cohort_id)
    selected = validate_variant_selection(variant_ids)
    path = output_dir / "views" / cohort_id / "selection.json"
    value = {
        "schema_version": 1,
        "cohort_id": cohort_id,
        "variant_ids": list(selected),
        "updated_utc": utc_now(),
        "raw_data_mutated": False,
    }
    atomic_write_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())
    return path


def benchmark_slug(benchmark: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "__", benchmark).strip("_")
    return f"{readable[:180]}-{sha256_bytes(benchmark.encode())[:12]}"


def raw_record_path(output_dir: Path, record: Mapping[str, Any]) -> Path:
    phase = str(record["phase"])
    round_index = int(record["round"])
    return (
        output_dir
        / "raw"
        / str(record["cohort_id"])
        / str(record["variant_id"])
        / benchmark_slug(str(record["benchmark"]))
        / phase
        / f"round-{round_index:02d}"
        / "record.json"
    )


def validate_raw_record(record: Mapping[str, Any]) -> None:
    """Reject derived data and malformed absolute process evidence."""

    if record.get("schema_version") != RAW_RECORD_SCHEMA_VERSION:
        raise RuntimeError("raw record schema mismatch")
    for key in record:
        lowered = str(key).lower()
        if any(token in lowered for token in DERIVED_METRIC_TOKENS):
            raise RuntimeError(f"derived metric is forbidden in raw records: {key}")
    required_identity = {
        "protocol_sha256",
        "cohort_id",
        "measurement_session_id",
        "measurement_anchor",
        "variant_id",
        "allocator",
        "benchmark",
        "phase",
        "round",
        "status",
        "valid",
        "timed_out",
        "timeout_seconds",
        "cpu",
        "numa_node",
        "binary_sha256",
        "fixed_work_contract",
        "performance_claim_eligible",
        "peak_rss_claim_eligible",
        "rss_work_model",
    }
    missing = sorted(required_identity - set(record))
    if missing:
        raise RuntimeError("raw record is missing identity fields: " + ",".join(missing))
    if record["variant_id"] != record["allocator"]:
        raise RuntimeError("raw record variant identity is inconsistent")
    if record.get("measurement_anchor") is not (
        record["variant_id"] == "unialloc"
    ):
        raise RuntimeError("raw record measurement-anchor identity is inconsistent")
    if record["variant_id"] not in VARIANT_REGISTRY:
        raise RuntimeError("raw record uses an unknown variant")
    try:
        session_id = str(uuid.UUID(str(record["measurement_session_id"])))
    except (ValueError, AttributeError) as error:
        raise RuntimeError("raw record measurement session id is invalid") from error
    if session_id != record["measurement_session_id"]:
        raise RuntimeError("raw record measurement session id is not canonical")
    variant = VARIANT_REGISTRY[str(record["variant_id"])]
    claim_contract = variant_claim_contract(variant)
    mismatched_contract = []
    for field, expected in claim_contract.items():
        observed = record.get(field)
        if isinstance(expected, bool):
            matches = observed is expected
        else:
            matches = observed == expected
        if not matches:
            mismatched_contract.append(field)
    if mismatched_contract:
        raise RuntimeError(
            "raw record claim contract mismatch: " + ",".join(mismatched_contract)
        )
    if record["phase"] not in {"warmup", "measured"}:
        raise RuntimeError("raw record phase is invalid")
    if record["phase"] == "warmup" and int(record["round"]) != 0:
        raise RuntimeError("warmup raw record must use round zero")
    if record["phase"] == "measured" and int(record["round"]) <= 0:
        raise RuntimeError("measured raw record must use a positive round")
    valid = record.get("valid") is True and record.get("timed_out") is False
    timeout = record.get("valid") is not True and record.get("timed_out") is True
    if valid:
        if record.get("status") != "valid":
            raise RuntimeError("valid raw record has the wrong status")
        for field in (
            "ns_per_iter",
            "peak_rss_kib",
            "wall_seconds",
            "user_seconds",
            "system_seconds",
        ):
            value = record.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RuntimeError(f"valid raw record lacks absolute metric {field}")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise RuntimeError(f"absolute metric {field} is invalid")
    elif timeout:
        if record.get("status") != "timeout_censored":
            raise RuntimeError("timeout raw record has the wrong status")
    else:
        raise RuntimeError("raw record is neither valid nor timeout-censored")


def persist_raw_record(output_dir: Path, record: Mapping[str, Any]) -> Path:
    validate_raw_record(record)
    return persist_immutable_json(
        raw_record_path(output_dir, record),
        dict(record),
        label="immutable raw record",
    )


def validate_selected_variant_completeness(
    variant_ids: Sequence[str],
    manifests: Mapping[str, Mapping[str, Any]],
    canonical_benchmarks: Sequence[str],
) -> None:
    """Require every selected build to expose the complete canonical surface."""

    canonical = full.validate_canonical_inventory(tuple(canonical_benchmarks))
    for variant_id in variant_ids:
        manifest = manifests.get(variant_id)
        if not isinstance(manifest, Mapping):
            raise RuntimeError(f"variant {variant_id} is missing its inventory manifest")
        observed = tuple(str(value) for value in manifest.get("canonical_benchmarks", ()))
        if (
            manifest.get("success") is not True
            or manifest.get("variant_id") != variant_id
            or observed != canonical
        ):
            raise RuntimeError(
                f"variant {variant_id} does not expose the complete 468-leaf inventory"
            )


def command_output(command: Sequence[str], *, cwd: Path) -> str:
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ).stdout


def validate_clean_implementation_source(source_root: Path) -> dict[str, Any]:
    """Bind allocator, benchmark, and wrapper inputs to the current Git HEAD."""

    inside = command_output(["git", "rev-parse", "--is-inside-work-tree"], cwd=source_root)
    if inside.strip() != "true":
        raise CampaignError("source root is not a Git worktree")
    status = command_output(
        ["git", "status", "--porcelain", "--", *BUILD_INPUT_PATHS], cwd=source_root
    )
    if status:
        raise CampaignError(
            "allocator, std_bench, or MIR-wrapper inputs differ from clean dev HEAD:\n"
            + status
        )
    head = command_output(["git", "rev-parse", "HEAD"], cwd=source_root).strip()
    branch = command_output(["git", "branch", "--show-current"], cwd=source_root).strip()
    objects = {
        path: command_output(["git", "rev-parse", f"HEAD:{path}"], cwd=source_root).strip()
        for path in BUILD_INPUT_PATHS
    }
    return {
        "source_head": head,
        "source_branch": branch,
        "source_status_for_build_inputs": "",
        "git_objects": objects,
    }


def clean_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(source or os.environ)
    exact = {
        "CARGO_ENCODED_RUSTFLAGS",
        "GLIBC_TUNABLES",
        "LD_PRELOAD",
        "MALLOC_ARENA_MAX",
        "MALLOC_CONF",
        "RUSTC_WRAPPER",
        "RUSTC_WORKSPACE_WRAPPER",
        "RUSTFLAGS",
        "SCUDO_OPTIONS",
    }
    for name in list(env):
        if name in exact or name.startswith(("UNIALLOC_", "MIMALLOC_", "TCMALLOC_")):
            env.pop(name, None)
    return env


def variant_build_environment(
    variant: VariantSpec,
    *,
    base_environment: Mapping[str, str] | None,
    wrapper: Path,
    sysroot: Path,
    audit_dir: Path,
    pass_log_dir: Path,
    target_dir: Path,
    temporary_dir: Path,
) -> dict[str, str]:
    """Create the exact build environment for one registry entry."""

    env = clean_environment(base_environment)
    audit_dir.mkdir(parents=True, exist_ok=True)
    pass_log_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    temporary_dir.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "CARGO_TARGET_DIR": str(target_dir.resolve()),
            "CARGO_INCREMENTAL": "0",
            "CARGO_PROFILE_RELEASE_STRIP": "none",
            "TMPDIR": str(temporary_dir.resolve()),
        }
    )
    if variant.mir_policy_flags is not None:
        env = matrix.typeiso_environment(
            env,
            wrapper=wrapper,
            audit_dir=audit_dir,
            pass_log_dir=pass_log_dir,
            sysroot=sysroot,
            target_crates=("std_bench",),
            policy_flags=variant.mir_policy_flags,
        )
    if "UNIALLOC_AUTO_RUST_LIFETIME_PRIOR" in env:
        raise CampaignError("std_bench diagnostic unexpectedly enables a compiler prior")
    return env


def variant_runtime_environment(
    variant: VariantSpec, temporary_dir: Path
) -> dict[str, str]:
    overrides = {
        "UNIALLOC_STD_BENCH_DISABLE_TYPE_STATS": "1",
        "UNIALLOC_STD_BENCH_DISABLE_AGGREGATE_STATS": "1",
    }
    if variant.runtime_arm is not None:
        overrides["UNIALLOC_LIFETIME_EXPERIMENT_ARM"] = variant.runtime_arm
    return suite_contract.clean_runtime_environment(
        temporary_dir, overrides=overrides
    )


def copy_build_source(source_root: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for relative in SOURCE_COPY_PATHS:
        source = source_root / relative
        target = destination / relative
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns("target", "__pycache__", "*.pyc"),
            )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def lifetime_instrumentation_source() -> str:
    """Return the exact initializer/reporter source used by Stage A."""

    source = lifetime_campaign().runtime_instrumentation_rust()
    required = (
        "UNIALLOC_LIFETIME_EXPERIMENT_INITIALIZER",
        "lifetime_hugepage_configure(policy)",
        LIFETIME_RUNTIME_ARM,
    )
    if any(marker not in source for marker in required):
        raise CampaignError("authoritative Lifetime initializer contract changed")
    return source


def materialize_variant_source(
    source_root: Path,
    build_root: Path,
    variant: VariantSpec,
    source_identity: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    source = build_root / "source"
    copy_build_source(source_root, source)
    initializer_sha256: str | None = None
    if variant.initializer_required:
        instrumentation = lifetime_instrumentation_source()
        initializer_sha256 = sha256_bytes(instrumentation.encode())
        bench = source / "unialloc/benches/lib.rs"
        marker = "// Full std_bench all-Unknown Lifetime diagnostic initializer."
        text = bench.read_text(encoding="utf-8")
        if marker in text:
            raise CampaignError("Lifetime instrumentation was already injected")
        bench.write_text(
            text.rstrip() + "\n\n" + marker + "\n" + instrumentation + "\n",
            encoding="utf-8",
        )
    manifest = {
        "schema_version": 1,
        "source_head": source_identity["source_head"],
        "source_git_objects": source_identity["git_objects"],
        "variant_id": variant.variant_id,
        "initializer_required": variant.initializer_required,
        "initializer_source_sha256": initializer_sha256,
        "derived_std_bench_sha256": sha256_file(source / "unialloc/benches/lib.rs"),
    }
    persist_immutable_json(build_root / "source.json", manifest, label="variant source manifest")
    return source, manifest


def persist_variant_manifest(
    output_dir: Path,
    variant: VariantSpec,
    *,
    source_identity: Mapping[str, Any],
    toolchain: str,
) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        **asdict(variant),
        "source_head": source_identity["source_head"],
        "toolchain": toolchain,
        "raw_metrics": ["ns_per_iter", "peak_rss_kib", "wall_seconds"],
        "derived_metrics_stored": False,
        "peak_rss_interpretation": "diagnostic_only",
        "scientific_boundary": (
            "libtest adaptive iterations with no explicit UniAlloc epoch; "
            "transport and all-Unknown runtime diagnostic only"
            if variant.variant_id == LIFETIME_VARIANT_ID
            else None
        ),
    }
    value["variant_manifest_sha256"] = sha256_bytes(canonical_json_bytes(value))
    path = output_dir / "variants" / f"{variant.variant_id}.json"
    persist_immutable_json(path, value, label="variant manifest")
    return value


def resolved_features(source_root: Path, env: Mapping[str, str], feature: str) -> list[str]:
    result = subprocess.run(
        [
            "cargo",
            "metadata",
            "--locked",
            "--format-version",
            "1",
            "--features",
            feature,
        ],
        cwd=source_root,
        env=dict(env),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return base.resolved_unialloc_features(json.loads(result.stdout), source_root)


def validate_build_cache(
    record_path: Path,
    record: Mapping[str, Any],
    *,
    build_root: Path,
    variant: VariantSpec,
    source_identity: Mapping[str, Any],
    variant_manifest: Mapping[str, Any],
    toolchain: str,
    wrapper: Path,
) -> None:
    immutable_evidence.validate_committed_file(record_path)
    expected = {
        "schema_version": 2,
        "success": True,
        "variant_id": variant.variant_id,
        "variant_manifest_sha256": variant_manifest["variant_manifest_sha256"],
        "source_head": source_identity["source_head"],
        "toolchain": toolchain,
        "actual_mir_wrapper": variant.mir_policy_flags is not None,
        "mir_policy_flags": variant.mir_policy_flags,
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise CampaignError(f"cached build identity failed for {variant.variant_id}")
    artifacts = record.get("artifacts")
    required = {"build_stdout", "build_stderr", "binary", "source_manifest"}
    if variant.mir_policy_flags is not None:
        required.add("wrapper")
    if not isinstance(artifacts, dict) or set(artifacts) != required:
        raise CampaignError(f"cached build artifacts failed for {variant.variant_id}")
    paths = {
        name: immutable_evidence.validate_artifact_ref(
            artifacts[name], context=f"cached build {variant.variant_id}/{name}"
        )
        for name in required
    }
    if (
        paths["binary"] != Path(str(record.get("binary", ""))).resolve()
        or record.get("binary_sha256") != artifacts["binary"]["sha256"]
        or record.get("build_stdout_sha256")
        != artifacts["build_stdout"]["sha256"]
        or record.get("build_stderr_sha256")
        != artifacts["build_stderr"]["sha256"]
        or paths["source_manifest"] != (build_root / "source.json").resolve()
    ):
        raise CampaignError(f"cached build digests failed for {variant.variant_id}")
    try:
        parsed_binary = base.parse_cargo_executable(
            paths["build_stdout"].read_bytes()
        ).resolve()
        source_manifest = json.loads(
            paths["source_manifest"].read_text(encoding="utf-8")
        )
    except Exception as error:
        raise CampaignError(
            f"cached build evidence is invalid for {variant.variant_id}"
        ) from error
    if parsed_binary != paths["binary"] or source_manifest != record.get(
        "source_manifest"
    ):
        raise CampaignError(
            f"cached build derivation failed for {variant.variant_id}"
        )
    derived_bench = build_root / "source/unialloc/benches/lib.rs"
    if (
        not derived_bench.is_file()
        or source_manifest.get("derived_std_bench_sha256")
        != sha256_file(derived_bench)
    ):
        raise CampaignError(
            f"cached build source artifact failed for {variant.variant_id}"
        )
    if variant.mir_policy_flags is not None:
        if (
            paths["wrapper"] != wrapper.resolve()
            or record.get("wrapper_sha256") != artifacts["wrapper"]["sha256"]
            or record.get("compiler_audit")
            != matrix.summarize_audits(build_root / "audits")
        ):
            raise CampaignError(
                f"cached build compiler artifacts failed for {variant.variant_id}"
            )


def build_variant(
    source_root: Path,
    output_dir: Path,
    variant: VariantSpec,
    *,
    source_identity: Mapping[str, Any],
    toolchain: str,
    wrapper: Path,
    sysroot: Path,
    build_timeout: int,
) -> dict[str, Any]:
    """Build or validate one independent variant artifact."""

    variant_manifest = persist_variant_manifest(
        output_dir, variant, source_identity=source_identity, toolchain=toolchain
    )
    build_root = output_dir / "builds" / variant.variant_id
    record_path = build_root / "build.json"
    if record_path.is_file():
        cached = json.loads(record_path.read_text(encoding="utf-8"))
        validate_build_cache(
            record_path,
            cached,
            build_root=build_root,
            variant=variant,
            source_identity=source_identity,
            variant_manifest=variant_manifest,
            toolchain=toolchain,
            wrapper=wrapper,
        )
        return cached

    build_root.mkdir(parents=True, exist_ok=True)
    derived_source, source_manifest = materialize_variant_source(
        source_root, build_root, variant, source_identity
    )
    target_dir = build_root / "target"
    audit_dir = build_root / "audits"
    pass_log_dir = build_root / "pass-logs"
    temporary_dir = output_dir / "tmp"
    for generated in (target_dir, audit_dir, pass_log_dir):
        shutil.rmtree(generated, ignore_errors=True)
    env = variant_build_environment(
        variant,
        base_environment=os.environ,
        wrapper=wrapper,
        sysroot=sysroot,
        audit_dir=audit_dir,
        pass_log_dir=pass_log_dir,
        target_dir=target_dir,
        temporary_dir=temporary_dir,
    )
    command = [
        "cargo",
        f"+{toolchain}",
        "bench",
        "-p",
        "unialloc",
        "--bench",
        "std_bench",
        "--features",
        variant.feature,
        "--no-run",
        "--locked",
        "--message-format=json-render-diagnostics",
    ]
    started = utc_now()
    try:
        result = subprocess.run(
            command,
            cwd=derived_source,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=build_timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise CampaignError(f"build timed out for {variant.variant_id}") from error
    atomic_write_bytes(build_root / "build.stdout", result.stdout)
    atomic_write_bytes(build_root / "build.stderr", result.stderr)
    if result.returncode != 0:
        raise CampaignError(
            f"build failed for {variant.variant_id} ({result.returncode}):\n"
            + result.stderr.decode("utf-8", errors="replace")[-8000:]
        )
    binary = base.parse_cargo_executable(result.stdout)
    features = resolved_features(derived_source, env, variant.feature)
    selected_allocators = base.ALL_ALLOCATOR_SELECTORS.intersection(features)
    if selected_allocators != {"bench_ourself"}:
        raise CampaignError(
            f"{variant.variant_id} resolved unexpected allocators: {sorted(selected_allocators)}"
        )
    if not set(variant.cargo_features).issubset(features):
        raise CampaignError(f"{variant.variant_id} omitted requested Cargo features")

    audit: dict[str, Any] | None = None
    lifetime_audit: dict[str, Any] | None = None
    if variant.mir_policy_flags is not None:
        audit = matrix.summarize_audits(audit_dir)
        try:
            matrix.validate_typeiso_audits(audit, ("std_bench",))
        except Exception as error:
            raise CampaignError(
                f"actual MIR audit failed for {variant.variant_id}: {error}"
            ) from error
        if variant.variant_id == LIFETIME_VARIANT_ID:
            lifetime = lifetime_campaign()
            lifetime_audit = lifetime.summarize_compiler_prior_audits(audit_dir)
            lifetime.validate_build_audit_for_arm(
                lifetime.ARM_BY_NAME[LIFETIME_RUNTIME_ARM],
                lifetime_audit,
                ("std_bench",),
            )
            if lifetime_audit.get("automatic_rust_lifetime_prior_enabled") is not False:
                raise CampaignError("Lifetime std_bench build is not all-Unknown")

    record = {
        "schema_version": 2,
        "success": True,
        "variant_id": variant.variant_id,
        "variant_manifest_sha256": variant_manifest["variant_manifest_sha256"],
        "source_head": source_identity["source_head"],
        "source_manifest": source_manifest,
        "toolchain": toolchain,
        "build_started_utc": started,
        "build_finished_utc": utc_now(),
        "command": command,
        "build_stdout_sha256": sha256_bytes(result.stdout),
        "build_stderr_sha256": sha256_bytes(result.stderr),
        "binary": str(binary.resolve()),
        "binary_sha256": sha256_file(binary),
        "cargo_features": list(variant.cargo_features),
        "resolved_unialloc_features": features,
        "actual_mir_wrapper": variant.mir_policy_flags is not None,
        "mir_policy_flags": variant.mir_policy_flags,
        "wrapper": str(wrapper.resolve()) if variant.mir_policy_flags is not None else None,
        "wrapper_sha256": (
            sha256_file(wrapper) if variant.mir_policy_flags is not None else None
        ),
        "compiler_audit": audit,
        "lifetime_all_unknown_audit": lifetime_audit,
    }
    artifacts = {
        "build_stdout": immutable_evidence.artifact_ref(
            build_root / "build.stdout"
        ),
        "build_stderr": immutable_evidence.artifact_ref(
            build_root / "build.stderr"
        ),
        "binary": immutable_evidence.artifact_ref(binary),
        "source_manifest": immutable_evidence.artifact_ref(
            build_root / "source.json"
        ),
    }
    if variant.mir_policy_flags is not None:
        artifacts["wrapper"] = immutable_evidence.artifact_ref(wrapper)
    record["artifacts"] = artifacts
    persist_immutable_json(record_path, record, label="variant build record")
    return record


def parse_prefixed_json(stderr: str, prefix: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line in stderr.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            value = json.loads(line[len(prefix) :])
        except json.JSONDecodeError as error:
            raise CampaignError(f"invalid runtime JSON after {prefix}") from error
        if not isinstance(value, dict):
            raise CampaignError(f"runtime record after {prefix} is not an object")
        values.append(value)
    return values


def validate_lifetime_runtime(stderr: str) -> dict[str, Any]:
    """Prove that the authoritative all-Unknown diagnostic arm ran."""

    lifetime = lifetime_campaign()
    stats = lifetime.runtime_lifetime.parse_runtime_stats(stderr)
    sites = lifetime.runtime_lifetime.parse_runtime_site_rows(stderr)
    if not isinstance(stats, dict):
        raise CampaignError("Lifetime diagnostic emitted no runtime stats")
    lifetime.validate_runtime_evidence(
        lifetime.ARM_BY_NAME[LIFETIME_RUNTIME_ARM], stats, sites
    )
    non_unknown = sum(int(row.get("latest_static_prior", 0)) != 0 for row in sites)
    if non_unknown:
        raise CampaignError("Lifetime diagnostic observed a non-Unknown static prior")
    fragmentation = parse_prefixed_json(stderr, lifetime.FRAGMENTATION_PREFIX)
    mechanism = parse_prefixed_json(stderr, lifetime.MECHANISM_PREFIX)
    if len(fragmentation) != 1 or len(mechanism) != 1:
        raise CampaignError("Lifetime diagnostic emitted incomplete initializer telemetry")
    return {
        "runtime_arm": LIFETIME_RUNTIME_ARM,
        "policy": stats.get("policy"),
        "adaptive_force_track_all": stats.get("adaptive_force_track_all"),
        "runtime_site_count": len(sites),
        "non_unknown_static_prior_count": non_unknown,
        "runtime_stats_sha256": sha256_bytes(canonical_json_bytes(stats)),
        "runtime_sites_sha256": sha256_bytes(canonical_json_bytes(sites)),
        "fragmentation_sha256": sha256_bytes(canonical_json_bytes(fragmentation[0])),
        "mechanism_sha256": sha256_bytes(canonical_json_bytes(mechanism[0])),
        "fixed_work_contract": False,
        "epoch_contract": False,
        "claim_scope": "transport_all_unknown_diagnostic",
    }


def validate_inventory_cache(
    path: Path,
    record: Mapping[str, Any],
    *,
    variant: VariantSpec,
    build: Mapping[str, Any],
    canonical_benchmarks: Sequence[str],
) -> None:
    immutable_evidence.validate_committed_file(path)
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "stdout",
        "stderr",
        "binary",
    }:
        raise CampaignError(f"cached inventory artifacts failed for {variant.variant_id}")
    paths = {
        name: immutable_evidence.validate_artifact_ref(
            artifacts[name], context=f"cached inventory {variant.variant_id}/{name}"
        )
        for name in artifacts
    }
    if (
        record.get("schema_version") != 2
        or record.get("success") is not True
        or record.get("variant_id") != variant.variant_id
        or record.get("binary_sha256") != build.get("binary_sha256")
        or paths["binary"] != Path(str(build.get("binary", ""))).resolve()
        or record.get("stdout_sha256") != artifacts["stdout"]["sha256"]
        or record.get("stderr_sha256") != artifacts["stderr"]["sha256"]
    ):
        raise CampaignError(f"cached inventory identity failed for {variant.variant_id}")
    stdout = paths["stdout"].read_bytes()
    stderr = paths["stderr"].read_bytes()
    observed = tuple(base.parse_inventory(stdout))
    canonical = tuple(canonical_benchmarks)
    lifetime_activation = (
        validate_lifetime_runtime(stderr.decode("utf-8", errors="replace"))
        if variant.variant_id == LIFETIME_VARIANT_ID
        else None
    )
    if (
        tuple(record.get("canonical_benchmarks", ())) != canonical
        or not set(canonical).issubset(observed)
        or record.get("observed_benchmark_count") != len(observed)
        or record.get("observed_benchmarks_sha256")
        != sha256_bytes(("\n".join(observed) + "\n").encode())
        or record.get("lifetime_runtime_activation") != lifetime_activation
    ):
        raise CampaignError(
            f"cached inventory derivation failed for {variant.variant_id}"
        )


def inventory_variant(
    output_dir: Path,
    variant: VariantSpec,
    build: Mapping[str, Any],
    canonical_benchmarks: Sequence[str],
) -> dict[str, Any]:
    build_root = output_dir / "builds" / variant.variant_id
    path = build_root / "inventory.json"
    if path.is_file():
        cached = json.loads(path.read_text(encoding="utf-8"))
        validate_inventory_cache(
            path,
            cached,
            variant=variant,
            build=build,
            canonical_benchmarks=canonical_benchmarks,
        )
        return cached
    binary = Path(str(build["binary"]))
    env = variant_runtime_environment(variant, build_root / "tmp" / "inventory")
    result = subprocess.run(
        [str(binary), "--list", "--format", "terse"],
        cwd=Path(str(build["source_manifest"].get("source_root", build_root / "source"))),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    atomic_write_bytes(build_root / "inventory.stdout", result.stdout)
    atomic_write_bytes(build_root / "inventory.stderr", result.stderr)
    if result.returncode != 0:
        raise CampaignError(
            f"inventory failed for {variant.variant_id}:\n"
            + result.stderr.decode("utf-8", errors="replace")[-8000:]
        )
    observed = tuple(base.parse_inventory(result.stdout))
    canonical = tuple(canonical_benchmarks)
    missing = sorted(set(canonical) - set(observed))
    if missing:
        raise CampaignError(
            f"{variant.variant_id} misses canonical std_bench leaves: {missing[:10]}"
        )
    lifetime_evidence = None
    if variant.variant_id == LIFETIME_VARIANT_ID:
        lifetime_evidence = validate_lifetime_runtime(
            result.stderr.decode("utf-8", errors="replace")
        )
    record = {
        "schema_version": 2,
        "success": True,
        "variant_id": variant.variant_id,
        "binary_sha256": build["binary_sha256"],
        "observed_benchmark_count": len(observed),
        "observed_benchmarks_sha256": sha256_bytes(
            ("\n".join(observed) + "\n").encode()
        ),
        "canonical_benchmark_count": len(canonical),
        "canonical_benchmarks": list(canonical),
        "canonical_benchmarks_sha256": sha256_bytes(
            ("\n".join(canonical) + "\n").encode()
        ),
        "extra_noncanonical_benchmarks": sorted(set(observed) - set(canonical)),
        "stdout_sha256": sha256_bytes(result.stdout),
        "stderr_sha256": sha256_bytes(result.stderr),
        "runtime_environment": {
            **suite_contract.runtime_environment_record(env),
            "lifetime_runtime_arm": env.get("UNIALLOC_LIFETIME_EXPERIMENT_ARM"),
        },
        "lifetime_runtime_activation": lifetime_evidence,
        "artifacts": {
            "stdout": immutable_evidence.artifact_ref(
                build_root / "inventory.stdout"
            ),
            "stderr": immutable_evidence.artifact_ref(
                build_root / "inventory.stderr"
            ),
            "binary": immutable_evidence.artifact_ref(binary),
        },
    }
    persist_immutable_json(path, record, label="variant inventory record")
    return record


def process_runtime_evidence(variant: VariantSpec, stderr: bytes) -> dict[str, Any] | None:
    if variant.variant_id != LIFETIME_VARIANT_ID:
        return None
    return validate_lifetime_runtime(stderr.decode("utf-8", errors="replace"))


PROCESS_ARTIFACT_FIELDS = {
    "stdout": ("stdout_path", "stdout_sha256", "stdout_bytes"),
    "stderr": ("stderr_path", "stderr_sha256", "stderr_bytes"),
    "time": ("time_path", "time_sha256", "time_bytes"),
}


def validate_process_artifacts(
    output_dir: Path, record: Mapping[str, Any]
) -> dict[str, Path]:
    """Validate every process artifact and its attempt-local placement."""

    artifacts = record.get("artifacts")
    if not isinstance(artifacts, dict):
        raise immutable_evidence.ImmutableEvidenceError(
            "process record has no artifact attestations"
        )
    commit_path = raw_record_path(output_dir, record).resolve()
    attempts_root = (commit_path.parent / "attempts" / commit_path.stem).resolve()
    paths: dict[str, Path] = {}
    for name, (path_field, digest_field, bytes_field) in PROCESS_ARTIFACT_FIELDS.items():
        reference = artifacts.get(name)
        artifact_path = immutable_evidence.validate_artifact_ref(
            reference, context=f"std_bench {name}"
        )
        try:
            artifact_path.relative_to(attempts_root)
        except ValueError as error:
            raise immutable_evidence.ImmutableEvidenceError(
                f"std_bench {name} is outside its attempt directory"
            ) from error
        recorded_path = record.get(path_field)
        if (
            not isinstance(recorded_path, str)
            or artifact_path
            != (output_dir.resolve() / recorded_path).resolve()
            or record.get(digest_field) != reference["sha256"]
            or record.get(bytes_field) != reference["bytes"]
        ):
            raise immutable_evidence.ImmutableEvidenceError(
                f"std_bench {name} attestation differs from its raw record"
            )
        paths[name] = artifact_path
    binary_reference = artifacts.get("binary")
    binary_path = immutable_evidence.validate_artifact_ref(
        binary_reference, context="std_bench binary"
    )
    if (
        binary_path != Path(str(record.get("binary", ""))).resolve()
        or record.get("binary_sha256") != binary_reference["sha256"]
    ):
        raise immutable_evidence.ImmutableEvidenceError(
            "std_bench binary attestation differs from its raw record"
        )
    command = record.get("command")
    if (
        not isinstance(command, list)
        or len(command) < 4
        or command[:3] != ["/usr/bin/time", "-v", "-o"]
        or Path(str(command[3])).resolve()
        != immutable_evidence.validate_artifact_ref(
            artifacts["time"], context="std_bench time"
        )
    ):
        raise immutable_evidence.ImmutableEvidenceError(
            "std_bench command is not bound to its time artifact"
        )
    paths["binary"] = binary_path
    return paths


def derive_process_evidence(
    record: Mapping[str, Any], paths: Mapping[str, Path]
) -> dict[str, Any]:
    """Rederive process status and every reported metric from retained artifacts."""

    stdout = paths["stdout"].read_bytes()
    stderr = paths["stderr"].read_bytes()
    try:
        time_metrics = base.parse_time(paths["time"])
    except Exception as error:
        raise immutable_evidence.ImmutableEvidenceError(
            "std_bench GNU-time artifact is invalid"
        ) from error
    matches = list(
        base.BENCH_RE.finditer(stdout.decode("utf-8", errors="replace"))
    )
    timed_out = record.get("timed_out") is True
    derived: dict[str, Any] = dict(time_metrics)
    if len(matches) == 1:
        match = matches[0]
        derived["reported_benchmark"] = match.group("name")
        derived["ns_per_iter"] = float(match.group("ns").replace(",", ""))
        deviation = match.group("dev")
        derived["ns_per_iter_deviation"] = (
            float(deviation.replace(",", "")) if deviation is not None else None
        )
    else:
        derived["benchmark_line_count"] = len(matches)
    valid = bool(
        not timed_out
        and record.get("exit_code") == 0
        and len(matches) == 1
        and matches[0].group("name") == record.get("benchmark")
        and time_metrics.get("time_exit_status") == 0
        and record.get("glibc_tunables_present") is False
    )
    if valid:
        derived.update(
            {
                "valid": True,
                "status": "valid",
                "lifetime_runtime_activation": process_runtime_evidence(
                    VARIANT_REGISTRY[str(record["variant_id"])], stderr
                ),
            }
        )
    elif timed_out:
        derived.update({"valid": False, "status": "timeout_censored"})
    else:
        raise immutable_evidence.ImmutableEvidenceError(
            "std_bench artifacts do not describe a valid or timeout-censored process"
        )
    return derived


def validate_committed_process_record(
    output_dir: Path,
    record: Mapping[str, Any],
    *,
    protocol: Mapping[str, Any],
    cohort_id: str,
    variant: VariantSpec,
    variant_manifest: Mapping[str, Any],
    build: Mapping[str, Any],
    benchmark: str,
    phase: str,
    round_index: int,
    cpu: int,
    numa_node: int,
    timeout_seconds: int,
    measurement_session_id: str,
) -> None:
    validate_raw_record(record)
    expected = {
        "protocol_sha256": protocol["protocol_sha256"],
        "cohort_id": cohort_id,
        "measurement_session_id": measurement_session_id,
        "measurement_anchor": variant.variant_id == "unialloc",
        "variant_id": variant.variant_id,
        "allocator": variant.variant_id,
        "variant_manifest_sha256": variant_manifest["variant_manifest_sha256"],
        "benchmark": benchmark,
        "phase": phase,
        "round": round_index,
        "cpu": cpu,
        "numa_node": numa_node,
        "timeout_seconds": timeout_seconds,
        "binary_sha256": build["binary_sha256"],
    }
    for field, expected_value in expected.items():
        if record.get(field) != expected_value:
            raise immutable_evidence.ImmutableEvidenceError(
                f"std_bench committed identity mismatch for {field}"
            )
    paths = validate_process_artifacts(output_dir, record)
    derived = derive_process_evidence(record, paths)
    for field, value in derived.items():
        if record.get(field) != value:
            raise immutable_evidence.ImmutableEvidenceError(
                f"std_bench metric/status differs from artifacts: {field}"
            )
    expected_command = [
        "/usr/bin/time",
        "-v",
        "-o",
        str(
            immutable_evidence.validate_artifact_ref(
                record["artifacts"]["time"], context="std_bench time"
            )
        ),
        "numactl",
        f"--cpunodebind={numa_node}",
        f"--membind={numa_node}",
        "taskset",
        "-c",
        str(cpu),
        str(Path(str(build["binary"])).resolve()),
        "--bench",
        "--exact",
        benchmark,
        "--test-threads=1",
    ]
    if record.get("command") != expected_command:
        raise immutable_evidence.ImmutableEvidenceError(
            "std_bench command differs from the committed process contract"
        )


def execute_benchmark_process(
    *,
    output_dir: Path,
    protocol: Mapping[str, Any],
    cohort_id: str,
    variant: VariantSpec,
    variant_manifest: Mapping[str, Any],
    build: Mapping[str, Any],
    benchmark: str,
    phase: str,
    round_index: int,
    cpu: int,
    numa_node: int,
    timeout_seconds: int,
    measurement_session_id: str,
    process_registry: ProcessGroupRegistry = ACTIVE_PROCESS_GROUPS,
) -> dict[str, Any]:
    """Reuse one valid cell or publish one crash-recoverable process attempt."""

    binary = Path(str(build["binary"])).resolve()
    placeholder = {
        "cohort_id": cohort_id,
        "variant_id": variant.variant_id,
        "benchmark": benchmark,
        "phase": phase,
        "round": round_index,
    }
    commit_path = raw_record_path(output_dir, placeholder)

    def collect(attempt_dir: Path) -> Mapping[str, Any]:
        stdout_path = attempt_dir / "stdout.txt"
        stderr_path = attempt_dir / "stderr.txt"
        time_path = attempt_dir / "time.txt"
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
        env = variant_runtime_environment(variant, attempt_dir / "tmp")
        started = utc_now()
        started_monotonic = time.monotonic()
        proc: subprocess.Popen[Any] | None = None
        timed_out = False
        try:
            proc = subprocess.Popen(
                command,
                cwd=binary.parent,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            process_registry.add(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_and_wait_process_group(proc)
                stdout, stderr = proc.communicate()
        finally:
            if proc is not None:
                terminate_and_wait_process_group(proc)
                process_registry.discard(proc)
        assert proc is not None
        elapsed = time.monotonic() - started_monotonic
        immutable_evidence.atomic_write_bytes(stdout_path, stdout)
        immutable_evidence.atomic_write_bytes(stderr_path, stderr)
        if not time_path.is_file():
            raise CampaignError(
                f"GNU time emitted no artifact for {variant.variant_id}/{benchmark}"
            )

        def relative(path: Path) -> str:
            return str(path.resolve().relative_to(output_dir.resolve()))

        record: dict[str, Any] = {
            "schema_version": RAW_RECORD_SCHEMA_VERSION,
            "protocol_sha256": protocol["protocol_sha256"],
            "cohort_id": cohort_id,
            "measurement_session_id": measurement_session_id,
            "measurement_anchor": variant.variant_id == "unialloc",
            "variant_id": variant.variant_id,
            "allocator": variant.variant_id,
            "variant_manifest_sha256": variant_manifest[
                "variant_manifest_sha256"
            ],
            "benchmark": benchmark,
            "phase": phase,
            "round": round_index,
            "started_utc": started,
            "finished_utc": utc_now(),
            "supervisor_elapsed_seconds": elapsed,
            "timeout_seconds": timeout_seconds,
            "timed_out": timed_out,
            "exit_code": proc.returncode,
            "command": command,
            "cpu": cpu,
            "numa_node": numa_node,
            "glibc_tunables_present": "GLIBC_TUNABLES" in env,
            "runtime_environment": suite_contract.runtime_environment_record(env),
            "rseq_policy": suite_contract.PRODUCTION_RSEQ_POLICY,
            "binary": str(binary.resolve()),
            "binary_sha256": build["binary_sha256"],
            "stdout_path": relative(stdout_path),
            "stderr_path": relative(stderr_path),
            "time_path": relative(time_path),
            "stdout_sha256": sha256_bytes(stdout),
            "stderr_sha256": sha256_bytes(stderr),
            "time_sha256": sha256_file(time_path),
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
            "time_bytes": time_path.stat().st_size,
            "claim_scope": variant.claim_scope,
            "fixed_work_contract": variant.fixed_work_contract,
            "performance_claim_eligible": variant.performance_claim_eligible,
            "peak_rss_claim_eligible": variant.peak_rss_claim_eligible,
            "rss_work_model": variant.rss_work_model,
        }
        try:
            record.update(base.parse_time(time_path))
        except Exception as error:
            raise CampaignError(
                f"GNU time failed closed for {variant.variant_id}/{benchmark}"
            ) from error
        matches = list(
            base.BENCH_RE.finditer(stdout.decode("utf-8", errors="replace"))
        )
        if len(matches) == 1:
            match = matches[0]
            record["reported_benchmark"] = match.group("name")
            record["ns_per_iter"] = float(match.group("ns").replace(",", ""))
            deviation = match.group("dev")
            record["ns_per_iter_deviation"] = (
                float(deviation.replace(",", ""))
                if deviation is not None
                else None
            )
        else:
            record["benchmark_line_count"] = len(matches)
        valid = bool(
            not timed_out
            and proc.returncode == 0
            and len(matches) == 1
            and matches[0].group("name") == benchmark
            and record.get("time_exit_status") == 0
            and record.get("glibc_tunables_present") is False
        )
        if valid:
            record["lifetime_runtime_activation"] = process_runtime_evidence(
                variant, stderr
            )
            record["valid"] = True
            record["status"] = "valid"
        elif timed_out:
            record["valid"] = False
            record["status"] = "timeout_censored"
        else:
            raise CampaignError(
                "process failed closed for "
                f"{variant.variant_id}/{benchmark}/{phase}/{round_index}"
            )
        record["artifacts"] = {
            "stdout": immutable_evidence.artifact_ref(stdout_path),
            "stderr": immutable_evidence.artifact_ref(stderr_path),
            "time": immutable_evidence.artifact_ref(time_path),
            "binary": immutable_evidence.artifact_ref(binary),
        }
        return record

    def validate(record: Mapping[str, Any]) -> None:
        validate_committed_process_record(
            output_dir,
            record,
            protocol=protocol,
            cohort_id=cohort_id,
            variant=variant,
            variant_manifest=variant_manifest,
            build=build,
            benchmark=benchmark,
            phase=phase,
            round_index=round_index,
            cpu=cpu,
            numa_node=numa_node,
            timeout_seconds=timeout_seconds,
            measurement_session_id=measurement_session_id,
        )

    committed = immutable_evidence.run_or_reuse_json(
        commit_path, collect, validate
    )
    record = dict(committed.value)
    record["record_path"] = str(committed.path.relative_to(output_dir.resolve()))
    record["record_sha256"] = committed.record_sha256
    return record


def load_cohort_records(
    output_dir: Path,
    *,
    cohort_id: str,
    variant_ids: Sequence[str],
    canonical_benchmarks: Sequence[str],
    protocol: Mapping[str, Any],
    builds: Mapping[str, Mapping[str, Any]],
    variant_manifests: Mapping[str, Mapping[str, Any]],
    lane_cpus: Mapping[str, int],
    timeout_seconds: int,
    numa_node: int,
    measurement_session_id: str,
) -> dict[tuple[str, int, str, str], dict[str, Any]]:
    records: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    canonical = set(canonical_benchmarks)
    for variant_id in variant_ids:
        root = output_dir / "raw" / cohort_id / variant_id
        if not root.exists():
            continue
        for path in root.glob("*/**/record.json"):
            immutable_evidence.validate_committed_file(path)
            record = json.loads(path.read_text(encoding="utf-8"))
            if path.resolve() != raw_record_path(output_dir, record).resolve():
                raise CampaignError(f"raw process is stored under the wrong key: {path}")
            benchmark = str(record["benchmark"])
            if benchmark not in canonical:
                raise CampaignError(f"raw process uses an unknown benchmark: {path}")
            if record.get("variant_id") != variant_id:
                raise CampaignError(f"raw process uses a wrong variant shard: {path}")
            validate_committed_process_record(
                output_dir,
                record,
                protocol=protocol,
                cohort_id=cohort_id,
                variant=VARIANT_REGISTRY[variant_id],
                variant_manifest=variant_manifests[variant_id],
                build=builds[variant_id],
                benchmark=benchmark,
                phase=str(record["phase"]),
                round_index=int(record["round"]),
                cpu=lane_cpus[benchmark],
                numa_node=numa_node,
                timeout_seconds=timeout_seconds,
                measurement_session_id=measurement_session_id,
            )
            key = full.record_key(record)
            if key in records:
                raise CampaignError(f"duplicate raw process key: {key}")
            record["record_path"] = str(path.relative_to(output_dir))
            record["record_sha256"] = sha256_file(path)
            records[key] = record
    return records


def _cell_records(
    records: Iterable[Mapping[str, Any]], variant_id: str, benchmark: str
) -> list[Mapping[str, Any]]:
    return [
        record
        for record in records
        if record.get("variant_id") == variant_id and record.get("benchmark") == benchmark
    ]


def rebuild_absolute_index(
    output_dir: Path,
    *,
    cohort_id: str,
    variant_ids: Sequence[str],
    canonical_benchmarks: Sequence[str],
    protocol: Mapping[str, Any],
    builds: Mapping[str, Mapping[str, Any]],
    variant_manifests: Mapping[str, Mapping[str, Any]],
    lane_cpus: Mapping[str, int],
    timeout_seconds: int,
    numa_node: int,
    measurement_session_id: str,
) -> Path:
    """Rebuild the index only from records accepted by the complete validator."""

    records = load_cohort_records(
        output_dir,
        cohort_id=cohort_id,
        variant_ids=variant_ids,
        canonical_benchmarks=canonical_benchmarks,
        protocol=protocol,
        builds=builds,
        variant_manifests=variant_manifests,
        lane_cpus=lane_cpus,
        timeout_seconds=timeout_seconds,
        numa_node=numa_node,
        measurement_session_id=measurement_session_id,
    )
    rows = [
        {
            "cohort_id": record["cohort_id"],
            "measurement_session_id": record["measurement_session_id"],
            "measurement_anchor": record["measurement_anchor"],
            "variant_id": record["variant_id"],
            "benchmark": record["benchmark"],
            "phase": record["phase"],
            "round": record["round"],
            "status": record["status"],
            "ns_per_iter": record.get("ns_per_iter"),
            "peak_rss_kib": record.get("peak_rss_kib"),
            "fixed_work_contract": record["fixed_work_contract"],
            "performance_claim_eligible": record[
                "performance_claim_eligible"
            ],
            "peak_rss_claim_eligible": record[
                "peak_rss_claim_eligible"
            ],
            "rss_work_model": record["rss_work_model"],
            "wall_seconds": record.get("wall_seconds"),
            "record_path": record["record_path"],
            "record_sha256": record["record_sha256"],
        }
        for _key, record in sorted(records.items())
    ]
    path = output_dir / "derived" / cohort_id / "absolute-process-index.jsonl"
    payload = b"".join(canonical_json_bytes(row) for row in rows)
    atomic_write_bytes(path, payload)
    return path


def build_protocol(
    *,
    source_identity: Mapping[str, Any],
    canonical_benchmarks: Sequence[str],
    toolchain: str,
    measured_rounds: int,
    timeout_seconds: int,
    cpus: Sequence[int],
    numa_node: int,
) -> dict[str, Any]:
    inventory_bytes = ("\n".join(canonical_benchmarks) + "\n").encode()
    compatibility = {
        "protocol_revision": PROTOCOL_REVISION,
        "raw_record_schema_version": RAW_RECORD_SCHEMA_VERSION,
        "evaluator_sha256": sha256_file(SCRIPT_PATH),
        "full_runner_sha256": sha256_file(full.SCRIPT_PATH),
        "base_runner_sha256": sha256_file(full.BASE_SCRIPT),
        "transitive_evaluator_sha256": transitive_evaluator_digests(
            tuple(
                Path(path).resolve()
                for path in (
                    SCRIPT_PATH,
                    full.SCRIPT_PATH,
                    full.BASE_SCRIPT,
                    Path(matrix.__file__).resolve(),
                    Path(suite_contract.__file__).resolve(),
                    Path(immutable_evidence.__file__).resolve(),
                    Path(base.google_tcmalloc.__file__).resolve(),
                    SCRIPT_DIR / "lifetime_prior_six_program_campaign.py",
                )
            )
        ),
        "source_head": source_identity["source_head"],
        "source_git_objects": source_identity["git_objects"],
        "toolchain": toolchain,
        "canonical_inventory_count": len(canonical_benchmarks),
        "inventory_sha256": sha256_bytes(inventory_bytes),
        "warmup_processes_per_cell": 1,
        "measured_rounds": measured_rounds,
        "timeout_seconds": timeout_seconds,
        "cpus": list(cpus),
        "lane_rule": "canonical benchmark index modulo cpus",
        "numa_node": numa_node,
        "command_contract": (
            "/usr/bin/time -v; numactl CPU/NUMA bind; taskset; "
            "exact libtest leaf; one test thread"
        ),
        "raw_metrics_only": True,
        "fixed_work_contract": False,
        "performance_claim_eligible_variants": [
            variant_id
            for variant_id, variant in VARIANT_REGISTRY.items()
            if variant.performance_claim_eligible
        ],
        "peak_rss_claim_eligible": False,
        "peak_rss_interpretation": "diagnostic_only",
        "rss_work_model": RSS_WORK_MODEL,
        "runtime_environment_policy": suite_contract.RUNTIME_ENVIRONMENT_POLICY,
        "rseq_policy": suite_contract.PRODUCTION_RSEQ_POLICY,
        "measurement_lock": str(MEASUREMENT_LOCK),
        "measurement_session_scope": "one-immutable-session-per-cohort",
        "measurement_anchor_variant": "unialloc",
        "anchor_order_contract": "unialloc-first-for-each-phase-round-benchmark",
        "variant_claim_contracts": {
            variant_id: variant_claim_contract(variant)
            for variant_id, variant in VARIANT_REGISTRY.items()
        },
    }
    return {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "compatibility": compatibility,
        "provenance": {
            "runner_path": str(SCRIPT_PATH),
            "runner_sha256": sha256_file(SCRIPT_PATH),
            "full_runner_path": str(full.SCRIPT_PATH),
            "full_runner_sha256": sha256_file(full.SCRIPT_PATH),
            "base_runner_path": str(full.BASE_SCRIPT),
            "base_runner_sha256": sha256_file(full.BASE_SCRIPT),
            "source_branch": source_identity["source_branch"],
        },
    }


def campaign_plan(
    *,
    output_dir: Path,
    cohort_id: str,
    variant_ids: Sequence[str],
    canonical_benchmarks: Sequence[str],
    args: argparse.Namespace,
) -> dict[str, Any]:
    mode = (
        "plan_only"
        if args.plan_only
        else "build_only"
        if args.build_only
        else "smoke"
        if args.smoke_benchmark
        else "full_collection"
    )
    return {
        "schema_version": 1,
        "mode": mode,
        "output_dir": str(output_dir),
        "cohort_id": cohort_id,
        "selected_variants": [asdict(VARIANT_REGISTRY[value]) for value in variant_ids],
        "canonical_benchmark_count": len(canonical_benchmarks),
        "benchmark_processes_planned": (
            0
            if mode in {"plan_only", "build_only"}
            else len(variant_ids)
            if mode == "smoke"
            else len(canonical_benchmarks)
            * len(variant_ids)
            * (1 + args.measured_rounds)
        ),
        "raw_store": "raw/<cohort>/<variant>/<benchmark>/<phase>/<round>/record.json",
        "ratios_derived_later": True,
        "fixed_work_contract": False,
        "performance_claim_eligible_variants": [
            variant_id
            for variant_id in variant_ids
            if VARIANT_REGISTRY[variant_id].performance_claim_eligible
        ],
        "peak_rss_claim_eligible": False,
        "peak_rss_interpretation": "diagnostic_only",
        "rss_work_model": RSS_WORK_MODEL,
        "lifetime_boundary": (
            "transport/all-Unknown diagnostic; no fixed-work or explicit epoch contract"
        ),
    }


def ensure_host_tools() -> None:
    for command in ("cargo", "git", "numactl", "taskset", "/usr/bin/time", "rustc"):
        if shutil.which(command) is None:
            raise CampaignError(f"required command is missing: {command}")


def run_campaign(args: argparse.Namespace) -> dict[str, Any]:
    source_root = args.source_root.expanduser().resolve(strict=True)
    output_dir = args.output_dir.expanduser()
    if not output_dir.is_absolute():
        output_dir = (source_root / output_dir).resolve()
    canonical = load_canonical_inventory()
    plan = campaign_plan(
        output_dir=output_dir,
        cohort_id=args.cohort_id,
        variant_ids=args.variants,
        canonical_benchmarks=canonical,
        args=args,
    )
    if args.plan_only:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return plan
    if not args.build_only and "unialloc" not in args.variants:
        raise CampaignError(
            "measurement cohorts require UniAlloc as the fresh anchor variant"
        )
    validate_existing_session_selection(
        output_dir,
        cohort_id=args.cohort_id,
        variant_ids=args.variants,
    )

    ensure_host_tools()
    source_identity = validate_clean_implementation_source(source_root)
    toolchain = (source_root / "rust-toolchain").read_text(encoding="utf-8").strip()
    protocol = ensure_campaign_protocol(
        output_dir,
        build_protocol(
            source_identity=source_identity,
            canonical_benchmarks=canonical,
            toolchain=toolchain,
            measured_rounds=args.measured_rounds,
            timeout_seconds=args.timeout_seconds,
            cpus=args.cpus,
            numa_node=args.numa_node,
        ),
    )
    persist_selection(
        output_dir, cohort_id=args.cohort_id, variant_ids=args.variants
    )

    with full.exclusive_campaign_lock(output_dir):
        needs_wrapper = any(
            VARIANT_REGISTRY[value].mir_policy_flags is not None for value in args.variants
        )
        wrapper = output_dir / "tools/unialloc-rustc-wrapper"
        if needs_wrapper:
            wrapper = matrix.ensure_wrapper(wrapper, toolchain, args.build_timeout)
        sysroot = matrix.rustc_sysroot(toolchain)
        builds: dict[str, dict[str, Any]] = {}
        inventories: dict[str, dict[str, Any]] = {}
        variant_manifests: dict[str, dict[str, Any]] = {}
        for variant_id in args.variants:
            variant = VARIANT_REGISTRY[variant_id]
            variant_manifests[variant_id] = persist_variant_manifest(
                output_dir, variant, source_identity=source_identity, toolchain=toolchain
            )
            builds[variant_id] = build_variant(
                source_root,
                output_dir,
                variant,
                source_identity=source_identity,
                toolchain=toolchain,
                wrapper=wrapper,
                sysroot=sysroot,
                build_timeout=args.build_timeout,
            )
            inventories[variant_id] = inventory_variant(
                output_dir, variant, builds[variant_id], canonical
            )
        validate_selected_variant_completeness(args.variants, inventories, canonical)

        build_result = {
            **plan,
            "protocol_sha256": protocol["protocol_sha256"],
            "builds": {
                variant_id: {
                    "binary": builds[variant_id]["binary"],
                    "binary_sha256": builds[variant_id]["binary_sha256"],
                    "actual_mir_wrapper": builds[variant_id]["actual_mir_wrapper"],
                    "mir_policy_flags": builds[variant_id]["mir_policy_flags"],
                    "fixed_work_contract": VARIANT_REGISTRY[
                        variant_id
                    ].fixed_work_contract,
                    "performance_claim_eligible": VARIANT_REGISTRY[
                        variant_id
                    ].performance_claim_eligible,
                    "peak_rss_claim_eligible": VARIANT_REGISTRY[
                        variant_id
                    ].peak_rss_claim_eligible,
                    "rss_work_model": VARIANT_REGISTRY[variant_id].rss_work_model,
                    "canonical_benchmark_count": inventories[variant_id][
                        "canonical_benchmark_count"
                    ],
                    "lifetime_runtime_activation": inventories[variant_id].get(
                        "lifetime_runtime_activation"
                    ),
                }
                for variant_id in args.variants
            },
        }
        atomic_write_bytes(
            output_dir / "derived" / args.cohort_id / "build-validation.json",
            (json.dumps(build_result, indent=2, sort_keys=True) + "\n").encode(),
        )
        if args.build_only:
            return build_result

        session = ensure_measurement_session(
            output_dir,
            protocol=protocol,
            cohort_id=args.cohort_id,
            variant_ids=args.variants,
        )
        measurement_session_id = str(session["measurement_session_id"])
        lanes = full.assign_benchmark_lanes(canonical, args.cpus)
        lane_cpus = {
            benchmark: lane.cpu for lane in lanes for benchmark in lane.benchmarks
        }
        records = load_cohort_records(
            output_dir,
            cohort_id=args.cohort_id,
            variant_ids=args.variants,
            canonical_benchmarks=canonical,
            protocol=protocol,
            builds=builds,
            variant_manifests=variant_manifests,
            lane_cpus=lane_cpus,
            timeout_seconds=args.timeout_seconds,
            numa_node=args.numa_node,
            measurement_session_id=measurement_session_id,
        )
        append_lock = threading.Lock()
        stop = threading.Event()

        def state(variant_id: str, benchmark: str) -> Any:
            with append_lock:
                history = _cell_records(records.values(), variant_id, benchmark)
            return full.classify_cell_history(
                history, measured_rounds=args.measured_rounds
            )

        def execute(
            variant_id: str,
            benchmark: str,
            phase: str,
            round_index: int,
            cpu: int,
        ) -> None:
            key = (phase, round_index, variant_id, benchmark)
            with append_lock:
                if key in records:
                    return
                if variant_id != "unialloc":
                    anchor_key = (phase, round_index, "unialloc", benchmark)
                    anchor = records.get(anchor_key)
                    if (
                        not isinstance(anchor, dict)
                        or anchor.get("valid") is not True
                        or anchor.get("measurement_session_id")
                        != measurement_session_id
                    ):
                        raise CampaignError(
                            "comparison process lacks a fresh same-session UniAlloc anchor: "
                            f"{variant_id}/{benchmark}/{phase}/{round_index}"
                        )
            record = execute_benchmark_process(
                output_dir=output_dir,
                protocol=protocol,
                cohort_id=args.cohort_id,
                variant=VARIANT_REGISTRY[variant_id],
                variant_manifest=variant_manifests[variant_id],
                build=builds[variant_id],
                benchmark=benchmark,
                phase=phase,
                round_index=round_index,
                cpu=cpu,
                numa_node=args.numa_node,
                timeout_seconds=args.timeout_seconds,
                measurement_session_id=measurement_session_id,
            )
            with append_lock:
                if key in records:
                    raise CampaignError(f"duplicate process key during collection: {key}")
                records[key] = record

        if args.smoke_benchmark:
            if args.smoke_benchmark not in set(canonical):
                raise CampaignError("smoke benchmark is outside the canonical inventory")
            cpu = lane_cpus[args.smoke_benchmark]
            with cleanup_signal_handlers(), suite_contract.primary_measurement_lock(
                runner=SCRIPT_PATH.name,
                raw_root=output_dir,
                phase="smoke-warmup",
            ) as measurement_lock:
                for variant_id in (
                    "unialloc",
                    *(value for value in args.variants if value != "unialloc"),
                ):
                    current = state(variant_id, args.smoke_benchmark)
                    if current.status == "pending" and not current.valid_measured_rounds:
                        execute(variant_id, args.smoke_benchmark, "warmup", 0, cpu)
            index = rebuild_absolute_index(
                output_dir,
                cohort_id=args.cohort_id,
                variant_ids=args.variants,
                canonical_benchmarks=canonical,
                protocol=protocol,
                builds=builds,
                variant_manifests=variant_manifests,
                lane_cpus=lane_cpus,
                timeout_seconds=args.timeout_seconds,
                numa_node=args.numa_node,
                measurement_session_id=measurement_session_id,
            )
            return {
                **build_result,
                "mode": "smoke",
                "smoke_benchmark": args.smoke_benchmark,
                "absolute_index": str(index),
                "measurement_lock": dict(measurement_lock),
                "measurement_session_id": measurement_session_id,
            }

        benchmark_indexes = {name: index for index, name in enumerate(canonical)}

        def rotated(round_index: int, benchmark_index: int) -> tuple[str, ...]:
            candidates = tuple(
                value for value in args.variants if value != "unialloc"
            )
            if not candidates:
                return ("unialloc",)
            offset = (round_index + benchmark_index) % len(candidates)
            return ("unialloc", *candidates[offset:], *candidates[:offset])

        def run_lane(lane: Any) -> None:
            for benchmark in lane.benchmarks:
                if stop.is_set():
                    return
                benchmark_index = benchmark_indexes[benchmark]
                for variant_id in rotated(0, benchmark_index):
                    current = state(variant_id, benchmark)
                    if current.status == "pending" and not current.valid_measured_rounds:
                        execute(variant_id, benchmark, "warmup", 0, lane.cpu)
                for round_index in range(1, args.measured_rounds + 1):
                    for variant_id in rotated(round_index, benchmark_index):
                        current = state(variant_id, benchmark)
                        if current.status in {"complete", "censored"}:
                            continue
                        expected_round = len(current.valid_measured_rounds) + 1
                        if expected_round == round_index:
                            execute(
                                variant_id,
                                benchmark,
                                "measured",
                                round_index,
                                lane.cpu,
                            )

        with cleanup_signal_handlers(), suite_contract.primary_measurement_lock(
            runner=SCRIPT_PATH.name,
            raw_root=output_dir,
            phase="warmup-and-measured",
        ) as measurement_lock:
            with ThreadPoolExecutor(
                max_workers=len(lanes), thread_name_prefix="std-bench-feature-lane"
            ) as pool:
                futures = [pool.submit(run_lane, lane) for lane in lanes]
                await_worker_futures(futures, stop=stop)
        for benchmark in canonical:
            for variant_id in args.variants:
                if state(variant_id, benchmark).status == "pending":
                    raise CampaignError(
                        f"full campaign ended with a pending cell: {variant_id}/{benchmark}"
                    )
        index = rebuild_absolute_index(
            output_dir,
            cohort_id=args.cohort_id,
            variant_ids=args.variants,
            canonical_benchmarks=canonical,
            protocol=protocol,
            builds=builds,
            variant_manifests=variant_manifests,
            lane_cpus=lane_cpus,
            timeout_seconds=args.timeout_seconds,
            numa_node=args.numa_node,
            measurement_session_id=measurement_session_id,
        )
        result = {
            **build_result,
            "mode": "complete",
            "raw_process_record_count": len(records),
            "absolute_index": str(index),
            "derived_ratios_written": False,
            "measurement_lock": dict(measurement_lock),
            "measurement_session_id": measurement_session_id,
        }
        atomic_write_bytes(
            output_dir / "derived" / args.cohort_id / "campaign-state.json",
            (json.dumps(result, indent=2, sort_keys=True) + "\n").encode(),
        )
        return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("evaluation/raw/std-bench-feature-incremental-v1"),
    )
    parser.add_argument("--cohort-id", type=validate_cohort_id, default="primary")
    parser.add_argument(
        "--variants",
        type=parse_variant_ids,
        default=DEFAULT_VARIANT_IDS,
        help=(
            "comma-separated registry ids; a measurement session freezes the "
            "ordered selection for its cohort"
        ),
    )
    parser.add_argument("--jobs", type=int, required=True)
    parser.add_argument("--cpus", type=full.parse_cpu_list, required=True)
    parser.add_argument("--numa-node", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--build-timeout", type=int, default=1800)
    parser.add_argument(
        "--measured-rounds", type=int, choices=range(3, 6), default=DEFAULT_MEASURED_ROUNDS
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--plan-only", action="store_true", help="print the exact no-execution plan"
    )
    modes.add_argument(
        "--build-only",
        action="store_true",
        help="build variants and validate the full inventory without benchmark cells",
    )
    modes.add_argument(
        "--smoke-benchmark",
        help="collect only the warmup process for one exact canonical leaf",
    )
    args = parser.parse_args(argv)
    if args.jobs <= 0 or args.jobs != len(args.cpus):
        parser.error("--jobs must be positive and equal the number of --cpus")
    if args.timeout_seconds <= 0 or args.build_timeout <= 0:
        parser.error("timeouts must be positive")
    if args.numa_node < 0:
        parser.error("--numa-node must be non-negative")
    args.variants = tuple(args.variants)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    run_campaign(parse_args(argv))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"fatal: {error}", file=sys.stderr)
        raise
