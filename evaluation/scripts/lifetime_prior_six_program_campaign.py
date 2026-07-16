#!/usr/bin/env python3
"""Build and run the two-stage Rust lifetime-prior macro campaign.

This module is deliberately split from the existing primary-suite runners.  It
reuses their exact source identities, target-crate lists, and harness names,
while defining the additional contracts needed by the lifetime experiment:

* Stage A is a 20--60 second, force-track, ordinary-page screening run.
* Stage B runs matched 2--5 minute arms only after an opportunity gate passes.
* A ten-minute per-process timeout is a hard experiment boundary.

Stage A has executable same-process adapters for all six exact-pinned targets.
It builds an all-Unknown control and a compiler-prior group from one frozen
allocator/pass snapshot, runs both with force-tracked ordinary backing, and
retains compiler audits, exact runtime sites, fragmentation, and procfs samples.
Criterion remains diagnostic-only for SWC, RustPython, and Actix Web; it gives
the classifier a long-lived process without entering fixed-work performance
claims.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from typing import Any, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import polars_swc_rustpython_primary_campaign as psr  # noqa: E402
import realworld_type_isolation_matrix as matrix  # noqa: E402
import runtime_lifetime_classifier_realapp as runtime_lifetime  # noqa: E402
import type_isolation_primary_collections_oxipng as collections_oxipng  # noqa: E402
import type_isolation_redb_actix_campaign as redb_actix  # noqa: E402


SCHEMA_VERSION = 1
MIB = 1024 * 1024
GIB = 1024 * MIB
DEFAULT_SCREEN_MIN_SECONDS = 20.0
DEFAULT_SCREEN_TARGET_SECONDS = 40.0
DEFAULT_SCREEN_MAX_SECONDS = 60.0
DEFAULT_PERFORMANCE_MIN_SECONDS = 120.0
DEFAULT_PERFORMANCE_TARGET_SECONDS = 180.0
DEFAULT_PERFORMANCE_MAX_SECONDS = 300.0
HARD_PROCESS_CAP_SECONDS = 600.0
DEFAULT_LONG_COHORT_BYTES = 2 * MIB
DEFAULT_CONFIRMED_SHORT_BYTES = 8 * MIB
RUNTIME_LONG_AGE_BYTES = 8 * MIB
AUTO_PRIOR_ENV = "UNIALLOC_AUTO_RUST_LIFETIME_PRIOR"
RELEASE_STRIP_ENV = "CARGO_PROFILE_RELEASE_STRIP"
RUNTIME_ARM_ENV = "UNIALLOC_LIFETIME_EXPERIMENT_ARM"
FRAGMENTATION_PREFIX = "UNIALLOC_LIFETIME_FRAGMENTATION="
MECHANISM_PREFIX = "UNIALLOC_LIFETIME_MECHANISM="
OXIPNG_INPUT_SHA256 = (
    "909418f09bfb42612575aa3d12c7d7b57546d361b382a9ebd8d3c83531948334"
)
FORCE_TRACK_MAXIMUM_ALLOCATIONS = 200_000_000
FORCE_TRACK_MAXIMUM_REQUESTED_BYTES = 2 * 1024 * GIB
TOOLCHAIN = psr.TOOLCHAIN
BUILD_GROUPS = ("all-unknown", "compiler-prior")
STAGE_A_ARM_BY_BUILD_GROUP = {
    "all-unknown": "force-track-all-unknown-diagnostic",
    "compiler-prior": "force-track-compiler-prior-diagnostic",
}
SOURCE_REPOSITORIES = {
    "oxipng": collections_oxipng.TARGETS["oxipng"].repository,
    "redb": redb_actix.TARGETS["redb"].repository,
    "polars": "https://github.com/pola-rs/polars.git",
    "swc": "https://github.com/swc-project/swc.git",
    "rustpython": "https://github.com/RustPython/RustPython.git",
    "actix_web": redb_actix.TARGETS["actix_web"].repository,
}
CHECKOUT_NAMES = {
    "oxipng": "primary-oxipng-v10.1.1",
    "redb": redb_actix.TARGETS["redb"].checkout_name,
    "polars": psr.TARGET_SPECS["polars"].checkout,
    "swc": psr.TARGET_SPECS["swc"].checkout,
    "rustpython": psr.TARGET_SPECS["rustpython"].checkout,
    "actix_web": redb_actix.TARGETS["actix_web"].checkout_name,
}
PRIOR_CLASSIFIED_BASIS_CONTRACT = {
    "automatic_rust_lifetime_prior_all_path_local_release_short": (1, 85),
    "automatic_rust_lifetime_prior_receiver_owned_short": (1, 85),
    "automatic_rust_lifetime_prior_borrowed_vec_reserve_observe": (0xA103, 70),
    "automatic_rust_lifetime_prior_dynamic_buffer_observe": (0xA103, 70),
    "automatic_rust_lifetime_prior_return_long": (2, 70),
    "automatic_rust_lifetime_prior_escape_long": (2, 70),
}
BORROWED_VEC_RESERVE_OBSERVE_HINT = 0xA103
BORROWED_VEC_RESERVE_OBSERVE_BASIS = (
    "automatic_rust_lifetime_prior_borrowed_vec_reserve_observe"
)
DYNAMIC_BUFFER_OBSERVE_BASES = frozenset(
    {
        BORROWED_VEC_RESERVE_OBSERVE_BASIS,
        "automatic_rust_lifetime_prior_dynamic_buffer_observe",
    }
)
BORROWED_VEC_RESERVE_SUBCOHORT_CONTRACT = (
    "authenticated-key3-to-runtime-key5-4k-through-32k"
)
BORROWED_VEC_RESERVE_MIN_REQUESTED_BYTES = 4 * 1024
BORROWED_VEC_RESERVE_MAX_REQUESTED_BYTES = 32 * 1024
APPLIED_ALLOCATION_SCOPE_STATUSES = frozenset(
    {
        "actual_allocator_call_replacement_applied",
        "actual_semantic_scope_enter_exit_rewrite_applied",
        "actual_semantic_scope_generic_type_rewrite_applied",
    }
)
GENERIC_RUNTIME_TYPE_ID_BASIS = "monomorphized_compiler_type_id_runtime"
GENERIC_REWRITE_STATUS = "actual_semantic_scope_generic_type_rewrite_applied"
GENERIC_REPLACEMENT_CONTRACTS = {
    "__unialloc_semantic_scope_push_for_rust_type": (
        "resolved_unialloc_semantic_scope_push_for_rust_type_pop",
        "semantic_scope_monomorphized_runtime_type_metadata",
    ),
    "__unialloc_semantic_scope_push_for_rust_type_local": (
        "resolved_unialloc_semantic_scope_push_for_rust_type_local_pop",
        "semantic_scope_monomorphized_runtime_type_metadata_local_no_recovery",
    ),
    "__unialloc_semantic_scope_push_for_rust_type_hints": (
        "resolved_unialloc_semantic_scope_push_for_rust_type_hints_pop",
        "semantic_scope_monomorphized_runtime_type_metadata",
    ),
    "__unialloc_semantic_scope_push_for_rust_type_hints_local": (
        "resolved_unialloc_semantic_scope_push_for_rust_type_hints_local_pop",
        "semantic_scope_monomorphized_runtime_type_metadata_local_no_recovery",
    ),
}
GENERIC_RESOLUTION_KEY_FIELDS = (
    "callsite",
    "module_id",
    "requested_size",
    "align",
)
COMPILER_SITE_EXPORT_SOURCE = "unialloc-compiler-runtime-exact-site-export-v3"
COMPILER_SITE_EXPORT_SUMMARY_FIELDS = (
    "source",
    "runtime_join_key",
    "generic_resolution_key",
    "claim_scope",
    "site_count",
    "numeric_site_count",
    "generic_site_count",
    "complete_join_key_count",
    "audit_complete_join_key_count",
    "numeric_exact_key_complete_count",
    "dynamic_layout_site_count",
    "dynamic_layout_key_complete_count",
    "generic_resolution_key_complete_count",
    "applied_prior_hinted_count",
    "applied_observation_candidate_count",
)
DYNAMIC_LAYOUT_IDENTITY_KEY_FIELDS = ("callsite", "type_id", "module_id")
RUNTIME_OUTCOME_AGGREGATE_FIELDS = (
    "allocation_count",
    "allocation_requested_bytes",
    "long_outcomes",
    "short_outcomes",
    "censored_outcomes",
    "long_requested_bytes",
    "short_requested_bytes",
    "censored_requested_bytes",
    "live_survival_observations",
    "live_survival_requested_bytes",
    "live_survival_payload_bytes",
    "current_live_survival_inflight",
    "current_live_survival_inflight_requested_bytes",
    "current_live_survival_inflight_payload_bytes",
)
TARGET_SNAPSHOT_DEPENDENCY_REWRITES: dict[str, tuple[dict[str, Any], ...]] = {
    "swc": (
        {
            "package": "num_cpus",
            "old_version": "1.13.0",
            "sections": ("build-dependencies",),
            "expected_occurrences": 1,
        },
    ),
    "actix_web": (
        {
            "package": "num_cpus",
            "old_version": "1.13.0",
            "sections": ("build-dependencies",),
            "expected_occurrences": 1,
        },
    ),
    "rustpython": (
        {
            "package": "libc",
            "old_version": "0.2.183",
            "sections": ("dependencies", "build-dependencies"),
            "expected_occurrences": 2,
        },
        {
            "package": "num_cpus",
            "old_version": "1.13.0",
            "sections": ("build-dependencies",),
            "expected_occurrences": 1,
        },
    ),
}


class CampaignContractError(RuntimeError):
    """An input or evidence row cannot satisfy the experiment contract."""


class AdapterBlocked(CampaignContractError):
    """A target has no claim-grade fixed-work adapter yet."""


class BuildBlocked(CampaignContractError):
    """A real compiler/build command failed and retained its artifacts."""


@dataclass(frozen=True)
class ArmSpec:
    name: str
    stage: str
    build_group: str
    automatic_rust_lifetime_prior: bool
    expected_policy: int
    backing: str
    force_track: bool
    performance_arm: bool


ARMS: tuple[ArmSpec, ...] = (
    ArmSpec(
        "default",
        "performance",
        "all-unknown",
        False,
        0,
        "allocator-default",
        False,
        True,
    ),
    ArmSpec(
        "adaptive-ordinary-all-unknown",
        "performance",
        "all-unknown",
        False,
        8,
        "ordinary",
        False,
        True,
    ),
    ArmSpec(
        "adaptive-ordinary-compiler-prior",
        "performance",
        "compiler-prior",
        True,
        8,
        "ordinary",
        False,
        True,
    ),
    ArmSpec(
        "adaptive-selective-thp-all-unknown",
        "performance",
        "all-unknown",
        False,
        5,
        "selective-thp",
        False,
        True,
    ),
    ArmSpec(
        "adaptive-selective-thp-compiler-prior",
        "performance",
        "compiler-prior",
        True,
        5,
        "selective-thp",
        False,
        True,
    ),
    ArmSpec(
        "force-track-all-unknown-diagnostic",
        "screening",
        "all-unknown",
        False,
        8,
        "ordinary",
        True,
        False,
    ),
    ArmSpec(
        "force-track-compiler-prior-diagnostic",
        "screening",
        "compiler-prior",
        True,
        8,
        "ordinary",
        True,
        False,
    ),
)
ARM_BY_NAME = {arm.name: arm for arm in ARMS}
PERFORMANCE_ARMS = tuple(arm for arm in ARMS if arm.performance_arm)
SCREENING_ARMS = tuple(arm for arm in ARMS if arm.stage == "screening")
SCREENING_ARM = ARM_BY_NAME["force-track-compiler-prior-diagnostic"]
RUNTIME_ARM_SELECTOR_ALIASES = {
    "adaptive-selective-thp-all-unknown": (
        "adaptive-selective-thp-compiler-prior"
    ),
}


def runtime_arm_selector(arm: ArmSpec) -> str:
    selector = RUNTIME_ARM_SELECTOR_ALIASES.get(arm.name, arm.name)
    selected = ARM_BY_NAME[selector]
    if (
        selected.expected_policy != arm.expected_policy
        or selected.backing != arm.backing
        or selected.force_track != arm.force_track
    ):
        raise CampaignContractError(
            f"runtime selector changes arm semantics: {arm.name} -> {selector}"
        )
    return selector


@dataclass(frozen=True)
class Calibration:
    source: str
    work_units: int
    elapsed_seconds: float
    peak_rss_kib: int | None
    claim_grade: bool
    note: str


@dataclass(frozen=True)
class TargetContract:
    target_id: str
    label: str
    source_commit: str
    source_ref: str
    runner: Path
    target_crates: tuple[str, ...]
    harness_id: str
    adapter_kind: str
    work_unit: str
    work_granularity: int
    screening_ready: bool
    performance_ready: bool
    screening_boundary: str
    performance_blocker: str | None
    calibration: Calibration | None


def _target_contracts() -> dict[str, TargetContract]:
    oxipng = collections_oxipng.TARGETS["oxipng"]
    redb = redb_actix.TARGETS["redb"]
    actix = redb_actix.TARGETS["actix_web"]
    polars = psr.TARGET_SPECS["polars"]
    swc = psr.TARGET_SPECS["swc"]
    rustpython = psr.TARGET_SPECS["rustpython"]
    return {
        "oxipng": TargetContract(
            target_id="oxipng",
            label="Oxipng",
            source_commit=oxipng.commit,
            source_ref=oxipng.ref,
            runner=Path(collections_oxipng.__file__).resolve(),
            target_crates=collections_oxipng.target_crates(oxipng),
            harness_id="cli_issue_141_t1",
            adapter_kind="oxipng-multi-input-v1",
            work_unit="pinned issue-141 input copy",
            work_granularity=8,
            screening_ready=True,
            performance_ready=True,
            screening_boundary="fixed work and per-output PNG digest",
            performance_blocker=None,
            calibration=Calibration(
                source="old-f5d0-typeiso-perf-host-calibration",
                work_units=8,
                elapsed_seconds=10.87,
                peak_rss_kib=42_276,
                claim_grade=False,
                note=(
                    "Compatibility calibration only; refresh with the frozen "
                    "experiment binary before claim-grade collection."
                ),
            ),
        ),
        "redb": TargetContract(
            target_id="redb",
            label="redb",
            source_commit=redb.source_commit,
            source_ref=redb.source_ref,
            runner=Path(redb_actix.__file__).resolve(),
            target_crates=tuple(redb.target_crates),
            harness_id="delete_reinsert",
            adapter_kind="redb-fixed-repeat-driver-v1",
            work_unit="delete/reinsert epoch on one persistent Database",
            work_granularity=1,
            screening_ready=True,
            performance_ready=True,
            screening_boundary="fixed repeat count and final table digest",
            performance_blocker=None,
            calibration=None,
        ),
        "polars": TargetContract(
            target_id="polars",
            label="Polars",
            source_commit=polars.commit,
            source_ref=polars.source_ref,
            runner=Path(psr.__file__).resolve(),
            target_crates=tuple(polars.target_crates),
            harness_id="filter_retain",
            adapter_kind="polars-persistent-frame-loop-v1",
            work_unit="fixed collect over one retained input/plan",
            work_granularity=1,
            screening_ready=True,
            performance_ready=False,
            screening_boundary=(
                "diagnostic until the repeated generated driver is built and its "
                "fixed-work output digest is paired across arms"
            ),
            performance_blocker=(
                "Build the repeated generated driver and validate fixed work/output "
                "digest across every matched arm."
            ),
            calibration=Calibration(
                source="old-f5d0-unialloc-primary-median",
                work_units=1,
                elapsed_seconds=0.4538,
                peak_rss_kib=151_552,
                claim_grade=False,
                note="Planning estimate; refresh with the amplified prior binary.",
            ),
        ),
        "swc": TargetContract(
            target_id="swc",
            label="SWC",
            source_commit=swc.commit,
            source_ref=swc.source_ref,
            runner=Path(psr.__file__).resolve(),
            target_crates=tuple(swc.target_crates),
            harness_id="large_fixer",
            adapter_kind="swc-persistent-compiler-loop-v1",
            work_unit="fixed clone/fix/codegen epoch with retained Compiler/AST",
            work_granularity=1,
            screening_ready=True,
            performance_ready=False,
            screening_boundary=(
                "Criterion 20-60 second diagnostic; dynamic iteration count is "
                "excluded from performance claims"
            ),
            performance_blocker=(
                "Criterion controls time rather than exact work. Add a fixed-count "
                "driver that retains Compiler, SourceMap, and the parsed module."
            ),
            calibration=None,
        ),
        "rustpython": TargetContract(
            target_id="rustpython",
            label="RustPython",
            source_commit=rustpython.commit,
            source_ref=rustpython.source_ref,
            runner=Path(psr.__file__).resolve(),
            target_crates=tuple(rustpython.target_crates),
            harness_id="execute_mandelbrot",
            adapter_kind="rustpython-persistent-vm-loop-v1",
            work_unit="fixed execution with one retained Interpreter",
            work_granularity=1,
            screening_ready=True,
            performance_ready=False,
            screening_boundary=(
                "Criterion 20-60 second diagnostic; dynamic iteration count is "
                "excluded from performance claims"
            ),
            performance_blocker=(
                "Criterion controls time rather than exact work. Add a fixed-count "
                "driver that constructs Interpreter once and hashes every result."
            ),
            calibration=None,
        ),
        "actix_web": TargetContract(
            target_id="actix_web",
            label="Actix Web",
            source_commit=actix.source_commit,
            source_ref=actix.source_ref,
            runner=Path(redb_actix.__file__).resolve(),
            target_crates=tuple(actix.target_crates),
            harness_id="async_web_service_direct",
            adapter_kind="actix-fixed-request-driver-v1",
            work_unit="fixed request with retained runtime/service/router",
            work_granularity=256,
            screening_ready=True,
            performance_ready=False,
            screening_boundary=(
                "Criterion 20-60 second diagnostic; dynamic request count is "
                "excluded from performance claims"
            ),
            performance_blocker=(
                "Criterion iter_custom batching has no fixed request-count oracle. "
                "Add a bounded-concurrency request driver and retain the service."
            ),
            calibration=None,
        ),
    }


TARGETS = _target_contracts()
TARGET_ORDER = ("oxipng", "redb", "polars", "swc", "rustpython", "actix_web")
TARGET_CRATE_COVERAGE_GAPS: dict[str, tuple[dict[str, str], ...]] = {
    "rustpython": (
        {
            "crate": "rustpython_ruff_python_parser",
            "reason": (
                "external Git dependency outside the copied RustPython workspace; "
                "its rustc invocation has no direct --extern unialloc transport"
            ),
            "claim_boundary": "first-party RustPython crates only",
        },
    ),
}


def stage_a_target_crates(target_id: str) -> tuple[str, ...]:
    if target_id == "oxipng":
        return ("oxipng",)
    if target_id == "actix_web":
        return ("service", "actix_web", "actix_http", "actix_router")
    excluded = {
        row["crate"] for row in TARGET_CRATE_COVERAGE_GAPS.get(target_id, ())
    }
    return tuple(
        crate for crate in TARGETS[target_id].target_crates if crate not in excluded
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def campaign_evaluator_provenance() -> dict[str, Any]:
    paths = {
        "stage_a_runner": Path(__file__).resolve(),
        "primary_campaign_helper": Path(str(psr.__file__)).resolve(),
        "realworld_matrix_helper": Path(str(matrix.__file__)).resolve(),
        "runtime_lifetime_helper": Path(str(runtime_lifetime.__file__)).resolve(),
        "collections_oxipng_helper": Path(str(collections_oxipng.__file__)).resolve(),
        "redb_actix_helper": Path(str(redb_actix.__file__)).resolve(),
    }
    files = {
        name: {"path": str(path), "sha256": sha256_file(path)}
        for name, path in paths.items()
    }
    return {
        "source": "unialloc-stage-a-evaluator-files-v1",
        "files": files,
        "digest": canonical_json_sha256(files),
    }


def arm_build_environment(arm: ArmSpec) -> dict[str, str]:
    """Return the only experiment-specific compiler environment for an arm."""
    return {AUTO_PRIOR_ENV: "1"} if arm.automatic_rust_lifetime_prior else {}


def validate_arm_contract() -> None:
    if len(ARM_BY_NAME) != len(ARMS):
        raise CampaignContractError("duplicate arm name")
    if [arm.name for arm in PERFORMANCE_ARMS] != [
        "default",
        "adaptive-ordinary-all-unknown",
        "adaptive-ordinary-compiler-prior",
        "adaptive-selective-thp-all-unknown",
        "adaptive-selective-thp-compiler-prior",
    ]:
        raise CampaignContractError("matched performance arm order changed")
    for arm in ARMS:
        environment = arm_build_environment(arm)
        if arm.automatic_rust_lifetime_prior:
            if environment != {AUTO_PRIOR_ENV: "1"}:
                raise CampaignContractError(f"prior arm is not opt-in: {arm.name}")
        elif AUTO_PRIOR_ENV in environment:
            raise CampaignContractError(f"baseline arm enables compiler prior: {arm.name}")
    if tuple(arm.build_group for arm in SCREENING_ARMS) != BUILD_GROUPS:
        raise CampaignContractError("Stage-A build-group order changed")
    for arm in SCREENING_ARMS:
        if arm.force_track is not True or arm.performance_arm:
            raise CampaignContractError("force-track diagnostic leaked into matched arms")
        if arm.expected_policy != 8 or arm.backing != "ordinary":
            raise CampaignContractError("Stage A must use force-tracked ordinary backing")


def validate_target_contracts() -> None:
    if tuple(TARGETS) != TARGET_ORDER:
        raise CampaignContractError("six-target order changed")
    for target in TARGETS.values():
        if not target.runner.is_file():
            raise CampaignContractError(f"runner is missing: {target.runner}")
        if len(target.source_commit) != 40:
            raise CampaignContractError(f"source pin is not exact: {target.target_id}")
        if not target.target_crates:
            raise CampaignContractError(f"target crate list is empty: {target.target_id}")
        if not target.screening_ready:
            raise CampaignContractError(
                f"screening adapter is unavailable: {target.target_id}"
            )
        if target.performance_ready == (target.performance_blocker is not None):
            raise CampaignContractError(
                f"adapter readiness/blocker mismatch: {target.target_id}"
            )


def round_work_units(value: float, granularity: int) -> int:
    if not math.isfinite(value) or value <= 0 or granularity <= 0:
        raise CampaignContractError("invalid work-unit rounding input")
    return max(granularity, int(math.ceil(value / granularity)) * granularity)


def derive_work_units(
    calibration: Calibration,
    *,
    target_seconds: float,
    granularity: int,
    hard_cap_seconds: float = HARD_PROCESS_CAP_SECONDS,
) -> dict[str, Any]:
    if (
        calibration.work_units <= 0
        or not math.isfinite(calibration.elapsed_seconds)
        or calibration.elapsed_seconds <= 0
        or target_seconds <= 0
        or target_seconds > hard_cap_seconds
    ):
        raise CampaignContractError("invalid calibration geometry")
    seconds_per_unit = calibration.elapsed_seconds / calibration.work_units
    work_units = round_work_units(target_seconds / seconds_per_unit, granularity)
    estimated_seconds = seconds_per_unit * work_units
    if estimated_seconds > hard_cap_seconds:
        raise CampaignContractError("derived work exceeds the ten-minute hard cap")
    return {
        "calibration": asdict(calibration),
        "seconds_per_work_unit": seconds_per_unit,
        "target_seconds": target_seconds,
        "work_granularity": granularity,
        "work_units": work_units,
        "estimated_seconds": estimated_seconds,
        "hard_cap_seconds": hard_cap_seconds,
    }


def calibration_record(target: TargetContract) -> dict[str, Any] | None:
    calibration = target.calibration
    if calibration is None:
        return None
    record = asdict(calibration)
    if target.target_id == "oxipng":
        seconds_per_unit = calibration.elapsed_seconds / calibration.work_units
        record["input_sha256"] = OXIPNG_INPUT_SHA256
        record["validated_multi_input_command"] = (
            "oxipng --opt 2 --threads 1 --force --quiet --dir OUT IN/*.png"
        )
        record["estimated_seconds_by_work_units"] = {
            "64": 64 * seconds_per_unit,
            "128": 128 * seconds_per_unit,
        }
    return record


def validate_stage_duration(stage: str, elapsed_seconds: float) -> None:
    if not math.isfinite(elapsed_seconds) or elapsed_seconds <= 0:
        raise CampaignContractError("elapsed time is invalid")
    if elapsed_seconds > HARD_PROCESS_CAP_SECONDS:
        raise CampaignContractError("sample exceeded the ten-minute hard cap")
    if stage == "screening":
        if not DEFAULT_SCREEN_MIN_SECONDS <= elapsed_seconds <= DEFAULT_SCREEN_MAX_SECONDS:
            raise CampaignContractError("screening sample is outside 20--60 seconds")
        return
    if stage == "performance":
        if not (
            DEFAULT_PERFORMANCE_MIN_SECONDS
            <= elapsed_seconds
            <= DEFAULT_PERFORMANCE_MAX_SECONDS
        ):
            raise CampaignContractError("performance sample is outside 2--5 minutes")
        return
    raise CampaignContractError(f"unknown stage: {stage}")


def _nonnegative_integer(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CampaignContractError(f"{field} is not a nonnegative integer")
    return value


def summarize_screening_sites(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    long_bytes = 0
    short_bytes = 0
    censored_bytes = 0
    confirmed_short_bytes = 0
    allocation_bytes = 0
    long_sites = 0
    short_sites = 0
    confirmed_short_sites = 0
    decisive_sites = 0
    live_survival_observations = 0
    current_live_survival_inflight = 0
    current_live_survival_requested_bytes = 0
    for row in rows:
        requested_size = _nonnegative_integer(row, "requested_size")
        if requested_size == 0:
            raise CampaignContractError("screening site has zero requested size")
        completed_long = _nonnegative_integer(row, "long_requested_bytes")
        row_current_live_survival = _nonnegative_integer(
            row, "current_live_survival_inflight"
        )
        row_current_live_survival_bytes = _nonnegative_integer(
            row, "current_live_survival_inflight_requested_bytes"
        )
        row_live_survival_observations = _nonnegative_integer(
            row, "live_survival_observations"
        )
        if row_current_live_survival > row_live_survival_observations:
            raise CampaignContractError(
                "current live-survival count exceeds cumulative provenance"
            )
        if row_current_live_survival_bytes != row_current_live_survival * requested_size:
            raise CampaignContractError(
                "current live-survival requested-byte accounting does not close"
            )
        row_long = completed_long + row_current_live_survival_bytes
        row_short = _nonnegative_integer(row, "short_requested_bytes")
        row_censored = _nonnegative_integer(row, "censored_requested_bytes")
        row_allocation = _nonnegative_integer(row, "allocation_requested_bytes")
        latest_prediction = _nonnegative_integer(row, "latest_prediction")
        if latest_prediction not in {0, 1, 2}:
            raise CampaignContractError("screening site prediction is invalid")
        long_bytes += row_long
        short_bytes += row_short
        censored_bytes += row_censored
        allocation_bytes += row_allocation
        long_sites += int(row_long > 0)
        short_sites += int(row_short > 0)
        decisive_sites += int(row_long + row_short > 0)
        live_survival_observations += row_live_survival_observations
        current_live_survival_inflight += row_current_live_survival
        current_live_survival_requested_bytes += row_current_live_survival_bytes
        if latest_prediction == 1:
            confirmed_short_bytes += row_short
            confirmed_short_sites += 1
    decisive_bytes = long_bytes + short_bytes
    return {
        "runtime_site_count": len(rows),
        "decisive_site_count": decisive_sites,
        "long_site_count": long_sites,
        "short_site_count": short_sites,
        "confirmed_short_site_count": confirmed_short_sites,
        "long_requested_bytes": long_bytes,
        "completed_long_requested_bytes": sum(
            _nonnegative_integer(row, "long_requested_bytes") for row in rows
        ),
        "live_survival_observations": live_survival_observations,
        "current_live_survival_inflight": current_live_survival_inflight,
        "current_live_survival_inflight_requested_bytes": (
            current_live_survival_requested_bytes
        ),
        "short_requested_bytes": short_bytes,
        "confirmed_short_requested_bytes": confirmed_short_bytes,
        "censored_requested_bytes": censored_bytes,
        "allocation_requested_bytes": allocation_bytes,
        "decisive_requested_bytes": decisive_bytes,
        "candidate_long_byte_density": (
            long_bytes / decisive_bytes if decisive_bytes else 0.0
        ),
        "confirmed_short_byte_density": (
            confirmed_short_bytes / decisive_bytes if decisive_bytes else 0.0
        ),
    }


def opportunity_gate(
    summary: Mapping[str, Any],
    *,
    minimum_long_bytes: int = DEFAULT_LONG_COHORT_BYTES,
    minimum_confirmed_short_bytes: int = DEFAULT_CONFIRMED_SHORT_BYTES,
) -> dict[str, Any]:
    long_bytes = _nonnegative_integer(summary, "long_requested_bytes")
    short_bytes = _nonnegative_integer(summary, "confirmed_short_requested_bytes")
    long_pass = long_bytes >= minimum_long_bytes
    short_pass = short_bytes >= minimum_confirmed_short_bytes
    return {
        "passed": long_pass or short_pass,
        "long_cohort_passed": long_pass,
        "confirmed_short_passed": short_pass,
        "observed_long_requested_bytes": long_bytes,
        "observed_confirmed_short_requested_bytes": short_bytes,
        "minimum_long_requested_bytes": minimum_long_bytes,
        "minimum_confirmed_short_requested_bytes": minimum_confirmed_short_bytes,
    }


def summarize_compiler_prior_audits(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        raise CampaignContractError(f"compiler audit root is missing: {root}")
    files: list[dict[str, Any]] = []
    enabled_values: set[bool] = set()
    basis_counts: Counter[str] = Counter()
    hint_counts: Counter[int] = Counter()
    confidence_counts: Counter[int] = Counter()
    classified_candidates = 0
    audited_crates: set[str] = set()
    provenance_failures: list[dict[str, Any]] = []
    applied_allocation_candidates = 0
    applied_prior_hinted_candidates = 0
    applied_observation_candidates = 0
    crate_provenance: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict) or not isinstance(
            document.get("rewrite_candidates"), list
        ):
            continue
        compiler_pass = document.get("compiler_pass")
        if not isinstance(compiler_pass, dict):
            raise CampaignContractError(f"audit lacks compiler_pass: {path}")
        enabled = compiler_pass.get("automatic_rust_lifetime_prior_enabled")
        if not isinstance(enabled, bool):
            raise CampaignContractError(f"audit lacks lifetime-prior mode: {path}")
        enabled_values.add(enabled)
        actual_requested = compiler_pass.get(
            "actual_semantic_scope_rewrite_requested"
        )
        actual_rewrite = compiler_pass.get("actual_semantic_scope_rewrite")
        body_clone = compiler_pass.get("body_clone_returned_to_rustc")
        continue_compilation = compiler_pass.get("continue_compilation")
        candidates = document["rewrite_candidates"]
        if any(not isinstance(raw, dict) for raw in candidates):
            raise CampaignContractError(f"audit candidate is invalid: {path}")
        file_applied_allocation_candidates = sum(
            int(str(raw.get("rewrite_status") or "") in APPLIED_ALLOCATION_SCOPE_STATUSES)
            for raw in candidates
        )
        rustc_args = document.get("rustc_args")
        crate_name = ""
        if isinstance(rustc_args, list):
            for index, value in enumerate(rustc_args):
                if value == "--crate-name" and index + 1 < len(rustc_args):
                    crate_name = str(rustc_args[index + 1]).replace("-", "_")
                    audited_crates.add(crate_name)
                elif isinstance(value, str) and value.startswith("--crate-name="):
                    crate_name = value.split("=", 1)[1].replace("-", "_")
                    audited_crates.add(crate_name)
        base_provenance_valid = (
            actual_requested is True
            and body_clone is True
            and continue_compilation is True
        )
        allocation_rewrite_required = file_applied_allocation_candidates > 0
        allocation_rewrite_valid = (
            not allocation_rewrite_required or actual_rewrite is True
        )
        provenance_valid = base_provenance_valid and allocation_rewrite_valid
        if not provenance_valid:
            provenance_failures.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "crate_name": crate_name,
                    "actual_semantic_scope_rewrite_requested": actual_requested,
                    "actual_semantic_scope_rewrite": actual_rewrite,
                    "body_clone_returned_to_rustc": body_clone,
                    "continue_compilation": continue_compilation,
                    "base_provenance_valid": base_provenance_valid,
                    "allocation_rewrite_required": allocation_rewrite_required,
                    "allocation_rewrite_valid": allocation_rewrite_valid,
                    "applied_allocation_candidate_count": (
                        file_applied_allocation_candidates
                    ),
                }
            )
        if crate_name:
            state = crate_provenance.setdefault(
                crate_name,
                {
                    "audit_file_count": 0,
                    "base_valid_file_count": 0,
                    "applied_allocation_file_count": 0,
                    "valid_applied_allocation_file_count": 0,
                    "valid_file_count": 0,
                },
            )
            state["audit_file_count"] += 1
            state["base_valid_file_count"] += int(base_provenance_valid)
            state["applied_allocation_file_count"] += int(
                allocation_rewrite_required
            )
            state["valid_applied_allocation_file_count"] += int(
                allocation_rewrite_required and allocation_rewrite_valid
            )
            state["valid_file_count"] += int(provenance_valid)
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
            }
        )
        for raw in candidates:
            basis = str(raw.get("lifetime_hint_basis") or "")
            hint = raw.get("lifetime_hint", 0)
            confidence = raw.get("lifetime_hint_confidence", 0)
            if not isinstance(hint, int) or isinstance(hint, bool) or hint < 0:
                raise CampaignContractError(f"audit hint is invalid: {path}")
            if (
                not isinstance(confidence, int)
                or isinstance(confidence, bool)
                or not 0 <= confidence <= 100
            ):
                raise CampaignContractError(f"audit confidence is invalid: {path}")
            if basis.startswith("automatic_rust_lifetime_prior_"):
                expected = PRIOR_CLASSIFIED_BASIS_CONTRACT.get(basis)
                if expected is not None and (hint, confidence) != expected:
                    raise CampaignContractError(
                        f"lifetime-prior basis contract changed: {basis}"
                    )
                if expected is None and (not basis.endswith("_unknown") or hint != 0):
                    raise CampaignContractError(
                        f"unrecognized lifetime-prior basis: {basis}"
                    )
                basis_counts[basis] += 1
                hint_counts[hint] += 1
                confidence_counts[confidence] += 1
                if hint in {1, 2}:
                    classified_candidates += 1
            rewrite_status = str(raw.get("rewrite_status") or "")
            applied_allocation = rewrite_status in APPLIED_ALLOCATION_SCOPE_STATUSES
            if applied_allocation:
                applied_allocation_candidates += 1
                if basis.startswith("automatic_rust_lifetime_prior_") and hint in {
                    1,
                    2,
                }:
                    applied_prior_hinted_candidates += 1
                if (
                    basis in DYNAMIC_BUFFER_OBSERVE_BASES
                    and hint == BORROWED_VEC_RESERVE_OBSERVE_HINT
                ):
                    applied_observation_candidates += 1
    if not files:
        raise CampaignContractError("compiler audit root has no rewrite audits")
    if len(enabled_values) != 1:
        raise CampaignContractError("compiler audit prior modes are mixed")
    enabled = next(iter(enabled_values))
    return {
        "source": "automatic-rust-lifetime-prior-audit-summary-v1",
        "audit_root": str(root.resolve()),
        "audit_file_count": len(files),
        "automatic_rust_lifetime_prior_enabled": enabled,
        "prior_basis_counts": dict(sorted(basis_counts.items())),
        "prior_hint_counts": {
            str(key): value for key, value in sorted(hint_counts.items())
        },
        "prior_confidence_counts": {
            str(key): value for key, value in sorted(confidence_counts.items())
        },
        "classified_candidate_count": classified_candidates,
        "applied_allocation_candidate_count": applied_allocation_candidates,
        "applied_prior_hinted_candidate_count": applied_prior_hinted_candidates,
        "applied_observation_candidate_count": applied_observation_candidates,
        "audited_crates": sorted(audited_crates),
        "actual_rewrite_provenance_valid": not provenance_failures,
        "provenance_failures": provenance_failures,
        "crate_provenance": dict(sorted(crate_provenance.items())),
        "files": files,
        "audit_digest": canonical_json_sha256(files),
    }


def validate_build_audit_for_arm(
    arm: ArmSpec,
    summary: Mapping[str, Any],
    expected_target_crates: Sequence[str] = (),
) -> None:
    enabled = summary.get("automatic_rust_lifetime_prior_enabled")
    if enabled is not arm.automatic_rust_lifetime_prior:
        raise CampaignContractError(f"compiler prior audit mismatch for {arm.name}")
    if summary.get("actual_rewrite_provenance_valid") is not True:
        raise CampaignContractError(
            f"compiler actual-rewrite/body-clone provenance failed for {arm.name}"
        )
    if _nonnegative_integer(summary, "applied_allocation_candidate_count") == 0:
        raise CampaignContractError(
            f"compiler emitted no applied allocation rewrite for {arm.name}"
        )
    if arm.automatic_rust_lifetime_prior:
        classified = _nonnegative_integer(summary, "classified_candidate_count")
        transported = _nonnegative_integer(
            summary, "applied_prior_hinted_candidate_count"
        )
        if classified > 0 and transported != classified:
            raise CampaignContractError(
                f"compiler prior transport is incomplete for {arm.name}: "
                f"classified={classified}, applied={transported}"
            )
        if transported > classified:
            raise CampaignContractError(
                f"compiler prior applied count exceeds classified count for {arm.name}"
            )
    expected = {value.replace("-", "_") for value in expected_target_crates}
    audited = {
        str(value).replace("-", "_") for value in summary.get("audited_crates", [])
    }
    missing = sorted(expected - audited)
    if missing:
        raise CampaignContractError(
            f"compiler audit misses requested target crates for {arm.name}: "
            + ",".join(missing)
        )
    crate_provenance = summary.get("crate_provenance")
    if not isinstance(crate_provenance, dict):
        raise CampaignContractError("compiler audit lacks per-crate provenance")
    invalid_crates = sorted(
        crate
        for crate in expected
        if not isinstance(crate_provenance.get(crate), dict)
        or crate_provenance[crate].get("base_valid_file_count", 0)
        != crate_provenance[crate].get("audit_file_count", 0)
        or crate_provenance[crate].get("valid_applied_allocation_file_count", 0)
        != crate_provenance[crate].get("applied_allocation_file_count", 0)
    )
    if invalid_crates:
        raise CampaignContractError(
            f"compiler rewrite provenance invalid for requested target crates: "
            + ",".join(invalid_crates)
        )


REQUIRED_FRAGMENTATION_FIELDS = frozenset(
    {
        "extent_bytes",
        "current_extents",
        "peak_extents",
        "current_ordinary_extents",
        "peak_ordinary_extents",
        "current_thp_extents",
        "peak_thp_extents",
        "live_objects",
        "live_slot_bytes",
        "retained_bytes",
        "reusable_unassigned_region_bytes",
        "cohort_pinned_unassigned_region_bytes",
        "assigned_region_slack_bytes",
        "retained_slack_bytes",
        "stranded_bytes",
    }
)

REQUIRED_MECHANISM_FIELDS = frozenset(
    {
        "ordinary_extent_mappings",
        "thp_extent_mappings",
        "thp_candidate_extent_mappings",
        "thp_advice_attempts",
        "thp_advice_successes",
        "thp_advice_errors",
        "thp_collapse_eligible_extents",
        "thp_collapse_low_occupancy_skips",
        "thp_collapse_attempts",
        "thp_collapse_successes",
        "thp_collapse_errors",
        "current_thp_collapse_confirmed_extents",
        "peak_thp_collapse_confirmed_extents",
        "extent_unmaps",
        "extent_unmap_failures",
        "current_extents",
        "peak_extents",
        "empty_extent_retention_capacity",
        "current_retained_empty_extents",
        "peak_retained_empty_extents",
        "current_retained_empty_bytes",
        "peak_retained_empty_bytes",
        "retained_empty_extent_insertions",
        "retained_empty_extent_reuse_hits",
        "retained_empty_extent_evictions",
        "retained_empty_extent_trims",
        "all_mappings_released",
    }
)


def parse_fragmentation(stderr: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for line in stderr.splitlines():
        if not line.startswith(FRAGMENTATION_PREFIX):
            continue
        try:
            value = json.loads(line[len(FRAGMENTATION_PREFIX) :])
        except json.JSONDecodeError as error:
            raise CampaignContractError("malformed fragmentation JSON") from error
        if isinstance(value, dict):
            rows.append(value)
    if len(rows) != 1:
        raise CampaignContractError(
            f"expected one fragmentation record, observed {len(rows)}"
        )
    row = rows[0]
    if set(row) != REQUIRED_FRAGMENTATION_FIELDS:
        raise CampaignContractError("fragmentation record does not match schema")
    for field in REQUIRED_FRAGMENTATION_FIELDS:
        _nonnegative_integer(row, field)
    if row["stranded_bytes"] != row["retained_slack_bytes"]:
        raise CampaignContractError("fragmentation compatibility counters diverged")
    if row["live_slot_bytes"] > row["retained_bytes"]:
        raise CampaignContractError("live slot bytes exceed retained bytes")
    if row["current_extents"] > row["peak_extents"]:
        raise CampaignContractError("current extents exceed peak extents")
    return row


def parse_mechanism(stderr: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for line in stderr.splitlines():
        if not line.startswith(MECHANISM_PREFIX):
            continue
        try:
            value = json.loads(line[len(MECHANISM_PREFIX) :])
        except json.JSONDecodeError as error:
            raise CampaignContractError("malformed lifetime mechanism JSON") from error
        if isinstance(value, dict):
            rows.append(value)
    if len(rows) != 1:
        raise CampaignContractError(
            f"expected one lifetime mechanism record, observed {len(rows)}"
        )
    row = rows[0]
    if set(row) != REQUIRED_MECHANISM_FIELDS:
        raise CampaignContractError("lifetime mechanism record does not match schema")
    for field in REQUIRED_MECHANISM_FIELDS - {"all_mappings_released"}:
        _nonnegative_integer(row, field)
    if not isinstance(row["all_mappings_released"], bool):
        raise CampaignContractError("lifetime mechanism release state is invalid")
    if row["current_extents"] > row["peak_extents"]:
        raise CampaignContractError("current mechanism extents exceed peak extents")
    if row["current_retained_empty_extents"] > row["peak_retained_empty_extents"]:
        raise CampaignContractError("current retained extents exceed peak retained extents")
    return row


PROC_SMAPS_FIELDS = (
    "Rss",
    "Pss",
    "Anonymous",
    "Private_Clean",
    "Private_Dirty",
    "Shared_Clean",
    "Shared_Dirty",
    "AnonHugePages",
    "ShmemPmdMapped",
    "FilePmdMapped",
    "Shared_Hugetlb",
    "Private_Hugetlb",
)


def parse_smaps_rollup(text: str, *, elapsed_seconds: float) -> dict[str, Any]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, separator, remainder = line.partition(":")
        if not separator or name not in PROC_SMAPS_FIELDS:
            continue
        fields = remainder.split()
        if len(fields) != 2 or fields[1] != "kB":
            raise CampaignContractError(f"invalid smaps field: {line!r}")
        try:
            value = int(fields[0])
        except ValueError as error:
            raise CampaignContractError(f"invalid smaps value: {line!r}") from error
        if value < 0:
            raise CampaignContractError(f"negative smaps value: {line!r}")
        values[name] = value
    missing = sorted(set(PROC_SMAPS_FIELDS) - set(values))
    if missing:
        raise CampaignContractError("smaps_rollup fields missing: " + ",".join(missing))
    if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise CampaignContractError("smaps sample timestamp is invalid")
    return {
        "elapsed_seconds": elapsed_seconds,
        "rss_kib": values["Rss"],
        "pss_kib": values["Pss"],
        "anonymous_kib": values["Anonymous"],
        "private_clean_kib": values["Private_Clean"],
        "private_dirty_kib": values["Private_Dirty"],
        "shared_clean_kib": values["Shared_Clean"],
        "shared_dirty_kib": values["Shared_Dirty"],
        "anon_hugepages_kib": values["AnonHugePages"],
        "shmem_pmd_mapped_kib": values["ShmemPmdMapped"],
        "file_pmd_mapped_kib": values["FilePmdMapped"],
        "shared_hugetlb_kib": values["Shared_Hugetlb"],
        "private_hugetlb_kib": values["Private_Hugetlb"],
    }


def summarize_proc_samples(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not samples:
        raise CampaignContractError("sample has no /proc backing observations")
    required = {
        "elapsed_seconds",
        "rss_kib",
        "pss_kib",
        "anonymous_kib",
        "anon_hugepages_kib",
    }
    for sample in samples:
        if not required.issubset(sample):
            raise CampaignContractError("/proc sample is incomplete")
        for field in required - {"elapsed_seconds"}:
            _nonnegative_integer(sample, field)
    return {
        "sample_count": len(samples),
        "peak_rss_kib": max(int(row["rss_kib"]) for row in samples),
        "peak_pss_kib": max(int(row["pss_kib"]) for row in samples),
        "peak_anonymous_kib": max(int(row["anonymous_kib"]) for row in samples),
        "peak_anon_hugepages_kib": max(
            int(row["anon_hugepages_kib"]) for row in samples
        ),
        "anon_hugepages_observed": any(
            int(row["anon_hugepages_kib"]) > 0 for row in samples
        ),
    }


def validate_thp_pair_backing(
    ordinary_samples: Sequence[Mapping[str, Any]],
    thp_samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if len(ordinary_samples) != len(thp_samples):
        raise CampaignContractError("THP comparison has unmatched measured samples")
    if not ordinary_samples:
        raise CampaignContractError("THP comparison has no measured pairs")
    ordinary = summarize_proc_samples(ordinary_samples)
    thp = summarize_proc_samples(thp_samples)
    pairs: list[dict[str, Any]] = []
    for index, (ordinary_sample, thp_sample) in enumerate(
        zip(ordinary_samples, thp_samples, strict=True)
    ):
        ordinary_backing = _nonnegative_integer(
            ordinary_sample, "anon_hugepages_kib"
        )
        thp_backing = _nonnegative_integer(thp_sample, "anon_hugepages_kib")
        pair_reasons: list[str] = []
        if ordinary_backing != 0:
            pair_reasons.append("ordinary prior sample has resident AnonHugePages")
        if thp_backing <= 0:
            pair_reasons.append(
                "selective-THP prior sample has no resident AnonHugePages"
            )
        pairs.append(
            {
                "pair_index": index,
                "eligible": not pair_reasons,
                "ordinary_anon_hugepages_kib": ordinary_backing,
                "selective_thp_anon_hugepages_kib": thp_backing,
                "reasons": pair_reasons,
            }
        )
    excluded = [pair for pair in pairs if not pair["eligible"]]
    reasons = [
        f"pair {pair['pair_index']}: {reason}"
        for pair in excluded
        for reason in pair["reasons"]
    ]
    return {
        "passed": not excluded,
        "eligible_pair_count": len(pairs) - len(excluded),
        "excluded_pair_count": len(excluded),
        "pair_eligibility": pairs,
        "ordinary": ordinary,
        "selective_thp": thp,
        "reasons": reasons,
        "claim_boundary": (
            "every measured pair is checked independently; zero-backed THP "
            "samples are excluded and make the aggregate causal gate fail"
        ),
    }


def validate_runtime_evidence(
    arm: ArmSpec,
    stats: Mapping[str, Any],
    sites: Sequence[Mapping[str, Any]],
) -> None:
    if stats.get("policy") != arm.expected_policy:
        raise CampaignContractError(f"runtime policy mismatch for {arm.name}")
    if stats.get("adaptive_observation_recording") is not True:
        raise CampaignContractError("runtime site observation recording is disabled")
    if stats.get("adaptive_force_track_all") is not arm.force_track:
        raise CampaignContractError("force-track state does not match arm")
    for field in (
        "mapping_failures",
        "adaptive_trailer_corruptions",
        "adaptive_observation_table_bypasses",
    ):
        if _nonnegative_integer(stats, field) != 0:
            raise CampaignContractError(f"runtime fail-closed counter is nonzero: {field}")
    if arm.backing == "ordinary":
        for field in (
            "thp_extent_mappings",
            "thp_advice_attempts",
            "thp_advice_successes",
            "thp_advice_errors",
        ):
            if _nonnegative_integer(stats, field) != 0:
                raise CampaignContractError(f"ordinary arm attempted THP: {field}")
    if arm.force_track:
        for field in (
            "adaptive_force_track_all_guard_bypasses",
            "adaptive_cold_bypassed_allocations",
            "adaptive_short_bypassed_allocations",
            "adaptive_site_table_bypasses",
        ):
            if _nonnegative_integer(stats, field) != 0:
                raise CampaignContractError(f"screening coverage is incomplete: {field}")
    runtime_lifetime.validate_runtime_site_rows(list(map(dict, sites)), dict(stats))


def runtime_instrumentation_rust() -> str:
    """Generate the shared init/fini telemetry used by every Stage-A binary."""
    initializer = f'''
#[inline(never)]
extern "C" fn unialloc_lifetime_experiment_initialize() {{
    let arm = std::env::var("{RUNTIME_ARM_ENV}")
        .expect("missing UniAlloc lifetime experiment arm");
    let (policy, force_track) = match arm.as_str() {{
        "default" => (unialloc::LifetimeHugepagePolicy::Disabled, false),
        "adaptive-ordinary-all-unknown" => (
            unialloc::LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary,
            false,
        ),
        "adaptive-ordinary-compiler-prior" => (
            unialloc::LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary,
            false,
        ),
        "adaptive-selective-thp-all-unknown" => (
            unialloc::LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
            false,
        ),
        "adaptive-selective-thp-compiler-prior" => (
            unialloc::LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
            false,
        ),
        "force-track-all-unknown-diagnostic" => (
            unialloc::LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary,
            true,
        ),
        "force-track-compiler-prior-diagnostic" => (
            unialloc::LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary,
            true,
        ),
        _ => panic!("unknown UniAlloc lifetime experiment arm: {{arm}}"),
    }};
    drop(arm);
    assert!(unialloc::lifetime_hugepage_stats_reset());
    assert!(unialloc::lifetime_hugepage_adaptive_site_recording_enable());
    if force_track {{
        assert!(unialloc::lifetime_hugepage_adaptive_site_force_track_all_enable(
            {FORCE_TRACK_MAXIMUM_ALLOCATIONS},
            {FORCE_TRACK_MAXIMUM_REQUESTED_BYTES},
        ));
    }}
    assert!(unialloc::lifetime_hugepage_configure(policy));
}}

#[used]
#[cfg_attr(target_family = "unix", unsafe(link_section = ".init_array"))]
static UNIALLOC_LIFETIME_EXPERIMENT_INITIALIZER: extern "C" fn() =
    unialloc_lifetime_experiment_initialize;
'''
    fragmentation = f'''
    eprintln!(
        r#"{FRAGMENTATION_PREFIX}{{{{\"extent_bytes\":{{}},\"current_extents\":{{}},\"peak_extents\":{{}},\"current_ordinary_extents\":{{}},\"peak_ordinary_extents\":{{}},\"current_thp_extents\":{{}},\"peak_thp_extents\":{{}},\"live_objects\":{{}},\"live_slot_bytes\":{{}},\"retained_bytes\":{{}},\"reusable_unassigned_region_bytes\":{{}},\"cohort_pinned_unassigned_region_bytes\":{{}},\"assigned_region_slack_bytes\":{{}},\"retained_slack_bytes\":{{}},\"stranded_bytes\":{{}}}}}}"#,
        lifetime.extent_bytes,
        lifetime.current_extents,
        lifetime.peak_extents,
        lifetime.current_ordinary_extents,
        lifetime.peak_ordinary_extents,
        lifetime.current_thp_extents,
        lifetime.peak_thp_extents,
        lifetime.live_objects,
        lifetime.live_slot_bytes,
        lifetime.retained_bytes,
        lifetime.reusable_unassigned_region_bytes,
        lifetime.cohort_pinned_unassigned_region_bytes,
        lifetime.assigned_region_slack_bytes,
        lifetime.retained_slack_bytes,
        lifetime.stranded_bytes,
    );
    eprintln!(
        r#"{MECHANISM_PREFIX}{{{{\"ordinary_extent_mappings\":{{}},\"thp_extent_mappings\":{{}},\"thp_candidate_extent_mappings\":{{}},\"thp_advice_attempts\":{{}},\"thp_advice_successes\":{{}},\"thp_advice_errors\":{{}},\"thp_collapse_eligible_extents\":{{}},\"thp_collapse_low_occupancy_skips\":{{}},\"thp_collapse_attempts\":{{}},\"thp_collapse_successes\":{{}},\"thp_collapse_errors\":{{}},\"current_thp_collapse_confirmed_extents\":{{}},\"peak_thp_collapse_confirmed_extents\":{{}},\"extent_unmaps\":{{}},\"extent_unmap_failures\":{{}},\"current_extents\":{{}},\"peak_extents\":{{}},\"empty_extent_retention_capacity\":{{}},\"current_retained_empty_extents\":{{}},\"peak_retained_empty_extents\":{{}},\"current_retained_empty_bytes\":{{}},\"peak_retained_empty_bytes\":{{}},\"retained_empty_extent_insertions\":{{}},\"retained_empty_extent_reuse_hits\":{{}},\"retained_empty_extent_evictions\":{{}},\"retained_empty_extent_trims\":{{}},\"all_mappings_released\":{{}}}}}}"#,
        lifetime.ordinary_extent_mappings,
        lifetime.thp_extent_mappings,
        lifetime.thp_candidate_extent_mappings,
        lifetime.thp_advice_attempts,
        lifetime.thp_advice_successes,
        lifetime.thp_advice_errors,
        lifetime.thp_collapse_eligible_extents,
        lifetime.thp_collapse_low_occupancy_skips,
        lifetime.thp_collapse_attempts,
        lifetime.thp_collapse_successes,
        lifetime.thp_collapse_errors,
        lifetime.current_thp_collapse_confirmed_extents,
        lifetime.peak_thp_collapse_confirmed_extents,
        lifetime.extent_unmaps,
        lifetime.extent_unmap_failures,
        lifetime.current_extents,
        lifetime.peak_extents,
        lifetime.empty_extent_retention_capacity,
        lifetime.current_retained_empty_extents,
        lifetime.peak_retained_empty_extents,
        lifetime.current_retained_empty_bytes,
        lifetime.peak_retained_empty_bytes,
        lifetime.retained_empty_extent_insertions,
        lifetime.retained_empty_extent_reuse_hits,
        lifetime.retained_empty_extent_evictions,
        lifetime.retained_empty_extent_trims,
        lifetime.all_mappings_released,
    );
'''
    reporter = f'''
#[inline(never)]
extern "C" fn unialloc_lifetime_experiment_report() {{
{runtime_lifetime.RUNTIME_CAPTURE_RUST}
{runtime_lifetime.RUNTIME_REPORT_RUST}
{fragmentation}
}}

#[used]
#[cfg_attr(target_family = "unix", unsafe(link_section = ".fini_array"))]
static UNIALLOC_LIFETIME_EXPERIMENT_REPORTER: extern "C" fn() =
    unialloc_lifetime_experiment_report;
'''
    return (
        runtime_lifetime.RUNTIME_SITE_BUFFER_RUST
        + initializer
        + reporter
    )


def allocator_instrumentation_source() -> str:
    return (
        "\n#[global_allocator]\n"
        "static UNIALLOC_LIFETIME_EXPERIMENT_ALLOCATOR: unialloc::UniAlloc = "
        "unialloc::UniAlloc;\n"
        + runtime_instrumentation_rust()
    )


def amplified_redb_source() -> str:
    """Return the pinned redb driver with fixed repeats and runtime evidence."""
    source = redb_actix.REDB_RUNNER_SOURCE
    allocator_marker = (
        "#[global_allocator]\n"
        "static ALLOCATOR: unialloc::UniAlloc = unialloc::UniAlloc;\n"
    )
    if allocator_marker not in source:
        raise CampaignContractError("pinned redb allocator marker changed")
    source = source.replace(
        allocator_marker,
        allocator_marker + runtime_instrumentation_rust(),
        1,
    )
    table_len_marker = r'''fn table_len(database: &Database) -> u64 {
    let transaction = database.begin_read().expect("begin read");
    let table = transaction.open_table(TABLE).expect("open table");
    table.len().expect("read table length")
}
'''
    table_digest = table_len_marker + r'''

fn table_digest(database: &Database) -> u64 {
    let transaction = database.begin_read().expect("begin digest read");
    let table = transaction.open_table(TABLE).expect("open digest table");
    let mut digest = 0xcbf29ce484222325u64;
    for entry in table.iter().expect("iterate digest table") {
        let (key, value) = entry.expect("read digest entry");
        for byte in key.value().to_le_bytes() {
            digest = (digest ^ u64::from(byte)).wrapping_mul(0x100000001b3);
        }
        for &byte in value.value() {
            digest = (digest ^ u64::from(byte)).wrapping_mul(0x100000001b3);
        }
    }
    digest
}
'''
    if table_len_marker not in source:
        raise CampaignContractError("pinned redb table_len changed")
    source = source.replace(table_len_marker, table_digest, 1)
    source = source.replace(
        '    assert_eq!(arguments.len(), 3, "usage: runner HARNESS WORK_DIR");\n',
        '    assert_eq!(arguments.len(), 4, "usage: runner HARNESS WORK_DIR WORK_UNITS");\n',
        1,
    )
    source = source.replace(
        "    let work_dir = Path::new(&arguments[2]);\n",
        "    let work_dir = Path::new(&arguments[2]);\n"
        "    let work_units: u64 = arguments[3].parse()\n"
        '        .expect("parse fixed work-unit count");\n'
        '    assert!(work_units > 0, "fixed work-unit count must be positive");\n',
        1,
    )
    original_work = r'''    let operations = match harness {
        "bulk_small" => bulk_small(&database),
        "transaction_churn" => transaction_churn(&database),
        "delete_reinsert" => delete_reinsert(&database),
        "large_values" => large_values(&database),
        other => panic!("unknown harness: {other}"),
    };
    let elapsed = started.elapsed().as_secs_f64();
    drop(database);
'''
    repeated_work = r'''    let mut operations = 0u64;
    for _ in 0..work_units {
        operations += match harness {
            "bulk_small" => bulk_small(&database),
            "transaction_churn" => transaction_churn(&database),
            "delete_reinsert" => delete_reinsert(&database),
            "large_values" => large_values(&database),
            other => panic!("unknown harness: {other}"),
        };
    }
    let elapsed = started.elapsed().as_secs_f64();
    let output_digest = table_digest(&database);
    drop(database);
'''
    if original_work not in source:
        raise CampaignContractError("pinned redb work dispatch changed")
    source = source.replace(original_work, repeated_work, 1)
    source = source.replace(
        '        "UNIALLOC_REDB_ACTIX_RESULT={{\\"harness\\":\\"{}\\",\\"operations\\":{},\\"elapsed_seconds\\":{:.9},\\"correctness\\":true}}",\n'
        "        harness, operations, elapsed\n",
        '        "UNIALLOC_REDB_ACTIX_RESULT={{\\"harness\\":\\"{}\\",\\"work_units\\":{},\\"operations\\":{},\\"elapsed_seconds\\":{:.9},\\"output_digest\\":\\"{:016x}\\",\\"correctness\\":true}}",\n'
        "        harness, work_units, operations, elapsed, output_digest\n",
        1,
    )
    if "fixed work-unit count" not in source or "output_digest" not in source:
        raise CampaignContractError("redb fixed-work rewrite did not apply")
    return source


def oxipng_command(
    *, binary: Path, input_paths: Sequence[Path], output_dir: Path
) -> list[str]:
    if not binary.is_file():
        raise CampaignContractError(f"Oxipng binary is missing: {binary}")
    if not input_paths:
        raise CampaignContractError("Oxipng fixed-work input set is empty")
    for path in input_paths:
        if not path.is_file() or sha256_file(path) != OXIPNG_INPUT_SHA256:
            raise CampaignContractError(f"Oxipng input identity mismatch: {path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    return [
        str(binary.resolve()),
        "--opt",
        "2",
        "--threads",
        "1",
        "--force",
        "--quiet",
        "--dir",
        str(output_dir.resolve()),
        *(str(path.resolve()) for path in input_paths),
    ]


def materialize_oxipng_inputs(
    source: Path, destination: Path, work_units: int
) -> list[Path]:
    if work_units <= 0:
        raise CampaignContractError("Oxipng work units must be positive")
    if not source.is_file() or sha256_file(source) != OXIPNG_INPUT_SHA256:
        raise CampaignContractError("pinned Oxipng source input mismatch")
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    paths: list[Path] = []
    for index in range(work_units):
        path = destination / f"issue-141-{index:06d}.png"
        shutil.copyfile(source, path)
        paths.append(path)
    return paths


def redb_command(
    *, binary: Path, work_dir: Path, work_units: int, harness_id: str = "delete_reinsert"
) -> list[str]:
    if not binary.is_file():
        raise CampaignContractError(f"redb driver is missing: {binary}")
    if work_units <= 0:
        raise CampaignContractError("redb work units must be positive")
    if harness_id not in {harness.id for harness in redb_actix.TARGETS["redb"].harnesses}:
        raise CampaignContractError(f"unknown redb harness: {harness_id}")
    work_dir.mkdir(parents=True, exist_ok=True)
    return [str(binary.resolve()), harness_id, str(work_dir.resolve()), str(work_units)]


def amplified_polars_source() -> str:
    """Return the pinned generated Polars driver with a fixed in-process loop."""
    source = psr.POLARS_SOURCE
    source = source.replace(
        "fn run(harness: &str, csv_path: Option<&str>) -> PolarsResult<DataFrame> {",
        "fn run(\n"
        "    harness: &str,\n"
        "    csv_path: Option<&str>,\n"
        "    retained_frame: Option<&DataFrame>,\n"
        ") -> PolarsResult<DataFrame> {",
        1,
    )
    source = source.replace(
        "    let frame = input_frame()?;\n",
        "    let frame = match retained_frame {\n"
        "        Some(frame) => frame.clone(),\n"
        "        None => input_frame()?,\n"
        "    };\n",
        1,
    )
    original_main = r'''fn main() -> PolarsResult<()> {
    let mut args = std::env::args().skip(1);
    let harness = args.next().expect("missing harness id");
    let csv_path = args.next();
    let started = Instant::now();
    let output = run(&harness, csv_path.as_deref())?;
    let seconds = started.elapsed().as_secs_f64();
    println!(
        "{{\"harness_id\":\"{}\",\"operation_seconds\":{:.9},\"rows\":{},\"columns\":{},\"estimated_size\":{},\"fingerprint\":\"{:016x}\"}}",
        harness,
        seconds,
        output.height(),
        output.width(),
        output.estimated_size(),
        fingerprint(&output),
    );
    Ok(())
}
'''
    replacement_main = r'''fn main() -> PolarsResult<()> {
    let mut args = std::env::args().skip(1);
    let harness = args.next().expect("missing harness id");
    let csv_path = if harness == "csv_scan" {
        Some(args.next().expect("csv_scan requires an input path"))
    } else {
        None
    };
    let work_units: usize = args
        .next()
        .expect("missing fixed work-unit count")
        .parse()
        .expect("invalid fixed work-unit count");
    assert!(work_units > 0, "fixed work-unit count must be positive");
    assert!(args.next().is_none(), "unexpected driver arguments");
    let retained_frame = if harness == "csv_scan" {
        None
    } else {
        Some(input_frame()?)
    };
    let started = Instant::now();
    let mut combined = DefaultHasher::new();
    let mut final_rows = 0usize;
    let mut final_columns = 0usize;
    let mut final_size = 0usize;
    for work_index in 0..work_units {
        let output = run(
            &harness,
            csv_path.as_deref(),
            retained_frame.as_ref(),
        )?;
        work_index.hash(&mut combined);
        fingerprint(&output).hash(&mut combined);
        final_rows = output.height();
        final_columns = output.width();
        final_size = output.estimated_size();
    }
    let seconds = started.elapsed().as_secs_f64();
    println!(
        "{{\"harness_id\":\"{}\",\"work_units\":{},\"operation_seconds\":{:.9},\"rows\":{},\"columns\":{},\"estimated_size\":{},\"fingerprint\":\"{:016x}\"}}",
        harness,
        work_units,
        seconds,
        final_rows,
        final_columns,
        final_size,
        combined.finish(),
    );
    Ok(())
}
'''
    if original_main not in source:
        raise CampaignContractError("pinned Polars generated-driver main changed")
    source = source.replace(original_main, replacement_main, 1)
    if "retained_frame: Option<&DataFrame>" not in source:
        raise CampaignContractError("Polars retained-frame rewrite did not apply")
    return source


def criterion_screening_command(
    *,
    binary: Path,
    selector: str,
    measurement_seconds: float,
    warm_up_seconds: float = 1.0,
) -> list[str]:
    if not binary.is_file():
        raise CampaignContractError(f"Criterion binary is missing: {binary}")
    if not (
        DEFAULT_SCREEN_MIN_SECONDS
        <= measurement_seconds
        <= DEFAULT_SCREEN_MAX_SECONDS
    ):
        raise CampaignContractError("Criterion screening time is outside 20--60 seconds")
    if not math.isfinite(warm_up_seconds) or not 0.0 < warm_up_seconds <= 20.0:
        raise CampaignContractError("Criterion warm-up time is outside 0--20 seconds")
    return [
        str(binary.resolve()),
        "--bench",
        selector,
        "--warm-up-time",
        f"{warm_up_seconds:.3f}",
        "--measurement-time",
        f"{measurement_seconds:.3f}",
        "--sample-size",
        "10",
        "--noplot",
    ]


def screening_command(
    target_id: str,
    *,
    binary: Path,
    work_dir: Path,
    work_units: int | None = None,
    measurement_seconds: float = 30.0,
    criterion_warm_up_seconds: float = 1.0,
    input_path: Path | None = None,
) -> list[str]:
    """Build one same-process Stage-A command for every pinned target."""
    target = TARGETS[target_id]
    if not target.screening_ready:
        raise AdapterBlocked(f"screening adapter is blocked: {target_id}")
    if target_id == "oxipng":
        if input_path is None or work_units is None:
            raise CampaignContractError("Oxipng screening needs input and work units")
        inputs = materialize_oxipng_inputs(input_path, work_dir / "inputs", work_units)
        return oxipng_command(binary=binary, input_paths=inputs, output_dir=work_dir / "out")
    if target_id == "redb":
        if work_units is None:
            raise CampaignContractError("redb screening needs fixed work units")
        return redb_command(binary=binary, work_dir=work_dir, work_units=work_units)
    if target_id == "polars":
        if not binary.is_file() or work_units is None or work_units <= 0:
            raise CampaignContractError("Polars screening artifact/work is invalid")
        work_dir.mkdir(parents=True, exist_ok=True)
        return [str(binary.resolve()), target.harness_id, str(work_units)]
    if target_id in {"swc", "rustpython"}:
        spec = psr.TARGET_SPECS[target_id]
        return criterion_screening_command(
            binary=binary,
            selector=spec.harness_filters[target.harness_id],
            measurement_seconds=measurement_seconds,
            warm_up_seconds=criterion_warm_up_seconds,
        )
    if target_id == "actix_web":
        _package, _bench, selector, _source = redb_actix.ACTIX_BENCHES[
            target.harness_id
        ]
        return criterion_screening_command(
            binary=binary,
            selector=selector,
            measurement_seconds=measurement_seconds,
            warm_up_seconds=criterion_warm_up_seconds,
        )
    raise CampaignContractError(f"unknown target: {target_id}")


def planned_fixed_work_command(
    target_id: str,
    *,
    binary: Path,
    work_dir: Path,
    work_units: int,
    input_path: Path | None = None,
    allow_blocked_adapter: bool = False,
) -> list[str]:
    target = TARGETS[target_id]
    if not target.performance_ready and not allow_blocked_adapter:
        raise AdapterBlocked(
            target.performance_blocker or f"adapter blocked: {target_id}"
        )
    if target_id == "oxipng":
        if input_path is None:
            raise CampaignContractError("Oxipng requires the pinned input")
        inputs = materialize_oxipng_inputs(input_path, work_dir / "inputs", work_units)
        return oxipng_command(binary=binary, input_paths=inputs, output_dir=work_dir / "out")
    if target_id == "redb":
        return redb_command(binary=binary, work_dir=work_dir, work_units=work_units)
    if not binary.is_file() or work_units <= 0:
        raise CampaignContractError("fixed-work adapter binary or work count is invalid")
    # Standardized CLI expected from the pending generated adapters.  Retaining
    # this shape in the manifest lets build integration remain target-agnostic.
    return [
        str(binary.resolve()),
        "--unialloc-harness",
        target.harness_id,
        "--unialloc-fixed-work-units",
        str(work_units),
        "--unialloc-output-digest",
        str((work_dir / "output-digest.json").resolve()),
    ]


def _run_checked(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: int = 900,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        [str(value) for value in command],
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise CampaignContractError(
            f"command failed ({result.returncode}): {' '.join(map(str, command))}\n"
            + result.stderr.decode(errors="replace")[-8000:]
        )
    return result


def _git_text(checkout: Path, *arguments: str) -> str:
    return _run_checked(["git", *arguments], cwd=checkout).stdout.decode().strip()


def ensure_pinned_checkout(target_id: str, checkout_root: Path) -> dict[str, Any]:
    """Materialize one clean exact source pin without mutating a dirty checkout."""
    target = TARGETS[target_id]
    checkout = checkout_root / CHECKOUT_NAMES[target_id]
    if not (checkout / ".git").exists():
        checkout_root.mkdir(parents=True, exist_ok=True)
        _run_checked(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                SOURCE_REPOSITORIES[target_id],
                checkout,
            ],
            cwd=checkout_root,
            timeout=1800,
        )
    status = _git_text(checkout, "status", "--short")
    if status:
        raise CampaignContractError(
            f"refusing to alter dirty {target_id} checkout:\n{status}"
        )
    head_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    head = head_result.stdout.decode().strip() if head_result.returncode == 0 else ""
    if head != target.source_commit:
        _run_checked(
            ["git", "fetch", "--depth=1", "origin", target.source_commit],
            cwd=checkout,
            timeout=1800,
        )
        _run_checked(
            ["git", "checkout", "--detach", target.source_commit],
            cwd=checkout,
            timeout=120,
        )
        head = _git_text(checkout, "rev-parse", "HEAD")
    if head != target.source_commit or _git_text(checkout, "status", "--short"):
        raise CampaignContractError(f"{target_id} exact source pin verification failed")
    return {
        "target_id": target_id,
        "repository": SOURCE_REPOSITORIES[target_id],
        "source_ref": target.source_ref,
        "source_commit": head,
        "source_tree": _git_text(checkout, "rev-parse", "HEAD^{tree}"),
        "checkout": str(checkout.resolve()),
        "remote": _git_text(checkout, "remote", "get-url", "origin"),
        "cargo_lock_sha256": (
            sha256_file(checkout / "Cargo.lock")
            if (checkout / "Cargo.lock").is_file()
            else None
        ),
    }


def materialize_pinned_oxipng_input(checkout: Path, destination: Path) -> dict[str, Any]:
    commit = collections_oxipng.LEGACY_OXIPNG_INPUT_COMMIT
    source_path = collections_oxipng.LEGACY_OXIPNG_INPUT_PATH
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{source_path}"],
        cwd=checkout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if exists.returncode != 0:
        _run_checked(
            ["git", "fetch", "--depth=1", "origin", commit],
            cwd=checkout,
            timeout=1800,
        )
    content = _run_checked(
        ["git", "show", f"{commit}:{source_path}"], cwd=checkout
    ).stdout
    digest = hashlib.sha256(content).hexdigest()
    if digest != OXIPNG_INPUT_SHA256:
        raise CampaignContractError("materialized Oxipng fixture digest changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return {
        "path": str(destination.resolve()),
        "sha256": digest,
        "size_bytes": len(content),
        "source_commit": commit,
        "source_path": source_path,
    }


def _copy_checkout(checkout: Path, destination: Path) -> None:
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(
        checkout,
        destination,
        ignore=shutil.ignore_patterns(".git", "target", "__pycache__"),
    )


def _append_instrumentation(path: Path, *, remove: str | None = None) -> None:
    text_value = path.read_text(encoding="utf-8")
    if "UNIALLOC_LIFETIME_EXPERIMENT_INITIALIZER" in text_value:
        raise CampaignContractError(f"duplicate runtime instrumentation: {path}")
    if remove is not None:
        if remove not in text_value:
            raise CampaignContractError(f"expected allocator source marker is absent: {path}")
        text_value = text_value.replace(remove, "", 1)
    instrumentation = allocator_instrumentation_source().strip()
    path.write_text(
        text_value.rstrip() + "\n\n" + instrumentation + "\n",
        encoding="utf-8",
    )


def _stage_a_dependency(snapshot: Mapping[str, Any]) -> str:
    return matrix.cargo_path_dependency(
        Path(str(snapshot["path"])) / "unialloc",
        ("lifetime_hugepage",),
    )


def _locked_package_version(lock_path: Path, package: str) -> str:
    if not lock_path.is_file():
        raise CampaignContractError(f"Cargo lock is missing: {lock_path}")
    try:
        document = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CampaignContractError(f"Cargo lock is invalid: {lock_path}") from error
    versions = {
        str(row.get("version"))
        for row in document.get("package", [])
        if isinstance(row, dict) and row.get("name") == package
    }
    if len(versions) != 1:
        raise CampaignContractError(
            f"target Cargo lock must resolve exactly one {package} version; "
            f"observed {sorted(versions)}"
        )
    version = next(iter(versions))
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version) is None:
        raise CampaignContractError(
            f"target Cargo lock has invalid {package} version: {version}"
        )
    return version


def _rewrite_snapshot_dependency_requirements(
    manifest_text: str,
    rules: Sequence[Mapping[str, Any]],
    resolved_versions: Mapping[str, str],
) -> tuple[str, list[dict[str, Any]]]:
    counts = [0 for _rule in rules]
    records: list[dict[str, Any]] = []
    section = ""
    output: list[str] = []
    for line_number, original_line in enumerate(manifest_text.splitlines(), start=1):
        line = original_line
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
        for index, rule in enumerate(rules):
            package = str(rule["package"])
            allowed_sections = tuple(str(value) for value in rule["sections"])
            if section not in allowed_sections or re.match(
                rf"^\s*{re.escape(package)}\s*=", line
            ) is None:
                continue
            old_requirement = f'"={rule["old_version"]}"'
            new_requirement = f'"={resolved_versions[package]}"'
            if line.count(old_requirement) != 1:
                raise CampaignContractError(
                    f"snapshot {package} requirement drifted in [{section}]"
                )
            before = line
            line = line.replace(old_requirement, new_requirement, 1)
            counts[index] += 1
            records.append(
                {
                    "package": package,
                    "section": section,
                    "line_number": line_number,
                    "old_requirement": f'={rule["old_version"]}',
                    "new_requirement": f'={resolved_versions[package]}',
                    "target_resolved_version": resolved_versions[package],
                    "before": before.strip(),
                    "after": line.strip(),
                }
            )
        output.append(line)
    for index, rule in enumerate(rules):
        expected = int(rule["expected_occurrences"])
        if counts[index] != expected:
            raise CampaignContractError(
                f"snapshot {rule['package']} rewrite count changed: "
                f"expected {expected}, observed {counts[index]}"
            )
    suffix = "\n" if manifest_text.endswith("\n") else ""
    return "\n".join(output) + suffix, records


def _compatible_snapshot_mapping(
    baseline: Mapping[str, Any], root: Path, provenance: Mapping[str, Any]
) -> dict[str, Any]:
    compatible = dict(baseline)
    compatible.update(
        {
            "path": str(root.resolve()),
            "pass_source": str(
                (
                    root
                    / "tools"
                    / "unialloc-rustc-pass"
                    / "unialloc-rustc-mir-rewrite-dry-run.rs"
                ).resolve()
            ),
            "campaign_snapshot_sha256": provenance[
                "target_campaign_snapshot_sha256"
            ],
            "base_campaign_snapshot_sha256": baseline.get(
                "campaign_snapshot_sha256"
            ),
            "dependency_compatibility": dict(provenance),
        }
    )
    return compatible


def prepare_target_compatible_allocator_snapshot(
    target_id: str,
    *,
    checkout: Path,
    raw_dir: Path,
    baseline: Mapping[str, Any],
    timeout: int,
) -> dict[str, Any]:
    """Rewrite dependency pins only inside a copied, target-specific snapshot."""
    rules = TARGET_SNAPSHOT_DEPENDENCY_REWRITES.get(target_id, ())
    baseline_root = Path(str(baseline["path"])).resolve()
    baseline_manifest = baseline_root / "unialloc" / "Cargo.toml"
    baseline_lock = baseline_root / "Cargo.lock"
    if not baseline_manifest.is_file() or not baseline_lock.is_file():
        raise CampaignContractError("frozen allocator snapshot lacks Cargo inputs")
    baseline_manifest_sha = sha256_file(baseline_manifest)
    baseline_lock_sha = sha256_file(baseline_lock)
    if not rules:
        provenance = {
            "source": "unialloc-target-snapshot-dependency-compatibility-v1",
            "success": True,
            "status": "not-required",
            "target_id": target_id,
            "rewrites": [],
            "base_campaign_snapshot_sha256": baseline.get(
                "campaign_snapshot_sha256"
            ),
            "pre_rewrite_manifest_sha256": baseline_manifest_sha,
            "post_rewrite_manifest_sha256": baseline_manifest_sha,
            "pre_rewrite_lock_sha256": baseline_lock_sha,
            "post_rewrite_lock_sha256": baseline_lock_sha,
            "target_campaign_snapshot_sha256": baseline.get(
                "campaign_snapshot_sha256"
            ),
        }
        provenance["provenance_digest"] = canonical_json_sha256(provenance)
        compatible = dict(baseline)
        compatible["dependency_compatibility"] = provenance
        return compatible

    target_lock = checkout / "Cargo.lock"
    resolved_versions = {
        str(rule["package"]): _locked_package_version(
            target_lock, str(rule["package"])
        )
        for rule in rules
    }
    input_identity = {
        "target_id": target_id,
        "base_campaign_snapshot_sha256": baseline.get("campaign_snapshot_sha256"),
        "base_implementation_sha256": baseline.get(
            "unialloc_implementation_sha256"
        ),
        "baseline_manifest_sha256": baseline_manifest_sha,
        "baseline_lock_sha256": baseline_lock_sha,
        "target_lock_sha256": sha256_file(target_lock),
        "rules": [dict(rule) for rule in rules],
        "resolved_versions": resolved_versions,
    }
    input_digest = canonical_json_sha256(input_identity)
    compatibility_root = raw_dir / "allocator-compatibility" / target_id
    snapshot_root = compatibility_root / "snapshot"
    record_path = compatibility_root / "provenance.json"
    if record_path.is_file() and snapshot_root.is_dir():
        cached = json.loads(record_path.read_text(encoding="utf-8"))
        cached_manifest = snapshot_root / "unialloc" / "Cargo.toml"
        cached_lock = snapshot_root / "Cargo.lock"
        cached_payload = dict(cached)
        cached_provenance_digest = cached_payload.pop("provenance_digest", None)
        expected_target_digest = canonical_json_sha256(
            {
                "base_campaign_snapshot_sha256": baseline.get(
                    "campaign_snapshot_sha256"
                ),
                "manifest_sha256": cached.get("post_rewrite_manifest_sha256"),
                "lock_sha256": cached.get("post_rewrite_lock_sha256"),
                "input_digest": input_digest,
            }
        )
        if (
            cached.get("success") is True
            and cached.get("input_digest") == input_digest
            and cached_provenance_digest == canonical_json_sha256(cached_payload)
            and cached.get("target_campaign_snapshot_sha256")
            == expected_target_digest
            and cached_manifest.is_file()
            and cached_lock.is_file()
            and cached.get("post_rewrite_manifest_sha256")
            == sha256_file(cached_manifest)
            and cached.get("post_rewrite_lock_sha256") == sha256_file(cached_lock)
        ):
            return _compatible_snapshot_mapping(baseline, snapshot_root, cached)
    shutil.rmtree(compatibility_root, ignore_errors=True)
    compatibility_root.mkdir(parents=True)
    shutil.copytree(baseline_root, snapshot_root)
    copied_manifest = snapshot_root / "unialloc" / "Cargo.toml"
    copied_lock = snapshot_root / "Cargo.lock"
    if (
        sha256_file(copied_manifest) != baseline_manifest_sha
        or sha256_file(copied_lock) != baseline_lock_sha
    ):
        raise CampaignContractError("target snapshot copy changed before rewriting")
    rewritten, rewrite_records = _rewrite_snapshot_dependency_requirements(
        copied_manifest.read_text(encoding="utf-8"), rules, resolved_versions
    )
    copied_manifest.write_text(rewritten, encoding="utf-8")
    cargo_commands: list[dict[str, Any]] = []
    cargo_env = os.environ.copy()
    cargo_env["CARGO_NET_OFFLINE"] = "true"
    for package, version in sorted(resolved_versions.items()):
        command = [
            "cargo",
            f"+{TOOLCHAIN}",
            "update",
            "--offline",
            "--manifest-path",
            str((snapshot_root / "Cargo.toml").resolve()),
            "-p",
            package,
            "--precise",
            version,
        ]
        result = matrix.execute(
            command, cwd=snapshot_root, env=cargo_env, timeout=min(timeout, 600)
        )
        stdout_path = compatibility_root / f"cargo-update-{package}.stdout"
        stderr_path = compatibility_root / f"cargo-update-{package}.stderr"
        stdout_path.write_bytes(result["stdout"])
        stderr_path.write_bytes(result["stderr"])
        cargo_commands.append(
            {
                "command": result["command"],
                "exit_code": result["exit_code"],
                "timed_out": result["timed_out"],
                "stdout_sha256": sha256_file(stdout_path),
                "stderr_sha256": sha256_file(stderr_path),
            }
        )
        if result["exit_code"] != 0 or result["timed_out"]:
            raise BuildBlocked(
                f"{target_id} compatibility lock update failed for {package}: "
                + result["stderr"].decode(errors="replace")[-8000:]
            )
    metadata_command = [
        "cargo",
        f"+{TOOLCHAIN}",
        "metadata",
        "--locked",
        "--offline",
        "--format-version",
        "1",
        "--no-deps",
        "--manifest-path",
        str((snapshot_root / "Cargo.toml").resolve()),
    ]
    metadata = matrix.execute(
        metadata_command,
        cwd=snapshot_root,
        env=cargo_env,
        timeout=min(timeout, 600),
    )
    (compatibility_root / "cargo-metadata.stdout").write_bytes(metadata["stdout"])
    (compatibility_root / "cargo-metadata.stderr").write_bytes(metadata["stderr"])
    if metadata["exit_code"] != 0 or metadata["timed_out"]:
        raise BuildBlocked(
            f"{target_id} compatibility snapshot metadata failed: "
            + metadata["stderr"].decode(errors="replace")[-8000:]
        )
    post_resolved = {
        package: _locked_package_version(copied_lock, package)
        for package in resolved_versions
    }
    if post_resolved != resolved_versions:
        raise CampaignContractError(
            f"{target_id} compatibility lock does not match target resolution"
        )
    post_manifest_sha = sha256_file(copied_manifest)
    post_lock_sha = sha256_file(copied_lock)
    target_campaign_digest = canonical_json_sha256(
        {
            "base_campaign_snapshot_sha256": baseline.get(
                "campaign_snapshot_sha256"
            ),
            "manifest_sha256": post_manifest_sha,
            "lock_sha256": post_lock_sha,
            "input_digest": input_digest,
        }
    )
    provenance = {
        "source": "unialloc-target-snapshot-dependency-compatibility-v1",
        "success": True,
        "status": "rewritten-target-snapshot",
        "target_id": target_id,
        "input_identity": input_identity,
        "input_digest": input_digest,
        "snapshot_path": str(snapshot_root.resolve()),
        "production_root": str(ROOT.resolve()),
        "production_inputs_modified": False,
        "target_lock": str(target_lock.resolve()),
        "target_lock_sha256": input_identity["target_lock_sha256"],
        "resolved_versions": resolved_versions,
        "post_rewrite_resolved_versions": post_resolved,
        "rewrites": rewrite_records,
        "cargo_update_commands": cargo_commands,
        "cargo_metadata_command": metadata["command"],
        "cargo_metadata_stdout_sha256": sha256_file(
            compatibility_root / "cargo-metadata.stdout"
        ),
        "cargo_metadata_stderr_sha256": sha256_file(
            compatibility_root / "cargo-metadata.stderr"
        ),
        "base_campaign_snapshot_sha256": baseline.get("campaign_snapshot_sha256"),
        "pre_rewrite_manifest_sha256": baseline_manifest_sha,
        "post_rewrite_manifest_sha256": post_manifest_sha,
        "pre_rewrite_lock_sha256": baseline_lock_sha,
        "post_rewrite_lock_sha256": post_lock_sha,
        "target_campaign_snapshot_sha256": target_campaign_digest,
    }
    provenance["provenance_digest"] = canonical_json_sha256(provenance)
    write_json(record_path, provenance)
    return _compatible_snapshot_mapping(baseline, snapshot_root, provenance)


def _inject_workspace_dependencies(
    root: Path, target_crates: Sequence[str], dependency: str
) -> list[str]:
    try:
        patched = matrix.inject_unialloc_workspace_crates(
            root,
            target_crates=target_crates,
            dependency=dependency,
        )
    except matrix.MatrixError as error:
        raise CampaignContractError(str(error)) from error
    injection_root = root.resolve()
    return [
        str(
            path.resolve()
            if path.is_absolute()
            else (injection_root / path).resolve()
        )
        for path in patched
    ]


def prepare_stage_a_source(
    target_id: str,
    build_group: str,
    *,
    checkout: Path,
    raw_dir: Path,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Create one build tree and return its exact Cargo artifact contract."""
    if build_group not in BUILD_GROUPS:
        raise CampaignContractError(f"unknown build group: {build_group}")
    target = TARGETS[target_id]
    worktree = raw_dir / "build-work" / target_id / build_group
    dependency = _stage_a_dependency(snapshot)
    spin = matrix.find_cached_spin()
    patched_manifests: list[str]
    if target_id == "redb":
        # Keep the generated runner as a bin target in redb's copied workspace.
        # A generated outer package containing a nested redb workspace makes
        # Cargo discover two workspace roots during metadata resolution.
        _copy_checkout(checkout, worktree)
        patched_manifests = _inject_workspace_dependencies(
            worktree,
            stage_a_target_crates(target_id),
            dependency,
        )
        matrix.add_spin_patch(worktree / "Cargo.toml", spin)
        runner_source = worktree / "src" / "bin" / "unialloc-redb-actix-runner.rs"
        runner_source.parent.mkdir(parents=True, exist_ok=True)
        runner_source.write_text(
            amplified_redb_source(), encoding="utf-8"
        )
        build_command = [
            "cargo",
            f"+{TOOLCHAIN}",
            "build",
            "--release",
            "--bin",
            "unialloc-redb-actix-runner",
        ]
        artifact_name = "unialloc-redb-actix-runner"
        manifest_path = worktree / "Cargo.toml"
    else:
        _copy_checkout(checkout, worktree)
        patched_manifests = _inject_workspace_dependencies(
            worktree,
            stage_a_target_crates(target_id),
            dependency,
        )
        matrix.add_spin_patch(worktree / "Cargo.toml", spin)
        manifest_path = worktree / "Cargo.toml"
        if target_id == "oxipng":
            matrix.ensure_standalone_workspace(manifest_path)
            _append_instrumentation(worktree / "src" / "main.rs")
            build_command = [
                "cargo",
                f"+{TOOLCHAIN}",
                "build",
                "--release",
                "--bin",
                "oxipng",
            ]
            artifact_name = "oxipng"
        elif target_id == "polars":
            package = worktree / "crates" / "polars-unialloc-primary"
            (package / "src").mkdir(parents=True, exist_ok=True)
            (package / "Cargo.toml").write_text(psr.POLARS_MANIFEST, encoding="utf-8")
            matrix.add_dependency(package / "Cargo.toml", dependency)
            patched_manifests.append(str((package / "Cargo.toml").resolve()))
            (package / "src" / "main.rs").write_text(
                allocator_instrumentation_source().lstrip()
                + "\n"
                + amplified_polars_source(),
                encoding="utf-8",
            )
            build_command = [
                "cargo",
                f"+{TOOLCHAIN}",
                "build",
                "--release",
                "--package",
                psr.TARGET_SPECS["polars"].package,
            ]
            artifact_name = psr.TARGET_SPECS["polars"].package
        elif target_id in {"swc", "rustpython"}:
            spec = psr.TARGET_SPECS[target_id]
            assert spec.allocator_source is not None and spec.bench_name is not None
            if target_id == "rustpython":
                # execution.rs is a root-package bench.  The shared workspace
                # scanner intentionally ignores [[bench]], so bind UniAlloc on
                # the package manifest explicitly before compiling that target.
                matrix.add_dependency(worktree / "Cargo.toml", dependency)
            _append_instrumentation(
                worktree / spec.allocator_source,
                remove="extern crate swc_malloc;" if target_id == "swc" else None,
            )
            build_command = [
                "cargo",
                f"+{TOOLCHAIN}",
                "bench",
                "--package",
                spec.package,
                "--bench",
                spec.bench_name,
                "--no-run",
            ]
            artifact_name = spec.bench_name
        elif target_id == "actix_web":
            package, bench, _selector, source = redb_actix.ACTIX_BENCHES[
                target.harness_id
            ]
            _append_instrumentation(worktree / source)
            build_command = [
                "cargo",
                f"+{TOOLCHAIN}",
                "bench",
                "--package",
                package,
                "--bench",
                bench,
                "--no-run",
            ]
            artifact_name = bench
        else:
            raise CampaignContractError(f"unknown target: {target_id}")
    return {
        "target_id": target_id,
        "build_group": build_group,
        "worktree": str(worktree.resolve()),
        "manifest": str(manifest_path.resolve()),
        "patched_manifests": patched_manifests,
        "build_command": build_command,
        "artifact_name": artifact_name,
        "generated_source_sha256": (
            hashlib.sha256(amplified_redb_source().encode()).hexdigest()
            if target_id == "redb"
            else (
                hashlib.sha256(amplified_polars_source().encode()).hexdigest()
                if target_id == "polars"
                else None
            )
        ),
    }


