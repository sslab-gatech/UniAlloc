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
AUTO_PRIOR_ENV = "UNIALLOC_AUTO_RUST_LIFETIME_PRIOR"
RUNTIME_ARM_ENV = "UNIALLOC_LIFETIME_EXPERIMENT_ARM"
FRAGMENTATION_PREFIX = "UNIALLOC_LIFETIME_FRAGMENTATION="
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
    "automatic_rust_lifetime_prior_return_long": (2, 70),
    "automatic_rust_lifetime_prior_escape_long": (2, 70),
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
            harness_id="router_actix",
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


def stage_a_target_crates(target_id: str) -> tuple[str, ...]:
    if target_id == "oxipng":
        return ("oxipng",)
    if target_id == "actix_web":
        return ("router", "actix_router")
    return TARGETS[target_id].target_crates


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
    for row in rows:
        requested_size = _nonnegative_integer(row, "requested_size")
        if requested_size == 0:
            raise CampaignContractError("screening site has zero requested size")
        row_long = _nonnegative_integer(row, "long_requested_bytes")
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
        provenance_valid = (
            actual_requested is True
            and actual_rewrite is True
            and body_clone is True
            and continue_compilation is True
        )
        if not provenance_valid:
            provenance_failures.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "crate_name": crate_name,
                    "actual_semantic_scope_rewrite_requested": actual_requested,
                    "actual_semantic_scope_rewrite": actual_rewrite,
                    "body_clone_returned_to_rustc": body_clone,
                    "continue_compilation": continue_compilation,
                }
            )
        if crate_name:
            state = crate_provenance.setdefault(
                crate_name,
                {"audit_file_count": 0, "valid_file_count": 0},
            )
            state["audit_file_count"] += 1
            state["valid_file_count"] += int(provenance_valid)
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
            }
        )
        for raw in document["rewrite_candidates"]:
            if not isinstance(raw, dict):
                raise CampaignContractError(f"audit candidate is invalid: {path}")
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
            applied_allocation = rewrite_status in {
                "actual_allocator_call_replacement_applied",
                "actual_semantic_scope_enter_exit_rewrite_applied",
            }
            if applied_allocation:
                applied_allocation_candidates += 1
                if basis.startswith("automatic_rust_lifetime_prior_") and hint in {
                    1,
                    2,
                }:
                    applied_prior_hinted_candidates += 1
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
        or crate_provenance[crate].get("valid_file_count", 0) <= 0
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
    *, binary: Path, selector: str, measurement_seconds: float
) -> list[str]:
    if not binary.is_file():
        raise CampaignContractError(f"Criterion binary is missing: {binary}")
    if not (
        DEFAULT_SCREEN_MIN_SECONDS
        <= measurement_seconds
        <= DEFAULT_SCREEN_MAX_SECONDS
    ):
        raise CampaignContractError("Criterion screening time is outside 20--60 seconds")
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


