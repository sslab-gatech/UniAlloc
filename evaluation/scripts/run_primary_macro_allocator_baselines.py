#!/usr/bin/env python3
"""Run exact-pin allocator baselines on the primary macro workloads.

The campaign stores one immutable JSON cell per target, workload, allocator,
phase, and round.  Build records are reusable only after exact protocol,
source, and binary verification.  Derived summaries can therefore add or
remove allocator columns without rebuilding unrelated cells.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import statistics
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
for _path in (ROOT, SCRIPT_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from evaluation.scripts import (  # noqa: E402
    google_tcmalloc_support,
    immutable_evidence,
    polars_swc_rustpython_primary_campaign as psr,
    realworld_type_isolation_matrix as matrix,
    type_isolation_primary_collections_oxipng as collections_oxipng,
    type_isolation_redb_actix_campaign as redb_actix,
    type_isolation_suite_contract,
)


PROTOCOL_ID = "primary-macro-allocator-baselines-v1"
PROTOCOL_SCHEMA_VERSION = 1
CELL_SCHEMA_VERSION = 1
BUILD_SCHEMA_VERSION = 1
BUILD_ADAPTER_VERSION = 2
GNU_TIME = pathlib.Path("/usr/bin/time")
DEFAULT_TOOLCHAIN = "nightly-2026-06-11"
DEFAULT_CHECKOUT_ROOT = ROOT / "evaluation/external/_checkouts"
DEFAULT_TCMALLOC_PREFIX = ROOT / "evaluation/deps/tcmalloc"
DEFAULT_QUIESCENCE_TIMEOUT = 3600
DEFAULT_QUIET_SECONDS = 15

VARIANT_ORDER = (
    "unialloc",
    "mimalloc",
    "mimalloc_no_thp",
    "google_tcmalloc",
    "jemalloc",
)
VARIANT_DEFINITIONS: Mapping[str, Mapping[str, Any]] = {
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

TARGET_EXECUTION_CONTRACTS: Mapping[str, Mapping[str, Any]] = {
    "collections": {
        "cpu_list": "0-15",
        "numa_node": 0,
        "runtime_overrides": {},
    },
    "oxipng": {
        "cpu_list": "0-15",
        "numa_node": 0,
        "runtime_overrides": {},
    },
    "redb": {
        "cpu_list": "16-31",
        "numa_node": 0,
        "runtime_overrides": {},
    },
    "actix_web": {
        "cpu_list": "16-31",
        "numa_node": 0,
        "runtime_overrides": {},
    },
    "polars": {
        "cpu_list": "32-63",
        "numa_node": 1,
        "runtime_overrides": {
            "POLARS_MAX_THREADS": "1",
            "RAYON_NUM_THREADS": "1",
            "TOKIO_WORKER_THREADS": "1",
        },
    },
    "swc": {
        "cpu_list": "32-63",
        "numa_node": 1,
        "runtime_overrides": {
            "POLARS_MAX_THREADS": "1",
            "RAYON_NUM_THREADS": "1",
            "TOKIO_WORKER_THREADS": "1",
        },
    },
    "rustpython": {
        "cpu_list": "32-63",
        "numa_node": 1,
        "runtime_overrides": {
            "POLARS_MAX_THREADS": "1",
            "RAYON_NUM_THREADS": "1",
            "TOKIO_WORKER_THREADS": "1",
        },
    },
}

PSR_REPOSITORIES = {
    "polars": "https://github.com/pola-rs/polars.git",
    "swc": "https://github.com/swc-project/swc.git",
    "rustpython": "https://github.com/RustPython/RustPython.git",
}

MIMALLOC_THP_PROBE_SOURCE = r'''
#[cfg(target_os = "linux")]
mod unialloc_mimalloc_thp_runtime_probe {
    use std::ffi::{c_int, c_void};

    const PR_GET_THP_DISABLE: c_int = 42;
    const ENABLED: &[u8] = b"UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE=0\n";
    const DISABLED: &[u8] = b"UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE=1\n";
    const ERROR: &[u8] = b"UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE=error\n";

    unsafe extern "C" {
        fn prctl(
            option: c_int,
            argument2: usize,
            argument3: usize,
            argument4: usize,
            argument5: usize,
        ) -> c_int;
        fn write(fd: c_int, buffer: *const c_void, count: usize) -> isize;
    }

    unsafe extern "C" fn report() {
        let state = unsafe { prctl(PR_GET_THP_DISABLE, 0, 0, 0, 0) };
        let message = if state == 0 {
            ENABLED
        } else if state == 1 {
            DISABLED
        } else {
            ERROR
        };
        unsafe {
            write(2, message.as_ptr().cast(), message.len());
        }
    }

    #[used]
    #[cfg_attr(target_family = "unix", unsafe(link_section = ".fini_array"))]
    static REPORT: unsafe extern "C" fn() = report;
}
'''


class CampaignError(RuntimeError):
    """Raised when a campaign input or retained artifact fails closed."""


@dataclasses.dataclass(frozen=True)
class HarnessContract:
    id: str
    selector: str
    metric_direction: str
    performance_unit: str
    performance_source: str


@dataclasses.dataclass(frozen=True)
class TargetContract:
    id: str
    label: str
    source_ref: str
    source_commit: str
    rss_work_model: str
    harnesses: tuple[HarnessContract, ...]

    @property
    def fixed_work_rss(self) -> bool:
        return self.rss_work_model == "fixed_work"


@dataclasses.dataclass(frozen=True)
class CampaignContract:
    suite: type_isolation_suite_contract.SuiteContract
    targets: Mapping[str, TargetContract]


@dataclasses.dataclass(frozen=True)
class Protocol:
    payload: Mapping[str, Any]
    fingerprint: str


@dataclasses.dataclass(frozen=True)
class CellIdentity:
    cohort_id: str
    target_id: str
    harness_id: str
    variant: str
    phase: str
    round: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "cohort_id": self.cohort_id,
            "target_id": self.target_id,
            "harness_id": self.harness_id,
            "variant": self.variant,
            "phase": self.phase,
            "round": self.round,
        }


@dataclasses.dataclass(frozen=True)
class PlannedCell:
    identity: CellIdentity
    relative_path: str
    target_fingerprint: str
    variant_fingerprint: str
    cell_fingerprint: str


@dataclasses.dataclass(frozen=True)
class CohortPlan:
    target_id: str
    subject_variant: str
    variants: tuple[str, ...]
    cohort_id: str
    target_fingerprint: str
    variant_fingerprints: Mapping[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "subject_variant": self.subject_variant,
            "variants": list(self.variants),
            "cohort_id": self.cohort_id,
            "target_fingerprint": self.target_fingerprint,
            "variant_fingerprints": dict(self.variant_fingerprints),
        }


@dataclasses.dataclass(frozen=True)
class CampaignPlan:
    protocol_fingerprint: str
    requested_targets: tuple[str, ...]
    requested_variants: tuple[str, ...]
    execution_variants: tuple[str, ...]
    warmup_rounds: int
    measured_rounds: int
    cohorts: tuple[CohortPlan, ...]
    cells: tuple[PlannedCell, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "protocol_id": PROTOCOL_ID,
            "protocol_fingerprint": self.protocol_fingerprint,
            "requested_targets": list(self.requested_targets),
            "requested_variants": list(self.requested_variants),
            "execution_variants": list(self.execution_variants),
            "warmup_rounds": self.warmup_rounds,
            "measured_rounds": self.measured_rounds,
            "cohorts": [cohort.as_dict() for cohort in self.cohorts],
            "cell_count": len(self.cells),
            "cells": [
                {
                    **cell.identity.as_dict(),
                    "relative_path": cell.relative_path,
                    "target_fingerprint": cell.target_fingerprint,
                    "variant_fingerprint": cell.variant_fingerprint,
                    "cell_fingerprint": cell.cell_fingerprint,
                }
                for cell in self.cells
            ],
        }


@dataclasses.dataclass(frozen=True)
class SourceContext:
    target_id: str
    checkout: pathlib.Path
    record: Mapping[str, Any]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(value: Any) -> str:
    return sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def load_json(path: pathlib.Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CampaignError(f"cannot load {context}: {path}") from error
    if not isinstance(value, dict):
        raise CampaignError(f"{context} must be a JSON object: {path}")
    return value


def load_campaign_contract(
    path: pathlib.Path | str = type_isolation_suite_contract.CURRENT_SUITE_PATH,
) -> CampaignContract:
    try:
        suite = type_isolation_suite_contract.load_suite_contract(path)
    except type_isolation_suite_contract.SuiteContractError as error:
        raise CampaignError(str(error)) from error
    targets: dict[str, TargetContract] = {}
    for row in suite.manifest["targets"]:
        if not isinstance(row, dict):
            raise CampaignError("suite target entries must be objects")
        source = row.get("source")
        harness_rows = row.get("harnesses")
        if not isinstance(source, dict) or not isinstance(harness_rows, list):
            raise CampaignError("suite target source or harness contract is malformed")
        harnesses: list[HarnessContract] = []
        for harness in harness_rows:
            if not isinstance(harness, dict):
                raise CampaignError("suite harness entries must be objects")
            harnesses.append(
                HarnessContract(
                    id=str(harness["id"]),
                    selector=str(harness["selector"]),
                    metric_direction=str(harness["metric_direction"]),
                    performance_unit=str(harness["performance_unit"]),
                    performance_source=str(harness["performance_source"]),
                )
            )
        target = TargetContract(
            id=str(row["id"]),
            label=str(row["label"]),
            source_ref=str(source["ref"]),
            source_commit=str(source["commit"]),
            rss_work_model=str(row["rss_work_model"]),
            harnesses=tuple(harnesses),
        )
        if target.id in targets:
            raise CampaignError(f"duplicate suite target: {target.id}")
        targets[target.id] = target
    expected = {
        *collections_oxipng.TARGETS,
        *redb_actix.TARGETS,
        *psr.TARGET_SPECS,
    }
    if set(targets) != expected:
        raise CampaignError(
            "macro baseline adapters and suite targets differ: "
            f"suite={sorted(targets)}, adapters={sorted(expected)}"
        )
    return CampaignContract(suite=suite, targets=targets)


def validate_adapter_contract(contract: CampaignContract) -> None:
    try:
        collections_oxipng.verify_suite_contract(contract.suite)
        redb_actix.validate_specs_against_suite(contract.suite)
        psr.validate_specs_against_suite(contract.suite)
    except (
        collections_oxipng.CampaignError,
        redb_actix.CampaignError,
        psr.CampaignError,
    ) as error:
        raise CampaignError(str(error)) from error


def build_variant_for(variant: str) -> str:
    try:
        return str(VARIANT_DEFINITIONS[variant]["build_variant"])
    except KeyError as error:
        raise CampaignError(f"unknown allocator variant: {variant}") from error


def allocator_source(variant: str) -> str:
    build_variant = (
        build_variant_for(variant) if variant in VARIANT_DEFINITIONS else variant
    )
    if build_variant == "system":
        return ""
    if build_variant not in {"unialloc", "mimalloc", "jemalloc"}:
        raise CampaignError(f"unsupported build allocator: {build_variant}")
    try:
        source = matrix.allocator_source(build_variant)
    except matrix.MatrixError as error:
        raise CampaignError(str(error)) from error
    if build_variant == "mimalloc":
        source += MIMALLOC_THP_PROBE_SOURCE
    return source


def dependencies_for(
    variant: str, implementation_snapshot: pathlib.Path
) -> tuple[str, ...]:
    build_variant = (
        build_variant_for(variant) if variant in VARIANT_DEFINITIONS else variant
    )
    if build_variant == "system":
        return ()
    if build_variant == "unialloc":
        return (
            matrix.cargo_path_dependency(
                implementation_snapshot / "unialloc",
                (),
                default_features_enabled=True,
            ),
        )
    if build_variant == "mimalloc":
        return (
            'mimalloc = { version = "=0.1.52", default-features = false }',
            matrix.MIMALLOC_SYS_DEPENDENCY,
        )
    if build_variant == "jemalloc":
        return (
            'jemallocator = { package = "tikv-jemallocator", version = "=0.7.0" }',
        )
    raise CampaignError(f"unsupported dependency allocator: {build_variant}")


def _target_protocol(target: TargetContract) -> dict[str, Any]:
    execution = TARGET_EXECUTION_CONTRACTS[target.id]
    return {
        "id": target.id,
        "source_ref": target.source_ref,
        "source_commit": target.source_commit,
        "rss_work_model": target.rss_work_model,
        "runtime_rseq_mode": type_isolation_suite_contract.PRODUCTION_RSEQ_POLICY,
        "execution": {
            "cpu_list": execution["cpu_list"],
            "numa_node": execution["numa_node"],
            "runtime_overrides": dict(execution["runtime_overrides"]),
        },
        "harnesses": [dataclasses.asdict(harness) for harness in target.harnesses],
    }


def build_protocol(
    contract: CampaignContract,
    *,
    toolchain: str,
    tcmalloc_identity: Mapping[str, Any] | None = None,
    cpu_list: str | None = None,
    numa_node: int | None = None,
) -> Protocol:
    del tcmalloc_identity, cpu_list, numa_node
    payload = {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "build_adapter_version": BUILD_ADAPTER_VERSION,
        "runner": {
            "path": str(pathlib.Path(__file__).resolve().relative_to(ROOT)),
            "sha256": sha256_file(pathlib.Path(__file__).resolve()),
        },
        "suite": {
            "id": contract.suite.suite_id,
            "path": str(contract.suite.path),
            "manifest_sha256": contract.suite.manifest_sha256,
            "preregistration_manifest_sha256": (
                contract.suite.preregistration_manifest_sha256
            ),
        },
        "implementation": {
            "git_revision": contract.suite.implementation_revision,
            "canonical_sha256": contract.suite.implementation_sha256,
            "canonical_file_count": contract.suite.implementation_file_count,
            "canonical_size_bytes": contract.suite.implementation_size_bytes,
        },
        "measurement": {
            "warmup_rounds": contract.suite.warmup_rounds,
            "measured_rounds": contract.suite.measured_rounds,
            "pairing": "same-target-same-workload-same-round",
            "performance_point_estimate": "median-of-same-round-cost-ratios",
            "rss_point_estimate": "median-of-same-round-peak-rss-ratios",
            "rss_headline_rule": "fixed-work-targets-only",
            "runtime_environment_policy": (
                type_isolation_suite_contract.RUNTIME_ENVIRONMENT_POLICY
            ),
            "rseq_policy": type_isolation_suite_contract.PRODUCTION_RSEQ_POLICY,
            "toolchain": toolchain,
        },
        "targets": [
            _target_protocol(target) for target in contract.targets.values()
        ],
        "variant_contract_schema_version": 1,
    }
    return Protocol(payload=payload, fingerprint=canonical_json_sha256(payload))


def target_fingerprint(protocol: Protocol, target_id: str) -> str:
    targets = {
        str(row["id"]): row for row in protocol.payload["targets"]  # type: ignore[index]
    }
    if target_id not in targets:
        raise CampaignError(f"unknown target for fingerprint: {target_id}")
    return canonical_json_sha256(
        {
            "protocol_id": PROTOCOL_ID,
            "runner": protocol.payload["runner"],
            "suite": protocol.payload["suite"],
            "implementation": protocol.payload["implementation"],
            "measurement": protocol.payload["measurement"],
            "target": targets[target_id],
        }
    )


def variant_contract_payload(
    protocol: Protocol,
    *,
    target_id: str,
    variant: str,
    tcmalloc_identity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if variant not in VARIANT_DEFINITIONS:
        raise CampaignError(f"unknown variant for fingerprint: {variant}")
    runtime_overrides = (
        matrix.allocator_runtime_environment_overrides(variant)
        if variant in {"mimalloc", "mimalloc_no_thp"}
        else {}
    )
    dependency_pins: dict[str, str] = {}
    if variant in {"mimalloc", "mimalloc_no_thp"}:
        dependency_pins = {
            "mimalloc": "0.1.52",
            "libmimalloc-sys": "0.1.49",
        }
    elif variant == "jemalloc":
        dependency_pins = {"tikv-jemallocator": "0.7.0"}
    google_identity = None
    if variant == "google_tcmalloc":
        if tcmalloc_identity is None:
            raise CampaignError("Google TCMalloc identity is required for its cohort")
        google_identity = dict(tcmalloc_identity)
    return {
        "schema_version": 1,
        "target_fingerprint": target_fingerprint(protocol, target_id),
        "variant": variant,
        **dict(VARIANT_DEFINITIONS[variant]),
        "runtime_environment_overrides": runtime_overrides,
        "dependency_pins": dependency_pins,
        "google_tcmalloc_identity": google_identity,
    }


def variant_fingerprint(
    protocol: Protocol,
    *,
    target_id: str,
    variant: str,
    tcmalloc_identity: Mapping[str, Any] | None,
) -> str:
    return canonical_json_sha256(
        variant_contract_payload(
            protocol,
            target_id=target_id,
            variant=variant,
            tcmalloc_identity=tcmalloc_identity,
        )
    )


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def build_plan(
    protocol: Protocol,
    *,
    target_ids: Sequence[str],
    variant_ids: Sequence[str],
    tcmalloc_identity: Mapping[str, Any] | None = None,
) -> CampaignPlan:
    protocol_targets = {
        str(row["id"]): row
        for row in protocol.payload["targets"]  # type: ignore[index]
    }
    targets = _ordered_unique(target_ids)
    variants = _ordered_unique(variant_ids)
    unknown_targets = sorted(set(targets) - set(protocol_targets))
    unknown_variants = sorted(set(variants) - set(VARIANT_ORDER))
    if not targets or unknown_targets:
        raise CampaignError(
            "unknown or empty target selection: " + ",".join(unknown_targets)
        )
    if not variants or unknown_variants:
        raise CampaignError(
            "unknown or empty variant selection: " + ",".join(unknown_variants)
        )
    subjects = tuple(variant for variant in variants if variant != "unialloc")
    if not subjects:
        subjects = ("unialloc",)
    execution_variants = tuple(
        variant for variant in VARIANT_ORDER if variant in set(variants) | {"unialloc"}
    )
    measurement = protocol.payload["measurement"]  # type: ignore[index]
    warmups = int(measurement["warmup_rounds"])
    measured = int(measurement["measured_rounds"])
    cells: list[PlannedCell] = []
    cohorts: list[CohortPlan] = []
    for target_id in targets:
        target = protocol_targets[target_id]
        target_digest = target_fingerprint(protocol, target_id)
        for subject in subjects:
            cohort_variants = (
                ("unialloc",) if subject == "unialloc" else ("unialloc", subject)
            )
            fingerprints = {
                variant: variant_fingerprint(
                    protocol,
                    target_id=target_id,
                    variant=variant,
                    tcmalloc_identity=(
                        tcmalloc_identity if variant == "google_tcmalloc" else None
                    ),
                )
                for variant in cohort_variants
            }
            cohort_id = canonical_json_sha256(
                {
                    "target_fingerprint": target_digest,
                    "subject_variant": subject,
                    "variant_fingerprints": fingerprints,
                    "warmup_rounds": warmups,
                    "measured_rounds": measured,
                }
            )
            cohort = CohortPlan(
                target_id=target_id,
                subject_variant=subject,
                variants=cohort_variants,
                cohort_id=cohort_id,
                target_fingerprint=target_digest,
                variant_fingerprints=fingerprints,
            )
            cohorts.append(cohort)
            for harness in target["harnesses"]:
                harness_id = str(harness["id"])
                rounds = [
                    *(('warmup', index) for index in range(warmups)),
                    *(('measurement', index) for index in range(1, measured + 1)),
                ]
                for variant in cohort_variants:
                    for phase, round_number in rounds:
                        identity = CellIdentity(
                            cohort_id,
                            target_id,
                            harness_id,
                            variant,
                            phase,
                            round_number,
                        )
                        cell_digest = canonical_json_sha256(
                            {
                                **identity.as_dict(),
                                "target_fingerprint": target_digest,
                                "variant_fingerprint": fingerprints[variant],
                                "selector": harness["selector"],
                                "performance_unit": harness["performance_unit"],
                                "performance_source": harness["performance_source"],
                                "rss_work_model": target["rss_work_model"],
                            }
                        )
                        cells.append(
                            PlannedCell(
                                identity,
                                f"cells/{target_id}/{harness_id}/{cohort_id}/"
                                f"{variant}/{phase}-{round_number:02d}.json",
                                target_digest,
                                fingerprints[variant],
                                cell_digest,
                            )
                        )
    return CampaignPlan(
        protocol_fingerprint=protocol.fingerprint,
        requested_targets=targets,
        requested_variants=variants,
        execution_variants=execution_variants,
        warmup_rounds=warmups,
        measured_rounds=measured,
        cohorts=tuple(cohorts),
        cells=tuple(cells),
    )


def ensure_campaign_manifest(
    raw_dir: pathlib.Path,
    protocol: Protocol,
    *,
    reuse: bool,
    suite_binding: Mapping[str, Any] | None = None,
) -> pathlib.Path:
    del reuse  # Exact manifests are always safe to retain; artifacts decide reuse.
    path = raw_dir / "campaigns" / protocol.fingerprint / "campaign.json"
    stable_suite_binding = {
        key: suite_binding[key]
        for key in ("path", "sha256", "bytes")
        if suite_binding is not None and key in suite_binding
    }
    if suite_binding is not None and len(stable_suite_binding) != 3:
        raise CampaignError("suite binding lacks immutable artifact identity")
    expected = {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "protocol_fingerprint": protocol.fingerprint,
        "protocol": protocol.payload,
        "suite_manifest": stable_suite_binding,
    }
    try:
        return immutable_evidence.persist_immutable_json(path, expected).path
    except immutable_evidence.ImmutableEvidenceError as error:
        raise CampaignError(
            f"campaign protocol mismatch in existing raw directory: {path}"
        ) from error


def validate_reusable_cell(
    path: pathlib.Path,
    *,
    expected: CellIdentity,
    expected_identity: Mapping[str, Any] | None = None,
    expected_metrics: Mapping[str, Any] | None = None,
    protocol_fingerprint: str | None = None,
    build_sha256: str | None = None,
) -> dict[str, Any]:
    row = load_json(path, "reusable measurement cell")
    identity = {**expected.as_dict(), **dict(expected_identity or {})}
    if protocol_fingerprint is not None:
        identity["protocol_fingerprint"] = protocol_fingerprint
    if build_sha256 is not None:
        identity["binary_sha256"] = build_sha256
    try:
        immutable_evidence.validate_measurement_record(
            row,
            expected_identity=identity,
            expected_metrics=expected_metrics,
        )
    except immutable_evidence.ImmutableEvidenceError as error:
        raise CampaignError(f"reusable cell identity mismatch: {path}: {error}") from error
    if row.get("success") is not True:
        raise CampaignError(f"reusable cell is unsuccessful: {path}")
    correctness = row.get("correctness")
    if not isinstance(correctness, dict) or correctness.get("passed") is not True:
        raise CampaignError(f"reusable cell correctness mismatch: {path}")
    return row


def _cost_ratio(subject: float, reference: float, direction: str) -> float:
    if subject <= 0 or reference <= 0:
        raise CampaignError("ratio input must be positive")
    if direction == "lower_is_better":
        return subject / reference
    if direction == "higher_is_better":
        return reference / subject
    raise CampaignError(f"unknown metric direction: {direction}")


def summarize_cells(
    cells: Sequence[Mapping[str, Any]], contract: CampaignContract
) -> dict[str, Any]:
    measured = [row for row in cells if row.get("phase") == "measurement"]
    indexed: dict[tuple[str, str, str, str, int], Mapping[str, Any]] = {}
    for row in measured:
        if row.get("success") is not True:
            continue
        key = (
            str(row.get("target_id")),
            str(row.get("harness_id")),
            str(row.get("cohort_id", "legacy")),
            str(row.get("variant")),
            int(row.get("round", 0)),
        )
        if key in indexed:
            raise CampaignError(f"duplicate measured cell: {key}")
        indexed[key] = row
    workloads: dict[str, Any] = {}
    target_rows: dict[str, dict[str, dict[str, list[float]]]] = {}
    measured_rounds = contract.suite.measured_rounds
    for target in contract.targets.values():
        for harness in target.harnesses:
            workload_key = f"{target.id}/{harness.id}"
            cohorts = sorted(
                {
                    key[2]
                    for key in indexed
                    if key[0] == target.id and key[1] == harness.id
                }
            )
            subject_rows: dict[str, Any] = {}
            for cohort_id in cohorts:
                variants = sorted(
                    {
                        key[3]
                        for key in indexed
                        if key[0] == target.id
                        and key[1] == harness.id
                        and key[2] == cohort_id
                    },
                    key=lambda value: VARIANT_ORDER.index(value),
                )
                subjects = [value for value in variants if value != "unialloc"]
                if "unialloc" not in variants or len(subjects) != 1:
                    continue
                variant = subjects[0]
                performance_ratios: list[float] = []
                rss_ratios: list[float] = []
                observations: list[dict[str, Any]] = []
                for round_number in range(1, measured_rounds + 1):
                    reference_key = (
                        target.id,
                        harness.id,
                        cohort_id,
                        "unialloc",
                        round_number,
                    )
                    subject_key = (
                        target.id,
                        harness.id,
                        cohort_id,
                        variant,
                        round_number,
                    )
                    if reference_key not in indexed or subject_key not in indexed:
                        raise CampaignError(
                            "incomplete same-round comparison: "
                            f"{workload_key}/{variant}/round-{round_number:02d}"
                        )
                    reference = indexed[reference_key]
                    subject = indexed[subject_key]
                    tokens = {
                        str(row["correctness"].get("comparison_token"))
                        for row in (reference, subject)
                        if isinstance(row.get("correctness"), dict)
                        and row["correctness"].get("comparison_token")
                    }
                    if len(tokens) > 1:
                        raise CampaignError(
                            f"correctness token mismatch for {workload_key}/{variant}"
                        )
                    performance_ratio = _cost_ratio(
                        float(subject["performance"]),
                        float(reference["performance"]),
                        harness.metric_direction,
                    )
                    rss_ratio = float(subject["peak_rss_mib"]) / float(
                        reference["peak_rss_mib"]
                    )
                    performance_ratios.append(performance_ratio)
                    rss_ratios.append(rss_ratio)
                    observations.append(
                        {
                            "round": round_number,
                            "performance_cost_ratio": performance_ratio,
                            "peak_rss_ratio": rss_ratio,
                        }
                    )
                performance_median = statistics.median(performance_ratios)
                rss_median = statistics.median(rss_ratios)
                subject_rows[variant] = {
                    "cohort_id": cohort_id,
                    "performance_cost_ratio_median": performance_median,
                    "peak_rss_ratio_median": rss_median,
                    "peak_rss_headline_eligible": target.fixed_work_rss,
                    "same_round_observations": observations,
                }
                aggregate = target_rows.setdefault(target.id, {}).setdefault(
                    variant, {"performance": [], "rss": []}
                )
                aggregate["performance"].append(performance_median)
                if target.fixed_work_rss:
                    aggregate["rss"].append(rss_median)
            if subject_rows:
                workloads[workload_key] = subject_rows
    targets: dict[str, Any] = {}
    for target_id, variants in target_rows.items():
        targets[target_id] = {
            variant: {
                "equal_weight_harness_geomean_performance_cost_ratio": math.exp(
                    statistics.fmean(
                        math.log(value) for value in values["performance"]
                    )
                ),
                "equal_weight_harness_geomean_peak_rss_ratio": (
                    math.exp(
                        statistics.fmean(
                            math.log(value) for value in values["rss"]
                        )
                    )
                    if values["rss"]
                    else None
                ),
                "peak_rss_headline_eligible": bool(values["rss"]),
                "eligible_harness_count": len(values["performance"]),
            }
            for variant, values in variants.items()
        }
    return {
        "schema_version": 1,
        "protocol_id": PROTOCOL_ID,
        "measured_rounds": measured_rounds,
        "workloads": workloads,
        "targets": targets,
    }


def _command_record(
    result: Mapping[str, Any], directory: pathlib.Path, stem: str
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    stdout_path = directory / f"{stem}.stdout"
    stderr_path = directory / f"{stem}.stderr"
    stdout = bytes(result["stdout"])
    stderr = bytes(result["stderr"])
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    return {
        "command": list(result["command"]),
        "exit_code": int(result["exit_code"]),
        "timed_out": bool(result["timed_out"]),
        "wall_seconds": float(result["wall_seconds"]),
        "stdout": str(stdout_path.resolve()),
        "stdout_sha256": sha256_bytes(stdout),
        "stderr": str(stderr_path.resolve()),
        "stderr_sha256": sha256_bytes(stderr),
    }


def _clean_build_environment(
    target_dir: pathlib.Path, temporary: pathlib.Path, *, build_variant: str
) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("UNIALLOC_") or key in {
            "LD_PRELOAD",
            "MALLOC_CONF",
            "MALLOC_ARENA_MAX",
            "MIMALLOC_ALLOW_THP",
            "MIMALLOC_ALLOW_LARGE_OS_PAGES",
            "MIMALLOC_RESERVE_HUGE_OS_PAGES",
            "RUSTC_WORKSPACE_WRAPPER",
            "RUSTC_WRAPPER",
        }:
            env.pop(key, None)
    temporary.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "CARGO_TARGET_DIR": str(target_dir.resolve()),
            "CARGO_INCREMENTAL": "0",
            "CARGO_PROFILE_RELEASE_STRIP": "none",
            "TMPDIR": str(temporary.resolve()),
            "RUSTFLAGS": f"--cfg unialloc_allocator_baseline_{build_variant}",
        }
    )
    return env


def _add_dependencies(
    manifests: Iterable[pathlib.Path],
    *,
    build_variant: str,
    implementation: redb_actix.ImplementationSnapshot,
    workspace_manifest: pathlib.Path | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    manifest_paths = tuple(dict.fromkeys(path.resolve() for path in manifests))
    for manifest in manifest_paths:
        before = sha256_file(manifest)
        for dependency in dependencies_for(build_variant, implementation.path):
            matrix.add_dependency(manifest, dependency)
        rows.append(
            {
                "path": str(manifest),
                "before_sha256": before,
                "after_sha256": sha256_file(manifest),
            }
        )
    if build_variant == "unialloc":
        if not manifest_paths:
            raise CampaignError("UniAlloc dependency injection requires a manifest")
        patch_manifest = (workspace_manifest or manifest_paths[0]).resolve()
        before = sha256_file(patch_manifest)
        matrix.add_spin_patch(patch_manifest, matrix.find_cached_spin())
        rows.append(
            {
                "path": str(patch_manifest),
                "patch": "exact-cached-spin",
                "before_sha256": before,
                "after_sha256": sha256_file(patch_manifest),
            }
        )
    return rows


def _activation_patterns(build_variant: str) -> tuple[str, ...]:
    return {
        "unialloc": ("unialloc::", "__unialloc_"),
        "mimalloc": ("mi_malloc", "mi_free", "mimalloc::"),
        "jemalloc": ("_rjem_", "jemallocator", "jemalloc_sys"),
    }.get(build_variant, ())


def allocator_activation_proof(
    binary: pathlib.Path, build_variant: str
) -> dict[str, Any]:
    binary = binary.resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise CampaignError(f"allocator target binary is not executable: {binary}")
    patterns = (
        tuple(
            pattern
            for allocator in ("unialloc", "mimalloc", "jemalloc")
            for pattern in _activation_patterns(allocator)
        )
        if build_variant == "system"
        else _activation_patterns(build_variant)
    )
    if not patterns:
        raise CampaignError(f"missing activation patterns for {build_variant}")
    result = matrix.execute(
        ["nm", "-C", "--defined-only", binary],
        cwd=binary.parent,
        env=os.environ.copy(),
        timeout=300,
    )
    text = result["stdout"].decode("utf-8", errors="replace")
    matches = sorted(
        {
            line.strip()
            for line in text.splitlines()
            if any(pattern in line for pattern in patterns)
        }
    )
    success = result["exit_code"] == 0 and (
        not matches if build_variant == "system" else bool(matches)
    )
    proof = {
        "success": success,
        "route": (
            "rust-system-default-with-forbidden-symbol-absence"
            if build_variant == "system"
            else "injected-rust-global-allocator"
        ),
        "binary": str(binary),
        "binary_sha256": sha256_file(binary),
        "nm_exit_code": result["exit_code"],
        "nm_stdout_sha256": sha256_bytes(result["stdout"]),
        "matched_symbol_count": len(matches),
        "sample_symbols": matches[:32],
    }
    if not proof["success"]:
        raise CampaignError(
            f"allocator activation proof failed for {build_variant}: {binary}"
        )
    return proof


def system_source_absence_proof(
    sources: Sequence[pathlib.Path], manifests: Sequence[pathlib.Path]
) -> dict[str, Any]:
    forbidden = (
        "#[global_allocator]",
        "unialloc::UniAlloc",
        "mimalloc::MiMalloc",
        "jemallocator::Jemalloc",
        "extern crate swc_malloc",
    )
    rows: list[dict[str, Any]] = []
    matches: list[dict[str, str]] = []
    for path in (*sources, *manifests):
        text = path.read_text(encoding="utf-8")
        rows.append({"path": str(path.resolve()), "sha256": sha256_file(path)})
        for marker in forbidden:
            if marker in text:
                matches.append({"path": str(path.resolve()), "marker": marker})
    proof = {
        "success": not matches,
        "checked_files": rows,
        "forbidden_markers": list(forbidden),
        "matches": matches,
    }
    if matches:
        raise CampaignError(f"System allocator source absence proof failed: {matches}")
    return proof


def retain_mimalloc_provenance(
    provenance: Mapping[str, Any], build_root: pathlib.Path
) -> dict[str, Any]:
    wrapper = provenance.get("wrapper_package")
    sys_package = provenance.get("sys_package")
    archives = provenance.get("static_archives")
    if (
        not isinstance(wrapper, dict)
        or wrapper.get("version") != "0.1.52"
        or not isinstance(sys_package, dict)
        or sys_package.get("version") != "0.1.49"
        or not isinstance(archives, list)
        or not archives
    ):
        raise CampaignError("mimalloc build provenance has unexpected versions")
    retained_root = build_root / "provenance" / "mimalloc"
    header_source = pathlib.Path(str(provenance["core_header"]))
    try:
        header = immutable_evidence.bind_immutable_file(
            header_source,
            retained_root / "mimalloc.h",
            expected_sha256=str(provenance["core_header_sha256"]),
        )
    except immutable_evidence.ImmutableEvidenceError as error:
        raise CampaignError(str(error)) from error
    retained_archives: list[dict[str, Any]] = []
    for index, row in enumerate(archives):
        if not isinstance(row, dict):
            raise CampaignError("mimalloc archive provenance is malformed")
        source = pathlib.Path(str(row["path"]))
        try:
            committed = immutable_evidence.bind_immutable_file(
                source,
                retained_root / f"libmimalloc-{index:02d}.a",
                expected_sha256=str(row["sha256"]),
            )
        except immutable_evidence.ImmutableEvidenceError as error:
            raise CampaignError(str(error)) from error
        retained_archives.append(immutable_evidence.artifact_ref(committed.path))
    return {
        **dict(provenance),
        "core_header": immutable_evidence.artifact_ref(header.path),
        "static_archives": retained_archives,
        "retained_after_cargo_target_cleanup": True,
    }


def validate_mimalloc_provenance(
    provenance: Any, *, lock_path: pathlib.Path
) -> None:
    if not isinstance(provenance, dict):
        raise CampaignError("reusable mimalloc build lacks provenance")
    wrapper = provenance.get("wrapper_package")
    sys_package = provenance.get("sys_package")
    if (
        not isinstance(wrapper, dict)
        or wrapper.get("version") != "0.1.52"
        or not isinstance(sys_package, dict)
        or sys_package.get("version") != "0.1.49"
        or provenance.get("lockfile_sha256") != sha256_file(lock_path)
    ):
        raise CampaignError("reusable mimalloc provenance version or lock mismatch")
    try:
        immutable_evidence.validate_artifact_ref(
            provenance.get("core_header"), context="mimalloc core header"
        )
        archives = provenance.get("static_archives")
        if not isinstance(archives, list) or not archives:
            raise immutable_evidence.ImmutableEvidenceError(
                "mimalloc archive list is empty"
            )
        for index, archive in enumerate(archives):
            immutable_evidence.validate_artifact_ref(
                archive, context=f"mimalloc static archive {index}"
            )
    except immutable_evidence.ImmutableEvidenceError as error:
        raise CampaignError(str(error)) from error


def _binary_rows(paths: Mapping[str, pathlib.Path]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "path": str(path.resolve()),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        for key, path in paths.items()
    }


def build_contract_fingerprint(
    protocol: Protocol, *, target_id: str, build_variant: str
) -> str:
    if build_variant not in {"unialloc", "mimalloc", "jemalloc", "system"}:
        raise CampaignError(f"unsupported build variant: {build_variant}")
    pins = {
        "unialloc": dict(protocol.payload["implementation"]),
        "mimalloc": {"mimalloc": "0.1.52", "libmimalloc-sys": "0.1.49"},
        "jemalloc": {"tikv-jemallocator": "0.7.0"},
        "system": {},
    }[build_variant]
    if target_id == "swc" and build_variant == "unialloc":
        pins = {
            **pins,
            "injection_route": "rustc-workspace-wrapper-direct-load-rlib-v1",
        }
    return canonical_json_sha256(
        {
            "schema_version": 1,
            "build_adapter_version": BUILD_ADAPTER_VERSION,
            "target_fingerprint": target_fingerprint(protocol, target_id),
            "build_variant": build_variant,
            "dependency_pins": pins,
        }
    )


def _build_record_path(
    raw_dir: pathlib.Path,
    target_id: str,
    variant: str,
    fingerprint: str | None = None,
) -> pathlib.Path:
    root = raw_dir / "builds" / target_id / variant
    return root / fingerprint / "build.json" if fingerprint else root / "build.json"


def _finalize_build_record(
    record: dict[str, Any], path: pathlib.Path
) -> dict[str, Any]:
    record["build_path"] = str(path.resolve())
    record["build_id"] = canonical_json_sha256(
        {
            "build_fingerprint": record["build_fingerprint"],
            "target_id": record["target_id"],
            "variant": record["variant"],
            "source_commit": record["source_commit"],
            "harness_binaries": record["harness_binaries"],
        }
    )
    record["success"] = True
    try:
        immutable_evidence.persist_immutable_json(path, record)
    except immutable_evidence.ImmutableEvidenceError as error:
        raise CampaignError(f"immutable build record collision: {path}") from error
    return record


def validate_reusable_build(
    path: pathlib.Path,
    *,
    target: TargetContract,
    variant: str,
    protocol_fingerprint: str,
    expected_implementation: redb_actix.ImplementationSnapshot,
    build_fingerprint: str | None = None,
    expected_tcmalloc_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    row = load_json(path, "reusable build record")
    expected = {
        "schema_version": BUILD_SCHEMA_VERSION,
        "protocol_fingerprint": protocol_fingerprint,
        "target_id": target.id,
        "variant": variant,
        "source_commit": target.source_commit,
        "implementation_revision": expected_implementation.revision,
        "implementation_sha256": expected_implementation.sha256,
        "success": True,
    }
    if build_fingerprint is not None:
        expected["build_fingerprint"] = build_fingerprint
    if any(row.get(key) != value for key, value in expected.items()):
        raise CampaignError(f"reusable build identity mismatch: {path}")
    if row.get("source_ref") != target.source_ref:
        raise CampaignError(f"reusable build source reference mismatch: {path}")
    if pathlib.Path(str(row.get("implementation_snapshot", ""))).resolve() != (
        expected_implementation.path.resolve()
    ):
        raise CampaignError(f"reusable build implementation snapshot mismatch: {path}")
    worktree = pathlib.Path(str(row.get("worktree", "")))
    lock_path = worktree / "Cargo.lock"
    if (
        not worktree.is_dir()
        or not lock_path.is_file()
        or row.get("derived_cargo_lock_sha256") != sha256_file(lock_path)
    ):
        raise CampaignError(f"reusable build source or lock mismatch: {path}")
    commands = row.get("commands")
    if not isinstance(commands, list) or not commands:
        raise CampaignError(f"reusable build command evidence is missing: {path}")
    for command in commands:
        if not isinstance(command, dict) or command.get("exit_code") != 0:
            raise CampaignError(f"reusable build command evidence is invalid: {path}")
        for stream in ("stdout", "stderr"):
            artifact = pathlib.Path(str(command.get(stream, "")))
            if (
                not artifact.is_file()
                or command.get(f"{stream}_sha256") != sha256_file(artifact)
            ):
                raise CampaignError(
                    f"reusable build {stream} evidence mismatch: {path}"
                )
    cargo_target = pathlib.Path(str(row.get("cargo_target_dir", "")))
    if (
        row.get("disposable_cargo_target_removed") is not True
        or cargo_target.exists()
    ):
        raise CampaignError(f"reusable build retained disposable cargo target: {path}")
    binaries = row.get("harness_binaries")
    if not isinstance(binaries, dict):
        raise CampaignError(f"reusable build lacks harness binaries: {path}")
    if set(binaries) != {harness.id for harness in target.harnesses}:
        raise CampaignError(f"reusable build harness set mismatch: {path}")
    for harness_id, artifact in binaries.items():
        if not isinstance(artifact, dict):
            raise CampaignError(f"reusable build artifact is malformed: {harness_id}")
        binary = pathlib.Path(str(artifact.get("path", "")))
        if (
            not binary.is_file()
            or not os.access(binary, os.X_OK)
            or artifact.get("sha256") != sha256_file(binary)
        ):
            raise CampaignError(f"reusable build binary mismatch: {harness_id}")
    build_variant = str(row.get("base_build_variant", row.get("variant")))
    activations = row.get("activation")
    if not isinstance(activations, dict) or not activations:
        raise CampaignError(f"reusable build lacks activation evidence: {path}")
    checked: set[str] = set()
    for artifact in binaries.values():
        binary = pathlib.Path(str(artifact["path"]))
        digest = str(artifact["sha256"])
        if digest not in checked:
            allocator_activation_proof(binary, build_variant)
            checked.add(digest)
    if target.id == "swc" and build_variant == "unialloc":
        route = row.get("direct_load_route")
        if (
            not isinstance(route, dict)
            or route.get("id")
            != "rustc-workspace-wrapper-direct-load-rlib-v1"
            or route.get("implementation_revision")
            != expected_implementation.revision
            or route.get("implementation_sha256") != expected_implementation.sha256
            or route.get("swc_cargo_lock_before_sha256") != sha256_file(lock_path)
            or route.get("swc_cargo_lock_after_sha256") != sha256_file(lock_path)
        ):
            raise CampaignError(f"reusable SWC direct-load route mismatch: {path}")
        rlib_build = route.get("rlib_build")
        if (
            not isinstance(rlib_build, dict)
            or rlib_build.get("success") is not True
            or rlib_build.get("allocator_revision")
            != expected_implementation.revision
            or rlib_build.get("unialloc_implementation_sha256")
            != expected_implementation.sha256
            or rlib_build.get("campaign_snapshot_sha256")
            != expected_implementation.sha256
            or rlib_build.get("rlib_sha256") != route.get("rlib", {}).get("sha256")
        ):
            raise CampaignError(f"reusable SWC direct-load build mismatch: {path}")
        try:
            immutable_evidence.validate_artifact_ref(
                route.get("rlib"), context="SWC direct-load rlib"
            )
            immutable_evidence.validate_artifact_ref(
                route.get("wrapper"), context="SWC direct-load wrapper"
            )
            immutable_evidence.validate_artifact_ref(
                route.get("build_stdout"), context="SWC direct-load build stdout"
            )
            immutable_evidence.validate_artifact_ref(
                route.get("build_stderr"), context="SWC direct-load build stderr"
            )
        except immutable_evidence.ImmutableEvidenceError as error:
            raise CampaignError(str(error)) from error
    if build_variant == "mimalloc":
        validate_mimalloc_provenance(
            row.get("allocator_provenance"), lock_path=lock_path
        )
    if build_variant == "jemalloc":
        package = matrix.locked_package(lock_path, "tikv-jemallocator")
        if not isinstance(package, dict) or package.get("version") != "0.7.0":
            raise CampaignError(f"reusable jemalloc lock pin mismatch: {path}")
    if build_variant == "system":
        absence = row.get("system_allocator_absence")
        if not isinstance(absence, dict) or absence.get("success") is not True:
            raise CampaignError(f"reusable System absence proof is missing: {path}")
        checked = absence.get("checked_files")
        if not isinstance(checked, list) or not checked:
            raise CampaignError(f"reusable System source proof is malformed: {path}")
        paths = [pathlib.Path(str(item.get("path", ""))) for item in checked]
        system_source_absence_proof(paths, ())
    if variant == "google_tcmalloc":
        if dict(row.get("tcmalloc_identity", {})) != dict(
            expected_tcmalloc_identity or {}
        ):
            raise CampaignError(f"reusable Google TCMalloc identity mismatch: {path}")
        proofs = row.get("tcmalloc_preload_proofs")
        if not isinstance(proofs, dict) or not proofs:
            raise CampaignError(f"reusable Google TCMalloc proof is missing: {path}")
        runtime = row.get("tcmalloc_runtime")
        if not isinstance(runtime, dict):
            raise CampaignError(f"reusable Google TCMalloc runtime is missing: {path}")
        library = pathlib.Path(str(runtime.get("library", "")))
        if (
            not library.is_file()
            or runtime.get("library_sha256") != sha256_file(library)
            or any(
                not isinstance(proof, dict)
                or proof.get("success") is not True
                or proof.get("runtime_identity")
                != {
                    "revision": google_tcmalloc_support.UPSTREAM_REVISION,
                    "hpaa_active": 1,
                    "malloc_provider_is_self": 1,
                    "exact_library_mapped": True,
                }
                for proof in proofs.values()
            )
        ):
            raise CampaignError(f"reusable Google TCMalloc proof mismatch: {path}")
    expected_id = canonical_json_sha256(
        {
            "build_fingerprint": row.get("build_fingerprint"),
            "target_id": target.id,
            "variant": variant,
            "source_commit": target.source_commit,
            "harness_binaries": binaries,
        }
    )
    if row.get("build_id") != expected_id:
        raise CampaignError(f"reusable build record digest mismatch: {path}")
    return row


def _persist_source_record(
    raw_dir: pathlib.Path, target_id: str, record: Mapping[str, Any]
) -> None:
    write_json(raw_dir / "sources" / target_id / "source.json", dict(record))


def _ensure_redb_actix_source(
    spec: redb_actix.TargetSpec,
    checkout_root: pathlib.Path,
    raw_dir: pathlib.Path,
) -> tuple[pathlib.Path, dict[str, Any]]:
    checkout = checkout_root / spec.checkout_name
    if (checkout / ".git").is_dir():
        try:
            head = redb_actix.git_output(checkout, "rev-parse", "HEAD")
            status = redb_actix.git_output(checkout, "status", "--short")
        except redb_actix.CampaignError as error:
            raise CampaignError(str(error)) from error
        if status:
            raise CampaignError(f"{spec.id} checkout is dirty:\n{status}")
        if head == spec.source_commit:
            record = {
                "target_id": spec.id,
                "repository": spec.repository,
                "source_ref": spec.source_ref,
                "source_commit": head,
                "tree": redb_actix.git_output(
                    checkout, "rev-parse", "HEAD^{tree}"
                ),
                "checkout": str(checkout.resolve()),
                "status": status,
                "cargo_lock_sha256": (
                    sha256_file(checkout / "Cargo.lock")
                    if (checkout / "Cargo.lock").is_file()
                    else None
                ),
            }
            redb_actix.write_json(
                raw_dir / "sources" / spec.id / "source.json", record
            )
            return checkout.resolve(), record
    try:
        return redb_actix.materialize_source(spec, checkout_root, raw_dir)
    except redb_actix.CampaignError as error:
        raise CampaignError(str(error)) from error


def _ensure_psr_checkout(
    spec: psr.TargetSpec, checkout_root: pathlib.Path
) -> pathlib.Path:
    checkout = checkout_root / spec.checkout
    if (checkout / ".git").is_dir():
        try:
            status = psr.git_output(checkout, "status", "--short")
            head = psr.git_output(checkout, "rev-parse", "HEAD")
        except psr.CampaignError as error:
            raise CampaignError(str(error)) from error
        if status:
            raise CampaignError(f"{spec.target_id} checkout is dirty:\n{status}")
        if head == spec.commit:
            return checkout.resolve()
    if not (checkout / ".git").is_dir():
        repository = PSR_REPOSITORIES[spec.target_id]
        checkout_root.mkdir(parents=True, exist_ok=True)
        clone = matrix.execute(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                repository,
                checkout,
            ],
            cwd=checkout_root,
            env=os.environ.copy(),
            timeout=1800,
        )
        if clone["exit_code"] != 0 or clone["timed_out"]:
            raise CampaignError(
                f"clone failed for {spec.target_id}: "
                + clone["stderr"].decode(errors="replace")[-8000:]
            )
    fetch = matrix.execute(
        ["git", "fetch", "--depth=1", "origin", spec.commit],
        cwd=checkout,
        env=os.environ.copy(),
        timeout=1800,
    )
    if fetch["exit_code"] != 0 or fetch["timed_out"]:
        raise CampaignError(
            f"fetch failed for {spec.target_id}: "
            + fetch["stderr"].decode(errors="replace")[-8000:]
        )
    reset = matrix.execute(
        ["git", "checkout", "--detach", spec.commit],
        cwd=checkout,
        env=os.environ.copy(),
        timeout=300,
    )
    if reset["exit_code"] != 0 or reset["timed_out"]:
        raise CampaignError(
            f"checkout failed for {spec.target_id}: "
            + reset["stderr"].decode(errors="replace")[-8000:]
        )
    try:
        record = psr.verify_checkout(checkout, spec)
    except psr.CampaignError as error:
        raise CampaignError(str(error)) from error
    return pathlib.Path(str(record["checkout"]))


def materialize_sources(
    target_ids: Sequence[str],
    *,
    contract: CampaignContract,
    checkout_root: pathlib.Path,
    raw_dir: pathlib.Path,
) -> dict[str, SourceContext]:
    contexts: dict[str, SourceContext] = {}
    for target_id in target_ids:
        if target_id in collections_oxipng.TARGETS:
            spec = collections_oxipng.TARGETS[target_id]
            try:
                _path, record = collections_oxipng.write_source_audit(spec, raw_dir)
            except collections_oxipng.CampaignError as error:
                raise CampaignError(str(error)) from error
            checkout = (
                spec.checkout / "library/alloctests"
                if target_id == "collections"
                else spec.checkout
            )
            source_record = {
                **record,
                "source_ref": contract.targets[target_id].source_ref,
                "source_commit": contract.targets[target_id].source_commit,
            }
        elif target_id in redb_actix.TARGETS:
            spec = redb_actix.TARGETS[target_id]
            checkout, source_record = _ensure_redb_actix_source(
                spec, checkout_root, raw_dir
            )
        else:
            spec = psr.TARGET_SPECS[target_id]
            checkout = _ensure_psr_checkout(spec, checkout_root)
            try:
                source_record = psr.verify_checkout(checkout, spec)
            except psr.CampaignError as error:
                raise CampaignError(str(error)) from error
            source_record = {
                **source_record,
                "repository": PSR_REPOSITORIES[target_id],
                "source_commit": spec.commit,
            }
        if str(source_record.get("source_commit", source_record.get("head"))) != (
            contract.targets[target_id].source_commit
        ):
            raise CampaignError(f"materialized source pin mismatch: {target_id}")
        _persist_source_record(raw_dir, target_id, source_record)
        contexts[target_id] = SourceContext(
            target_id=target_id,
            checkout=pathlib.Path(checkout).resolve(),
            record=source_record,
        )
    return contexts


def _base_build_record(
    *,
    protocol: Protocol,
    contract: CampaignContract,
    target: TargetContract,
    build_variant: str,
    implementation: redb_actix.ImplementationSnapshot,
    toolchain: str,
    source: SourceContext,
) -> dict[str, Any]:
    return {
        "schema_version": BUILD_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "protocol_fingerprint": protocol.fingerprint,
        "target_fingerprint": target_fingerprint(protocol, target.id),
        "build_fingerprint": build_contract_fingerprint(
            protocol, target_id=target.id, build_variant=build_variant
        ),
        "target_id": target.id,
        "variant": build_variant,
        "source_ref": target.source_ref,
        "source_commit": target.source_commit,
        "source_record": dict(source.record),
        "toolchain": toolchain,
        "implementation_revision": contract.suite.implementation_revision,
        "implementation_sha256": contract.suite.implementation_sha256,
        "implementation_snapshot": str(implementation.path),
        "stats_enabled": False,
        "allocator_route": (
            "rust-system-default"
            if build_variant == "system"
            else "injected-rust-global-allocator"
        ),
    }


def _copy_executable(source: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
    if not source.is_file() or not os.access(source, os.X_OK):
        raise CampaignError(f"built executable is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(destination.stat().st_mode | 0o111)
    return destination.resolve()


def _build_collections_or_oxipng(
    *,
    protocol: Protocol,
    contract: CampaignContract,
    target: TargetContract,
    source: SourceContext,
    build_variant: str,
    implementation: redb_actix.ImplementationSnapshot,
    raw_dir: pathlib.Path,
    toolchain: str,
    jobs: int,
    timeout: int,
) -> dict[str, Any]:
    spec = collections_oxipng.TARGETS[target.id]
    build_root = raw_dir / "builds" / target.id / build_variant
    worktree = build_root / "source"
    target_dir = build_root / "cargo-target"
    shutil.rmtree(worktree, ignore_errors=True)
    shutil.rmtree(target_dir, ignore_errors=True)
    collections_oxipng.copy_source(spec, worktree)
    manifest = worktree / "Cargo.toml"
    matrix.ensure_standalone_workspace(manifest)
    dependency_audit: list[dict[str, Any]] = []
    injected_sources: list[pathlib.Path] = []
    if build_variant != "system":
        dependency_audit = _add_dependencies(
            [manifest],
            build_variant=build_variant,
            implementation=implementation,
            workspace_manifest=manifest,
        )
        injected_sources = (
            [worktree / "benches/lib.rs"]
            if target.id == "collections"
            else [
                worktree / "src/main.rs",
                worktree / "benches/filters.rs",
                worktree / "benches/strategies.rs",
                worktree / "benches/reductions.rs",
            ]
        )
        injection = allocator_source(build_variant)
        for path in injected_sources:
            before = path.read_text(encoding="utf-8")
            if matrix.INSTRUMENTATION_MARKER in before:
                raise CampaignError(f"allocator injection already exists: {path}")
            path.write_text(before.rstrip() + injection, encoding="utf-8")
    env = _clean_build_environment(
        target_dir,
        raw_dir / "tmp" / "builds" / target.id / build_variant,
        build_variant=build_variant,
    )
    commands: list[dict[str, Any]] = []
    for index, original in enumerate(
        collections_oxipng.build_commands(spec, toolchain, jobs)
    ):
        command = [value for value in original if value != "--locked"]
        result = matrix.execute(command, cwd=worktree, env=env, timeout=timeout)
        commands.append(_command_record(result, build_root, f"build-{index:02d}"))
        if result["exit_code"] != 0 or result["timed_out"]:
            raise CampaignError(
                f"build failed for {target.id}/{build_variant}:\n"
                + result["stderr"].decode(errors="replace")[-8000:]
            )
    binary_names = sorted({harness.binary for harness in spec.harnesses})
    binaries: dict[str, pathlib.Path] = {}
    for name in binary_names:
        built = collections_oxipng.find_executable(target_dir, name)
        binaries[name] = _copy_executable(
            built, build_root / "binaries" / built.name
        )
    activations = {
        name: allocator_activation_proof(binary, build_variant)
        for name, binary in binaries.items()
    }
    binary_rows = _binary_rows(binaries)
    harness_binaries = {
        harness.id: dict(binary_rows[harness.binary]) for harness in spec.harnesses
    }
    lock = worktree / "Cargo.lock"
    if not lock.is_file():
        raise CampaignError(f"derived Cargo.lock is missing: {target.id}")
    record = {
        **_base_build_record(
            protocol=protocol,
            contract=contract,
            target=target,
            build_variant=build_variant,
            implementation=implementation,
            toolchain=toolchain,
            source=source,
        ),
        "worktree": str(worktree.resolve()),
        "derived_source_sha256": collections_oxipng.sha256_tree(worktree),
        "derived_cargo_lock": str(lock.resolve()),
        "derived_cargo_lock_sha256": sha256_file(lock),
        "dependency_audit": dependency_audit,
        "injected_sources": {
            str(path.relative_to(worktree)): sha256_file(path)
            for path in injected_sources
        },
        "commands": commands,
        "binaries": binary_rows,
        "harness_binaries": harness_binaries,
        "activation": activations,
        "patch": {
            "manifest": str(manifest.resolve()),
            "manifest_sha256": sha256_file(manifest),
        },
    }
    if build_variant == "system":
        record["system_allocator_absence"] = system_source_absence_proof(
            (
                [worktree / "benches/lib.rs"]
                if target.id == "collections"
                else [
                    worktree / "src/main.rs",
                    worktree / "benches/filters.rs",
                    worktree / "benches/strategies.rs",
                    worktree / "benches/reductions.rs",
                ]
            ),
            [manifest],
        )
    if build_variant == "mimalloc":
        try:
            record["allocator_provenance"] = retain_mimalloc_provenance(
                matrix.mimalloc_build_provenance(lock, target_dir), build_root
            )
        except matrix.MatrixError as error:
            raise CampaignError(str(error)) from error
    record["cargo_target_dir"] = str(target_dir.resolve())
    shutil.rmtree(target_dir)
    record["disposable_cargo_target_removed"] = not target_dir.exists()
    return _finalize_build_record(
        record, _build_record_path(raw_dir, target.id, build_variant)
    )


def _redb_source_for(build_variant: str) -> str:
    allocator = (
        "#[global_allocator]\n"
        "static ALLOCATOR: unialloc::UniAlloc = unialloc::UniAlloc;\n"
    )
    if redb_actix.REDB_RUNNER_SOURCE.count(allocator) != 1:
        raise CampaignError("redb workload allocator source drifted")
    replacement = allocator_source(build_variant).lstrip()
    return redb_actix.REDB_RUNNER_SOURCE.replace(allocator, replacement, 1)


def _build_redb(
    *,
    protocol: Protocol,
    contract: CampaignContract,
    target: TargetContract,
    source: SourceContext,
    build_variant: str,
    implementation: redb_actix.ImplementationSnapshot,
    raw_dir: pathlib.Path,
    toolchain: str,
    jobs: int,
    timeout: int,
) -> dict[str, Any]:
    build_root = raw_dir / "builds" / target.id / build_variant
    worktree = build_root / "source"
    target_dir = build_root / "cargo-target"
    shutil.rmtree(worktree, ignore_errors=True)
    shutil.rmtree(target_dir, ignore_errors=True)
    (worktree / "src").mkdir(parents=True)
    manifest = worktree / "Cargo.toml"
    manifest.write_text(
        '[package]\nname = "unialloc-redb-actix-runner"\nversion = "0.1.0"\n'
        'edition = "2024"\nrust-version = "1.89"\npublish = false\n\n'
        "[dependencies]\n"
        + f"redb = {{ path = {json.dumps(str(source.checkout))} }}\n\n"
        + "[workspace]\n",
        encoding="utf-8",
    )
    dependency_audit = (
        _add_dependencies(
            [manifest],
            build_variant=build_variant,
            implementation=implementation,
            workspace_manifest=manifest,
        )
        if build_variant != "system"
        else []
    )
    runner_source = _redb_source_for(build_variant)
    source_path = worktree / "src/main.rs"
    source_path.write_text(runner_source, encoding="utf-8")
    env = _clean_build_environment(
        target_dir,
        raw_dir / "tmp" / "builds" / target.id / build_variant,
        build_variant=build_variant,
    )
    command = [
        "cargo",
        f"+{toolchain}",
        "build",
        "--release",
        "--jobs",
        str(jobs),
    ]
    result = matrix.execute(command, cwd=worktree, env=env, timeout=timeout)
    command_record = _command_record(result, build_root, "build")
    built = target_dir / "release/unialloc-redb-actix-runner"
    if result["exit_code"] != 0 or result["timed_out"] or not built.is_file():
        raise CampaignError(
            f"redb build failed for {build_variant}:\n"
            + result["stderr"].decode(errors="replace")[-8000:]
        )
    binary = _copy_executable(built, build_root / "binaries/redb-primary")
    activation = allocator_activation_proof(binary, build_variant)
    binary_row = _binary_rows({"primary": binary})["primary"]
    lock = worktree / "Cargo.lock"
    record = {
        **_base_build_record(
            protocol=protocol,
            contract=contract,
            target=target,
            build_variant=build_variant,
            implementation=implementation,
            toolchain=toolchain,
            source=source,
        ),
        "worktree": str(worktree.resolve()),
        "workload_source_sha256": sha256_file(source_path),
        "manifest_sha256": sha256_file(manifest),
        "derived_cargo_lock_sha256": sha256_file(lock),
        "dependency_audit": dependency_audit,
        "commands": [command_record],
        "binary": str(binary),
        "binary_sha256": binary_row["sha256"],
        "harness_binaries": {
            harness.id: dict(binary_row) for harness in target.harnesses
        },
        "activation": {"primary": activation},
    }
    if build_variant == "system":
        record["system_allocator_absence"] = system_source_absence_proof(
            [source_path], [manifest]
        )
    if build_variant == "mimalloc":
        try:
            record["allocator_provenance"] = retain_mimalloc_provenance(
                matrix.mimalloc_build_provenance(lock, target_dir), build_root
            )
        except matrix.MatrixError as error:
            raise CampaignError(str(error)) from error
    record["cargo_target_dir"] = str(target_dir.resolve())
    shutil.rmtree(target_dir)
    record["disposable_cargo_target_removed"] = not target_dir.exists()
    return _finalize_build_record(
        record, _build_record_path(raw_dir, target.id, build_variant)
    )


def _neutral_allocator_marker_source(build_variant: str) -> str:
    return (
        allocator_source(build_variant)
        + "\n#[unsafe(no_mangle)]\n"
        + "pub static UNIALLOC_ALLOCATOR_BASELINE_MARKER: u8 = 0xa5;\n"
        + "#[inline(never)]\n"
        + 'extern "C" fn unialloc_allocator_baseline_verify_marker() {\n'
        + "    let value = unsafe { std::ptr::read_volatile("
        + "&raw const UNIALLOC_ALLOCATOR_BASELINE_MARKER) };\n"
        + "    assert_eq!(value, 0xa5);\n"
        + "}\n"
        + "#[used]\n"
        + '#[cfg_attr(target_family = "unix", unsafe(link_section = ".init_array"))]\n'
        + 'static UNIALLOC_ALLOCATOR_BASELINE_MARKER_INIT: extern "C" fn() = '
        + "unialloc_allocator_baseline_verify_marker;\n"
    )


def _build_actix(
    *,
    protocol: Protocol,
    contract: CampaignContract,
    target: TargetContract,
    source: SourceContext,
    build_variant: str,
    implementation: redb_actix.ImplementationSnapshot,
    raw_dir: pathlib.Path,
    toolchain: str,
    jobs: int,
    timeout: int,
) -> dict[str, Any]:
    build_root = raw_dir / "builds" / target.id / build_variant
    worktree = build_root / "source"
    target_dir = build_root / "cargo-target"
    shutil.rmtree(worktree, ignore_errors=True)
    shutil.rmtree(target_dir, ignore_errors=True)
    matrix.copy_checkout(source.checkout, worktree)
    try:
        bounded_patch = redb_actix.patch_actix_get_body_benchmark(worktree)
    except redb_actix.CampaignError as error:
        raise CampaignError(str(error)) from error
    source_audit: list[dict[str, Any]] = []
    injection = _neutral_allocator_marker_source(build_variant)
    for relative in sorted({value[3] for value in redb_actix.ACTIX_BENCHES.values()}):
        path = worktree / relative
        before = path.read_bytes()
        text = before.decode("utf-8")
        if "UNIALLOC_ALLOCATOR_BASELINE_MARKER" in text:
            raise CampaignError(f"Actix allocator marker already exists: {relative}")
        path.write_text(text.rstrip() + "\n" + injection, encoding="utf-8")
        source_audit.append(
            {
                "path": relative.as_posix(),
                "upstream_or_bounded_sha256": sha256_bytes(before),
                "instrumented_sha256": sha256_file(path),
            }
        )
    package_manifests = sorted(
        {
            worktree / package / "Cargo.toml"
            for package, _bench, _selector, _source in redb_actix.ACTIX_BENCHES.values()
        }
    )
    dependency_audit = (
        _add_dependencies(
            package_manifests,
            build_variant=build_variant,
            implementation=implementation,
            workspace_manifest=worktree / "Cargo.toml",
        )
        if build_variant != "system"
        else []
    )
    env = _clean_build_environment(
        target_dir,
        raw_dir / "tmp" / "builds" / target.id / build_variant,
        build_variant=build_variant,
    )
    selections = sorted(
        {value[:2] for value in redb_actix.ACTIX_BENCHES.values()}
    )
    executables: dict[str, pathlib.Path] = {}
    commands: list[dict[str, Any]] = []
    for package, bench in selections:
        command = [
            "cargo",
            f"+{toolchain}",
            "bench",
            "--no-run",
            "--package",
            package,
            "--bench",
            bench,
            "--message-format=json",
            "--jobs",
            str(jobs),
        ]
        result = matrix.execute(command, cwd=worktree, env=env, timeout=timeout)
        commands.append(_command_record(result, build_root, f"build-{package}-{bench}"))
        candidates = redb_actix.executables_from_cargo_json(
            result["stdout"].decode(errors="replace"), bench
        )
        if result["exit_code"] != 0 or result["timed_out"] or len(candidates) != 1:
            raise CampaignError(
                f"Actix build failed for {build_variant}/{package}/{bench}:\n"
                + result["stderr"].decode(errors="replace")[-8000:]
            )
        executables[bench] = _copy_executable(
            candidates[0], build_root / "binaries" / bench
        )
    activations = {
        bench: allocator_activation_proof(binary, build_variant)
        for bench, binary in executables.items()
    }
    executable_rows = _binary_rows(executables)
    harness_binaries = {
        harness.id: dict(
            executable_rows[redb_actix.ACTIX_BENCHES[harness.id][1]]
        )
        for harness in target.harnesses
    }
    lock = worktree / "Cargo.lock"
    record = {
        **_base_build_record(
            protocol=protocol,
            contract=contract,
            target=target,
            build_variant=build_variant,
            implementation=implementation,
            toolchain=toolchain,
            source=source,
        ),
        "worktree": str(worktree.resolve()),
        "source_audit": source_audit,
        "bounded_get_body_patch": bounded_patch,
        "derived_cargo_lock_sha256": sha256_file(lock),
        "dependency_audit": dependency_audit,
        "commands": commands,
        "executables": {
            bench: str(path) for bench, path in executables.items()
        },
        "executable_artifacts": executable_rows,
        "harness_binaries": harness_binaries,
        "activation": activations,
    }
    if build_variant == "system":
        record["system_allocator_absence"] = system_source_absence_proof(
            [worktree / row["path"] for row in source_audit],
            [*package_manifests, worktree / "Cargo.toml"],
        )
    if build_variant == "mimalloc":
        try:
            record["allocator_provenance"] = retain_mimalloc_provenance(
                matrix.mimalloc_build_provenance(lock, target_dir), build_root
            )
        except matrix.MatrixError as error:
            raise CampaignError(str(error)) from error
    record["cargo_target_dir"] = str(target_dir.resolve())
    shutil.rmtree(target_dir)
    record["disposable_cargo_target_removed"] = not target_dir.exists()
    return _finalize_build_record(
        record, _build_record_path(raw_dir, target.id, build_variant)
    )


def _replace_psr_allocator(
    prepared: Mapping[str, Any], *, build_variant: str
) -> dict[str, Any]:
    source = pathlib.Path(str(prepared["allocator_source"]))
    text = source.read_text(encoding="utf-8")
    original = psr.allocator_source().lstrip()
    if text.count(original) != 1:
        raise CampaignError(f"primary allocator source drifted: {source}")
    replacement = allocator_source(build_variant).lstrip()
    source.write_text(text.replace(original, replacement, 1), encoding="utf-8")
    return {
        "path": str(source.resolve()),
        "sha256": sha256_file(source),
        "build_variant": build_variant,
    }


def _build_psr_target(
    *,
    protocol: Protocol,
    contract: CampaignContract,
    target: TargetContract,
    source: SourceContext,
    build_variant: str,
    implementation: redb_actix.ImplementationSnapshot,
    raw_dir: pathlib.Path,
    toolchain: str,
    jobs: int,
    timeout: int,
) -> dict[str, Any]:
    spec = psr.TARGET_SPECS[target.id]
    build_root = raw_dir / "builds" / target.id / build_variant
    worktree = build_root / "source"
    target_dir = build_root / "cargo-target"
    shutil.rmtree(worktree, ignore_errors=True)
    shutil.rmtree(target_dir, ignore_errors=True)
    try:
        prepared = psr.prepare_worktree(spec, source.checkout, worktree, build_root)
    except psr.CampaignError as error:
        raise CampaignError(str(error)) from error
    allocator_patch = _replace_psr_allocator(prepared, build_variant=build_variant)
    manifest = pathlib.Path(str(prepared["manifest"]))
    direct_load_route = target.id == "swc" and build_variant == "unialloc"
    dependency_audit = (
        _add_dependencies(
            [manifest],
            build_variant=build_variant,
            implementation=implementation,
            workspace_manifest=worktree / "Cargo.toml",
        )
        if build_variant != "system" and not direct_load_route
        else []
    )
    env = _clean_build_environment(
        target_dir,
        raw_dir / "tmp" / "builds" / target.id / build_variant,
        build_variant=build_variant,
    )
    env.update(
        {
            "POLARS_MAX_THREADS": "1",
            "RAYON_NUM_THREADS": "1",
            "TOKIO_WORKER_THREADS": "1",
            "PYTHON_SYS_EXECUTABLE": shutil.which("python3") or "python3",
        }
    )
    if target.id == "swc":
        env["RUSTFLAGS"] += (
            " -Zshare-generics=y -C target-feature=+sse2"
            " -C link-args=-Wl,-z,nodelete"
        )
    direct_load: dict[str, Any] | None = None
    direct_wrapper: pathlib.Path | None = None
    swc_lock_before = (
        sha256_file(worktree / "Cargo.lock") if direct_load_route else None
    )
    if direct_load_route:
        if toolchain != psr.TOOLCHAIN:
            raise CampaignError(
                "SWC direct-load route requires the primary PSR toolchain"
            )
        snapshot = {
            "path": str(implementation.path),
            "unialloc_implementation_sha256": implementation.sha256,
            "campaign_snapshot_sha256": implementation.sha256,
            "allocator_revision": implementation.revision,
        }
        try:
            direct_load = psr.build_direct_load_rlib(
                build_root, snapshot, jobs, timeout
            )
            direct_wrapper = psr.ensure_direct_load_wrapper(
                build_root / "tools/unialloc-direct-load-wrapper"
            )
        except psr.CampaignError as error:
            raise CampaignError(str(error)) from error
        env.update(
            {
                "RUSTC_WORKSPACE_WRAPPER": str(direct_wrapper),
                "UNIALLOC_DIRECT_LOAD_RLIB": str(direct_load["rlib"]),
                "UNIALLOC_DIRECT_LOAD_DEPENDENCY_DIR": str(
                    direct_load["dependency_dir"]
                ),
                "UNIALLOC_DIRECT_LOAD_TARGET_CRATE": str(
                    spec.bench_name
                ).replace("-", "_"),
            }
        )
    command = psr.build_command(spec, locked=direct_load_route)
    command[1] = f"+{toolchain}"
    command.extend(["--jobs", str(jobs)])
    result = matrix.execute(command, cwd=worktree, env=env, timeout=timeout)
    command_record = _command_record(result, build_root, "build")
    executable: pathlib.Path | None = None
    if result["exit_code"] == 0 and not result["timed_out"]:
        try:
            executable = psr.cargo_executable(result["stdout"], spec)
        except psr.CampaignError as error:
            raise CampaignError(str(error)) from error
    if executable is None:
        raise CampaignError(
            f"{target.id} build failed for {build_variant}:\n"
            + result["stderr"].decode(errors="replace")[-8000:]
        )
    output_name = (
        "polars-primary" if spec.mode == "polars-driver" else str(spec.bench_name)
    )
    binary = _copy_executable(executable, build_root / "binaries" / output_name)
    activation = allocator_activation_proof(binary, build_variant)
    binary_row = _binary_rows({"primary": binary})["primary"]
    lock = worktree / "Cargo.lock"
    if direct_load_route and sha256_file(lock) != swc_lock_before:
        raise CampaignError("SWC Cargo.lock changed during direct-load build")
    record = {
        **_base_build_record(
            protocol=protocol,
            contract=contract,
            target=target,
            build_variant=build_variant,
            implementation=implementation,
            toolchain=toolchain,
            source=source,
        ),
        "worktree": str(worktree.resolve()),
        "allocator_patch": allocator_patch,
        "direct_load_route": (
            {
                "id": "rustc-workspace-wrapper-direct-load-rlib-v1",
                "implementation_revision": implementation.revision,
                "implementation_sha256": implementation.sha256,
                "rlib": immutable_evidence.artifact_ref(
                    pathlib.Path(str(direct_load["rlib"]))
                ),
                "dependency_dir": str(direct_load["dependency_dir"]),
                "wrapper": immutable_evidence.artifact_ref(direct_wrapper),
                "build_stdout": immutable_evidence.artifact_ref(
                    build_root / "direct-load/build.stdout"
                ),
                "build_stderr": immutable_evidence.artifact_ref(
                    build_root / "direct-load/build.stderr"
                ),
                "target_crate": str(spec.bench_name).replace("-", "_"),
                "swc_cargo_lock_before_sha256": swc_lock_before,
                "swc_cargo_lock_after_sha256": sha256_file(lock),
                "rlib_build": direct_load,
            }
            if direct_load is not None and direct_wrapper is not None
            else None
        ),
        "manifest_sha256": sha256_file(manifest),
        "derived_cargo_lock_sha256": sha256_file(lock),
        "dependency_audit": dependency_audit,
        "commands": [command_record],
        "binary": str(binary),
        "binary_sha256": binary_row["sha256"],
        "harness_binaries": {
            harness.id: dict(binary_row) for harness in target.harnesses
        },
        "activation": {"primary": activation},
    }
    if build_variant == "system":
        record["system_allocator_absence"] = system_source_absence_proof(
            [pathlib.Path(str(prepared["allocator_source"]))], [manifest]
        )
    if build_variant == "mimalloc":
        try:
            record["allocator_provenance"] = retain_mimalloc_provenance(
                matrix.mimalloc_build_provenance(lock, target_dir), build_root
            )
        except matrix.MatrixError as error:
            raise CampaignError(str(error)) from error
    record["cargo_target_dir"] = str(target_dir.resolve())
    shutil.rmtree(target_dir)
    record["disposable_cargo_target_removed"] = not target_dir.exists()
    return _finalize_build_record(
        record, _build_record_path(raw_dir, target.id, build_variant)
    )


def build_base_variant(
    *,
    protocol: Protocol,
    contract: CampaignContract,
    target: TargetContract,
    source: SourceContext,
    build_variant: str,
    implementation: redb_actix.ImplementationSnapshot,
    raw_dir: pathlib.Path,
    toolchain: str,
    jobs: int,
    timeout: int,
    reuse: bool,
) -> dict[str, Any]:
    path = _build_record_path(raw_dir, target.id, build_variant)
    fingerprint = build_contract_fingerprint(
        protocol, target_id=target.id, build_variant=build_variant
    )
    if path.exists():
        if not reuse:
            raise CampaignError(
                "build record already exists; pass --reuse or select a new raw "
                f"directory: {path}"
            )
        return validate_reusable_build(
            path,
            target=target,
            variant=build_variant,
            protocol_fingerprint=protocol.fingerprint,
            expected_implementation=implementation,
            build_fingerprint=fingerprint,
        )
    if target.id in collections_oxipng.TARGETS:
        builder = _build_collections_or_oxipng
    elif target.id == "redb":
        builder = _build_redb
    elif target.id == "actix_web":
        builder = _build_actix
    else:
        builder = _build_psr_target
    return builder(
        protocol=protocol,
        contract=contract,
        target=target,
        source=source,
        build_variant=build_variant,
        implementation=implementation,
        raw_dir=raw_dir,
        toolchain=toolchain,
        jobs=jobs,
        timeout=timeout,
    )


def _alias_build_record(
    *,
    base: Mapping[str, Any],
    target: TargetContract,
    variant: str,
    protocol: Protocol,
    raw_dir: pathlib.Path,
    tcmalloc_runtime: Mapping[str, Any] | None,
    timeout: int,
) -> dict[str, Any]:
    tcmalloc_identity = (
        tcmalloc_protocol_identity(tcmalloc_runtime)
        if variant == "google_tcmalloc" and tcmalloc_runtime is not None
        else None
    )
    fingerprint = variant_fingerprint(
        protocol,
        target_id=target.id,
        variant=variant,
        tcmalloc_identity=tcmalloc_identity,
    )
    path = _build_record_path(raw_dir, target.id, variant, fingerprint)
    record = {
        **dict(base),
        "schema_version": BUILD_SCHEMA_VERSION,
        "protocol_fingerprint": protocol.fingerprint,
        "target_id": target.id,
        "variant": variant,
        "base_build_variant": str(base["variant"]),
        "base_build_path": str(base["build_path"]),
        "base_build_id": str(base["build_id"]),
        "build_fingerprint": fingerprint,
    }
    if variant == "mimalloc_no_thp":
        record.update(
            {
                "runtime_environment_overrides": (
                    matrix.allocator_runtime_environment_overrides(
                        "mimalloc_no_thp"
                    )
                ),
                "thp_mode": matrix.allocator_thp_mode("mimalloc_no_thp"),
            }
        )
    elif variant == "google_tcmalloc":
        if not isinstance(tcmalloc_runtime, Mapping):
            raise CampaignError("Google TCMalloc runtime evidence is missing")
        proofs: dict[str, Any] = {}
        seen: dict[str, str] = {}
        for harness_id, artifact in record["harness_binaries"].items():
            binary = pathlib.Path(str(artifact["path"]))
            digest = str(artifact["sha256"])
            if digest not in seen:
                try:
                    proof = matrix.prove_tcmalloc_preload(
                        binary, dict(tcmalloc_runtime), timeout=timeout
                    )
                except matrix.MatrixError as error:
                    raise CampaignError(str(error)) from error
                proof_id = canonical_json_sha256(proof)
                proofs[proof_id] = proof
                seen[digest] = proof_id
            artifact["google_tcmalloc_preflight_proof"] = seen[digest]
        record.update(
            {
                "allocator_route": "rust-system-api-with-authenticated-ld-preload",
                "tcmalloc_runtime": dict(tcmalloc_runtime),
                "tcmalloc_identity": tcmalloc_identity,
                "tcmalloc_preload_proofs": proofs,
                "thp_mode": "modern-hpaa-adaptive-subrelease",
            }
        )
    else:
        raise CampaignError(f"unsupported build alias: {variant}")
    return _finalize_build_record(record, path)


def ensure_variant_builds(
    *,
    protocol: Protocol,
    contract: CampaignContract,
    plan: CampaignPlan,
    sources: Mapping[str, SourceContext],
    implementation: redb_actix.ImplementationSnapshot,
    raw_dir: pathlib.Path,
    toolchain: str,
    jobs: int,
    timeout: int,
    reuse: bool,
    tcmalloc_runtime: Mapping[str, Any],
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for target_id in plan.requested_targets:
        target = contract.targets[target_id]
        base_builds: dict[str, dict[str, Any]] = {}
        variants: dict[str, dict[str, Any]] = {}
        for variant in plan.execution_variants:
            google_identity = (
                tcmalloc_protocol_identity(tcmalloc_runtime)
                if variant == "google_tcmalloc"
                else None
            )
            alias_fingerprint = (
                variant_fingerprint(
                    protocol,
                    target_id=target_id,
                    variant=variant,
                    tcmalloc_identity=google_identity,
                )
                if variant in {"mimalloc_no_thp", "google_tcmalloc"}
                else None
            )
            alias_path = _build_record_path(
                raw_dir, target_id, variant, alias_fingerprint
            )
            if (
                variant in {"mimalloc_no_thp", "google_tcmalloc"}
                and alias_path.exists()
            ):
                if not reuse:
                    raise CampaignError(
                        "build record already exists; pass --reuse or select a new "
                        f"raw directory: {alias_path}"
                    )
                variants[variant] = validate_reusable_build(
                    alias_path,
                    target=target,
                    variant=variant,
                    protocol_fingerprint=protocol.fingerprint,
                    expected_implementation=implementation,
                    build_fingerprint=alias_fingerprint,
                    expected_tcmalloc_identity=google_identity,
                )
                continue
            build_variant = build_variant_for(variant)
            if build_variant not in base_builds:
                base_builds[build_variant] = build_base_variant(
                    protocol=protocol,
                    contract=contract,
                    target=target,
                    source=sources[target_id],
                    build_variant=build_variant,
                    implementation=implementation,
                    raw_dir=raw_dir,
                    toolchain=toolchain,
                    jobs=jobs,
                    timeout=timeout,
                    reuse=reuse,
                )
            base = base_builds[build_variant]
            if variant in {"mimalloc_no_thp", "google_tcmalloc"}:
                variants[variant] = _alias_build_record(
                    base=base,
                    target=target,
                    variant=variant,
                    protocol=protocol,
                    raw_dir=raw_dir,
                    tcmalloc_runtime=tcmalloc_runtime,
                    timeout=timeout,
                )
            else:
                variants[variant] = base
        result[target_id] = variants
    return result


def _runtime_variant(variant: str) -> str:
    return "tcmalloc" if variant == "google_tcmalloc" else variant


def _runtime_prefix(
    target_id: str,
    variant: str,
    *,
    tcmalloc_runtime: Mapping[str, Any],
) -> list[str]:
    execution = TARGET_EXECUTION_CONTRACTS[target_id]
    try:
        affinity = matrix.measurement_command_prefix(
            str(execution["cpu_list"]), int(execution["numa_node"])
        )
        allocator = (
            matrix.allocator_runtime_prefix("tcmalloc", dict(tcmalloc_runtime))
            if variant == "google_tcmalloc"
            else []
        )
    except matrix.MatrixError as error:
        raise CampaignError(str(error)) from error
    return [*affinity, *allocator]


def _runtime_environment(
    target_id: str,
    *,
    variant: str,
    raw_dir: pathlib.Path,
    runtime_dependency: Mapping[str, Any] | None,
) -> dict[str, str]:
    overrides = dict(TARGET_EXECUTION_CONTRACTS[target_id]["runtime_overrides"])
    if target_id in psr.TARGET_SPECS:
        overrides["PYTHON_SYS_EXECUTABLE"] = shutil.which("python3") or "python3"
    if variant in {"mimalloc", "mimalloc_no_thp"}:
        overrides.update(
            matrix.allocator_runtime_environment_overrides(variant)
        )
    if target_id == "rustpython":
        if runtime_dependency is None:
            raise CampaignError("RustPython runtime dependency is missing")
        overrides["LD_LIBRARY_PATH"] = str(runtime_dependency["library_directory"])
    try:
        return type_isolation_suite_contract.clean_runtime_environment(
            raw_dir / "tmp" / "measurements" / target_id / variant,
            overrides=overrides,
        )
    except type_isolation_suite_contract.SuiteContractError as error:
        raise CampaignError(str(error)) from error


def _artifact(path: pathlib.Path) -> dict[str, Any]:
    try:
        return immutable_evidence.artifact_ref(path)
    except immutable_evidence.ImmutableEvidenceError as error:
        raise CampaignError(str(error)) from error


def _tcmalloc_cell_identity(
    variant: str, stderr: bytes
) -> dict[str, Any]:
    evidence = matrix.google_tcmalloc_target_identity_evidence(
        _runtime_variant(variant), stderr
    )
    if evidence["google_tcmalloc_target_identity_verified"] is not True:
        raise CampaignError(
            f"Google TCMalloc target identity marker mismatch for {variant}: {evidence}"
        )
    return evidence


def mimalloc_thp_runtime_evidence(variant: str, stderr: bytes) -> dict[str, Any]:
    values = [
        value.decode("ascii")
        for value in re.findall(
            rb"UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE=(0|1|error)\n", stderr
        )
    ]
    expected = {"mimalloc": "0", "mimalloc_no_thp": "1"}.get(variant)
    if expected is None:
        if values:
            raise CampaignError(f"unexpected mimalloc THP marker for {variant}")
        return {"applicable": False, "verified": True, "observed": None}
    if values != [expected]:
        raise CampaignError(
            f"mimalloc PR_GET_THP_DISABLE mismatch for {variant}: "
            f"expected {[expected]}, got {values}"
        )
    return {
        "applicable": True,
        "verified": True,
        "pr_get_thp_disable": int(expected),
        "observed": expected,
    }


def validate_cell_metrics_from_artifacts(
    row: Mapping[str, Any], *, target: TargetContract, harness: HarnessContract
) -> None:
    artifacts = row.get("artifacts")
    metrics = row.get("metrics")
    if not isinstance(artifacts, dict) or not isinstance(metrics, dict):
        raise CampaignError("cell artifact or metric contract is malformed")
    try:
        stdout_path = immutable_evidence.validate_artifact_ref(
            artifacts.get("stdout"), context="cell stdout"
        )
        stderr_path = immutable_evidence.validate_artifact_ref(
            artifacts.get("stderr"), context="cell stderr"
        )
        time_path = immutable_evidence.validate_artifact_ref(
            artifacts.get("gnu_time"), context="cell GNU time"
        )
    except immutable_evidence.ImmutableEvidenceError as error:
        raise CampaignError(str(error)) from error
    stdout = stdout_path.read_bytes()
    stderr = stderr_path.read_bytes()
    try:
        binary_path = immutable_evidence.validate_artifact_ref(
            artifacts.get("binary"), context="cell binary"
        )
    except immutable_evidence.ImmutableEvidenceError as error:
        raise CampaignError(str(error)) from error
    command = row.get("command")
    measured_command = row.get("measured_command")
    runtime_policy = row.get("runtime_policy")
    if (
        not isinstance(command, list)
        or not command
        or pathlib.Path(str(command[0])).resolve() != binary_path
        or not isinstance(measured_command, list)
        or not isinstance(runtime_policy, dict)
        or measured_command
        != [*runtime_policy.get("command_prefix", []), *command]
    ):
        raise CampaignError("cell measured command differs from its exact contract")
    try:
        time_metrics = matrix.parse_gnu_time_metrics(
            time_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, matrix.MatrixError) as error:
        raise CampaignError("cell GNU time evidence is invalid") from error
    derived_rss = float(time_metrics["peak_rss_kib"]) / 1024.0
    if float(metrics.get("peak_rss_mib", 0.0)) != derived_rss:
        raise CampaignError("cell peak RSS differs from retained GNU time evidence")
    try:
        if target.id in collections_oxipng.TARGETS:
            spec = collections_oxipng.TARGETS[target.id]
            adapter_harness = next(
                item for item in spec.harnesses if item.id == harness.id
            )
            if adapter_harness.kind == "libtest":
                performance = collections_oxipng.parse_libtest_benchmark(
                    stdout.decode("utf-8", errors="replace"),
                    adapter_harness.selector,
                )
            else:
                timing_path = immutable_evidence.validate_artifact_ref(
                    artifacts.get("process_timing"), context="process timing"
                )
                timing = json.loads(timing_path.read_text(encoding="utf-8"))
                performance = float(timing["wall_seconds"])
                output_path = immutable_evidence.validate_artifact_ref(
                    artifacts.get("output"), context="Oxipng output"
                )
                output = output_path.read_bytes()
                correctness = row.get("correctness")
                if (
                    not output.startswith(b"\x89PNG\r\n\x1a\n")
                    or not isinstance(correctness, dict)
                    or correctness.get("output_sha256") != sha256_bytes(output)
                ):
                    raise CampaignError("Oxipng retained output proof mismatch")
        elif target.id == "redb":
            performance = redb_actix.parse_redb_result(
                stdout.decode("utf-8", errors="replace"), harness.id
            )
        elif target.id == "actix_web":
            text = stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode(
                "utf-8", errors="replace"
            )
            if redb_actix.actix_failed_request_count(text):
                raise CampaignError("Actix retained output reports failed requests")
            performance = redb_actix.parse_criterion_estimate_seconds(text)
        elif target.id == "polars":
            performance, token = psr.polars_result(stdout, harness.id)
            correctness = row.get("correctness")
            if (
                not isinstance(correctness, dict)
                or correctness.get("comparison_token") != token
            ):
                raise CampaignError("Polars retained correctness token mismatch")
        else:
            performance = psr.criterion_seconds(stdout)
    except (
        collections_oxipng.CampaignError,
        redb_actix.CampaignError,
        psr.CampaignError,
        immutable_evidence.ImmutableEvidenceError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        StopIteration,
    ) as error:
        if isinstance(error, CampaignError):
            raise
        raise CampaignError("cell performance evidence cannot be re-derived") from error
    if float(metrics.get("performance", 0.0)) != float(performance):
        raise CampaignError("cell performance differs from retained output evidence")
    allocator_identity = _tcmalloc_cell_identity(str(row.get("variant")), stderr)
    if row.get("allocator_runtime_identity") != allocator_identity:
        raise CampaignError("cell allocator runtime identity evidence mismatch")
    thp_identity = mimalloc_thp_runtime_evidence(str(row.get("variant")), stderr)
    if row.get("mimalloc_thp_runtime") != thp_identity:
        raise CampaignError("cell mimalloc THP runtime evidence mismatch")


def persist_runtime_environment(
    raw_dir: pathlib.Path, environment: Mapping[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        record = type_isolation_suite_contract.runtime_environment_record(environment)
        digest = str(record["environment_sha256"])
        committed = immutable_evidence.persist_immutable_json(
            raw_dir / "environments" / f"{digest}.json", record
        )
        return record, immutable_evidence.artifact_ref(committed.path)
    except (
        type_isolation_suite_contract.SuiteContractError,
        immutable_evidence.ImmutableEvidenceError,
    ) as error:
        raise CampaignError(str(error)) from error


def _runtime_policy_record(
    *,
    target_id: str,
    variant: str,
    prefix: Sequence[str],
    environment: Mapping[str, str],
    tcmalloc_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    explicit = (
        matrix.allocator_runtime_environment_overrides(_runtime_variant(variant))
        if variant in {"mimalloc", "mimalloc_no_thp"}
        else {}
    )
    target_environment = dict(environment)
    if variant == "google_tcmalloc":
        target_environment["LD_PRELOAD"] = str(tcmalloc_runtime["library"])
    try:
        shared = type_isolation_suite_contract.runtime_environment_record(
            target_environment
        )
    except type_isolation_suite_contract.SuiteContractError as error:
        raise CampaignError(str(error)) from error
    return {
        **shared,
        "target_execution_contract": dict(TARGET_EXECUTION_CONTRACTS[target_id]),
        "allocator_overrides": explicit,
        "command_prefix": list(prefix),
        "google_tcmalloc": (
            {
                "library": tcmalloc_runtime.get("library"),
                "library_sha256": tcmalloc_runtime.get("library_sha256"),
                "revision": tcmalloc_runtime.get("runtime_requirements", {}).get(
                    "revision"
                ),
            }
            if variant == "google_tcmalloc"
            else None
        ),
    }


def _measure_collections_or_oxipng(
    *,
    identity: CellIdentity,
    build: Mapping[str, Any],
    source: SourceContext,
    raw_dir: pathlib.Path,
    command_prefix: Sequence[str],
    environment: Mapping[str, str],
    timeout: int,
) -> dict[str, Any]:
    spec = collections_oxipng.TARGETS[identity.target_id]
    harness = next(row for row in spec.harnesses if row.id == identity.harness_id)
    oxipng_input = None
    if identity.target_id == "oxipng":
        input_record = source.record.get("input")
        if not isinstance(input_record, dict):
            raise CampaignError("Oxipng input record is missing")
        oxipng_input = pathlib.Path(str(input_record["path"]))
    run_root = (
        raw_dir
        / "adapter/runs"
        / identity.target_id
        / identity.harness_id
        / identity.variant
        / f"{identity.phase}-{identity.round:02d}"
    )
    run_root.mkdir(parents=True, exist_ok=True)
    binary = pathlib.Path(str(build["binaries"][harness.binary]["path"]))
    output_file: pathlib.Path | None = None
    if harness.kind == "libtest":
        command = [
            str(binary),
            "--bench",
            "--exact",
            harness.selector,
            "--test-threads=1",
        ]
        cwd = pathlib.Path(str(build["patch"]["manifest"])).parent
    else:
        if oxipng_input is None or harness.threads is None:
            raise CampaignError("Oxipng CLI input or thread count is missing")
        output_file = run_root / "output.png"
        command = [
            str(binary),
            "--opt",
            "2",
            "--threads",
            str(harness.threads),
            "--force",
            "--quiet",
            "--out",
            str(output_file),
            str(oxipng_input),
        ]
        cwd = run_root
    measured = matrix.run_measured(
        command,
        cwd=cwd,
        env=dict(environment),
        time_binary=GNU_TIME,
        rss_path=run_root / "gnu-time.txt",
        timeout=timeout,
        command_prefix=command_prefix,
    )
    stdout = measured.pop("stdout")
    stderr = measured.pop("stderr")
    stdout_path = run_root / "stdout.bin"
    stderr_path = run_root / "stderr.bin"
    time_path = run_root / "gnu-time.txt"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    if (
        measured["exit_code"] != 0
        or measured["timed_out"]
        or measured["gnu_time_exit_status"] != 0
        or measured["peak_rss_kib"] <= 0
    ):
        raise CampaignError(
            f"workload failed for {identity.target_id}/{identity.harness_id}/"
            f"{identity.variant}: {measured}"
        )
    if harness.kind == "libtest":
        try:
            performance = collections_oxipng.parse_libtest_benchmark(
                stdout.decode("utf-8", errors="replace"), harness.selector
            )
        except collections_oxipng.CampaignError as error:
            raise CampaignError(str(error)) from error
        performance_unit = "ns_per_iter"
        correctness: dict[str, Any] = {
            "passed": True,
            "oracle": "exact libtest benchmark completed",
            "reported_benchmark": harness.selector,
        }
    else:
        if output_file is None or not output_file.is_file():
            raise CampaignError(
                f"Oxipng produced no output for {identity.harness_id}/"
                f"{identity.variant}"
            )
        output = output_file.read_bytes()
        if not output.startswith(b"\x89PNG\r\n\x1a\n"):
            raise CampaignError("Oxipng output lacks a PNG signature")
        performance = float(measured["wall_seconds"])
        performance_unit = "seconds"
        correctness = {
            "passed": True,
            "oracle": "successful PNG output with stable content digest",
            "output_sha256": sha256_bytes(output),
            "output_size_bytes": len(output),
        }
    output_digest = correctness.get("output_sha256")
    if output_digest:
        correctness["comparison_token"] = str(output_digest)
    artifacts = {
        "stdout": _artifact(stdout_path),
        "stderr": _artifact(stderr_path),
        "gnu_time": _artifact(time_path),
    }
    if output_file is not None:
        artifacts["output"] = _artifact(output_file)
    return {
        "performance": float(performance),
        "performance_unit": performance_unit,
        "performance_source": (
            "rust-libtest-benchmark-estimate"
            if harness.kind == "libtest"
            else "process-wall-seconds"
        ),
        "peak_rss_mib": float(measured["peak_rss_kib"]) / 1024.0,
        "correctness": correctness,
        "measurement": dict(measured),
        "command": list(measured["command"]),
        "measured_command": list(measured["measured_command"]),
        "artifacts": artifacts,
        "allocator_runtime_identity": _tcmalloc_cell_identity(
            identity.variant, stderr
        ),
        "mimalloc_thp_runtime": mimalloc_thp_runtime_evidence(
            identity.variant, stderr
        ),
    }


def _measure_redb_or_actix(
    *,
    identity: CellIdentity,
    build: Mapping[str, Any],
    raw_dir: pathlib.Path,
    command_prefix: Sequence[str],
    environment: Mapping[str, str],
    timeout: int,
) -> dict[str, Any]:
    spec = redb_actix.TARGETS[identity.target_id]
    harness = next(row for row in spec.harnesses if row.id == identity.harness_id)
    run_root = (
        raw_dir
        / "adapter/runs"
        / identity.target_id
        / identity.harness_id
        / identity.variant
        / f"{identity.phase}-{identity.round:02d}"
    )
    command, cwd = redb_actix.measured_command(
        spec, harness, dict(build), run_root / "work"
    )
    measured = matrix.run_measured(
        command,
        cwd=cwd,
        env=dict(environment),
        time_binary=GNU_TIME,
        rss_path=run_root / "gnu-time.txt",
        timeout=timeout,
        command_prefix=command_prefix,
    )
    stdout = bytes(measured.pop("stdout"))
    stderr = bytes(measured.pop("stderr"))
    run_root.mkdir(parents=True, exist_ok=True)
    stdout_path = run_root / "stdout.bin"
    stderr_path = run_root / "stderr.bin"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    text = stdout.decode("utf-8", errors="replace")
    error_text = stderr.decode("utf-8", errors="replace")
    if (
        measured["exit_code"] != 0
        or measured["timed_out"]
        or measured["gnu_time_exit_status"] != 0
        or measured["peak_rss_kib"] <= 0
    ):
        raise CampaignError(
            f"workload failed for {identity.target_id}/{identity.harness_id}: "
            f"{error_text[-4000:]}"
        )
    if identity.target_id == "actix_web" and redb_actix.actix_failed_request_count(
        text + "\n" + error_text
    ):
        raise CampaignError(f"Actix workload reported failed requests: {harness.id}")
    try:
        performance = (
            redb_actix.parse_redb_result(text, harness.id)
            if identity.target_id == "redb"
            else redb_actix.parse_criterion_estimate_seconds(text + "\n" + error_text)
        )
    except redb_actix.CampaignError as error:
        raise CampaignError(str(error)) from error
    return {
        "performance": float(performance),
        "performance_unit": "seconds",
        "performance_source": (
            "benchmark-owned-operation-seconds"
            if identity.target_id == "redb"
            else "criterion-median-seconds-per-iteration"
        ),
        "peak_rss_mib": float(measured["peak_rss_kib"]) / 1024.0,
        "correctness": {
            "passed": True,
            "oracle": (
                "redb workload result record"
                if identity.target_id == "redb"
                else "Criterion completion with zero failed requests"
            ),
        },
        "measurement": {
            key: value
            for key, value in measured.items()
            if key not in {"stdout", "stderr"}
        },
        "command": list(measured["command"]),
        "measured_command": list(measured["measured_command"]),
        "artifacts": {
            "stdout": _artifact(stdout_path),
            "stderr": _artifact(stderr_path),
            "gnu_time": _artifact(run_root / "gnu-time.txt"),
        },
        "allocator_runtime_identity": _tcmalloc_cell_identity(
            identity.variant, stderr
        ),
        "mimalloc_thp_runtime": mimalloc_thp_runtime_evidence(
            identity.variant, stderr
        ),
    }


def _measure_psr(
    *,
    identity: CellIdentity,
    build: Mapping[str, Any],
    raw_dir: pathlib.Path,
    command_prefix: Sequence[str],
    environment: Mapping[str, str],
    timeout: int,
    polars_csv: pathlib.Path | None,
) -> dict[str, Any]:
    spec = psr.TARGET_SPECS[identity.target_id]
    binary = pathlib.Path(str(build["binary"]))
    worktree = pathlib.Path(str(build["worktree"]))
    command = psr.command_for(
        spec, binary, identity.harness_id, polars_csv
    )
    run_root = (
        raw_dir
        / "adapter/runs"
        / identity.target_id
        / identity.harness_id
        / identity.variant
        / f"{identity.phase}-{identity.round:02d}"
    )
    run_root.mkdir(parents=True, exist_ok=True)
    measured = matrix.run_measured(
        command,
        cwd=worktree,
        env=dict(environment),
        time_binary=GNU_TIME,
        rss_path=run_root / "gnu-time.txt",
        timeout=timeout,
        command_prefix=command_prefix,
    )
    stdout = measured.pop("stdout")
    stderr = measured.pop("stderr")
    stdout_path = run_root / "stdout.bin"
    stderr_path = run_root / "stderr.bin"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    if (
        measured["exit_code"] != 0
        or measured["timed_out"]
        or measured["gnu_time_exit_status"] != 0
        or measured["peak_rss_kib"] <= 0
    ):
        raise CampaignError(
            f"workload failed: {identity.target_id}/{identity.harness_id}/"
            f"{identity.variant}: {measured}"
        )
    comparison_token: str | None = None
    if spec.mode == "polars-driver":
        try:
            performance, comparison_token = psr.polars_result(
                stdout, identity.harness_id
            )
        except psr.CampaignError as error:
            raise CampaignError(str(error)) from error
        performance_source = "benchmark-owned-operation-seconds"
        oracle = "Polars deterministic result fingerprint"
    else:
        try:
            performance = psr.criterion_seconds(stdout)
        except psr.CampaignError as error:
            raise CampaignError(str(error)) from error
        performance_source = "criterion-median-seconds-per-iteration"
        oracle = "Criterion benchmark completed successfully"
    correctness: dict[str, Any] = {"passed": True, "oracle": oracle}
    if comparison_token is not None:
        correctness["comparison_token"] = comparison_token
    return {
        "performance": float(performance),
        "performance_unit": "seconds",
        "performance_source": performance_source,
        "peak_rss_mib": float(measured["peak_rss_kib"]) / 1024.0,
        "correctness": correctness,
        "measurement": dict(measured),
        "command": list(measured["command"]),
        "measured_command": list(measured["measured_command"]),
        "artifacts": {
            "stdout": _artifact(stdout_path),
            "stderr": _artifact(stderr_path),
            "gnu_time": _artifact(run_root / "gnu-time.txt"),
        },
        "allocator_runtime_identity": _tcmalloc_cell_identity(
            identity.variant, stderr
        ),
        "mimalloc_thp_runtime": mimalloc_thp_runtime_evidence(
            identity.variant, stderr
        ),
    }


def execute_planned_cell(
    cell: PlannedCell,
    *,
    protocol: Protocol,
    contract: CampaignContract,
    build: Mapping[str, Any],
    source: SourceContext,
    raw_dir: pathlib.Path,
    timeout: int,
    reuse: bool,
    tcmalloc_runtime: Mapping[str, Any],
    runtime_dependency: Mapping[str, Any] | None,
    polars_csv: pathlib.Path | None,
    suite_binding: Mapping[str, Any],
) -> dict[str, Any]:
    identity = cell.identity
    path = raw_dir / cell.relative_path
    binary = build["harness_binaries"][identity.harness_id]
    build_sha256 = str(binary["sha256"])
    prefix = _runtime_prefix(
        identity.target_id,
        identity.variant,
        tcmalloc_runtime=tcmalloc_runtime,
    )
    environment = _runtime_environment(
        identity.target_id,
        variant=identity.variant,
        raw_dir=raw_dir,
        runtime_dependency=runtime_dependency,
    )
    target = contract.targets[identity.target_id]
    harness = next(row for row in target.harnesses if row.id == identity.harness_id)
    runtime_policy = _runtime_policy_record(
        target_id=identity.target_id,
        variant=identity.variant,
        prefix=prefix,
        environment=environment,
        tcmalloc_runtime=tcmalloc_runtime,
    )
    _environment_record, environment_artifact = persist_runtime_environment(
        raw_dir, runtime_policy["environment"]
    )
    command_contract = {
        "target_id": identity.target_id,
        "harness_id": identity.harness_id,
        "selector": harness.selector,
        "binary_sha256": build_sha256,
        "command_prefix": list(prefix),
        "performance_unit": harness.performance_unit,
        "performance_source": harness.performance_source,
        "rss_source": "gnu-time-maximum-resident-set-size-kib",
    }
    google_identity = (
        tcmalloc_protocol_identity(tcmalloc_runtime)
        if identity.variant == "google_tcmalloc"
        else None
    )
    expected_identity = {
        **identity.as_dict(),
        "suite_id": contract.suite.suite_id,
        "suite_manifest_sha256": contract.suite.manifest_sha256,
        "protocol_fingerprint": protocol.fingerprint,
        "target_fingerprint": cell.target_fingerprint,
        "variant_fingerprint": cell.variant_fingerprint,
        "cell_fingerprint": cell.cell_fingerprint,
        "source_ref": target.source_ref,
        "source_commit": target.source_commit,
        "source_record_sha256": canonical_json_sha256(source.record),
        "implementation_revision": contract.suite.implementation_revision,
        "implementation_sha256": contract.suite.implementation_sha256,
        "build_id": str(build["build_id"]),
        "build_fingerprint": str(build["build_fingerprint"]),
        "binary_sha256": build_sha256,
        "selector": harness.selector,
        "metric_direction": harness.metric_direction,
        "rss_work_model": target.rss_work_model,
        "runtime_environment_sha256": runtime_policy["environment_sha256"],
        "runtime_environment_policy": runtime_policy["policy"],
        "rseq_policy": runtime_policy["rseq_policy"],
        "command_contract_sha256": canonical_json_sha256(command_contract),
        "google_tcmalloc_identity": google_identity,
        "mimalloc_pr_get_thp_disable_expected": {
            "mimalloc": 0,
            "mimalloc_no_thp": 1,
        }.get(identity.variant),
    }
    expected_metrics = {
        "performance_unit": harness.performance_unit,
        "performance_source": harness.performance_source,
        "rss_source": "gnu-time-maximum-resident-set-size-kib",
    }
    if path.exists() and not reuse:
        raise CampaignError(
            "measurement cell already exists; pass --reuse or select a new "
            f"raw directory: {path}"
        )

    def validate(row: Mapping[str, Any]) -> None:
        try:
            immutable_evidence.validate_measurement_record(
                row,
                expected_identity=expected_identity,
                expected_metrics=expected_metrics,
            )
        except immutable_evidence.ImmutableEvidenceError as error:
            raise CampaignError(f"measurement cell validation failed: {error}") from error
        if row.get("success") is not True:
            raise CampaignError(f"measurement cell is unsuccessful: {path}")
        correctness = row.get("correctness")
        if not isinstance(correctness, dict) or correctness.get("passed") is not True:
            raise CampaignError(f"measurement cell correctness mismatch: {path}")
        if row.get("command_contract") != command_contract:
            raise CampaignError(f"reusable cell command contract mismatch: {path}")
        artifacts = row.get("artifacts")
        if not isinstance(artifacts, dict):
            raise CampaignError(f"reusable cell artifacts are malformed: {path}")
        try:
            environment_path = immutable_evidence.validate_artifact_ref(
                artifacts.get("runtime_environment"), context="runtime environment"
            )
            suite_path = immutable_evidence.validate_artifact_ref(
                artifacts.get("suite_manifest"), context="suite manifest"
            )
            environment_evidence = json.loads(
                environment_path.read_text(encoding="utf-8")
            )
        except (
            immutable_evidence.ImmutableEvidenceError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
        ) as error:
            raise CampaignError(f"reusable environment evidence is invalid: {path}") from error
        shared_environment = {
            key: runtime_policy[key]
            for key in (
                "policy",
                "inherited_allowlist",
                "rseq_policy",
                "glibc_tunables_present",
                "environment",
                "environment_sha256",
            )
        }
        if (
            environment_evidence != shared_environment
            or suite_path.read_bytes() != contract.suite.path.read_bytes()
            or sha256_file(suite_path) != contract.suite.manifest_sha256
        ):
            raise CampaignError(f"reusable environment or suite binding mismatch: {path}")
        runtime = row.get("mimalloc_thp_runtime")
        expected_thp = expected_identity["mimalloc_pr_get_thp_disable_expected"]
        if expected_thp is not None and (
            not isinstance(runtime, dict)
            or runtime.get("verified") is not True
            or runtime.get("pr_get_thp_disable") != expected_thp
        ):
            raise CampaignError(f"reusable cell mimalloc THP proof mismatch: {path}")
        if row.get("allocator_runtime_identity", {}).get(
            "google_tcmalloc_target_identity_verified"
        ) is not True:
            raise CampaignError(f"reusable cell allocator identity mismatch: {path}")
        validate_cell_metrics_from_artifacts(row, target=target, harness=harness)

    def collect(attempt_dir: pathlib.Path) -> Mapping[str, Any]:
        started = time.time()
        if identity.target_id in collections_oxipng.TARGETS:
            measurement = _measure_collections_or_oxipng(
                identity=identity,
                build=build,
                source=source,
                raw_dir=attempt_dir,
                command_prefix=prefix,
                environment=environment,
                timeout=timeout,
            )
        elif identity.target_id in redb_actix.TARGETS:
            measurement = _measure_redb_or_actix(
                identity=identity,
                build=build,
                raw_dir=attempt_dir,
                command_prefix=prefix,
                environment=environment,
                timeout=timeout,
            )
        else:
            measurement = _measure_psr(
                identity=identity,
                build=build,
                raw_dir=attempt_dir,
                command_prefix=prefix,
                environment=environment,
                timeout=timeout,
                polars_csv=polars_csv,
            )
        if measurement["performance_unit"] != harness.performance_unit:
            raise CampaignError(
                f"performance unit mismatch for {identity.target_id}/"
                f"{identity.harness_id}"
            )
        if measurement["performance_source"] != harness.performance_source:
            raise CampaignError(
                f"performance source mismatch for {identity.target_id}/"
                f"{identity.harness_id}"
            )
        artifacts = dict(measurement.pop("artifacts"))
        timing_path = attempt_dir / "process-timing.json"
        immutable_evidence.atomic_write_bytes(
            timing_path,
            (
                json.dumps(
                    {
                        "wall_seconds": measurement["measurement"]["wall_seconds"],
                        "source": "monotonic-subprocess-communicate-duration",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        )
        artifacts["process_timing"] = _artifact(timing_path)
        artifacts["binary"] = _artifact(pathlib.Path(str(binary["path"])))
        artifacts["runtime_environment"] = environment_artifact
        artifacts["suite_manifest"] = dict(suite_binding)
        metrics = {
            "performance": measurement["performance"],
            "performance_unit": measurement["performance_unit"],
            "performance_source": measurement["performance_source"],
            "peak_rss_mib": measurement["peak_rss_mib"],
            "rss_source": "gnu-time-maximum-resident-set-size-kib",
        }
        return {
            "evidence_schema_version": 1,
            "schema_version": CELL_SCHEMA_VERSION,
            "identity": expected_identity,
            "metrics": metrics,
            "artifacts": artifacts,
            **expected_identity,
            "protocol_id": PROTOCOL_ID,
            "performance": metrics["performance"],
            "performance_unit": metrics["performance_unit"],
            "performance_source": metrics["performance_source"],
            "peak_rss_mib": metrics["peak_rss_mib"],
            "correctness": measurement["correctness"],
            "command": measurement["command"],
            "measured_command": measurement["measured_command"],
            "measurement": measurement["measurement"],
            "allocator_runtime_identity": measurement["allocator_runtime_identity"],
            "mimalloc_thp_runtime": measurement["mimalloc_thp_runtime"],
            "command_contract": command_contract,
            "runtime_policy": runtime_policy,
            "peak_rss_headline_eligible": target.fixed_work_rss,
            "started_unix_seconds": started,
            "completed_unix_seconds": time.time(),
            "success": True,
        }

    try:
        committed = immutable_evidence.run_or_reuse_json(path, collect, validate)
    except (immutable_evidence.ImmutableEvidenceError, CampaignError) as error:
        if isinstance(error, CampaignError):
            raise
        raise CampaignError(str(error)) from error
    return dict(committed.value)


def _rotated(values: Sequence[str], offset: int) -> tuple[str, ...]:
    pivot = offset % len(values)
    return tuple(values[pivot:]) + tuple(values[:pivot])


def run_measurement_plan(
    *,
    plan: CampaignPlan,
    protocol: Protocol,
    contract: CampaignContract,
    builds: Mapping[str, Mapping[str, Mapping[str, Any]]],
    sources: Mapping[str, SourceContext],
    raw_dir: pathlib.Path,
    timeout: int,
    reuse: bool,
    tcmalloc_runtime: Mapping[str, Any],
    runtime_dependency: Mapping[str, Any] | None,
    polars_csv: pathlib.Path | None,
    suite_binding: Mapping[str, Any],
    quiescence_timeout: int = DEFAULT_QUIESCENCE_TIMEOUT,
    quiet_seconds: int = DEFAULT_QUIET_SECONDS,
) -> list[dict[str, Any]]:
    by_identity = {cell.identity: cell for cell in plan.cells}
    completed: list[dict[str, Any]] = []
    for target_index, target_id in enumerate(plan.requested_targets):
        execution = TARGET_EXECUTION_CONTRACTS[target_id]
        with type_isolation_suite_contract.primary_measurement_lock(
            runner=pathlib.Path(__file__).name,
            raw_root=raw_dir,
            target_id=target_id,
        ) as lock_record:
            quiescence_attempt = immutable_evidence.begin_attempt(
                raw_dir / "quiescence" / target_id / "record.json"
            )
            try:
                quiescence = psr.wait_for_build_quiescence(
                    quiescence_attempt,
                    target_id,
                    timeout=quiescence_timeout,
                    quiet_seconds=quiet_seconds,
                    cpu_list=str(execution["cpu_list"]),
                    numa_node=str(execution["numa_node"]),
                )
                quiescence_commit = immutable_evidence.persist_immutable_json(
                    raw_dir
                    / "quiescence"
                    / target_id
                    / f"{canonical_json_sha256(quiescence)}.json",
                    quiescence,
                )
            except (
                psr.CampaignError,
                immutable_evidence.ImmutableEvidenceError,
            ) as error:
                raise CampaignError(str(error)) from error
            target = contract.targets[target_id]
            target_cohorts = [
                cohort for cohort in plan.cohorts if cohort.target_id == target_id
            ]
            for cohort_index, cohort in enumerate(target_cohorts):
                for harness_index, harness in enumerate(target.harnesses):
                    phases = [
                        *(("warmup", index) for index in range(plan.warmup_rounds)),
                        *(("measurement", index) for index in range(1, plan.measured_rounds + 1)),
                    ]
                    for phase_index, (phase, round_number) in enumerate(phases):
                        order = _rotated(
                            cohort.variants,
                            target_index + cohort_index + harness_index + phase_index,
                        )
                        for variant in order:
                            identity = CellIdentity(
                                cohort.cohort_id,
                                target_id,
                                harness.id,
                                variant,
                                phase,
                                round_number,
                            )
                            completed.append(
                                execute_planned_cell(
                                    by_identity[identity],
                                    protocol=protocol,
                                    contract=contract,
                                    build=builds[target_id][variant],
                                    source=sources[target_id],
                                    raw_dir=raw_dir,
                                    timeout=timeout,
                                    reuse=reuse,
                                    tcmalloc_runtime=tcmalloc_runtime,
                                    runtime_dependency=runtime_dependency,
                                    polars_csv=polars_csv,
                                    suite_binding=suite_binding,
                                )
                            )
            lock_record["quiescence_evidence"] = immutable_evidence.artifact_ref(
                quiescence_commit.path
            )
        try:
            immutable_evidence.persist_immutable_json(
                raw_dir
                / "locks"
                / target_id
                / f"{canonical_json_sha256(lock_record)}.json",
                lock_record,
            )
        except immutable_evidence.ImmutableEvidenceError as error:
            raise CampaignError(str(error)) from error
    return completed


def _selection_fingerprint(plan: CampaignPlan) -> str:
    return canonical_json_sha256(
        {
            "protocol_fingerprint": plan.protocol_fingerprint,
            "targets": plan.requested_targets,
            "requested_variants": plan.requested_variants,
            "execution_variants": plan.execution_variants,
        }
    )


def persist_plan(raw_dir: pathlib.Path, plan: CampaignPlan) -> pathlib.Path:
    selection = _selection_fingerprint(plan)
    path = raw_dir / "selections" / selection / "plan.json"
    value = {**plan.as_dict(), "selection_fingerprint": selection}
    try:
        return immutable_evidence.persist_immutable_json(path, value).path
    except immutable_evidence.ImmutableEvidenceError as error:
        raise CampaignError(f"selection plan mismatch: {path}") from error


def regenerate_cell_index(raw_dir: pathlib.Path) -> pathlib.Path:
    rows: list[dict[str, Any]] = []
    for path in sorted((raw_dir / "cells").rglob("*.json")):
        row = load_json(path, "measurement cell")
        rows.append(
            {
                "path": str(path.relative_to(raw_dir)),
                "sha256": sha256_file(path),
                "target_id": row.get("target_id"),
                "harness_id": row.get("harness_id"),
                "cohort_id": row.get("cohort_id"),
                "variant": row.get("variant"),
                "phase": row.get("phase"),
                "round": row.get("round"),
                "protocol_fingerprint": row.get("protocol_fingerprint"),
            }
        )
    path = raw_dir / "cells.jsonl"
    temporary = path.with_suffix(".jsonl.tmp")
    temporary.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def persist_build_index(
    raw_dir: pathlib.Path,
    builds: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> pathlib.Path:
    rows = [
        {
            "target_id": target_id,
            "variant": variant,
            "build_id": build["build_id"],
            "build_path": build["build_path"],
            "harness_binaries": build["harness_binaries"],
        }
        for target_id, variants in builds.items()
        for variant, build in variants.items()
    ]
    path = raw_dir / "build-index.json"
    write_json(path, {"schema_version": 1, "builds": rows})
    return path


def persist_summary_view(
    *,
    raw_dir: pathlib.Path,
    plan: CampaignPlan,
    contract: CampaignContract,
    cells: Sequence[Mapping[str, Any]],
) -> pathlib.Path:
    selection = _selection_fingerprint(plan)
    summary = summarize_cells(cells, contract)
    summary.update(
        {
            "protocol_fingerprint": plan.protocol_fingerprint,
            "selection_fingerprint": selection,
            "requested_targets": list(plan.requested_targets),
            "requested_variants": list(plan.requested_variants),
            "execution_variants": list(plan.execution_variants),
        }
    )
    path = raw_dir / "selections" / selection / "summary.json"
    write_json(path, summary)
    write_json(
        raw_dir / "latest-selection.json",
        {
            "selection_fingerprint": selection,
            "plan": str((path.parent / "plan.json").resolve()),
            "summary": str(path.resolve()),
        },
    )
    return path


def authenticate_tcmalloc(prefix: pathlib.Path) -> dict[str, Any]:
    try:
        return matrix.tcmalloc_runtime_evidence(prefix=prefix)
    except matrix.MatrixError as error:
        raise CampaignError(str(error)) from error


def tcmalloc_protocol_identity(runtime: Mapping[str, Any]) -> dict[str, Any]:
    requirements = runtime.get("runtime_requirements")
    if not isinstance(requirements, Mapping):
        raise CampaignError("Google TCMalloc runtime requirements are malformed")
    identity = {
        "revision": requirements.get("revision"),
        "library_sha256": runtime.get("library_sha256"),
        "hpaa_active": requirements.get("hpaa_active"),
        "malloc_provider_is_self": requirements.get("malloc_provider_is_self"),
        "label": runtime.get("label"),
    }
    if (
        identity["revision"] != google_tcmalloc_support.UPSTREAM_REVISION
        or not re.fullmatch(r"[0-9a-f]{64}", str(identity["library_sha256"]))
        or identity["hpaa_active"] != 1
        or identity["malloc_provider_is_self"] != 1
    ):
        raise CampaignError(f"Google TCMalloc identity is incomplete: {identity}")
    return identity


def host_environment_record(
    *,
    tcmalloc_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    thp: dict[str, str] = {}
    for name, path in {
        "enabled": pathlib.Path("/sys/kernel/mm/transparent_hugepage/enabled"),
        "defrag": pathlib.Path("/sys/kernel/mm/transparent_hugepage/defrag"),
    }.items():
        if path.is_file():
            thp[name] = path.read_text(encoding="utf-8").strip()
    return {
        "recorded_unix_seconds": time.time(),
        "kernel": list(os.uname()),
        "host_cpu_count": os.cpu_count(),
        "process_cpu_affinity": sorted(os.sched_getaffinity(0)),
        "target_execution_contracts": {
            key: {
                "cpu_list": value["cpu_list"],
                "numa_node": value["numa_node"],
                "runtime_overrides": dict(value["runtime_overrides"]),
            }
            for key, value in TARGET_EXECUTION_CONTRACTS.items()
        },
        "transparent_hugepage": thp,
        "google_tcmalloc": dict(tcmalloc_runtime),
        "sanitized_runtime_environment": {
            "policy": type_isolation_suite_contract.RUNTIME_ENVIRONMENT_POLICY,
            "rseq_policy": type_isolation_suite_contract.PRODUCTION_RSEQ_POLICY,
            "allocator_variables_inherited": False,
            "explicit_allocator_overrides_only": True,
        },
    }


def _parse_csv(raw: str, allowed: Iterable[str], label: str) -> tuple[str, ...]:
    values = _ordered_unique(value.strip() for value in raw.split(",") if value.strip())
    unknown = sorted(set(values) - set(allowed))
    if not values or unknown:
        raise argparse.ArgumentTypeError(
            f"unknown or empty {label} selection: {','.join(unknown)}"
        )
    return values


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        type=pathlib.Path,
        default=type_isolation_suite_contract.CURRENT_SUITE_PATH,
    )
    parser.add_argument("--raw-dir", type=pathlib.Path)
    parser.add_argument(
        "--targets",
        default="collections,oxipng,redb,polars,swc,rustpython,actix_web",
    )
    parser.add_argument("--variants", default=",".join(VARIANT_ORDER))
    parser.add_argument(
        "--checkout-root", type=pathlib.Path, default=DEFAULT_CHECKOUT_ROOT
    )
    parser.add_argument("--implementation-revision")
    parser.add_argument("--toolchain", default=DEFAULT_TOOLCHAIN)
    parser.add_argument("--jobs", type=int, default=min(64, os.cpu_count() or 1))
    parser.add_argument("--build-timeout", type=int, default=7200)
    parser.add_argument("--run-timeout", type=int, default=1200)
    parser.add_argument("--warmups", type=int)
    parser.add_argument("--rounds", type=int)
    parser.add_argument(
        "--quiescence-timeout", type=int, default=DEFAULT_QUIESCENCE_TIMEOUT
    )
    parser.add_argument("--quiet-seconds", type=int, default=DEFAULT_QUIET_SECONDS)
    parser.add_argument(
        "--google-tcmalloc-prefix",
        type=pathlib.Path,
        default=pathlib.Path(
            os.environ.get("UNIALLOC_GOOGLE_TCMALLOC_PREFIX", DEFAULT_TCMALLOC_PREFIX)
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--build-only", action="store_true")
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args(argv)
    try:
        contract = load_campaign_contract(args.suite)
        args.targets = _parse_csv(args.targets, contract.targets, "target")
        args.variants = _parse_csv(args.variants, VARIANT_ORDER, "variant")
    except (CampaignError, argparse.ArgumentTypeError, ValueError) as error:
        parser.error(str(error))
    args.contract = contract
    args.raw_dir = (
        args.raw_dir
        or contract.suite.runner_raw_dir("macro-allocator-baselines")
    ).resolve()
    args.implementation_revision = (
        args.implementation_revision or contract.suite.implementation_revision
    )
    args.warmups = (
        contract.suite.warmup_rounds if args.warmups is None else args.warmups
    )
    args.rounds = contract.suite.measured_rounds if args.rounds is None else args.rounds
    if args.implementation_revision != contract.suite.implementation_revision:
        parser.error(
            "--implementation-revision must equal the suite revision: "
            + contract.suite.implementation_revision
        )
    if (
        args.warmups != contract.suite.warmup_rounds
        or args.rounds != contract.suite.measured_rounds
    ):
        parser.error(
            f"the suite requires {contract.suite.warmup_rounds} warmup and "
            f"{contract.suite.measured_rounds} measured rounds"
        )
    if (
        args.jobs < 1
        or args.build_timeout < 1
        or args.run_timeout < 1
        or args.quiescence_timeout < 1
        or args.quiet_seconds < 0
    ):
        parser.error("jobs and timeouts must be valid positive resources")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    contract: CampaignContract = args.contract
    validate_adapter_contract(contract)
    tcmalloc_runtime = (
        authenticate_tcmalloc(args.google_tcmalloc_prefix.resolve())
        if "google_tcmalloc" in args.variants
        else {}
    )
    tcmalloc_identity = (
        tcmalloc_protocol_identity(tcmalloc_runtime) if tcmalloc_runtime else None
    )
    protocol = build_protocol(
        contract,
        toolchain=args.toolchain,
    )
    plan = build_plan(
        protocol,
        target_ids=args.targets,
        variant_ids=args.variants,
        tcmalloc_identity=tcmalloc_identity,
    )
    raw_dir = args.raw_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    try:
        suite_binding = type_isolation_suite_contract.bind_suite_manifest(
            contract.suite, destination=raw_dir / "suite-manifest.json"
        )
        type_isolation_suite_contract.verify_suite_manifest_binding(
            contract.suite, path=pathlib.Path(str(suite_binding["path"]))
        )
    except type_isolation_suite_contract.SuiteContractError as error:
        raise CampaignError(str(error)) from error
    ensure_campaign_manifest(
        raw_dir, protocol, reuse=args.reuse, suite_binding=suite_binding
    )
    plan_path = persist_plan(raw_dir, plan)
    host_record = host_environment_record(tcmalloc_runtime=tcmalloc_runtime)
    try:
        immutable_evidence.persist_immutable_json(
            raw_dir
            / "host-environments"
            / f"{canonical_json_sha256(host_record)}.json",
            host_record,
        )
    except immutable_evidence.ImmutableEvidenceError as error:
        raise CampaignError(str(error)) from error
    if args.plan_only:
        print(plan_path)
        return 0
    if not GNU_TIME.is_file():
        raise CampaignError(f"GNU time is missing: {GNU_TIME}")
    if shutil.which("numactl") is None:
        raise CampaignError("numactl is required for paired macro measurements")
    available_cpus = set(os.sched_getaffinity(0))
    for target_id in plan.requested_targets:
        requested_cpus = psr.parse_cpu_list(
            str(TARGET_EXECUTION_CONTRACTS[target_id]["cpu_list"])
        )
        if not requested_cpus.issubset(available_cpus):
            raise CampaignError(
                f"measurement CPU set for {target_id} is outside process affinity"
            )
    try:
        implementation = redb_actix.materialize_implementation_revision(
            raw_dir,
            args.implementation_revision,
            contract=contract.suite,
        )
        redb_actix.verify_implementation_snapshot(implementation)
    except redb_actix.CampaignError as error:
        raise CampaignError(str(error)) from error
    sources = materialize_sources(
        plan.requested_targets,
        contract=contract,
        checkout_root=args.checkout_root.resolve(),
        raw_dir=raw_dir,
    )
    builds = ensure_variant_builds(
        protocol=protocol,
        contract=contract,
        plan=plan,
        sources=sources,
        implementation=implementation,
        raw_dir=raw_dir,
        toolchain=args.toolchain,
        jobs=args.jobs,
        timeout=args.build_timeout,
        reuse=args.reuse,
        tcmalloc_runtime=tcmalloc_runtime,
    )
    persist_build_index(raw_dir, builds)
    if args.build_only:
        print(raw_dir / "build-index.json")
        return 0
    runtime_dependency: Mapping[str, Any] | None = None
    if "rustpython" in plan.requested_targets:
        try:
            runtime_dependency = psr.rustpython_runtime_contract()
        except psr.CampaignError as error:
            raise CampaignError(str(error)) from error
        write_json(raw_dir / "runtime/rustpython.json", runtime_dependency)
    polars_csv: pathlib.Path | None = None
    if "polars" in plan.requested_targets:
        polars_csv = raw_dir / "inputs/polars/primary.csv"
        try:
            csv_record = psr.generate_polars_csv(polars_csv)
        except psr.CampaignError as error:
            raise CampaignError(str(error)) from error
        write_json(raw_dir / "inputs/polars/input.json", csv_record)
    cells = run_measurement_plan(
        plan=plan,
        protocol=protocol,
        contract=contract,
        builds=builds,
        sources=sources,
        raw_dir=raw_dir,
        timeout=args.run_timeout,
        reuse=args.reuse,
        tcmalloc_runtime=tcmalloc_runtime,
        runtime_dependency=runtime_dependency,
        polars_csv=polars_csv,
        suite_binding=suite_binding,
        quiescence_timeout=args.quiescence_timeout,
        quiet_seconds=args.quiet_seconds,
    )
    regenerate_cell_index(raw_dir)
    summary_path = persist_summary_view(
        raw_dir=raw_dir, plan=plan, contract=contract, cells=cells
    )
    print(summary_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CampaignError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