def _cargo_artifact(stdout: bytes, artifact_name: str) -> Path:
    paths: list[Path] = []
    for line in stdout.decode(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("reason") != "compiler-artifact":
            continue
        target = row.get("target")
        executable = row.get("executable")
        if (
            isinstance(target, dict)
            and target.get("name") == artifact_name
            and isinstance(executable, str)
            and Path(executable).is_file()
        ):
            paths.append(Path(executable).resolve())
    unique = list(dict.fromkeys(paths))
    if len(unique) != 1:
        raise BuildBlocked(
            f"expected one executable {artifact_name}, observed {len(unique)}"
        )
    return unique[0]


def retain_post_injection_build_inputs(
    prepared: Mapping[str, Any], *, worktree: Path, build_dir: Path
) -> dict[str, Any]:
    provenance_root = build_dir / "post-injection"
    worktree_root = worktree.resolve()

    def resolve_manifest(value: Any) -> Path:
        path = Path(str(value))
        return path.resolve() if path.is_absolute() else (worktree_root / path).resolve()

    manifests = {
        resolve_manifest(value) for value in prepared.get("patched_manifests", [])
    }
    manifests.add(resolve_manifest(prepared["manifest"]))
    manifest_rows: list[dict[str, Any]] = []
    for source in sorted(manifests):
        if not source.is_file():
            raise CampaignContractError(
                f"post-injection manifest is missing: {source}"
            )
        try:
            relative = source.relative_to(worktree_root)
        except ValueError as error:
            raise CampaignContractError(
                f"post-injection manifest escapes build tree: {source}"
            ) from error
        retained = provenance_root / "manifests" / relative
        retained.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, retained)
        manifest_rows.append(
            {
                "relative_path": relative.as_posix(),
                "sha256": sha256_file(source),
                "retained_path": str(retained.resolve()),
                "retained_sha256": sha256_file(retained),
            }
        )
    cargo_lock = worktree / "Cargo.lock"
    if not cargo_lock.is_file():
        raise CampaignContractError("generated post-injection Cargo.lock is missing")
    retained_lock = provenance_root / "Cargo.lock"
    retained_lock.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cargo_lock, retained_lock)
    record = {
        "source": "unialloc-stage-a-post-injection-build-inputs-v1",
        "manifest_count": len(manifest_rows),
        "manifests": manifest_rows,
        "cargo_lock_sha256": sha256_file(cargo_lock),
        "retained_cargo_lock": str(retained_lock.resolve()),
        "retained_cargo_lock_sha256": sha256_file(retained_lock),
    }
    write_json(build_dir / "post-injection-inputs.json", record)
    return record