def screening_command(
    target_id: str,
    *,
    binary: Path,
    work_dir: Path,
    work_units: int | None = None,
    measurement_seconds: float = 30.0,
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
        )
    if target_id == "actix_web":
        _package, _bench, selector, _source = redb_actix.ACTIX_BENCHES[
            target.harness_id
        ]
        return criterion_screening_command(
            binary=binary,
            selector=selector,
            measurement_seconds=measurement_seconds,
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
    path.write_text(
        allocator_instrumentation_source().lstrip() + "\n" + text_value.lstrip(),
        encoding="utf-8",
    )


def _stage_a_dependency(snapshot: Mapping[str, Any]) -> str:
    return matrix.cargo_path_dependency(
        Path(str(snapshot["path"])) / "unialloc",
        ("lifetime_hugepage",),
    )


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
        shutil.rmtree(worktree, ignore_errors=True)
        redb_source = worktree / "redb-source"
        _copy_checkout(checkout, redb_source)
        patched_manifests = _inject_workspace_dependencies(
            redb_source,
            stage_a_target_crates(target_id),
            dependency,
        )
        (worktree / "src").mkdir(parents=True, exist_ok=True)
        manifest = (
            '[package]\nname = "unialloc-redb-actix-runner"\nversion = "0.1.0"\n'
            'edition = "2024"\nrust-version = "1.89"\npublish = false\n\n'
            "[dependencies]\n"
            f"redb = {{ path = {json.dumps(str(redb_source.resolve()))} }}\n"
            f"{dependency}\n\n[workspace]\n"
        )
        (worktree / "Cargo.toml").write_text(manifest, encoding="utf-8")
        matrix.add_spin_patch(worktree / "Cargo.toml", spin)
        (worktree / "src" / "main.rs").write_text(
            amplified_redb_source(), encoding="utf-8"
        )
        build_command = ["cargo", f"+{TOOLCHAIN}", "build", "--release"]
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


def export_compiler_site_features(audit_root: Path) -> dict[str, Any]:
    """Export joinable compiler features without changing the runtime predictor key."""
    rows: list[dict[str, Any]] = []
    complete_keys: set[tuple[int, ...]] = set()
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
            if rewrite_status not in {
                "actual_allocator_call_replacement_applied",
                "actual_semantic_scope_enter_exit_rewrite_applied",
            }:
                continue
            features = candidate.get("lifetime_analysis_features")
            if not isinstance(features, dict):
                continue
            raw_key = features.get("runtime_join_key")
            if not isinstance(raw_key, dict):
                continue
            key = {
                "callsite": raw_key.get("callsite"),
                "type_id": raw_key.get("type_id"),
                "module_id": raw_key.get("module_id"),
                "requested_size": raw_key.get("requested_size_bytes"),
                "align": raw_key.get("requested_align_bytes"),
            }
            complete = features.get("runtime_join_key_complete") is True and all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in key.values()
            )
            if complete:
                exact_key = tuple(
                    int(key[field]) for field in runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
                )
                if exact_key in complete_keys:
                    raise CampaignContractError(
                        "compiler audit contains a duplicate exact runtime tuple"
                    )
                complete_keys.add(exact_key)
            rows.append(
                {
                    **key,
                    "runtime_join_key_complete": complete,
                    "audit_file": path.relative_to(audit_root).as_posix(),
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
                    "lifetime_analysis_features": features,
                }
            )
    return {
        "source": "unialloc-compiler-runtime-exact-site-export-v1",
        "runtime_join_key": list(runtime_lifetime.RUNTIME_SITE_KEY_FIELDS),
        "site_count": len(rows),
        "complete_join_key_count": sum(
            int(row["runtime_join_key_complete"]) for row in rows
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
        "rows": rows,
    }


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
        if (
            cached.get("success") is True
            and cached.get("implementation_sha256")
            == snapshot["unialloc_implementation_sha256"]
            and cached.get("source_commit") == TARGETS[target_id].source_commit
            and cached.get("toolchain") == TOOLCHAIN
            and cached.get("runtime_instrumentation_sha256")
            == instrumentation_sha256
            and cached.get("generated_source_sha256")
            == expected_generated_source_sha256
            and cached.get("automatic_rust_lifetime_prior")
            == (build_group == "compiler-prior")
            and binary.is_file()
            and cached.get("binary_sha256") == sha256_file(binary)
            and compiler_sites_path.is_file()
            and cached.get("compiler_sites_sha256")
            == sha256_file(compiler_sites_path)
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
        "toolchain": TOOLCHAIN,
        "runtime_instrumentation_sha256": instrumentation_sha256,
        "generated_source_sha256": prepared["generated_source_sha256"],
        "implementation_revision": snapshot.get("allocator_revision"),
        "implementation_sha256": snapshot["unialloc_implementation_sha256"],
        "campaign_snapshot_sha256": snapshot.get("campaign_snapshot_sha256"),
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
        "compiler_sites": {
            key: compiler_sites[key]
            for key in (
                "source",
                "runtime_join_key",
                "site_count",
                "complete_join_key_count",
                "applied_prior_hinted_count",
            )
        },
        "compiler_sites_path": str((build_dir / "compiler-sites.json").resolve()),
        "compiler_sites_sha256": sha256_file(build_dir / "compiler-sites.json"),
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
            RUNTIME_ARM_ENV: arm.name,
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


def runtime_classification_summary(stats: Mapping[str, Any]) -> dict[str, Any]:
    tp = _nonnegative_integer(stats, "static_hint_tp")
    tn = _nonnegative_integer(stats, "static_hint_tn")
    fp = _nonnegative_integer(stats, "static_hint_fp")
    fn = _nonnegative_integer(stats, "static_hint_fn")
    abstained = _nonnegative_integer(stats, "static_hint_abstained")
    decisive = tp + tn + fp + fn
    return {
        "static_hint_true_positive": tp,
        "static_hint_true_negative": tn,
        "static_hint_false_positive": fp,
        "static_hint_false_negative": fn,
        "static_hint_abstained": abstained,
        "static_hint_decisive": decisive,
        "stage_a_pressure_clock_accuracy": (tp + tn) / decisive if decisive else None,
        "stage_a_pressure_clock_long_precision": tp / (tp + fp) if tp + fp else None,
        "stage_a_pressure_clock_long_recall": tp / (tp + fn) if tp + fn else None,
        "production_accuracy": None,
        "production_accuracy_claim_eligible": False,
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


def join_compiler_runtime_sites(
    compiler_export: Mapping[str, Any], runtime_rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    key_fields = runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
    runtime_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in runtime_rows:
        key = tuple(row[field] for field in key_fields)
        if key in runtime_by_key:
            raise CampaignContractError(
                "runtime observation contains a duplicate exact site tuple"
            )
        runtime_by_key[key] = dict(row)
    joined: list[dict[str, Any]] = []
    incomplete = 0
    applied_prior_total = 0
    applied_prior_incomplete = 0
    compiler_keys: set[tuple[Any, ...]] = set()
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
        if compiler.get("runtime_join_key_complete") is not True:
            incomplete += 1
            applied_prior_incomplete += int(applied_prior)
            continue
        key = tuple(compiler[field] for field in key_fields)
        if key in compiler_keys:
            raise CampaignContractError(
                "compiler export contains a duplicate exact site tuple"
            )
        compiler_keys.add(key)
        runtime = runtime_by_key.get(key)
        joined.append(
            {
                "join_key": dict(zip(key_fields, key, strict=True)),
                "matched": runtime is not None,
                "applied_prior_hint": applied_prior,
                "compiler": compiler,
                "runtime": runtime,
            }
        )
    matched = sum(int(row["matched"]) for row in joined)
    complete = len(joined)
    generic_status = (
        "no-generic-rewrite-coverage"
        if matched == 0
        else (
            "complete-generic-rewrite-coverage"
            if matched == complete
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
    return {
        "source": "unialloc-compiler-runtime-exact-site-join-v1",
        "status": static_status,
        "generic_rewrite_status": generic_status,
        "compiler_complete_key_count": len(joined),
        "compiler_incomplete_key_count": incomplete,
        "matched_site_count": matched,
        "unmatched_site_count": complete - matched,
        "match_coverage": matched / complete if complete else 0.0,
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
        "static_coverage_claim_eligible": applied_prior_matched > 0,
        "generic_runtime_join_rewrite_success": matched > 0,
        "runtime_join_rewrite_success": matched > 0,
        "runtime_site_count": len(runtime_rows),
        "rows": joined,
    }


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
) -> dict[str, Any]:
    arm = ARM_BY_NAME[STAGE_A_ARM_BY_BUILD_GROUP[build_group]]
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
        input_path=input_path,
    )
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
            f"{target_id}/{build_group} Stage-A process failed: {stderr[-8000:]}"
        )
    if process["single_process_guard"]["passed"] is not True:
        raise CampaignContractError(
            f"{target_id}/{build_group} spawned descendant processes; "
            "Stage-A requires one persistent process"
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
        "work_units": work_units,
        "measurement_seconds": measurement_seconds,
        "runtime_stats": stats,
        "runtime_sites_path": str((artifact_dir / "runtime-sites.json").resolve()),
        "runtime_site_summary": summarize_screening_sites(sites),
        "classification": runtime_classification_summary(stats),
        "stage_a_pressure_basis": "process_wide_requested_generation_bytes",
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
        "compiler_runtime_exact_join": {
            key: exact_join[key]
            for key in (
                "source",
                "status",
                "compiler_complete_key_count",
                "compiler_incomplete_key_count",
                "matched_site_count",
                "unmatched_site_count",
                "match_coverage",
                "runtime_join_rewrite_success",
                "runtime_site_count",
            )
        },
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
        if (
            cached.get("success") is True
            and cached.get("source_sha256") == source_sha256
            and cached.get("implementation_sha256")
            == snapshot["unialloc_implementation_sha256"]
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
        "fragmentation": fragmentation,
        "compiler_audit": summarize_compiler_prior_audits(audit_dir),
    }
    write_json(record_path, record)
    return record


def _artifact_name(target_id: str) -> str:
    return {
        "oxipng": "oxipng",
        "redb": "unialloc-redb-actix-runner",
        "polars": psr.TARGET_SPECS["polars"].package,
        "swc": str(psr.TARGET_SPECS["swc"].bench_name),
        "rustpython": str(psr.TARGET_SPECS["rustpython"].bench_name),
        "actix_web": redb_actix.ACTIX_BENCHES["router_actix"][1],
    }[target_id]


def _planned_build_command(
    target_id: str, build_group: str, raw_dir: Path, jobs: int
) -> dict[str, Any]:
    worktree = raw_dir / "build-work" / target_id / build_group
    if target_id in {"oxipng", "redb"}:
        command = ["cargo", f"+{TOOLCHAIN}", "build", "--release"]
        if target_id == "oxipng":
            command.extend(["--bin", "oxipng"])
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
        package, bench, _selector, _source = redb_actix.ACTIX_BENCHES["router_actix"]
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
                redb_actix.ACTIX_BENCHES["router_actix"][2],
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