def validate_retained_post_injection_build_inputs(
    record: Mapping[str, Any],
) -> bool:
    """Validate the immutable copies used to substantiate a cached build."""
    manifests = record.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        return False
    for row in manifests:
        if not isinstance(row, dict):
            return False
        path = Path(str(row.get("retained_path", "")))
        expected = row.get("retained_sha256")
        if (
            not path.is_file()
            or not isinstance(expected, str)
            or sha256_file(path) != expected
            or row.get("sha256") != expected
        ):
            return False
    lock = Path(str(record.get("retained_cargo_lock", "")))
    expected_lock = record.get("retained_cargo_lock_sha256")
    return (
        lock.is_file()
        and isinstance(expected_lock, str)
        and sha256_file(lock) == expected_lock
        and record.get("cargo_lock_sha256") == expected_lock
    )


def _plain_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_power_of_two(value: Any) -> bool:
    return _plain_integer(value) and value > 0 and value & (value - 1) == 0


def _runtime_key_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: row.get(field) for field in runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
    }


def _generic_resolution_key_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in GENERIC_RESOLUTION_KEY_FIELDS}


def export_compiler_site_features(audit_root: Path) -> dict[str, Any]:
    """Export exact KEY5, dynamic-layout KEY3, and generic KEY4 identities.

    The runtime observation KEY5 and allocator ``AdaptiveSiteKey`` remain
    untouched. A borrowed Vec observation row authenticates KEY3 in the
    compiler and expands only to actual 4--32 KiB runtime KEY5 subcohorts. A
    generic compiler row retains the audit's zero TypeId sentinel and is
    resolved later only when runtime evidence gives KEY4 a unique positive
    TypeId.
    """
    rows: list[dict[str, Any]] = []
    numeric_keys: set[tuple[int, ...]] = set()
    dynamic_layout_keys: set[tuple[int, ...]] = set()
    generic_keys: set[tuple[int, ...]] = set()
    for path in sorted(audit_root.rglob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        candidates = document.get("rewrite_candidates") if isinstance(document, dict) else None
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            rewrite_status = str(candidate.get("rewrite_status") or "")
            if rewrite_status not in APPLIED_ALLOCATION_SCOPE_STATUSES:
                continue
            features = candidate.get("lifetime_analysis_features")
            if not isinstance(features, dict):
                features = {}
            raw_key = features.get("runtime_join_key")
            if not isinstance(raw_key, dict):
                raw_key = {}
            key = {
                "callsite": raw_key.get("callsite"),
                "type_id": raw_key.get("type_id"),
                "module_id": raw_key.get("module_id"),
                "requested_size": raw_key.get("requested_size_bytes"),
                "align": raw_key.get("requested_align_bytes"),
            }
            for field in ("callsite", "type_id", "module_id"):
                top_level = candidate.get(field)
                raw_value = key[field]
                if (
                    top_level is not None
                    and raw_value is not None
                    and top_level != raw_value
                ):
                    raise CampaignContractError(
                        "compiler audit top-level identity disagrees with its raw "
                        f"runtime join key: {path}:{field}"
                    )
                if raw_value is None:
                    key[field] = top_level

            audit_complete = features.get("runtime_join_key_complete") is True
            audit_numeric_complete = features.get(
                "numeric_exact_key_complete", audit_complete
            ) is True
            audit_generic_complete = features.get(
                "generic_resolution_key_complete", audit_complete
            ) is True
            positive_identity = all(
                _plain_integer(key[field]) and key[field] > 0
                for field in ("callsite", "module_id")
            )
            positive_layout = (
                _plain_integer(key["requested_size"])
                and key["requested_size"] > 0
                and _positive_power_of_two(key["align"])
            )
            observation_candidate = (
                candidate.get("lifetime_hint")
                == BORROWED_VEC_RESERVE_OBSERVE_HINT
                and candidate.get("lifetime_hint_basis")
                in DYNAMIC_BUFFER_OBSERVE_BASES
                and (
                    features.get("borrowed_vec_reserve_prior_eligible") is True
                    or features.get(
                        "dynamic_buffer_with_capacity_observation_eligible"
                    )
                    is True
                )
                and features.get("observation_candidate_rule_applied") is True
            )
            # A monomorphized semantic-scope helper can still carry a concrete,
            # compiler-derived TypeId in the audit.  Prefer that authenticated
            # KEY5 whenever it is present; reserve KEY4 runtime resolution for
            # the genuine definition-level type_id=0 sentinel.
            if _plain_integer(key["type_id"]) and key["type_id"] > 0:
                identity_mode = (
                    "numeric_dynamic_layout"
                    if observation_candidate and not positive_layout
                    else "numeric_exact"
                )
            elif rewrite_status == GENERIC_REWRITE_STATUS:
                identity_mode = "generic_runtime_type"
            else:
                identity_mode = "invalid"
            numeric_complete = (
                identity_mode == "numeric_exact"
                and positive_identity
                and positive_layout
                and audit_numeric_complete
            )
            dynamic_layout_complete = (
                identity_mode == "numeric_dynamic_layout"
                and positive_identity
                and _plain_integer(key["type_id"])
                and key["type_id"] > 0
                and key["requested_size"] is None
                and key["align"] is None
                and features.get("runtime_join_key_complete") is False
                and features.get("runtime_layout_captured_by_semantic_scope") is True
                and features.get("runtime_layout_subcohort_contract")
                == BORROWED_VEC_RESERVE_SUBCOHORT_CONTRACT
                and features.get("runtime_observation_min_requested_bytes")
                == BORROWED_VEC_RESERVE_MIN_REQUESTED_BYTES
                and features.get("runtime_observation_max_requested_bytes")
                == BORROWED_VEC_RESERVE_MAX_REQUESTED_BYTES
            )
            generic_complete = (
                identity_mode == "generic_runtime_type"
                and positive_identity
                and positive_layout
                and audit_generic_complete
            )
            generic_key_materialized = (
                identity_mode == "generic_runtime_type"
                and positive_identity
                and _plain_integer(key["requested_size"])
                and key["requested_size"] >= 0
                and _plain_integer(key["align"])
                and key["align"] > 0
            )
            compiler_audit_key = dict(key)
            generic_resolution_key = {
                field: key[field] for field in GENERIC_RESOLUTION_KEY_FIELDS
            }
            if numeric_complete:
                exact_key = tuple(
                    int(key[field]) for field in runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
                )
                if exact_key in numeric_keys:
                    raise CampaignContractError(
                        "compiler audit contains a duplicate exact runtime tuple"
                    )
                numeric_keys.add(exact_key)
            dynamic_layout_key = tuple(
                key[field] for field in DYNAMIC_LAYOUT_IDENTITY_KEY_FIELDS
            )
            if dynamic_layout_complete:
                if dynamic_layout_key in dynamic_layout_keys:
                    raise CampaignContractError(
                        "compiler audit contains a duplicate dynamic-layout KEY3"
                    )
                dynamic_layout_keys.add(dynamic_layout_key)
            if generic_key_materialized:
                resolution_key = tuple(
                    int(key[field]) for field in GENERIC_RESOLUTION_KEY_FIELDS
                )
                if resolution_key in generic_keys:
                    raise CampaignContractError(
                        "compiler audit contains a duplicate generic resolution tuple"
                    )
                generic_keys.add(resolution_key)
            rows.append(
                {
                    **key,
                    "identity_mode": identity_mode,
                    "compiler_audit_key": compiler_audit_key,
                    "audit_type_id_sentinel": (
                        key["type_id"]
                        if identity_mode == "generic_runtime_type"
                        else None
                    ),
                    "audit_runtime_join_key_complete": audit_complete,
                    "audit_numeric_exact_key_complete": audit_numeric_complete,
                    "audit_generic_resolution_key_complete": (
                        audit_generic_complete
                    ),
                    # Compatibility name now means a claim-complete numeric KEY5.
                    "runtime_join_key_complete": numeric_complete,
                    "numeric_exact_key_complete": numeric_complete,
                    "dynamic_layout_key_complete": dynamic_layout_complete,
                    "dynamic_layout_identity_key": {
                        field: key[field]
                        for field in DYNAMIC_LAYOUT_IDENTITY_KEY_FIELDS
                    },
                    "generic_resolution_key_complete": generic_complete,
                    "generic_resolution_key": generic_resolution_key,
                    "audit_file": path.relative_to(audit_root).as_posix(),
                    "audit_file_sha256": sha256_file(path),
                    "allocation_site_id": candidate.get("allocation_site_id"),
                    "mir_function": candidate.get("mir_function"),
                    "source_span": candidate.get("source_span"),
                    "callee": candidate.get("callee"),
                    "destination_type": candidate.get("destination_type"),
                    "lifetime_hint": candidate.get("lifetime_hint", 0),
                    "lifetime_hint_confidence": candidate.get(
                        "lifetime_hint_confidence", 0
                    ),
                    "lifetime_hint_basis": candidate.get("lifetime_hint_basis"),
                    "rewrite_status": rewrite_status,
                    "type_id_basis": candidate.get("type_id_basis"),
                    "replacement_symbol": candidate.get("replacement_symbol"),
                    "replacement_resolution_status": candidate.get(
                        "replacement_resolution_status"
                    ),
                    "metadata_pairing_contract": candidate.get(
                        "metadata_pairing_contract"
                    ),
                    "requested_layout_basis": candidate.get(
                        "requested_layout_basis",
                        features.get("requested_layout_basis"),
                    ),
                    "lifetime_analysis_features": features,
                }
            )
    numeric_rows = [row for row in rows if row["identity_mode"] == "numeric_exact"]
    dynamic_layout_rows = [
        row for row in rows if row["identity_mode"] == "numeric_dynamic_layout"
    ]
    generic_rows = [
        row for row in rows if row["identity_mode"] == "generic_runtime_type"
    ]
    return {
        "source": COMPILER_SITE_EXPORT_SOURCE,
        "runtime_join_key": list(runtime_lifetime.RUNTIME_SITE_KEY_FIELDS),
        "generic_resolution_key": list(GENERIC_RESOLUTION_KEY_FIELDS),
        "claim_scope": {
            "runtime_exact_observation_key_unchanged": True,
            "runtime_exact_observation_key": list(
                runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
            ),
            "adaptive_site_key_unchanged": True,
            "numeric_resolution": "exact-key5-only",
            "dynamic_layout_resolution": (
                "authenticated-key3-to-one-or-more-runtime-key5-in-4k-through-32k"
            ),
            "generic_resolution": (
                "authenticated-typeid-zero-key4-to-one-runtime-positive-typeid-key5"
            ),
        },
        "site_count": len(rows),
        "numeric_site_count": len(numeric_rows),
        "dynamic_layout_site_count": len(dynamic_layout_rows),
        "generic_site_count": len(generic_rows),
        "complete_join_key_count": sum(
            int(row["runtime_join_key_complete"]) for row in rows
        ),
        "audit_complete_join_key_count": sum(
            int(row["audit_runtime_join_key_complete"]) for row in rows
        ),
        "numeric_exact_key_complete_count": sum(
            int(row["numeric_exact_key_complete"]) for row in numeric_rows
        ),
        "dynamic_layout_key_complete_count": sum(
            int(row["dynamic_layout_key_complete"])
            for row in dynamic_layout_rows
        ),
        "generic_resolution_key_complete_count": sum(
            int(row["generic_resolution_key_complete"]) for row in generic_rows
        ),
        "applied_prior_hinted_count": sum(
            int(
                row["lifetime_hint"] in {1, 2}
                and str(row["lifetime_hint_basis"]).startswith(
                    "automatic_rust_lifetime_prior_"
                )
            )
            for row in rows
        ),
        "applied_observation_candidate_count": sum(
            int(
                row["lifetime_hint"] == BORROWED_VEC_RESERVE_OBSERVE_HINT
                and row["lifetime_hint_basis"] in DYNAMIC_BUFFER_OBSERVE_BASES
            )
            for row in rows
        ),
        "rows": rows,
    }


def compiler_site_export_summary(export: Mapping[str, Any]) -> dict[str, Any]:
    return {key: export[key] for key in COMPILER_SITE_EXPORT_SUMMARY_FIELDS}


def refresh_cached_compiler_site_export(
    cached: dict[str, Any], *, record_path: Path
) -> bool:
    """Regenerate evaluator-derived site rows without rebuilding the binary."""
    audit_root = Path(str(cached.get("audit_dir", "")))
    compiler_sites_path = Path(str(cached.get("compiler_sites_path", "")))
    compiler_audit = cached.get("compiler_audit")
    if (
        not audit_root.is_dir()
        or not compiler_sites_path.parent.is_dir()
        or not isinstance(compiler_audit, dict)
    ):
        return False
    current_audit = summarize_compiler_prior_audits(audit_root)
    if current_audit.get("audit_digest") != compiler_audit.get("audit_digest"):
        raise CampaignContractError(
            "cached compiler audit digest changed before site-export refresh"
        )
    export = export_compiler_site_features(audit_root)
    if export["applied_prior_hinted_count"] != compiler_audit.get(
        "applied_prior_hinted_candidate_count"
    ):
        raise CampaignContractError(
            "cached compiler audit and regenerated applied-prior rows disagree"
        )
    if export["applied_observation_candidate_count"] != compiler_audit.get(
        "applied_observation_candidate_count"
    ):
        raise CampaignContractError(
            "cached compiler audit and regenerated observation candidates disagree"
        )
    write_json(compiler_sites_path, export)
    cached["compiler_sites"] = compiler_site_export_summary(export)
    cached["compiler_sites_sha256"] = sha256_file(compiler_sites_path)
    cached["compiler_sites_derivation"] = {
        "source": COMPILER_SITE_EXPORT_SOURCE,
        "audit_digest": compiler_audit.get("audit_digest"),
        "evaluator_script_sha256": sha256_file(Path(__file__).resolve()),
        "regenerated_during_reuse": True,
        "binary_rebuilt": False,
    }
    write_json(record_path, cached)
    return True


def _compiler_build_environment(
    build_group: str,
    *,
    wrapper: Path,
    audit_dir: Path,
    pass_log_dir: Path,
    target_dir: Path,
    target_crates: Sequence[str],
    temporary_dir: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        AUTO_PRIOR_ENV,
        "UNIALLOC_AUTO_LIFETIME_CLASSIFIER",
        "UNIALLOC_AUTO_HEAP_LIFETIME_INFERENCE",
        "UNIALLOC_LIFETIME_PROFILE",
        "UNIALLOC_LOWERING_LIFETIME_HINT",
    ):
        env.pop(name, None)
    env.update(
        {
            "CARGO_TARGET_DIR": str(target_dir.resolve()),
            "CARGO_INCREMENTAL": "0",
            RELEASE_STRIP_ENV: "false",
            "TMPDIR": str(temporary_dir.resolve()),
            "RUSTFLAGS": f"--cfg unialloc_lifetime_prior_{build_group.replace('-', '_')}",
            "POLARS_MAX_THREADS": "1",
            "RAYON_NUM_THREADS": "1",
            "TOKIO_WORKER_THREADS": "1",
            "PYTHON_SYS_EXECUTABLE": shutil.which("python3") or "python3",
        }
    )
    env = matrix.typeiso_environment(
        env,
        wrapper=wrapper,
        audit_dir=audit_dir,
        pass_log_dir=pass_log_dir,
        sysroot=matrix.rustc_sysroot(TOOLCHAIN),
        target_crates=target_crates,
        policy_flags=0,
    )
    env.update(arm_build_environment(ARM_BY_NAME[STAGE_A_ARM_BY_BUILD_GROUP[build_group]]))
    return env


def compiler_build_environment_provenance(env: Mapping[str, str]) -> dict[str, Any]:
    release_strip = env.get(RELEASE_STRIP_ENV)
    if release_strip != "false":
        raise CampaignContractError("Stage-A release symbol retention is disabled")
    return {
        "source": "unialloc-stage-a-compiler-build-environment-v1",
        "cargo_profile_release_strip": release_strip,
        "release_symbols_retained": True,
        "rustflags": env.get("RUSTFLAGS"),
        "automatic_rust_lifetime_prior": env.get(AUTO_PRIOR_ENV),
    }


def stage_a_rustflags(target_id: str, build_group: str) -> str:
    flags = f"--cfg unialloc_lifetime_prior_{build_group.replace('-', '_')}"
    if target_id == "swc":
        flags += (
            " -Zshare-generics=y -C target-feature=+sse2 "
            "-C link-args=-Wl,-z,nodelete"
        )
    return flags


def prove_rustpython_binary_runtime(
    binary: Path, *, raw_dir: Path, build_group: str
) -> dict[str, Any]:
    dependency = psr.rustpython_runtime_contract()
    environment = psr.apply_runtime_environment(
        psr.TARGET_SPECS["rustpython"], os.environ.copy(), dependency
    )
    result = matrix.execute(
        ["ldd", binary], cwd=binary.parent, env=environment, timeout=60
    )
    proof_dir = raw_dir / "dynamic-dependencies" / "rustpython" / build_group
    proof_dir.mkdir(parents=True, exist_ok=True)
    output = proof_dir / "ldd.txt"
    output.write_bytes(result["stdout"])
    resolved = result["stdout"].decode(errors="replace")
    soname = str(dependency["library_soname"])
    match = re.search(
        rf"^\s*{re.escape(soname)}\s*=>\s*(\S+)", resolved, re.MULTILINE
    )
    resolved_library = Path(match.group(1)).resolve() if match is not None else None
    success = (
        result["exit_code"] == 0
        and not result["timed_out"]
        and resolved_library is not None
        and str(resolved_library) == dependency["library_path"]
        and sha256_file(resolved_library) == dependency["library_sha256"]
    )
    if not success:
        raise BuildBlocked("RustPython binary did not resolve the pinned libpython")
    return {
        **dependency,
        "success": True,
        "resolved_library": str(resolved_library),
        "ldd_path": str(output.resolve()),
        "ldd_sha256": sha256_file(output),
    }


def build_stage_a_binary(
    target_id: str,
    build_group: str,
    *,
    checkout: Path,
    raw_dir: Path,
    snapshot: Mapping[str, Any],
    wrapper: Path,
    jobs: int,
    timeout: int,
    reuse: bool,
    keep_build_work: bool = False,
) -> dict[str, Any]:
    build_dir = raw_dir / "builds" / target_id / build_group
    record_path = build_dir / "build.json"
    snapshot = prepare_target_compatible_allocator_snapshot(
        target_id,
        checkout=checkout,
        raw_dir=raw_dir,
        baseline=snapshot,
        timeout=timeout,
    )
    dependency_compatibility = snapshot["dependency_compatibility"]
    instrumentation_sha256 = hashlib.sha256(
        allocator_instrumentation_source().encode()
    ).hexdigest()
    expected_generated_source_sha256 = (
        hashlib.sha256(amplified_redb_source().encode()).hexdigest()
        if target_id == "redb"
        else (
            hashlib.sha256(amplified_polars_source().encode()).hexdigest()
            if target_id == "polars"
            else None
        )
    )
    if reuse and record_path.is_file():
        cached = json.loads(record_path.read_text(encoding="utf-8"))
        binary = Path(str(cached.get("binary", "")))
        compiler_sites_path = Path(str(cached.get("compiler_sites_path", "")))
        runtime_reuse_valid = True
        if target_id == "rustpython":
            runtime_cwd = Path(str(cached.get("runtime_cwd", "")))
            runtime_dependency = cached.get("runtime_dependency")
            runtime_reuse_valid = (
                (runtime_cwd / "benches" / "benchmarks").is_dir()
                and isinstance(runtime_dependency, dict)
                and runtime_dependency.get("success") is True
                and Path(str(runtime_dependency.get("library_path", ""))).is_file()
                and sha256_file(Path(str(runtime_dependency["library_path"])))
                == runtime_dependency.get("library_sha256")
            )
        audit_valid = False
        try:
            validate_build_audit_for_arm(
                ARM_BY_NAME[STAGE_A_ARM_BY_BUILD_GROUP[build_group]],
                cached.get("compiler_audit", {}),
                stage_a_target_crates(target_id),
            )
            audit_valid = True
        except CampaignContractError:
            pass
        retained_inputs_valid = validate_retained_post_injection_build_inputs(
            cached.get("post_injection_build_inputs", {})
        )
        compiler_sites_refresh_valid = False
        if audit_valid:
            compiler_sites_refresh_valid = refresh_cached_compiler_site_export(
                cached, record_path=record_path
            )
        if (
            cached.get("success") is True
            and cached.get("implementation_sha256")
            == snapshot["unialloc_implementation_sha256"]
            and cached.get("campaign_snapshot_sha256")
            == snapshot.get("campaign_snapshot_sha256")
            and cached.get("allocator_dependency_compatibility", {}).get(
                "provenance_digest"
            )
            == dependency_compatibility.get("provenance_digest")
            and cached.get("source_commit") == TARGETS[target_id].source_commit
            and cached.get("toolchain") == TOOLCHAIN
            and cached.get("runtime_instrumentation_sha256")
            == instrumentation_sha256
            and cached.get("generated_source_sha256")
            == expected_generated_source_sha256
            and cached.get("automatic_rust_lifetime_prior")
            == (build_group == "compiler-prior")
            and cached.get("compiler_build_environment", {}).get(
                "cargo_profile_release_strip"
            )
            == "false"
            and cached.get("compiler_build_environment", {}).get(
                "release_symbols_retained"
            )
            is True
            and binary.is_file()
            and cached.get("binary_sha256") == sha256_file(binary)
            and compiler_sites_path.is_file()
            and cached.get("compiler_sites_sha256")
            == sha256_file(compiler_sites_path)
            and compiler_sites_refresh_valid
            and audit_valid
            and retained_inputs_valid
            and runtime_reuse_valid
        ):
            return cached
    prepared = prepare_stage_a_source(
        target_id,
        build_group,
        checkout=checkout,
        raw_dir=raw_dir,
        snapshot=snapshot,
    )
    worktree = Path(prepared["worktree"])
    target_dir = raw_dir / "cargo-targets" / target_id / build_group
    audit_dir = raw_dir / "audits" / target_id / build_group
    pass_log_dir = raw_dir / "pass-logs" / target_id / build_group
    temporary_dir = raw_dir / "tmp" / target_id / build_group
    for path in (target_dir, audit_dir, pass_log_dir, temporary_dir):
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)
    env = _compiler_build_environment(
        build_group,
        wrapper=wrapper,
        audit_dir=audit_dir,
        pass_log_dir=pass_log_dir,
        target_dir=target_dir,
        target_crates=stage_a_target_crates(target_id),
        temporary_dir=temporary_dir,
    )
    env["RUSTFLAGS"] = stage_a_rustflags(target_id, build_group)
    compiler_build_environment = compiler_build_environment_provenance(env)
    metadata = matrix.execute(
        ["cargo", f"+{TOOLCHAIN}", "metadata", "--format-version", "1"],
        cwd=worktree,
        env=env,
        timeout=timeout,
    )
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "metadata.stdout").write_bytes(metadata["stdout"])
    (build_dir / "metadata.stderr").write_bytes(metadata["stderr"])
    if metadata["exit_code"] != 0 or metadata["timed_out"]:
        raise BuildBlocked(
            f"{target_id}/{build_group} Cargo metadata failed: "
            + metadata["stderr"].decode(errors="replace")[-8000:]
        )
    post_injection_inputs = retain_post_injection_build_inputs(
        prepared, worktree=worktree, build_dir=build_dir
    )
    command = [
        *prepared["build_command"],
        "--locked",
        "--message-format=json-render-diagnostics",
        "--jobs",
        str(jobs),
    ]
    started = time.time()
    result = matrix.execute(command, cwd=worktree, env=env, timeout=timeout)
    (build_dir / "build.stdout").write_bytes(result["stdout"])
    (build_dir / "build.stderr").write_bytes(result["stderr"])
    if result["exit_code"] != 0 or result["timed_out"]:
        raise BuildBlocked(
            f"{target_id}/{build_group} build failed: "
            + result["stderr"].decode(errors="replace")[-8000:]
        )
    executable = _cargo_artifact(result["stdout"], prepared["artifact_name"])
    binary = raw_dir / "binaries" / target_id / build_group / prepared["artifact_name"]
    binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(executable, binary)
    activation = psr.activation_proof(binary)
    if not activation.get("success"):
        raise BuildBlocked(f"{target_id}/{build_group} allocator activation failed")
    audit = summarize_compiler_prior_audits(audit_dir)
    arm = ARM_BY_NAME[STAGE_A_ARM_BY_BUILD_GROUP[build_group]]
    validate_build_audit_for_arm(arm, audit, stage_a_target_crates(target_id))
    compiler_sites = export_compiler_site_features(audit_dir)
    if (
        build_group == "compiler-prior"
        and compiler_sites["applied_prior_hinted_count"]
        != audit["applied_prior_hinted_candidate_count"]
    ):
        raise CampaignContractError(
            "compiler applied-prior audit rows do not equal exported applied rows"
        )
    if (
        build_group == "compiler-prior"
        and compiler_sites["applied_observation_candidate_count"]
        != audit["applied_observation_candidate_count"]
    ):
        raise CampaignContractError(
            "compiler observation-candidate audit rows do not equal exported rows"
        )
    write_json(build_dir / "compiler-sites.json", compiler_sites)
    semantic_rewrite_applied = (
        audit["actual_rewrite_provenance_valid"] is True
        and audit["applied_allocation_candidate_count"] > 0
    )
    compiler_prior_applied = (
        audit["applied_prior_hinted_candidate_count"] > 0
        and compiler_sites["applied_prior_hinted_count"] > 0
    )
    runtime_cwd: Path | None = None
    runtime_dependency: dict[str, Any] | None = None
    if target_id == "rustpython":
        runtime_cwd = raw_dir / "runtime-assets" / "rustpython" / build_group
        shutil.rmtree(runtime_cwd, ignore_errors=True)
        benchmark_source = worktree / "benches" / "benchmarks"
        benchmark_destination = runtime_cwd / "benches" / "benchmarks"
        benchmark_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(benchmark_source, benchmark_destination)
        runtime_dependency = prove_rustpython_binary_runtime(
            binary, raw_dir=raw_dir, build_group=build_group
        )
    record = {
        "schema_version": 1,
        "success": True,
        "target_id": target_id,
        "build_group": build_group,
        "automatic_rust_lifetime_prior": build_group == "compiler-prior",
        "compiler_build_environment": compiler_build_environment,
        "compiler_prior_applied_hint_provenance": (
            compiler_prior_applied if build_group == "compiler-prior" else None
        ),
        "rewrite_claim_success": semantic_rewrite_applied,
        "static_coverage_status": (
            "not-applicable-all-unknown"
            if build_group != "compiler-prior"
            else (
                "applied-prior-hints"
                if compiler_prior_applied
                else "no-static-coverage"
            )
        ),
        "source_commit": TARGETS[target_id].source_commit,
        "requested_target_crates": list(stage_a_target_crates(target_id)),
        "declared_target_crates": list(TARGETS[target_id].target_crates),
        "target_crate_coverage_gaps": list(
            TARGET_CRATE_COVERAGE_GAPS.get(target_id, ())
        ),
        "toolchain": TOOLCHAIN,
        "runtime_instrumentation_sha256": instrumentation_sha256,
        "generated_source_sha256": prepared["generated_source_sha256"],
        "implementation_revision": snapshot.get("allocator_revision"),
        "implementation_sha256": snapshot["unialloc_implementation_sha256"],
        "campaign_snapshot_sha256": snapshot.get("campaign_snapshot_sha256"),
        "base_campaign_snapshot_sha256": snapshot.get(
            "base_campaign_snapshot_sha256",
            snapshot.get("campaign_snapshot_sha256"),
        ),
        "allocator_dependency_compatibility": dependency_compatibility,
        "prepared": prepared,
        "post_injection_build_inputs": post_injection_inputs,
        "post_injection_build_inputs_path": str(
            (build_dir / "post-injection-inputs.json").resolve()
        ),
        "build_command": result["command"],
        "build_started_unix": started,
        "build_wall_seconds": result["wall_seconds"],
        "binary": str(binary.resolve()),
        "binary_sha256": sha256_file(binary),
        "activation": activation,
        "compiler_audit": audit,
        "compiler_sites": compiler_site_export_summary(compiler_sites),
        "compiler_sites_path": str((build_dir / "compiler-sites.json").resolve()),
        "compiler_sites_sha256": sha256_file(build_dir / "compiler-sites.json"),
        "compiler_sites_derivation": {
            "source": COMPILER_SITE_EXPORT_SOURCE,
            "audit_digest": audit.get("audit_digest"),
            "evaluator_script_sha256": sha256_file(Path(__file__).resolve()),
            "regenerated_during_reuse": False,
            "binary_rebuilt": True,
        },
        "audit_dir": str(audit_dir.resolve()),
        "pass_log_dir": str(pass_log_dir.resolve()),
        "runtime_cwd": str(runtime_cwd.resolve()) if runtime_cwd is not None else None,
        "runtime_dependency": runtime_dependency,
        "disposable_build_work_retained": keep_build_work,
    }
    write_json(record_path, record)
    if not keep_build_work:
        shutil.rmtree(target_dir, ignore_errors=True)
        shutil.rmtree(worktree, ignore_errors=True)
        record["disposable_cargo_target_removed"] = True
        record["disposable_build_work_removed"] = True
        write_json(record_path, record)
    return record


STAGE_A_FIXED_WORK_UNITS = {"oxipng": 32, "redb": 128, "polars": 88}
STAGE_A_CALIBRATION_UNITS = {"oxipng": 8, "redb": 4, "polars": 32}


def _runtime_environment(arm: ArmSpec) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        AUTO_PRIOR_ENV,
        "RUSTC_WRAPPER",
        "RUSTC_WORKSPACE_WRAPPER",
        "UNIALLOC_REWRITE_AUDIT_DIR",
        "UNIALLOC_PASS_LOG_DIR",
    ):
        env.pop(name, None)
    env.update(
        {
            RUNTIME_ARM_ENV: runtime_arm_selector(arm),
            "POLARS_MAX_THREADS": "1",
            "RAYON_NUM_THREADS": "1",
            "TOKIO_WORKER_THREADS": "1",
        }
    )
    return env


def stage_a_runtime_context(
    target_id: str, build: Mapping[str, Any], arm: ArmSpec
) -> tuple[Path, dict[str, str]]:
    cwd = ROOT
    environment = _runtime_environment(arm)
    if target_id != "rustpython":
        return cwd, environment
    cwd = Path(str(build.get("runtime_cwd", "")))
    dependency = build.get("runtime_dependency")
    if not cwd.is_dir() or not isinstance(dependency, dict):
        raise CampaignContractError("RustPython runtime assets/proof are missing")
    environment = psr.apply_runtime_environment(
        psr.TARGET_SPECS["rustpython"], environment, dependency
    )
    return cwd, environment


def _read_smaps(pid: int, elapsed_seconds: float) -> dict[str, Any] | None:
    try:
        text_value = Path(f"/proc/{pid}/smaps_rollup").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    return parse_smaps_rollup(text_value, elapsed_seconds=elapsed_seconds)


def _descendant_processes(root_pid: int) -> list[dict[str, Any]]:
    processes: dict[int, tuple[int, str]] = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            stat = (path / "stat").read_text(encoding="utf-8")
            close = stat.rfind(")")
            fields = stat[close + 2 :].split()
            ppid = int(fields[1])
            command = (path / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace"
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
            continue
        processes[int(path.name)] = (ppid, command.strip())
    descendants: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {
            pid
            for pid, (ppid, _command) in processes.items()
            if ppid in frontier and pid not in descendants
        }
        descendants.update(children)
        frontier = children
    return [
        {"pid": pid, "ppid": processes[pid][0], "command": processes[pid][1]}
        for pid in sorted(descendants)
    ]


def execute_monitored_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    artifact_dir: Path,
    timeout: float,
    sample_interval: float = 0.5,
) -> dict[str, Any]:
    """Run one persistent process and retain raw output plus procfs samples."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = artifact_dir / "stdout.bin"
    stderr_path = artifact_dir / "stderr.bin"
    started = time.monotonic()
    timed_out = False
    samples: list[dict[str, Any]] = []
    observed_descendants: dict[int, dict[str, Any]] = {}
    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(env),
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
        while process.poll() is None:
            elapsed = time.monotonic() - started
            for descendant in _descendant_processes(process.pid):
                observed_descendants[int(descendant["pid"])] = descendant
            sample = _read_smaps(process.pid, elapsed)
            if sample is not None:
                samples.append(sample)
            if elapsed > timeout:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                break
            time.sleep(sample_interval)
        return_code = process.wait()
    elapsed = time.monotonic() - started
    return {
        "command": list(command),
        "cwd": str(cwd.resolve()),
        "exit_code": return_code,
        "timed_out": timed_out,
        "wall_seconds": elapsed,
        "stdout_path": str(stdout_path.resolve()),
        "stderr_path": str(stderr_path.resolve()),
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_sha256": sha256_file(stderr_path),
        "smaps_samples": samples,
        "single_process_guard": {
            "passed": not observed_descendants,
            "root_pid": process.pid,
            "observed_descendant_count": len(observed_descendants),
            "observed_descendants": list(observed_descendants.values()),
            "poll_interval_seconds": sample_interval,
            "guard_kind": "procfs-parent-closure-poll",
        },
    }


def _json_line(stdout: str, prefix: str = "") -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if prefix and not line.startswith(prefix):
            continue
        payload = line[len(prefix) :] if prefix else line
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    if len(rows) != 1:
        raise CampaignContractError(
            f"expected one workload JSON record with prefix {prefix!r}, observed {len(rows)}"
        )
    return rows[0]


def workload_output_identity(
    target_id: str,
    *,
    stdout: str,
    work_dir: Path,
    work_units: int | None,
) -> dict[str, Any]:
    if target_id == "oxipng":
        outputs = sorted((work_dir / "out").glob("*.png"))
        if work_units is None or len(outputs) != work_units:
            raise CampaignContractError("Oxipng output count differs from fixed work")
        digests = [sha256_file(path) for path in outputs]
        return {
            "oracle": "per-output-png-sha256",
            "output_count": len(outputs),
            "unique_output_digests": sorted(set(digests)),
            "combined_digest": canonical_json_sha256(digests),
        }
    if target_id == "redb":
        row = _json_line(stdout, redb_actix.RESULT_PREFIX)
        if (
            row.get("correctness") is not True
            or row.get("work_units") != work_units
            or not isinstance(row.get("output_digest"), str)
        ):
            raise CampaignContractError("redb fixed-work correctness record failed")
        return {"oracle": "redb-final-table-digest", "record": row}
    if target_id == "polars":
        row = _json_line(stdout)
        if row.get("work_units") != work_units or not isinstance(
            row.get("fingerprint"), str
        ):
            raise CampaignContractError("Polars fixed-work result is incomplete")
        return {"oracle": "polars-combined-frame-fingerprint", "record": row}
    if target_id in {"swc", "rustpython", "actix_web"}:
        return {
            "oracle": "criterion-process-success",
            "diagnostic_only": True,
            "selector": TARGETS[target_id].harness_id,
            "output_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
            "cross_build_output_equivalence_required": False,
        }
    raise CampaignContractError(f"unknown target: {target_id}")


def runtime_classification_summary(
    stats: Mapping[str, Any], *, evidence_stage: str = "stage-a"
) -> dict[str, Any]:
    if evidence_stage not in {"stage-a", "production"}:
        raise CampaignContractError(
            f"unknown classification evidence stage: {evidence_stage}"
        )
    tp = _nonnegative_integer(stats, "static_hint_tp")
    tn = _nonnegative_integer(stats, "static_hint_tn")
    fp = _nonnegative_integer(stats, "static_hint_fp")
    fn = _nonnegative_integer(stats, "static_hint_fn")
    abstained = _nonnegative_integer(stats, "static_hint_abstained")
    decisive = tp + tn + fp + fn
    accuracy = (tp + tn) / decisive if decisive else None
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    predictor_tp = _nonnegative_integer(stats, "predictor_tp")
    predictor_tn = _nonnegative_integer(stats, "predictor_tn")
    predictor_fp = _nonnegative_integer(stats, "predictor_fp")
    predictor_fn = _nonnegative_integer(stats, "predictor_fn")
    predictor_decisive = predictor_tp + predictor_tn + predictor_fp + predictor_fn
    stage_a = evidence_stage == "stage-a"
    return {
        "static_hint_true_positive": tp,
        "static_hint_true_negative": tn,
        "static_hint_false_positive": fp,
        "static_hint_false_negative": fn,
        "static_hint_abstained": abstained,
        "static_hint_decisive": decisive,
        "accuracy_evidence_stage": evidence_stage,
        "stage_a_pressure_clock_accuracy": accuracy if stage_a else None,
        "stage_a_pressure_clock_long_precision": precision if stage_a else None,
        "stage_a_pressure_clock_long_recall": recall if stage_a else None,
        "production_accuracy": accuracy if not stage_a else None,
        "production_long_precision": precision if not stage_a else None,
        "production_long_recall": recall if not stage_a else None,
        "production_accuracy_claim_eligible": False,
        "runtime_predictor_true_positive": predictor_tp,
        "runtime_predictor_true_negative": predictor_tn,
        "runtime_predictor_false_positive": predictor_fp,
        "runtime_predictor_false_negative": predictor_fn,
        "runtime_predictor_decisive": predictor_decisive,
        "runtime_predictor_accuracy": (
            (predictor_tp + predictor_tn) / predictor_decisive
            if predictor_decisive
            else None
        ),
        "runtime_predictor_long_precision": (
            predictor_tp / (predictor_tp + predictor_fp)
            if predictor_tp + predictor_fp
            else None
        ),
        "runtime_predictor_long_recall": (
            predictor_tp / (predictor_tp + predictor_fn)
            if predictor_tp + predictor_fn
            else None
        ),
        "runtime_predictor_evidence_stage": evidence_stage,
        "cross_clock_accuracy_claim": False,
        "affinity_pinned": False,
        "measurement_lock_held": False,
        "performance_claim_eligible": False,
        "runtime_promotions": _nonnegative_integer(stats, "adaptive_promotions"),
        "runtime_demotions": _nonnegative_integer(stats, "adaptive_demotions"),
        "runtime_prior_corrections": _nonnegative_integer(
            stats, "adaptive_prior_corrections"
        ),
    }


def _aggregate_runtime_outcomes(
    rows: Sequence[Mapping[str, Any]], *, scope: str
) -> dict[str, Any]:
    """Sum deduplicated runtime outcome evidence, or fail the block closed."""
    identities: set[tuple[Any, ...]] = set()
    invalid_rows = 0
    for row in rows:
        identity = tuple(
            row.get(field) for field in runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
        )
        if identity in identities:
            raise CampaignContractError(
                f"{scope} outcome evidence repeats one runtime exact site tuple"
            )
        identities.add(identity)
        if any(
            not _plain_integer(row.get(field)) or row[field] < 0
            for field in RUNTIME_OUTCOME_AGGREGATE_FIELDS
        ):
            invalid_rows += 1
    evidence_complete = invalid_rows == 0
    encoded_identities = sorted(
        json.dumps(list(identity), separators=(",", ":"), ensure_ascii=True)
        for identity in identities
    )
    return {
        "scope": scope,
        "deduplicated_by": list(runtime_lifetime.RUNTIME_SITE_KEY_FIELDS),
        "exact_site_key_digest": canonical_json_sha256(encoded_identities),
        "site_count": len(rows),
        "short_prior_site_count": sum(
            int(row.get("latest_static_prior") == 1) for row in rows
        ),
        "long_prior_site_count": sum(
            int(row.get("latest_static_prior") == 2) for row in rows
        ),
        "evidence_complete": evidence_complete,
        "missing_or_invalid_field_site_count": invalid_rows,
        **{
            field: (
                sum(int(row[field]) for row in rows)
                if evidence_complete
                else None
            )
            for field in RUNTIME_OUTCOME_AGGREGATE_FIELDS
        },
    }


def join_compiler_runtime_sites(
    compiler_export: Mapping[str, Any], runtime_rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    key_fields = runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
    runtime_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    runtime_by_dynamic_key: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    runtime_by_generic_key: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    for row in runtime_rows:
        try:
            key = tuple(row[field] for field in key_fields)
        except KeyError as error:
            raise CampaignContractError(
                f"runtime observation lacks exact site field: {error.args[0]}"
            ) from error
        if key in runtime_by_key:
            raise CampaignContractError(
                "runtime observation contains a duplicate exact site tuple"
            )
        runtime_by_key[key] = dict(row)
        dynamic_key = tuple(row[field] for field in DYNAMIC_LAYOUT_IDENTITY_KEY_FIELDS)
        runtime_by_dynamic_key.setdefault(dynamic_key, []).append(key)
        generic_key = tuple(row[field] for field in GENERIC_RESOLUTION_KEY_FIELDS)
        runtime_by_generic_key.setdefault(generic_key, []).append(key)

    joined: list[dict[str, Any]] = []
    resolution_counts: Counter[str] = Counter()
    applied_prior_total = 0
    applied_prior_incomplete = 0
    numeric_keys: set[tuple[Any, ...]] = set()
    dynamic_keys: set[tuple[Any, ...]] = set()
    generic_keys: set[tuple[Any, ...]] = set()
    claimed_runtime_keys: set[tuple[Any, ...]] = set()
    numeric_total = 0
    numeric_complete = 0
    numeric_matched = 0
    dynamic_total = 0
    dynamic_complete = 0
    dynamic_resolved = 0
    dynamic_resolved_subcohorts = 0
    generic_total = 0
    generic_complete = 0
    generic_resolved = 0

    for compiler in compiler_export.get("rows", []):
        if not isinstance(compiler, dict):
            continue
        applied_prior = (
            compiler.get("lifetime_hint") in {1, 2}
            and str(compiler.get("lifetime_hint_basis") or "").startswith(
                "automatic_rust_lifetime_prior_"
            )
        )
        if applied_prior:
            applied_prior_total += 1

        compiler_audit_key = compiler.get("compiler_audit_key")
        if not isinstance(compiler_audit_key, dict):
            compiler_audit_key = _runtime_key_dict(compiler)
        identity_mode = compiler.get("identity_mode")
        if identity_mode not in {
            "numeric_exact",
            "numeric_dynamic_layout",
            "generic_runtime_type",
        }:
            if _plain_integer(compiler_audit_key.get("type_id")) and (
                compiler_audit_key["type_id"] > 0
            ):
                identity_mode = (
                    "numeric_dynamic_layout"
                    if compiler.get("dynamic_layout_key_complete") is True
                    else "numeric_exact"
                )
            elif compiler.get("rewrite_status") == GENERIC_REWRITE_STATUS:
                identity_mode = "generic_runtime_type"
            else:
                identity_mode = "invalid"

        runtime: dict[str, Any] | None = None
        runtime_subcohorts: list[dict[str, Any]] = []
        resolved_runtime_key: tuple[Any, ...] | None = None
        resolved_runtime_keys: list[tuple[Any, ...]] = []
        resolution_status = "rejected_compiler_identity_contract"
        rejection_reason: str | None = None
        key_complete = False

        if identity_mode == "numeric_exact":
            numeric_total += 1
            key = tuple(compiler_audit_key.get(field) for field in key_fields)
            computed_complete = (
                all(
                    _plain_integer(compiler_audit_key.get(field))
                    and compiler_audit_key[field] > 0
                    for field in ("callsite", "type_id", "module_id", "requested_size")
                )
                and _positive_power_of_two(compiler_audit_key.get("align"))
            )
            declared_complete = compiler.get(
                "numeric_exact_key_complete",
                compiler.get("runtime_join_key_complete"),
            )
            key_complete = declared_complete is True and computed_complete
            if key_complete:
                numeric_complete += 1
                if key in numeric_keys:
                    raise CampaignContractError(
                        "compiler export contains a duplicate exact site tuple"
                    )
                numeric_keys.add(key)
                candidate_runtime = runtime_by_key.get(key)
                if candidate_runtime is None:
                    resolution_status = "unobserved"
                elif (
                    not _plain_integer(candidate_runtime.get("allocation_count"))
                    or candidate_runtime["allocation_count"] <= 0
                ):
                    resolution_status = "rejected_zero_runtime_allocations"
                    rejection_reason = "runtime_allocation_count_not_positive"
                elif candidate_runtime.get("predictor_key_ambiguous") is not False:
                    resolution_status = "rejected_predictor_key_ambiguous"
                    rejection_reason = "runtime_predictor_key_ambiguous"
                elif compiler.get("lifetime_hint") in {1, 2} and (
                    candidate_runtime.get("latest_static_prior")
                    != compiler.get("lifetime_hint")
                ):
                    resolution_status = "rejected_static_prior_transport_mismatch"
                    rejection_reason = "runtime_latest_static_prior_mismatch"
                else:
                    runtime = candidate_runtime
                    runtime_subcohorts = [candidate_runtime]
                    resolved_runtime_key = key
                    resolved_runtime_keys = [key]
                    resolution_status = "exact_numeric_match"
                    numeric_matched += 1
            else:
                resolution_status = "ineligible_dynamic_layout"
                rejection_reason = "numeric_exact_key_incomplete"

        elif identity_mode == "numeric_dynamic_layout":
            dynamic_total += 1
            dynamic_key_dict = compiler.get("dynamic_layout_identity_key")
            if not isinstance(dynamic_key_dict, dict):
                dynamic_key_dict = {
                    field: compiler_audit_key.get(field)
                    for field in DYNAMIC_LAYOUT_IDENTITY_KEY_FIELDS
                }
            dynamic_key = tuple(
                dynamic_key_dict.get(field)
                for field in DYNAMIC_LAYOUT_IDENTITY_KEY_FIELDS
            )
            features = compiler.get("lifetime_analysis_features")
            if not isinstance(features, dict):
                features = {}
            computed_complete = (
                all(
                    _plain_integer(dynamic_key_dict.get(field))
                    and dynamic_key_dict[field] > 0
                    for field in DYNAMIC_LAYOUT_IDENTITY_KEY_FIELDS
                )
                and compiler_audit_key.get("requested_size") is None
                and compiler_audit_key.get("align") is None
                and compiler.get("lifetime_hint")
                == BORROWED_VEC_RESERVE_OBSERVE_HINT
                and compiler.get("lifetime_hint_basis")
                in DYNAMIC_BUFFER_OBSERVE_BASES
                and compiler.get("rewrite_status") in APPLIED_ALLOCATION_SCOPE_STATUSES
                and (
                    features.get("borrowed_vec_reserve_prior_eligible") is True
                    or features.get(
                        "dynamic_buffer_with_capacity_observation_eligible"
                    )
                    is True
                )
                and features.get("observation_candidate_rule_applied") is True
                and features.get("runtime_layout_captured_by_semantic_scope") is True
                and features.get("runtime_layout_subcohort_contract")
                == BORROWED_VEC_RESERVE_SUBCOHORT_CONTRACT
                and features.get("runtime_observation_min_requested_bytes")
                == BORROWED_VEC_RESERVE_MIN_REQUESTED_BYTES
                and features.get("runtime_observation_max_requested_bytes")
                == BORROWED_VEC_RESERVE_MAX_REQUESTED_BYTES
            )
            key_complete = (
                compiler.get("dynamic_layout_key_complete") is True
                and computed_complete
            )
            if key_complete:
                dynamic_complete += 1
                if dynamic_key in dynamic_keys:
                    raise CampaignContractError(
                        "compiler export contains a duplicate dynamic-layout KEY3"
                    )
                dynamic_keys.add(dynamic_key)
                candidate_keys = runtime_by_dynamic_key.get(dynamic_key, [])
                out_of_band = [
                    key
                    for key in candidate_keys
                    if not (
                        _plain_integer(key[3])
                        and BORROWED_VEC_RESERVE_MIN_REQUESTED_BYTES
                        <= key[3]
                        <= BORROWED_VEC_RESERVE_MAX_REQUESTED_BYTES
                        and _positive_power_of_two(key[4])
                    )
                ]
                if out_of_band:
                    resolution_status = "rejected_runtime_layout_outside_candidate_band"
                    rejection_reason = "runtime_key5_outside_authenticated_4k_32k_band"
                elif not candidate_keys:
                    resolution_status = "unobserved"
                else:
                    candidate_rows = [runtime_by_key[key] for key in candidate_keys]
                    if any(
                        not _plain_integer(row.get("allocation_count"))
                        or row["allocation_count"] <= 0
                        for row in candidate_rows
                    ):
                        resolution_status = "rejected_zero_runtime_allocations"
                        rejection_reason = "runtime_allocation_count_not_positive"
                    elif any(
                        row.get("predictor_key_ambiguous") is not False
                        for row in candidate_rows
                    ):
                        resolution_status = "rejected_predictor_key_ambiguous"
                        rejection_reason = "runtime_predictor_key_ambiguous"
                    elif any(
                        row.get("latest_static_prior") != 0
                        for row in candidate_rows
                    ):
                        resolution_status = "rejected_observation_transport_mismatch"
                        rejection_reason = (
                            "observation_candidate_must_transport_no_static_prior"
                        )
                    else:
                        runtime_subcohorts = candidate_rows
                        runtime = candidate_rows[0] if len(candidate_rows) == 1 else None
                        resolved_runtime_keys = list(candidate_keys)
                        resolved_runtime_key = (
                            candidate_keys[0] if len(candidate_keys) == 1 else None
                        )
                        resolution_status = "dynamic_key3_runtime_layout_match"
                        dynamic_resolved += 1
                        dynamic_resolved_subcohorts += len(candidate_keys)
            else:
                resolution_status = "rejected_dynamic_layout_contract"
                rejection_reason = "dynamic_layout_key3_contract_incomplete"

        elif identity_mode == "generic_runtime_type":
            generic_total += 1
            generic_key_dict = compiler.get("generic_resolution_key")
            if not isinstance(generic_key_dict, dict):
                generic_key_dict = _generic_resolution_key_dict(compiler_audit_key)
            generic_key = tuple(
                generic_key_dict.get(field) for field in GENERIC_RESOLUTION_KEY_FIELDS
            )
            layout_present = all(
                _plain_integer(generic_key_dict.get(field))
                for field in ("requested_size", "align")
            )
            computed_complete = (
                all(
                    _plain_integer(generic_key_dict.get(field))
                    and generic_key_dict[field] > 0
                    for field in ("callsite", "module_id", "requested_size")
                )
                and _positive_power_of_two(generic_key_dict.get("align"))
            )
            materialized_generic_key = (
                all(
                    _plain_integer(generic_key_dict.get(field))
                    for field in GENERIC_RESOLUTION_KEY_FIELDS
                )
                and generic_key_dict["callsite"] > 0
                and generic_key_dict["module_id"] > 0
                and generic_key_dict["requested_size"] >= 0
                and generic_key_dict["align"] > 0
            )
            declared_complete = compiler.get("generic_resolution_key_complete")
            if declared_complete is None:
                declared_complete = computed_complete
            key_complete = declared_complete is True and computed_complete
            if materialized_generic_key:
                if generic_key in generic_keys:
                    raise CampaignContractError(
                        "compiler export contains a duplicate generic resolution tuple"
                    )
                generic_keys.add(generic_key)
            if key_complete:
                generic_complete += 1

            replacement_symbol = compiler.get("replacement_symbol")
            replacement_contract = GENERIC_REPLACEMENT_CONTRACTS.get(
                replacement_symbol
            )
            if compiler.get("rewrite_status") != GENERIC_REWRITE_STATUS:
                resolution_status = "rejected_generic_contract"
                rejection_reason = "bad_rewrite_status"
            elif compiler_audit_key.get("type_id") != 0:
                resolution_status = "rejected_generic_contract"
                rejection_reason = "generic_audit_type_id_not_zero"
            elif compiler.get("type_id_basis") != GENERIC_RUNTIME_TYPE_ID_BASIS:
                resolution_status = "rejected_generic_contract"
                rejection_reason = "bad_type_id_basis"
            elif replacement_contract is None:
                resolution_status = "rejected_generic_contract"
                rejection_reason = "unauthenticated_replacement_symbol"
            elif compiler.get("replacement_resolution_status") != replacement_contract[0]:
                resolution_status = "rejected_generic_contract"
                rejection_reason = "replacement_resolution_status_mismatch"
            elif compiler.get("metadata_pairing_contract") != replacement_contract[1]:
                resolution_status = "rejected_generic_contract"
                rejection_reason = "metadata_pairing_contract_mismatch"
            elif not layout_present:
                resolution_status = "ineligible_dynamic_layout"
                rejection_reason = "generic_layout_missing"
            elif generic_key_dict.get("requested_size") == 0:
                resolution_status = "ineligible_zst"
                rejection_reason = "zero_sized_allocation"
            elif not _positive_power_of_two(generic_key_dict.get("align")):
                resolution_status = "rejected_generic_contract"
                rejection_reason = "invalid_requested_alignment"
            elif not key_complete:
                resolution_status = "rejected_generic_contract"
                rejection_reason = "generic_resolution_key_incomplete"
            else:
                candidate_keys = runtime_by_generic_key.get(generic_key, [])
                runtime_type_ids = {
                    runtime_key[1]
                    for runtime_key in candidate_keys
                    if _plain_integer(runtime_key[1]) and runtime_key[1] > 0
                }
                if not candidate_keys:
                    resolution_status = "unobserved"
                elif (
                    len(candidate_keys) != 1
                    or len(runtime_type_ids) != 1
                ):
                    resolution_status = "rejected_multi_runtime_type_id"
                    rejection_reason = "generic_key4_has_multiple_runtime_key5_rows"
                else:
                    candidate_key = candidate_keys[0]
                    candidate_runtime = runtime_by_key[candidate_key]
                    if not _plain_integer(candidate_key[1]) or candidate_key[1] <= 0:
                        resolution_status = "rejected_runtime_contract"
                        rejection_reason = "runtime_type_id_not_positive"
                    elif (
                        not _plain_integer(candidate_runtime.get("allocation_count"))
                        or candidate_runtime["allocation_count"] <= 0
                    ):
                        resolution_status = "rejected_zero_runtime_allocations"
                        rejection_reason = "runtime_allocation_count_not_positive"
                    elif candidate_runtime.get("predictor_key_ambiguous") is not False:
                        resolution_status = "rejected_predictor_key_ambiguous"
                        rejection_reason = "runtime_predictor_key_ambiguous"
                    elif compiler.get("lifetime_hint") in {1, 2} and (
                        candidate_runtime.get("latest_static_prior")
                        != compiler.get("lifetime_hint")
                    ):
                        resolution_status = (
                            "rejected_static_prior_transport_mismatch"
                        )
                        rejection_reason = "runtime_latest_static_prior_mismatch"
                    else:
                        runtime = candidate_runtime
                        runtime_subcohorts = [candidate_runtime]
                        resolved_runtime_key = candidate_key
                        resolved_runtime_keys = [candidate_key]
                        resolution_status = "generic_unique_runtime_type_match"
                        generic_resolved += 1

        else:
            applied_prior_incomplete += int(applied_prior)
            rejection_reason = "missing_positive_numeric_or_generic_identity"

        if key_complete is False and identity_mode != "invalid":
            applied_prior_incomplete += int(applied_prior)
        for claimed_key in resolved_runtime_keys:
            if claimed_key in claimed_runtime_keys:
                raise CampaignContractError(
                    "one runtime exact site tuple was claimed by multiple compiler rows"
                )
            claimed_runtime_keys.add(claimed_key)
        resolution_counts[resolution_status] += 1
        joined.append(
            {
                "compiler_audit_key": compiler_audit_key,
                "audit_type_id_sentinel": compiler.get(
                    "audit_type_id_sentinel",
                    compiler_audit_key.get("type_id")
                    if identity_mode == "generic_runtime_type"
                    else None,
                ),
                "identity_mode": identity_mode,
                "join_key": compiler_audit_key,
                "resolved_runtime_exact_key": (
                    dict(zip(key_fields, resolved_runtime_key, strict=True))
                    if resolved_runtime_key is not None
                    else None
                ),
                "resolved_runtime_exact_keys": [
                    dict(zip(key_fields, key, strict=True))
                    for key in resolved_runtime_keys
                ],
                "matched": bool(runtime_subcohorts),
                "applied_prior_hint": applied_prior,
                "resolution_status": resolution_status,
                "rejection_reason": rejection_reason,
                "compiler": compiler,
                "runtime": runtime,
                "runtime_subcohorts": runtime_subcohorts,
            }
        )

    matched = sum(int(row["matched"]) for row in joined)
    complete = numeric_complete + dynamic_complete + generic_complete
    generic_status = (
        "no-generic-rewrite-coverage"
        if generic_resolved == 0
        else (
            "complete-generic-rewrite-coverage"
            if generic_resolved == generic_total
            else "partial-generic-rewrite-coverage"
        )
    )
    applied_prior_complete = applied_prior_total - applied_prior_incomplete
    applied_prior_matched = sum(
        int(row["matched"] and row["applied_prior_hint"]) for row in joined
    )
    static_status = (
        "no-static-coverage"
        if applied_prior_matched == 0
        else (
            "complete-static-coverage"
            if applied_prior_matched == applied_prior_total
            else "partial-static-coverage"
        )
    )
    matched_applied_prior_runtime = [
        row["runtime"]
        for row in joined
        if row["matched"] and row["applied_prior_hint"]
    ]
    matched_observation_candidate_runtime = [
        runtime
        for row in joined
        if row["matched"]
        and row["identity_mode"] == "numeric_dynamic_layout"
        for runtime in row["runtime_subcohorts"]
    ]
    executed_static_prior_runtime = [
        row
        for row in runtime_by_key.values()
        if _plain_integer(row.get("latest_static_prior"))
        and row["latest_static_prior"] in {1, 2}
    ]
    return {
        "source": "unialloc-compiler-runtime-exact-site-join-v3",
        "status": static_status,
        "generic_rewrite_status": generic_status,
        "dynamic_layout_status": (
            "no-dynamic-layout-coverage"
            if dynamic_resolved == 0
            else (
                "complete-dynamic-layout-coverage"
                if dynamic_resolved == dynamic_total
                else "partial-dynamic-layout-coverage"
            )
        ),
        "claim_scope": {
            "runtime_exact_observation_key_unchanged": True,
            "runtime_exact_observation_key": list(key_fields),
            "adaptive_site_key_unchanged": True,
            "numeric_resolution": "exact-key5-only",
            "dynamic_layout_resolution": (
                "authenticated-key3-to-one-or-more-runtime-key5-in-4k-through-32k"
            ),
            "generic_resolution": (
                "authenticated-typeid-zero-key4-to-one-runtime-positive-typeid-key5"
            ),
            "generic_static_prior_transport_required": True,
        },
        "compiler_complete_key_count": complete,
        "compiler_incomplete_key_count": len(joined) - complete,
        "matched_site_count": matched,
        "unmatched_site_count": complete - matched,
        "match_coverage": matched / complete if complete else 0.0,
        "numeric_compiler_site_count": numeric_total,
        "numeric_complete_key_count": numeric_complete,
        "numeric_matched_site_count": numeric_matched,
        "numeric_unmatched_site_count": numeric_complete - numeric_matched,
        "dynamic_layout_compiler_site_count": dynamic_total,
        "dynamic_layout_key_complete_count": dynamic_complete,
        "dynamic_layout_resolved_site_count": dynamic_resolved,
        "dynamic_layout_resolved_subcohort_count": dynamic_resolved_subcohorts,
        "dynamic_layout_rejected_or_unobserved_site_count": (
            dynamic_total - dynamic_resolved
        ),
        "generic_compiler_site_count": generic_total,
        "generic_resolution_key_complete_count": generic_complete,
        "generic_resolved_site_count": generic_resolved,
        "generic_rejected_or_unobserved_site_count": generic_total - generic_resolved,
        "resolution_status_counts": dict(sorted(resolution_counts.items())),
        "matched_applied_prior_outcomes": _aggregate_runtime_outcomes(
            matched_applied_prior_runtime,
            scope="resolved-compiler-applied-prior-key5",
        ),
        "matched_observation_candidate_outcomes": _aggregate_runtime_outcomes(
            matched_observation_candidate_runtime,
            scope="resolved-borrowed-vec-observation-key3-runtime-key5-subcohorts",
        ),
        "executed_static_prior_outcomes": _aggregate_runtime_outcomes(
            executed_static_prior_runtime,
            scope="all-runtime-key5-with-latest-static-prior",
        ),
        "applied_prior_classified_count": applied_prior_total,
        "applied_prior_complete_key_count": applied_prior_complete,
        "applied_prior_incomplete_key_count": applied_prior_incomplete,
        "matched_applied_prior_site_count": applied_prior_matched,
        "unmatched_applied_prior_site_count": (
            applied_prior_complete - applied_prior_matched
        ),
        "applied_prior_match_coverage": (
            applied_prior_matched / applied_prior_total
            if applied_prior_total
            else 0.0
        ),
        "static_prior_join_success": applied_prior_matched > 0,
        "observation_candidate_join_success": dynamic_resolved > 0,
        "static_coverage_claim_eligible": applied_prior_matched > 0,
        "generic_runtime_join_rewrite_success": generic_resolved > 0,
        "runtime_join_rewrite_success": matched > 0,
        "runtime_site_count": len(runtime_rows),
        "rows": joined,
    }


def compact_compiler_runtime_exact_join(
    exact_join: Mapping[str, Any],
) -> dict[str, Any]:
    """Retain every join claim field while keeping large per-site rows external."""
    return {key: value for key, value in exact_join.items() if key != "rows"}


def run_stage_a_sample(
    target_id: str,
    build_group: str,
    *,
    build: Mapping[str, Any],
    raw_dir: Path,
    input_path: Path | None,
    work_units: int | None,
    measurement_seconds: float,
    timeout: float,
    sample_interval: float,
    validate_duration: bool = True,
    run_label: str = "measured",
    arm_name: str | None = None,
    command_prefix: Sequence[str] = (),
    evidence_stage: str = "stage-a",
    criterion_warm_up_seconds: float = 1.0,
) -> dict[str, Any]:
    arm = ARM_BY_NAME[
        arm_name or STAGE_A_ARM_BY_BUILD_GROUP[build_group]
    ]
    if arm.build_group != build_group:
        raise CampaignContractError(
            f"arm {arm.name} requires build group {arm.build_group}, got {build_group}"
        )
    if evidence_stage not in {"stage-a", "production"}:
        raise CampaignContractError(f"unknown sample evidence stage: {evidence_stage}")
    stage_label = "Stage-A" if evidence_stage == "stage-a" else "Stage-B"
    binary = Path(str(build["binary"]))
    work_dir = raw_dir / "workloads" / target_id / build_group / run_label
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)
    command = screening_command(
        target_id,
        binary=binary,
        work_dir=work_dir,
        work_units=work_units,
        measurement_seconds=measurement_seconds,
        criterion_warm_up_seconds=criterion_warm_up_seconds,
        input_path=input_path,
    )
    command = [*command_prefix, *command]
    artifact_dir = raw_dir / "runs" / target_id / build_group / run_label
    runtime_cwd, runtime_environment = stage_a_runtime_context(
        target_id, build, arm
    )
    process = execute_monitored_process(
        command,
        cwd=runtime_cwd,
        env=runtime_environment,
        artifact_dir=artifact_dir,
        timeout=timeout,
        sample_interval=sample_interval,
    )
    stdout = Path(process["stdout_path"]).read_text(encoding="utf-8", errors="replace")
    stderr = Path(process["stderr_path"]).read_text(encoding="utf-8", errors="replace")
    if process["timed_out"] or process["exit_code"] != 0:
        raise CampaignContractError(
            f"{target_id}/{build_group} {stage_label} process failed: {stderr[-8000:]}"
        )
    if process["single_process_guard"]["passed"] is not True:
        raise CampaignContractError(
            f"{target_id}/{build_group} spawned descendant processes; "
            f"{stage_label} requires one persistent process"
        )
    if validate_duration:
        validate_stage_duration("screening", float(process["wall_seconds"]))
    stats = runtime_lifetime.parse_runtime_stats(stderr)
    if stats is None:
        raise CampaignContractError("Stage-A process emitted no runtime stats")
    sites = runtime_lifetime.parse_runtime_site_rows(stderr)
    validate_runtime_evidence(arm, stats, sites)
    fragmentation = parse_fragmentation(stderr)
    if validate_duration and not process["smaps_samples"]:
        raise CampaignContractError("Stage-A process emitted no procfs samples")
    output_identity = workload_output_identity(
        target_id,
        stdout=stdout,
        work_dir=work_dir,
        work_units=work_units,
    )
    compiler_export = json.loads(
        Path(str(build["compiler_sites_path"])).read_text(encoding="utf-8")
    )
    exact_join = join_compiler_runtime_sites(compiler_export, sites)
    write_json(artifact_dir / "runtime-stats.json", stats)
    write_json(artifact_dir / "runtime-sites.json", sites)
    write_json(artifact_dir / "compiler-runtime-exact-join.json", exact_join)
    write_json(artifact_dir / "smaps-samples.json", process["smaps_samples"])
    record = {
        **{key: value for key, value in process.items() if key != "smaps_samples"},
        "target_id": target_id,
        "build_group": build_group,
        "arm": arm.name,
        "runtime_arm_selector": runtime_arm_selector(arm),
        "work_units": work_units,
        "measurement_seconds": measurement_seconds,
        "criterion_warm_up_seconds": (
            criterion_warm_up_seconds
            if target_id not in STAGE_A_FIXED_WORK_UNITS
            else None
        ),
        "runtime_stats": stats,
        "runtime_sites_path": str((artifact_dir / "runtime-sites.json").resolve()),
        "smaps_samples_path": str((artifact_dir / "smaps-samples.json").resolve()),
        "runtime_site_summary": summarize_screening_sites(sites),
        "classification": runtime_classification_summary(
            stats, evidence_stage=evidence_stage
        ),
        "evidence_stage": evidence_stage,
        "stage_a_pressure_basis": (
            "process_wide_requested_generation_bytes"
            if evidence_stage == "stage-a"
            else None
        ),
        "production_pressure_basis": "eligible_exact_site_payload_capacity",
        "cross_clock_accuracy_claim": False,
        "production_accuracy_claim_eligible": False,
        "affinity_pinned": False,
        "measurement_lock_held": False,
        "performance_claim_eligible": False,
        "fragmentation": fragmentation,
        "procfs": summarize_proc_samples(process["smaps_samples"])
        if process["smaps_samples"]
        else None,
        "output_identity": output_identity,
        "compiler_runtime_exact_join": compact_compiler_runtime_exact_join(
            exact_join
        ),
        "compiler_runtime_exact_join_path": str(
            (artifact_dir / "compiler-runtime-exact-join.json").resolve()
        ),
    }
    write_json(artifact_dir / "run.json", record)
    return record


def runtime_hook_smoke_source() -> str:
    return (
        allocator_instrumentation_source().lstrip()
        + r'''

fn main() {
    let owner = Box::new([0x5au8; 4096]);
    std::hint::black_box(&owner[..]);
    let pressure = vec![0xa5u8; 16 * 1024 * 1024];
    std::hint::black_box(&pressure[..]);
    drop(pressure);
    drop(owner);
    println!("UNIALLOC_LIFETIME_SMOKE=ok");
}
'''
    )


def validate_runtime_hook_smoke_admission(
    stats: Mapping[str, Any], sites: Sequence[Mapping[str, Any]]
) -> dict[str, int]:
    admitted = _nonnegative_integer(
        stats, "adaptive_force_track_all_admitted_allocations"
    )
    admitted_requested_bytes = _nonnegative_integer(
        stats, "adaptive_force_track_all_admitted_requested_bytes"
    )
    exact_site_allocations = sum(
        _nonnegative_integer(row, "allocation_count") for row in sites
    )
    decisive_long_rows = [
        row
        for row in sites
        if _nonnegative_integer(row, "long_outcomes") > 0
        and _nonnegative_integer(row, "long_requested_bytes") > 0
        and _nonnegative_integer(row, "maximum_completed_age_bytes")
        >= RUNTIME_LONG_AGE_BYTES
    ]
    decisive_long_outcomes = sum(
        _nonnegative_integer(row, "long_outcomes") for row in decisive_long_rows
    )
    decisive_long_requested_bytes = sum(
        _nonnegative_integer(row, "long_requested_bytes")
        for row in decisive_long_rows
    )
    maximum_completed_age_bytes = max(
        (
            _nonnegative_integer(row, "maximum_completed_age_bytes")
            for row in decisive_long_rows
        ),
        default=0,
    )
    if (
        admitted == 0
        or admitted_requested_bytes == 0
        or not sites
        or exact_site_allocations == 0
        or not decisive_long_rows
        or decisive_long_outcomes == 0
    ):
        raise BuildBlocked(
            "runtime-hook smoke captured no admitted exact Long lifetime site"
        )
    return {
        "admitted_allocations": admitted,
        "admitted_requested_bytes": admitted_requested_bytes,
        "runtime_exact_site_count": len(sites),
        "runtime_exact_site_allocation_count": exact_site_allocations,
        "decisive_long_site_count": len(decisive_long_rows),
        "decisive_long_outcome_count": decisive_long_outcomes,
        "decisive_long_requested_bytes": decisive_long_requested_bytes,
        "minimum_long_age_bytes": RUNTIME_LONG_AGE_BYTES,
        "maximum_completed_age_bytes": maximum_completed_age_bytes,
    }


def compile_run_runtime_hook_smoke(
    *,
    raw_dir: Path,
    snapshot: Mapping[str, Any],
    wrapper: Path,
    jobs: int,
    timeout: int,
    reuse: bool,
) -> dict[str, Any]:
    smoke_root = raw_dir / "runtime-hook-smoke"
    record_path = smoke_root / "smoke.json"
    source = runtime_hook_smoke_source()
    source_sha256 = hashlib.sha256(source.encode()).hexdigest()
    if reuse and record_path.is_file():
        cached = json.loads(record_path.read_text(encoding="utf-8"))
        cached_admission = cached.get("runtime_exact_site_admission", {})
        if (
            cached.get("success") is True
            and cached.get("source_sha256") == source_sha256
            and cached.get("implementation_sha256")
            == snapshot["unialloc_implementation_sha256"]
            and cached.get("runtime_site_count", 0) > 0
            and cached_admission.get("admitted_allocations", 0) > 0
            and cached_admission.get("admitted_requested_bytes", 0) > 0
            and cached_admission.get("runtime_exact_site_count", 0) > 0
            and cached_admission.get("runtime_exact_site_allocation_count", 0) > 0
            and cached_admission.get("decisive_long_site_count", 0) > 0
            and cached_admission.get("decisive_long_outcome_count", 0) > 0
            and cached_admission.get("maximum_completed_age_bytes", 0)
            >= RUNTIME_LONG_AGE_BYTES
        ):
            return cached
    shutil.rmtree(smoke_root, ignore_errors=True)
    (smoke_root / "crate" / "src").mkdir(parents=True)
    manifest = (
        '[package]\nname = "unialloc-lifetime-runtime-smoke"\nversion = "0.0.0"\n'
        'edition = "2024"\npublish = false\n\n[dependencies]\n'
        + _stage_a_dependency(snapshot)
        + "\n\n[workspace]\n"
    )
    manifest_path = smoke_root / "crate" / "Cargo.toml"
    manifest_path.write_text(manifest, encoding="utf-8")
    matrix.add_spin_patch(manifest_path, matrix.find_cached_spin())
    (smoke_root / "crate" / "src" / "main.rs").write_text(source, encoding="utf-8")
    audit_dir = smoke_root / "audits"
    pass_log_dir = smoke_root / "pass-logs"
    target_dir = smoke_root / "target"
    temporary_dir = smoke_root / "tmp"
    for path in (audit_dir, pass_log_dir, target_dir, temporary_dir):
        path.mkdir(parents=True, exist_ok=True)
    env = _compiler_build_environment(
        "all-unknown",
        wrapper=wrapper,
        audit_dir=audit_dir,
        pass_log_dir=pass_log_dir,
        target_dir=target_dir,
        target_crates=("unialloc_lifetime_runtime_smoke",),
        temporary_dir=temporary_dir,
    )
    metadata = matrix.execute(
        ["cargo", f"+{TOOLCHAIN}", "metadata", "--format-version", "1"],
        cwd=smoke_root / "crate",
        env=env,
        timeout=timeout,
    )
    if metadata["exit_code"] != 0 or metadata["timed_out"]:
        raise BuildBlocked(
            "runtime-hook smoke metadata failed: "
            + metadata["stderr"].decode(errors="replace")[-8000:]
        )
    build = matrix.execute(
        [
            "cargo",
            f"+{TOOLCHAIN}",
            "build",
            "--release",
            "--locked",
            "--message-format=json-render-diagnostics",
            "--jobs",
            str(jobs),
        ],
        cwd=smoke_root / "crate",
        env=env,
        timeout=timeout,
    )
    (smoke_root / "build.stdout").write_bytes(build["stdout"])
    (smoke_root / "build.stderr").write_bytes(build["stderr"])
    if build["exit_code"] != 0 or build["timed_out"]:
        raise BuildBlocked(
            "runtime-hook smoke build failed: "
            + build["stderr"].decode(errors="replace")[-8000:]
        )
    binary = _cargo_artifact(build["stdout"], "unialloc-lifetime-runtime-smoke")
    run = matrix.execute(
        [binary],
        cwd=smoke_root,
        env=_runtime_environment(
            ARM_BY_NAME["force-track-all-unknown-diagnostic"]
        ),
        timeout=min(timeout, 120),
    )
    (smoke_root / "run.stdout").write_bytes(run["stdout"])
    (smoke_root / "run.stderr").write_bytes(run["stderr"])
    stderr = run["stderr"].decode(errors="replace")
    stdout = run["stdout"].decode(errors="replace")
    if (
        run["exit_code"] != 0
        or run["timed_out"]
        or "UNIALLOC_LIFETIME_SMOKE=ok" not in stdout
        or runtime_lifetime.RUNTIME_STATS_PREFIX not in stderr
        or runtime_lifetime.RUNTIME_SITE_PREFIX not in stderr
        or FRAGMENTATION_PREFIX not in stderr
    ):
        raise BuildBlocked("runtime-hook smoke did not emit its complete hook contract")
    stats = runtime_lifetime.parse_runtime_stats(stderr)
    assert stats is not None
    sites = runtime_lifetime.parse_runtime_site_rows(stderr)
    validate_runtime_evidence(
        ARM_BY_NAME["force-track-all-unknown-diagnostic"], stats, sites
    )
    admission = validate_runtime_hook_smoke_admission(stats, sites)
    fragmentation = parse_fragmentation(stderr)
    record = {
        "schema_version": 1,
        "success": True,
        "source_sha256": source_sha256,
        "implementation_sha256": snapshot["unialloc_implementation_sha256"],
        "toolchain": TOOLCHAIN,
        "build_command": build["command"],
        "run_command": run["command"],
        "binary": str(binary.resolve()),
        "binary_sha256": sha256_file(binary),
        "runtime_policy": stats["policy"],
        "force_track_all": stats["adaptive_force_track_all"],
        "runtime_site_count": len(sites),
        "runtime_exact_site_admission": admission,
        "fragmentation": fragmentation,
        "compiler_audit": summarize_compiler_prior_audits(audit_dir),
    }
    write_json(record_path, record)
    return record


def _artifact_name(target_id: str) -> str:
    actix_harness = TARGETS["actix_web"].harness_id
    return {
        "oxipng": "oxipng",
        "redb": "unialloc-redb-actix-runner",
        "polars": psr.TARGET_SPECS["polars"].package,
        "swc": str(psr.TARGET_SPECS["swc"].bench_name),
        "rustpython": str(psr.TARGET_SPECS["rustpython"].bench_name),
        "actix_web": redb_actix.ACTIX_BENCHES[actix_harness][1],
    }[target_id]


def _planned_build_command(
    target_id: str, build_group: str, raw_dir: Path, jobs: int
) -> dict[str, Any]:
    worktree = raw_dir / "build-work" / target_id / build_group
    if target_id in {"oxipng", "redb"}:
        command = ["cargo", f"+{TOOLCHAIN}", "build", "--release"]
        if target_id == "oxipng":
            command.extend(["--bin", "oxipng"])
        else:
            command.extend(["--bin", "unialloc-redb-actix-runner"])
    elif target_id == "polars":
        command = [
            "cargo",
            f"+{TOOLCHAIN}",
            "build",
            "--release",
            "--package",
            psr.TARGET_SPECS["polars"].package,
        ]
    elif target_id in {"swc", "rustpython"}:
        spec = psr.TARGET_SPECS[target_id]
        command = [
            "cargo",
            f"+{TOOLCHAIN}",
            "bench",
            "--package",
            spec.package,
            "--bench",
            str(spec.bench_name),
            "--no-run",
        ]
    else:
        package, bench, _selector, _source = redb_actix.ACTIX_BENCHES[
            TARGETS["actix_web"].harness_id
        ]
        command = [
            "cargo",
            f"+{TOOLCHAIN}",
            "bench",
            "--package",
            package,
            "--bench",
            bench,
            "--no-run",
        ]
    return {
        "cwd": str(worktree.resolve()),
        "argv": [
            *command,
            "--locked",
            "--message-format=json-render-diagnostics",
            "--jobs",
            str(jobs),
        ],
    }


def _planned_run_command(
    target_id: str,
    build_group: str,
    *,
    raw_dir: Path,
    input_path: Path | None,
    work_units: Mapping[str, int],
    measurement_seconds: float,
) -> list[str]:
    binary = raw_dir / "binaries" / target_id / build_group / _artifact_name(target_id)
    work_dir = raw_dir / "workloads" / target_id / build_group / "measured"
    if target_id == "oxipng":
        assert input_path is not None
        count = work_units[target_id]
        return [
            str(binary.resolve()),
            "--opt",
            "2",
            "--threads",
            "1",
            "--force",
            "--quiet",
            "--dir",
            str((work_dir / "out").resolve()),
            *(
                str((work_dir / "inputs" / f"issue-141-{index:06d}.png").resolve())
                for index in range(count)
            ),
        ]
    if target_id == "redb":
        return [
            str(binary.resolve()),
            TARGETS[target_id].harness_id,
            str(work_dir.resolve()),
            str(work_units[target_id]),
        ]
    if target_id == "polars":
        return [
            str(binary.resolve()),
            TARGETS[target_id].harness_id,
            str(work_units[target_id]),
        ]
    if target_id in {"swc", "rustpython"}:
        selector = psr.TARGET_SPECS[target_id].harness_filters[
            TARGETS[target_id].harness_id
        ]
    else:
        selector = redb_actix.ACTIX_BENCHES[TARGETS[target_id].harness_id][2]
    return [
        str(binary.resolve()),
        "--bench",
        selector,
        "--warm-up-time",
        "1.0",
        "--measurement-time",
        f"{measurement_seconds:.3f}",
        "--sample-size",
        "10",
        "--noplot",
    ]


def stage_a_preflight(
    *,
    targets: Sequence[str],
    raw_dir: Path,
    checkout_root: Path,
    work_units: Mapping[str, int],
    measurement_seconds: float,
    build_groups: Sequence[str] = BUILD_GROUPS,
    jobs: int = 1,
) -> dict[str, Any]:
    if not build_groups or any(group not in BUILD_GROUPS for group in build_groups):
        raise CampaignContractError("invalid Stage-A build-group selection")
    tools = {
        name: shutil.which(name) for name in ("cargo", "rustc", "git", "nm")
    }
    missing = [name for name, path in tools.items() if path is None]
    if missing:
        raise CampaignContractError("missing Stage-A tools: " + ",".join(missing))
    toolchain = _run_checked(
        ["rustc", f"+{TOOLCHAIN}", "--version", "--verbose"], cwd=ROOT
    ).stdout.decode(errors="replace")
    sources: dict[str, Any] = {}
    source_failures: dict[str, dict[str, Any]] = {}
    for target_id in targets:
        try:
            sources[target_id] = ensure_pinned_checkout(target_id, checkout_root)
        except Exception as error:
            source_failures[target_id] = {
                "status": "source-preflight-failed",
                "error_type": type(error).__name__,
                "reason": str(error),
            }
    oxipng_input = None
    if "oxipng" in sources:
        try:
            oxipng_input = materialize_pinned_oxipng_input(
                Path(sources["oxipng"]["checkout"]),
                raw_dir / "inputs" / "oxipng" / "issue-141.png",
            )
        except Exception as error:
            sources.pop("oxipng", None)
            source_failures["oxipng"] = {
                "status": "source-preflight-failed",
                "error_type": type(error).__name__,
                "reason": str(error),
            }
    build_commands: dict[str, Any] = {}
    run_commands: dict[str, Any] = {}
    for target_id in targets:
        if target_id not in sources:
            blocked = {
                **source_failures[target_id],
                "commands_generated": False,
                "claim_eligible": False,
            }
            build_commands[target_id] = dict(blocked)
            run_commands[target_id] = dict(blocked)
            continue
        build_commands[target_id] = {
            build_group: _planned_build_command(
                target_id, build_group, raw_dir, jobs
            )
            for build_group in build_groups
        }
        run_commands[target_id] = {
            build_group: _planned_run_command(
                target_id,
                build_group,
                raw_dir=raw_dir,
                input_path=(
                    Path(str(oxipng_input["path"]))
                    if oxipng_input is not None
                    else None
                ),
                work_units=work_units,
                measurement_seconds=measurement_seconds,
            )
            for build_group in build_groups
        }
    return {
        "schema_version": 1,
        "success": not source_failures,
        "mode": "stage-a-preflight",
        "execution_performed": False,
        "tools": tools,
        "toolchain": TOOLCHAIN,
        "rustc_version": toolchain,
        "allocator_source": "current-working-tree-unless-allocator-revision-is-set",
        "sources": sources,
        "source_failures": source_failures,
        "oxipng_input": oxipng_input,
        "build_groups": list(build_groups),
        "full_build_group_contract": list(BUILD_GROUPS),
        "build_group_coverage_complete": set(build_groups) == set(BUILD_GROUPS),
        "compiler_prior_environment_by_build_group": {
            group: ({AUTO_PRIOR_ENV: "1"} if group == "compiler-prior" else {})
            for group in build_groups
        },
        "release_symbol_retention_environment": {RELEASE_STRIP_ENV: "false"},
        "activation_proof_requires_retained_symbols": True,
        "runtime_policy": 8,
        "runtime_force_track_all": True,
        "stage_a_pressure_basis": "process_wide_requested_generation_bytes",
        "production_pressure_basis": "eligible_exact_site_payload_capacity",
        "cross_clock_accuracy_claim": False,
        "affinity_pinned": False,
        "measurement_lock_held": False,
        "performance_claim_eligible": False,
        "claim_boundary": (
            "Stage A measures opportunity and prevalence only; production classifier "
            "accuracy requires a non-force Stage-B run on the production pressure clock"
        ),
        "work_units": dict(work_units),
        "fixed_work_calibration_units": dict(STAGE_A_CALIBRATION_UNITS),
        "fixed_work_live_calibration_default": True,
        "coverage_guard": {
            "fixed_work_targets": "same-binary calibration fails on any bypass",
            "criterion_targets": "20-60 second measured run fails on any bypass",
            "predictor_table_capacity": 4096,
            "observation_table_capacity": 4096,
            "maximum_probe_count": 16,
            "overflow_claim_boundary": "target becomes ineligible and later targets continue",
        },
        "single_process_contract": {
            "required": True,
            "guard_kind": "procfs-parent-closure-poll",
            "descendant_processes_allowed": False,
            "claim_boundary": "threaded work stays in one process; observed descendants fail the target",
        },
        "measurement_seconds": measurement_seconds,
        "build_commands": build_commands,
        "run_commands": run_commands,
        "runtime_hook_smoke": {
            "build": {
                "cwd": str((raw_dir / "runtime-hook-smoke" / "crate").resolve()),
                "argv": [
                    "cargo",
                    f"+{TOOLCHAIN}",
                    "build",
                    "--release",
                    "--locked",
                    "--message-format=json-render-diagnostics",
                    "--jobs",
                    str(jobs),
                ],
            },
            "required_markers": [
                runtime_lifetime.RUNTIME_STATS_PREFIX,
                runtime_lifetime.RUNTIME_SITE_PREFIX,
                FRAGMENTATION_PREFIX,
            ],
        },
    }


def _output_comparison_value(target_id: str, identity: Mapping[str, Any]) -> Any:
    if target_id == "oxipng":
        return identity["combined_digest"]
    if target_id == "redb":
        return identity["record"]["output_digest"]
    if target_id == "polars":
        return identity["record"]["fingerprint"]
    return None


def target_static_coverage_state(runs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    if "compiler-prior" not in runs:
        return {
            "status": "complete-static-coverage-not-measured",
            "measurement_status": "not-measured",
            "claim_eligible": False,
            "join": None,
        }
    exact_join = runs["compiler-prior"]["compiler_runtime_exact_join"]
    eligible = exact_join.get("static_coverage_claim_eligible") is True
    return {
        "status": "complete" if eligible else "complete-no-static-coverage",
        "measurement_status": exact_join.get("status"),
        "claim_eligible": eligible,
        "join": exact_join,
    }


def run_stage_a_campaign(
    *,
    targets: Sequence[str],
    raw_dir: Path,
    checkout_root: Path,
    work_units: Mapping[str, int],
    measurement_seconds: float,
    jobs: int,
    build_timeout: int,
    run_timeout: int,
    sample_interval: float,
    reuse: bool,
    allocator_revision: str | None,
    build_groups: Sequence[str] = BUILD_GROUPS,
    keep_build_work: bool = False,
    calibrate_fixed_work: bool = True,
) -> dict[str, Any]:
    preflight = stage_a_preflight(
        targets=targets,
        raw_dir=raw_dir,
        checkout_root=checkout_root,
        work_units=work_units,
        measurement_seconds=measurement_seconds,
        build_groups=build_groups,
        jobs=jobs,
    )
    write_json(raw_dir / "stage-a-preflight.json", preflight)
    snapshot = psr.snapshot_allocator(
        raw_dir,
        revision=allocator_revision,
        current_working_tree=allocator_revision is None,
    )
    wrapper = psr.build_mir_driver(raw_dir, snapshot, build_timeout)
    smoke = compile_run_runtime_hook_smoke(
        raw_dir=raw_dir,
        snapshot=snapshot,
        wrapper=wrapper,
        jobs=jobs,
        timeout=build_timeout,
        reuse=reuse,
    )
    results: dict[str, Any] = {}
    oxipng_input = (
        Path(str(preflight["oxipng_input"]["path"]))
        if preflight.get("oxipng_input")
        else None
    )
    for target_id in targets:
        if target_id not in preflight["sources"]:
            results[target_id] = {
                "status": "source-preflight-failed",
                **preflight["source_failures"][target_id],
                "claim_eligible": False,
            }
            write_json(raw_dir / "stage-a-results.json", {"targets": results})
            continue
        source = preflight["sources"][target_id]
        builds: dict[str, Any] = {}
        build_group: str | None = None
        try:
            for build_group in build_groups:
                builds[build_group] = build_stage_a_binary(
                    target_id,
                    build_group,
                    checkout=Path(source["checkout"]),
                    raw_dir=raw_dir,
                    snapshot=snapshot,
                    wrapper=wrapper,
                    jobs=jobs,
                    timeout=build_timeout,
                    reuse=reuse,
                    keep_build_work=keep_build_work,
                )
        except Exception as error:
            cleanup: dict[str, Any] = {"performed": False, "paths": []}
            if not keep_build_work:
                for path in (
                    raw_dir / "build-work" / target_id,
                    raw_dir / "cargo-targets" / target_id,
                ):
                    shutil.rmtree(path, ignore_errors=True)
                    cleanup["paths"].append(str(path.resolve()))
                cleanup["performed"] = True
            failure_dir = (
                raw_dir
                / "builds"
                / target_id
                / (build_group if build_group is not None else "prepare")
            )
            write_json(failure_dir / "failure-cleanup.json", cleanup)
            results[target_id] = {
                "status": "blocked_by_real_build_failure",
                "error_type": type(error).__name__,
                "reason": str(error),
                "completed_build_groups": sorted(builds),
                "failure_cleanup": cleanup,
                "claim_eligible": False,
            }
            write_json(raw_dir / "stage-a-results.json", {"targets": results})
            continue
        try:
            runs: dict[str, Any] = {}
            fixed_units = work_units.get(target_id)
            calibration: dict[str, Any] | None = None
            if target_id in STAGE_A_CALIBRATION_UNITS and calibrate_fixed_work:
                calibration_group = build_groups[0]
                seed_units = STAGE_A_CALIBRATION_UNITS[target_id]
                calibration_run = run_stage_a_sample(
                    target_id,
                    calibration_group,
                    build=builds[calibration_group],
                    raw_dir=raw_dir,
                    input_path=oxipng_input if target_id == "oxipng" else None,
                    work_units=seed_units,
                    measurement_seconds=measurement_seconds,
                    timeout=run_timeout,
                    sample_interval=sample_interval,
                    validate_duration=False,
                    run_label="calibration",
                )
                derived = derive_work_units(
                    Calibration(
                        source="same-binary-live-stage-a-calibration",
                        work_units=seed_units,
                        elapsed_seconds=float(calibration_run["wall_seconds"]),
                        peak_rss_kib=(
                            calibration_run["procfs"]["peak_rss_kib"]
                            if calibration_run.get("procfs")
                            else None
                        ),
                        claim_grade=False,
                        note=(
                            "Calibration only; the following 20-60 second run "
                            "is measured."
                        ),
                    ),
                    target_seconds=DEFAULT_SCREEN_TARGET_SECONDS,
                    granularity=TARGETS[target_id].work_granularity,
                )
                fixed_units = int(derived["work_units"])
                calibration = {
                    "build_group": calibration_group,
                    "seed_work_units": seed_units,
                    "run": calibration_run,
                    "derivation": derived,
                }
            duration_retry: dict[str, Any] | None = None
            for group_index, build_group in enumerate(build_groups):
                fixed_target = target_id in STAGE_A_FIXED_WORK_UNITS
                if fixed_target and group_index == 0:
                    first_run = run_stage_a_sample(
                        target_id,
                        build_group,
                        build=builds[build_group],
                        raw_dir=raw_dir,
                        input_path=oxipng_input if target_id == "oxipng" else None,
                        work_units=fixed_units,
                        measurement_seconds=measurement_seconds,
                        timeout=run_timeout,
                        sample_interval=sample_interval,
                        validate_duration=False,
                    )
                    try:
                        validate_stage_duration(
                            "screening", float(first_run["wall_seconds"])
                        )
                        runs[build_group] = first_run
                    except CampaignContractError as first_error:
                        retry_derivation = derive_work_units(
                            Calibration(
                                source="same-binary-measured-duration-retry",
                                work_units=int(fixed_units),
                                elapsed_seconds=float(first_run["wall_seconds"]),
                                peak_rss_kib=(
                                    first_run["procfs"]["peak_rss_kib"]
                                    if first_run.get("procfs")
                                    else None
                                ),
                                claim_grade=False,
                                note="One bounded retry after the first measured duration missed 20-60 seconds.",
                            ),
                            target_seconds=DEFAULT_SCREEN_TARGET_SECONDS,
                            granularity=TARGETS[target_id].work_granularity,
                        )
                        retry_units = int(retry_derivation["work_units"])
                        if retry_units == fixed_units:
                            raise CampaignContractError(
                                "fixed-work duration retry cannot change the work count"
                            ) from first_error
                        fixed_units = retry_units
                        retry_run = run_stage_a_sample(
                            target_id,
                            build_group,
                            build=builds[build_group],
                            raw_dir=raw_dir,
                            input_path=(
                                oxipng_input if target_id == "oxipng" else None
                            ),
                            work_units=fixed_units,
                            measurement_seconds=measurement_seconds,
                            timeout=run_timeout,
                            sample_interval=sample_interval,
                            validate_duration=True,
                            run_label="measured-retry",
                        )
                        runs[build_group] = retry_run
                        duration_retry = {
                            "performed": True,
                            "first_work_units": first_run["work_units"],
                            "first_wall_seconds": first_run["wall_seconds"],
                            "first_failure": str(first_error),
                            "derivation": retry_derivation,
                            "retry_work_units": fixed_units,
                            "retry_wall_seconds": retry_run["wall_seconds"],
                        }
                else:
                    runs[build_group] = run_stage_a_sample(
                        target_id,
                        build_group,
                        build=builds[build_group],
                        raw_dir=raw_dir,
                        input_path=oxipng_input if target_id == "oxipng" else None,
                        work_units=fixed_units,
                        measurement_seconds=measurement_seconds,
                        timeout=run_timeout,
                        sample_interval=sample_interval,
                    )
            output_equivalent: bool | None = None
            if target_id in STAGE_A_FIXED_WORK_UNITS and set(build_groups) == set(
                BUILD_GROUPS
            ):
                output_equivalent = _output_comparison_value(
                    target_id, runs["all-unknown"]["output_identity"]
                ) == _output_comparison_value(
                    target_id, runs["compiler-prior"]["output_identity"]
                )
                if not output_equivalent:
                    raise CampaignContractError(
                        f"{target_id} output differs between compiler build groups"
                    )
            gate_source_group = (
                "compiler-prior" if "compiler-prior" in runs else build_groups[0]
            )
            gate = opportunity_gate(runs[gate_source_group]["runtime_site_summary"])
            static_coverage = target_static_coverage_state(runs)
            results[target_id] = {
                "status": static_coverage["status"],
                "static_coverage_measurement_status": static_coverage[
                    "measurement_status"
                ],
                "static_coverage_claim_eligible": static_coverage[
                    "claim_eligible"
                ],
                "source": source,
                "builds": builds,
                "runs": runs,
                "output_equivalent": output_equivalent,
                "opportunity_gate": gate,
                "opportunity_gate_source_build_group": gate_source_group,
                "fixed_work_calibration": calibration,
                "fixed_work_duration_retry": duration_retry,
            }
        except Exception as error:
            results[target_id] = {
                "status": "ineligible_after_real_runtime_guard",
                "error_type": type(error).__name__,
                "reason": str(error),
                "builds": builds,
                "partial_runs": runs,
                "claim_eligible": False,
            }
        write_json(raw_dir / "stage-a-results.json", {"targets": results})
    completed = sum(
        row.get("status")
        in {
            "complete",
            "complete-no-static-coverage",
            "complete-static-coverage-not-measured",
        }
        for row in results.values()
    )
    final = {
        "schema_version": 1,
        "campaign": "rust-lifetime-prior-six-program-stage-a-v1",
        "success": completed == len(targets),
        "classification": "diagnostic",
        "stage_a_pressure_basis": "process_wide_requested_generation_bytes",
        "production_pressure_basis": "eligible_exact_site_payload_capacity",
        "cross_clock_accuracy_claim": False,
        "accuracy_claim_eligible": False,
        "affinity_pinned": False,
        "measurement_lock_held": False,
        "performance_claim_eligible": False,
        "allocator_snapshot": snapshot,
        "runtime_hook_smoke": smoke,
        "evaluator_provenance": campaign_evaluator_provenance(),
        "selected_targets": list(targets),
        "measured_build_groups": list(build_groups),
        "full_build_group_contract": list(BUILD_GROUPS),
        "build_group_coverage_complete": set(build_groups) == set(BUILD_GROUPS),
        "completed_target_count": completed,
        "blocked_target_count": len(targets) - completed,
        "targets": results,
    }
    write_json(raw_dir / "stage-a-results.json", final)
    return final


def build_campaign_manifest() -> dict[str, Any]:
    validate_arm_contract()
    validate_target_contracts()
    targets: dict[str, Any] = {}
    for target_id in TARGET_ORDER:
        target = TARGETS[target_id]
        targets[target_id] = {
            **asdict(target),
            "runner": str(target.runner),
            "runner_sha256": sha256_file(target.runner),
            "target_crates": list(target.target_crates),
            "calibration": calibration_record(target),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign": "rust-lifetime-prior-six-program-v1",
        "classification": "diagnostic-current-worktree-until-pinned",
        "stages": {
            "screening": {
                "arms": [arm.name for arm in SCREENING_ARMS],
                "build_groups": list(BUILD_GROUPS),
                "minimum_seconds": DEFAULT_SCREEN_MIN_SECONDS,
                "target_seconds": DEFAULT_SCREEN_TARGET_SECONDS,
                "maximum_seconds": DEFAULT_SCREEN_MAX_SECONDS,
                "force_track": True,
                "stage_a_pressure_basis": "process_wide_requested_generation_bytes",
                "production_pressure_basis": "eligible_exact_site_payload_capacity",
                "cross_clock_accuracy_claim": False,
                "opportunity_gate": {
                    "minimum_long_requested_bytes": DEFAULT_LONG_COHORT_BYTES,
                    "minimum_confirmed_short_requested_bytes": (
                        DEFAULT_CONFIRMED_SHORT_BYTES
                    ),
                },
            },
            "performance": {
                "arms": [arm.name for arm in PERFORMANCE_ARMS],
                "minimum_seconds": DEFAULT_PERFORMANCE_MIN_SECONDS,
                "target_seconds": DEFAULT_PERFORMANCE_TARGET_SECONDS,
                "maximum_seconds": DEFAULT_PERFORMANCE_MAX_SECONDS,
                "admission": "screening opportunity gate passed",
            },
        },
        "hard_process_cap_seconds": HARD_PROCESS_CAP_SECONDS,
        "compiler_prior_environment": AUTO_PRIOR_ENV,
        "runtime_arm_environment": RUNTIME_ARM_ENV,
        "generated_source_contracts": {
            "runtime_instrumentation_sha256": hashlib.sha256(
                runtime_instrumentation_rust().encode()
            ).hexdigest(),
            "redb_fixed_repeat_source_sha256": hashlib.sha256(
                amplified_redb_source().encode()
            ).hexdigest(),
            "polars_persistent_frame_source_sha256": hashlib.sha256(
                amplified_polars_source().encode()
            ).hexdigest(),
        },
        "screening_command_templates": {
            "oxipng": [
                "BINARY",
                "--opt",
                "2",
                "--threads",
                "1",
                "--force",
                "--quiet",
                "--dir",
                "OUT_DIR",
                "PINNED_INPUT_COPY...",
            ],
            "redb": ["BINARY", "delete_reinsert", "WORK_DIR", "WORK_UNITS"],
            "polars": ["BINARY", "filter_retain", "WORK_UNITS"],
            "swc": [
                "BINARY",
                "--bench",
                psr.TARGET_SPECS["swc"].harness_filters["large_fixer"],
                "--warm-up-time",
                "1.0",
                "--measurement-time",
                "30.000",
                "--sample-size",
                "10",
                "--noplot",
            ],
            "rustpython": [
                "BINARY",
                "--bench",
                psr.TARGET_SPECS["rustpython"].harness_filters[
                    "execute_mandelbrot"
                ],
                "--warm-up-time",
                "1.0",
                "--measurement-time",
                "30.000",
                "--sample-size",
                "10",
                "--noplot",
            ],
            "actix_web": [
                "BINARY",
                "--bench",
                redb_actix.ACTIX_BENCHES[TARGETS["actix_web"].harness_id][2],
                "--warm-up-time",
                "1.0",
                "--measurement-time",
                "30.000",
                "--sample-size",
                "10",
                "--noplot",
            ],
        },
        "arms": [
            {**asdict(arm), "build_environment": arm_build_environment(arm)}
            for arm in ARMS
        ],
        "targets": targets,
        "evidence_contract": {
            "fixed_work_required": True,
            "output_digest_required": True,
            "compiler_audit_digest_required": True,
            "runtime_stats_required": True,
            "runtime_exact_sites_required": True,
            "fragmentation_record_required": True,
            "proc_smaps_rollup_each_sample_required": True,
            "thp_causal_pairs_fail_closed": True,
            "force_track_diagnostic_excluded_from_performance": True,
        },
        "performance_blockers": {
            target_id: target.performance_blocker
            for target_id, target in TARGETS.items()
            if target.performance_blocker is not None
        },
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "evaluation/raw/lifetime-prior-six-program/manifest.json",
    )
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--stage-a", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--targets", default=",".join(TARGET_ORDER))
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=ROOT / "evaluation/raw/lifetime-prior-six-program-stage-a",
    )
    parser.add_argument(
        "--checkout-root",
        type=Path,
        default=ROOT / "evaluation/external/_checkouts",
    )
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument(
        "--build-groups",
        default=",".join(BUILD_GROUPS),
        help=(
            "comma-separated Stage-A build groups; compiler-prior alone is the "
            "space-saving opportunity screen"
        ),
    )
    parser.add_argument(
        "--keep-build-work",
        action="store_true",
        help="retain this campaign's disposable copied sources and Cargo targets",
    )
    parser.add_argument(
        "--skip-fixed-work-calibration",
        action="store_true",
        help="use --work-units literally; measured duration still fails closed at 20-60s",
    )
    parser.add_argument("--jobs", type=int, default=min(16, os.cpu_count() or 1))
    parser.add_argument("--build-timeout", type=int, default=3600)
    parser.add_argument("--run-timeout", type=int, default=600)
    parser.add_argument("--measurement-seconds", type=float, default=30.0)
    parser.add_argument("--sample-interval", type=float, default=0.5)
    parser.add_argument(
        "--work-units",
        action="append",
        default=[],
        metavar="TARGET=COUNT",
        help="override an amplified fixed-work count for oxipng, redb, or polars",
    )
    parser.add_argument(
        "--allocator-revision",
        help="freeze an exact commit; the current allocator/pass worktree is the default",
    )
    args = parser.parse_args(argv)
    targets = tuple(part.strip() for part in args.targets.split(",") if part.strip())
    unknown = sorted(set(targets) - set(TARGET_ORDER))
    if not targets or unknown:
        parser.error("unknown or empty targets: " + ",".join(unknown))
    build_groups = tuple(
        part.strip() for part in args.build_groups.split(",") if part.strip()
    )
    unknown_groups = sorted(set(build_groups) - set(BUILD_GROUPS))
    if not build_groups or unknown_groups or len(set(build_groups)) != len(build_groups):
        parser.error("unknown, empty, or duplicate build groups: " + ",".join(unknown_groups))
    work_units = dict(STAGE_A_FIXED_WORK_UNITS)
    for raw in args.work_units:
        target_id, separator, count_text = raw.partition("=")
        if not separator or target_id not in STAGE_A_FIXED_WORK_UNITS:
            parser.error(f"invalid --work-units value: {raw}")
        try:
            count = int(count_text)
        except ValueError:
            parser.error(f"invalid --work-units count: {raw}")
        if count <= 0:
            parser.error(f"invalid --work-units count: {raw}")
        work_units[target_id] = count
    if args.dry_run and not args.stage_a:
        parser.error("--dry-run requires --stage-a")
    if (
        args.jobs <= 0
        or args.build_timeout <= 0
        or not 0 < args.run_timeout <= HARD_PROCESS_CAP_SECONDS
        or not DEFAULT_SCREEN_MIN_SECONDS
        <= args.measurement_seconds
        <= DEFAULT_SCREEN_MAX_SECONDS
        or args.sample_interval <= 0
    ):
        parser.error("invalid Stage-A resource or duration boundary")
    args.targets = targets
    args.build_groups = build_groups
    args.work_units = work_units
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.stage_a:
        raw_dir = args.raw_dir.resolve()
        raw_dir.mkdir(parents=True, exist_ok=True)
        if args.dry_run:
            plan = stage_a_preflight(
                targets=args.targets,
                raw_dir=raw_dir,
                checkout_root=args.checkout_root.resolve(),
                work_units=args.work_units,
                measurement_seconds=args.measurement_seconds,
                build_groups=args.build_groups,
                jobs=args.jobs,
            )
            path = raw_dir / "stage-a-preflight.json"
            write_json(path, plan)
            print(
                json.dumps(
                    {
                        "success": plan["success"],
                        "dry_run": True,
                        "preflight": str(path.resolve()),
                        "targets": list(args.targets),
                    },
                    sort_keys=True,
                )
            )
            return 0 if plan["success"] else 2
        result = run_stage_a_campaign(
            targets=args.targets,
            raw_dir=raw_dir,
            checkout_root=args.checkout_root.resolve(),
            work_units=args.work_units,
            measurement_seconds=args.measurement_seconds,
            jobs=args.jobs,
            build_timeout=args.build_timeout,
            run_timeout=args.run_timeout,
            sample_interval=args.sample_interval,
            reuse=args.reuse,
            allocator_revision=args.allocator_revision,
            build_groups=args.build_groups,
            keep_build_work=args.keep_build_work,
            calibrate_fixed_work=not args.skip_fixed_work_calibration,
        )
        print(
            json.dumps(
                {
                    "success": result["success"],
                    "result": str((raw_dir / "stage-a-results.json").resolve()),
                    "completed_target_count": result["completed_target_count"],
                    "blocked_target_count": result["blocked_target_count"],
                },
                sort_keys=True,
            )
        )
        return 0 if result["success"] else 2
    manifest = build_campaign_manifest()
    if args.describe:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    write_json(args.output.resolve(), manifest)
    print(json.dumps({"manifest": str(args.output.resolve()), "success": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
