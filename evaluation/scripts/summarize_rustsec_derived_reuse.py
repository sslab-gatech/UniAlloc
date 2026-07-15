#!/usr/bin/env python3
"""Build fail-closed summaries for bounded RustSec derived-reuse evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any, Mapping, NamedTuple, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[2]
ARCHIVES = ("vulnerable", "patched")
ALLOCATOR_ORDER = {
    name: index
    for index, name in enumerate(("system", "unialloc", "typed_plain", "typeiso"))
}


class SummaryProfile(NamedTuple):
    """Static contract for one reproducible summary shape."""

    advisory_ids: Mapping[str, str]
    boundary: str
    case_ids: tuple[str, ...]
    catalog_count_contract: Mapping[str, int]
    catalog_path: pathlib.Path
    catalog_source: str
    default_experiments: Mapping[str, pathlib.Path]
    expected_variants: Mapping[str, tuple[str, ...]]
    source: str
    typed_allocator_expected_oracle: Mapping[str, bool]


PROFILES = {
    "rsh002": SummaryProfile(
        advisory_ids={"RSH-002": "RUSTSEC-2020-0007"},
        boundary=(
            "This result validates one manually attributed, layout-bounded "
            "BitVec-to-foreign-object reuse decision. Automatic compiler coverage "
            "of the vulnerable BitVec sites and complete source-level detection "
            "remain outside this result."
        ),
        case_ids=("RSH-002",),
        catalog_count_contract={
            "derived_reuse_scenario_count": 2,
            "repository_scenario_count": 28,
        },
        catalog_path=ROOT / "evaluation/config/rustsec_heap_harnesses.json",
        catalog_source="unialloc-rustsec-heap-harness-bundle",
        default_experiments={
            "RSH-002": ROOT
            / "evaluation/raw/rsh002-derived-final-current-20260715/experiment.json"
        },
        expected_variants={
            "RSH-002": ("system", "typed_plain", "typeiso"),
        },
        source="unialloc-rustsec-rsh002-derived-reuse-summary",
        typed_allocator_expected_oracle={"RSH-002": False},
    ),
    "expansion": SummaryProfile(
        advisory_ids={
            "RSH-041": "RUSTSEC-2026-0152",
            "RSH-042": "RUSTSEC-2026-0128",
        },
        boundary=(
            "These results validate two manually attributed, layout-bounded "
            "A-to-free-to-B reuse decisions. They do not validate automatic compiler "
            "coverage, complete source-vulnerability detection, stale-pointer "
            "elimination, or general UAF mitigation."
        ),
        case_ids=("RSH-041", "RSH-042"),
        catalog_count_contract={
            "derived_reuse_scenario_count": 2,
            "repository_scenario_count": 7,
        },
        catalog_path=(
            ROOT / "evaluation/config/rustsec_heap_expansion_harnesses.json"
        ),
        catalog_source="unialloc-rustsec-heap-expansion-harnesses",
        default_experiments={
            case_id: ROOT
            / (
                "docs/evidence/rustsec-security-expansion-20260714/raw/derived/"
                f"{case_id}-derived-reuse-experiment.json"
            )
            for case_id in ("RSH-041", "RSH-042")
        },
        expected_variants={
            "RSH-041": ("system", "typed_plain", "typeiso"),
            "RSH-042": ("system", "unialloc", "typed_plain", "typeiso"),
        },
        source="unialloc-rustsec-derived-reuse-expansion-summary",
        typed_allocator_expected_oracle={
            "RSH-041": True,
            "RSH-042": True,
        },
    ),
    "rsh008": SummaryProfile(
        advisory_ids={"RSH-008": "RUSTSEC-2021-0130"},
        boundary=(
            "This result validates one manually attributed, layout-bounded "
            "A-to-free-to-B reuse decision. Automatic victim-site coverage and "
            "complete source-level UAF detection remain outside this result."
        ),
        case_ids=("RSH-008",),
        catalog_count_contract={
            "derived_reuse_scenario_count": 2,
            "repository_scenario_count": 28,
        },
        catalog_path=ROOT / "evaluation/config/rustsec_heap_harnesses.json",
        catalog_source="unialloc-rustsec-heap-harness-bundle",
        default_experiments={
            "RSH-008": ROOT
            / "evaluation/raw/rsh008-derived-final-current-20260715/experiment.json"
        },
        expected_variants={
            "RSH-008": ("system", "typed_plain", "typeiso"),
        },
        source="unialloc-rustsec-rsh008-derived-reuse-summary",
        typed_allocator_expected_oracle={"RSH-008": True},
    ),
    "rsh052": SummaryProfile(
        advisory_ids={"RSH-052": "RUSTSEC-2019-0023"},
        boundary=(
            "This artifact validates one manually attributed exploit-enabling "
            "cross-identity reuse edge. It does not claim source-level stale-pointer "
            "detection or automatic compiler coverage of the victim sites."
        ),
        case_ids=("RSH-052",),
        catalog_count_contract={
            "derived_reuse_scenario_count": 1,
            "repository_scenario_count": 8,
        },
        catalog_path=(
            ROOT / "evaluation/config/rustsec_heap_strong_batch_a_harnesses.json"
        ),
        catalog_source="unialloc-rustsec-strong-candidate-batch-a",
        default_experiments={
            "RSH-052": ROOT
            / "evaluation/raw/rsh052-derived-final-current-20260715/experiment.json"
        },
        expected_variants={
            "RSH-052": ("system", "typed_plain", "typeiso"),
        },
        source="unialloc-rustsec-rsh052-derived-reuse-summary",
        typed_allocator_expected_oracle={"RSH-052": True},
    ),
    "rsh055": SummaryProfile(
        advisory_ids={"RSH-055": "RUSTSEC-2020-0145"},
        boundary=(
            "This artifact validates one manually attributed, layout-bounded "
            "consumed Box<VictimPayload>-to-Replacement reuse decision. The claim "
            "is limited to the derived RSH-055 cross-identity edge; automatic "
            "compiler victim coverage and complete source-level detection remain "
            "outside its scope."
        ),
        case_ids=("RSH-055",),
        catalog_count_contract={
            "derived_reuse_scenario_count": 1,
            "repository_scenario_count": 5,
        },
        catalog_path=(
            ROOT / "evaluation/config/rustsec_heap_strong_batch_b_harnesses.json"
        ),
        catalog_source="unialloc-strong-candidate-batch-b-fragment",
        default_experiments={
            "RSH-055": ROOT
            / "evaluation/raw/rsh055-derived-final-current-20260715/experiment.json"
        },
        expected_variants={
            "RSH-055": ("system", "typed_plain", "typeiso"),
        },
        source="unialloc-rustsec-rsh055-derived-reuse-summary",
        typed_allocator_expected_oracle={"RSH-055": True},
    ),
    "rsh064": SummaryProfile(
        advisory_ids={"RSH-064": "RUSTSEC-2022-0028"},
        boundary=(
            "This artifact validates one manually attributed exploit-enabling "
            "cross-identity reuse edge. Automatic compiler coverage of the published "
            "Neon Vec/external-buffer source path remains deferred."
        ),
        case_ids=("RSH-064",),
        catalog_count_contract={
            "harness_case_count": 1,
            "repository_scenario_count": 2,
        },
        catalog_path=(
            ROOT / "evaluation/config/rustsec_heap_neon_node_harnesses.json"
        ),
        catalog_source="unialloc-rustsec-neon-node-harness-catalog",
        default_experiments={
            "RSH-064": ROOT
            / "evaluation/raw/rsh064-derived-final-current-20260715/experiment.json"
        },
        expected_variants={
            "RSH-064": ("system", "typed_plain", "typeiso"),
        },
        source="unialloc-rustsec-rsh064-derived-reuse-summary",
        typed_allocator_expected_oracle={"RSH-064": True},
    ),
    "rsh065": SummaryProfile(
        advisory_ids={"RSH-065": "RUSTSEC-2023-0054"},
        boundary=(
            "This artifact validates one manually attributed, layout-bounded "
            "pre-reserve Vec<u8>-buffer-to-Replacement reuse decision. The claim "
            "is limited to the derived RSH-065 cross-identity edge; automatic "
            "compiler victim coverage and complete source-level detection remain "
            "outside its scope."
        ),
        case_ids=("RSH-065",),
        catalog_count_contract={
            "derived_reuse_scenario_count": 3,
            "repository_scenario_count": 11,
        },
        catalog_path=(
            ROOT / "evaluation/config/rustsec_heap_strong_batch_c_harnesses.json"
        ),
        catalog_source="unialloc-strong-candidate-batch-c-fragment",
        default_experiments={
            "RSH-065": ROOT
            / "evaluation/raw/rsh065-derived-final-current-20260715/experiment.json"
        },
        expected_variants={
            "RSH-065": ("system", "typed_plain", "typeiso"),
        },
        source="unialloc-rustsec-rsh065-derived-reuse-summary",
        typed_allocator_expected_oracle={"RSH-065": True},
    ),
    "rsh066": SummaryProfile(
        advisory_ids={"RSH-066": "RUSTSEC-2024-0007"},
        boundary=(
            "This artifact validates one manually attributed, layout-bounded stale "
            "AtomicStr String-buffer-to-Replacement reuse decision. The claim is "
            "limited to the derived RSH-066 cross-identity edge; automatic compiler "
            "victim coverage and complete source-level detection remain outside its "
            "scope."
        ),
        case_ids=("RSH-066",),
        catalog_count_contract={
            "derived_reuse_scenario_count": 3,
            "repository_scenario_count": 11,
        },
        catalog_path=(
            ROOT / "evaluation/config/rustsec_heap_strong_batch_c_harnesses.json"
        ),
        catalog_source="unialloc-strong-candidate-batch-c-fragment",
        default_experiments={
            "RSH-066": ROOT
            / "evaluation/raw/rsh066-derived-final-current-20260715/experiment.json"
        },
        expected_variants={
            "RSH-066": ("system", "typed_plain", "typeiso"),
        },
        source="unialloc-rustsec-rsh066-derived-reuse-summary",
        typed_allocator_expected_oracle={"RSH-066": True},
    ),
    "rsh067": SummaryProfile(
        advisory_ids={"RSH-067": "RUSTSEC-2025-0004"},
        boundary=(
            "This artifact validates one manually attributed, layout-bounded OpenSSL "
            "server-buffer-to-Replacement reuse decision. The claim is limited to "
            "the derived RSH-067 cross-identity edge; automatic compiler victim "
            "coverage and complete source-level detection remain outside its scope."
        ),
        case_ids=("RSH-067",),
        catalog_count_contract={
            "derived_reuse_scenario_count": 3,
            "repository_scenario_count": 11,
        },
        catalog_path=(
            ROOT / "evaluation/config/rustsec_heap_strong_batch_c_harnesses.json"
        ),
        catalog_source="unialloc-strong-candidate-batch-c-fragment",
        default_experiments={
            "RSH-067": ROOT
            / "evaluation/raw/rsh067-derived-final-current-20260715/experiment.json"
        },
        expected_variants={
            "RSH-067": ("system", "typed_plain", "typeiso"),
        },
        source="unialloc-rustsec-rsh067-derived-reuse-summary",
        typed_allocator_expected_oracle={"RSH-067": True},
    ),
    "rsh068": SummaryProfile(
        advisory_ids={"RSH-068": "RUSTSEC-2025-0016"},
        boundary=(
            "This artifact validates one manually attributed, layout-bounded external "
            "String-buffer-to-Replacement reuse decision. The claim is limited to "
            "the derived RSH-068 cross-identity edge; automatic compiler victim "
            "coverage and complete source-level detection remain outside its scope."
        ),
        case_ids=("RSH-068",),
        catalog_count_contract={
            "derived_reuse_scenario_count": 2,
            "repository_scenario_count": 7,
        },
        catalog_path=(
            ROOT / "evaluation/config/rustsec_heap_strong_batch_f_harnesses.json"
        ),
        catalog_source="unialloc-strong-rustsec-batch-f-harness-catalog",
        default_experiments={
            "RSH-068": ROOT
            / "evaluation/raw/rsh068-derived-final-current-20260715/experiment.json"
        },
        expected_variants={
            "RSH-068": ("system", "typed_plain", "typeiso"),
        },
        source="unialloc-rustsec-rsh068-derived-reuse-summary",
        typed_allocator_expected_oracle={"RSH-068": True},
    ),
    "rsh069": SummaryProfile(
        advisory_ids={"RSH-069": "RUSTSEC-2025-0022"},
        boundary=(
            "This artifact validates one manually attributed, layout-bounded "
            "properties-CString-to-Replacement reuse decision. The claim is limited "
            "to the derived RSH-069 cross-identity edge; automatic compiler victim "
            "coverage and complete source-level detection remain outside its scope."
        ),
        case_ids=("RSH-069",),
        catalog_count_contract={
            "derived_reuse_scenario_count": 2,
            "repository_scenario_count": 7,
        },
        catalog_path=(
            ROOT / "evaluation/config/rustsec_heap_strong_batch_f_harnesses.json"
        ),
        catalog_source="unialloc-strong-rustsec-batch-f-harness-catalog",
        default_experiments={
            "RSH-069": ROOT
            / "evaluation/raw/rsh069-derived-final-current-20260715/experiment.json"
        },
        expected_variants={
            "RSH-069": ("system", "typed_plain", "typeiso"),
        },
        source="unialloc-rustsec-rsh069-derived-reuse-summary",
        typed_allocator_expected_oracle={"RSH-069": True},
    ),
}

# Preserve the original expansion API for callers that import these constants.
DEFAULT_PROFILE = PROFILES["expansion"]
DEFAULT_CATALOG = DEFAULT_PROFILE.catalog_path
DEFAULT_EXPERIMENTS = dict(DEFAULT_PROFILE.default_experiments)
CASE_IDS = DEFAULT_PROFILE.case_ids


class SummaryError(RuntimeError):
    """Raised when the inputs cannot support the bounded summary."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256_bytes(payload)


def load_json_artifact(
    path: pathlib.Path, label: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved = path.resolve()
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise SummaryError(f"cannot read {label} {path}: {error}") from error
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SummaryError(f"cannot parse {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise SummaryError(f"{label} must contain a JSON object")
    try:
        display = str(resolved.relative_to(ROOT))
    except ValueError:
        display = str(resolved)
    return value, {
        "path": display,
        "bytes": len(payload),
        "sha256": sha256_bytes(payload),
    }


def verify_json_artifact(
    value: dict[str, Any], artifact: dict[str, Any], label: str
) -> None:
    """Bind a parsed value and its recorded digest back to the input bytes."""

    path_value = artifact.get("path")
    byte_count = artifact.get("bytes")
    digest = artifact.get("sha256")
    if (
        not isinstance(path_value, str)
        or isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count < 0
        or not isinstance(digest, str)
    ):
        raise SummaryError(f"{label} artifact record is incomplete")
    path = pathlib.Path(path_value)
    resolved = path if path.is_absolute() else ROOT / path
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise SummaryError(f"cannot read {label} artifact {path_value}: {error}") from error
    if len(payload) != byte_count or sha256_bytes(payload) != digest:
        raise SummaryError(f"{label} artifact hash mismatch")
    try:
        recorded_value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SummaryError(f"cannot parse {label} artifact {path_value}: {error}") from error
    if recorded_value != value:
        raise SummaryError(f"{label} parsed value does not match artifact bytes")


def nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SummaryError(f"{label} must be a non-negative integer")
    return value


def positive_int(value: object, label: str) -> int:
    result = nonnegative_int(value, label)
    if result == 0:
        raise SummaryError(f"{label} must be positive")
    return result


def require_false(value: object, label: str) -> None:
    if value is not False:
        raise SummaryError(f"{label} must be false")


def verify_inline_artifact(record: dict[str, Any], label: str) -> None:
    for stream in ("stdout", "stderr"):
        text = record.get(stream)
        digest = record.get(f"{stream}_sha256")
        if not isinstance(text, str) or not isinstance(digest, str):
            raise SummaryError(f"{label} lacks {stream} artifact data")
        if sha256_bytes(text.encode("utf-8")) != digest:
            raise SummaryError(f"{label} {stream} artifact hash mismatch")


def verify_source(record: dict[str, Any], *, path_key: str, hash_key: str) -> None:
    path_value = record.get(path_key)
    expected = record.get(hash_key)
    if not isinstance(path_value, str) or not isinstance(expected, str):
        raise SummaryError("scenario source record is incomplete")
    path = (ROOT / path_value).resolve()
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise SummaryError(
            f"cannot read scenario source {path_value}: {error}"
        ) from error
    if sha256_bytes(payload) != expected:
        raise SummaryError(f"scenario source hash mismatch: {path_value}")


def catalog_inputs(
    catalog: dict[str, Any], profile: SummaryProfile = DEFAULT_PROFILE
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    if (
        catalog.get("schema_version") != 1
        or catalog.get("source") != profile.catalog_source
    ):
        raise SummaryError(f"unsupported {profile.catalog_source} catalog")
    cases = catalog.get("cases")
    if not isinstance(cases, list):
        raise SummaryError("catalog cases must be a list")
    case_index: dict[str, dict[str, Any]] = {}
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
            raise SummaryError("invalid expansion catalog case")
        case_id = case["case_id"]
        if case_id in case_index:
            raise SummaryError(f"duplicate catalog case {case_id}")
        case_index[case_id] = case

    result: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for case_id in profile.case_ids:
        case = case_index.get(case_id)
        if case is None:
            raise SummaryError(f"catalog lacks {case_id}")
        catalog_advisory = case.get("advisory_id")
        if catalog_advisory is not None and catalog_advisory != profile.advisory_ids[
            case_id
        ]:
            raise SummaryError(f"catalog advisory mismatch for {case_id}")
        scenarios = case.get("scenarios")
        if not isinstance(scenarios, list):
            raise SummaryError(f"catalog lacks scenarios for {case_id}")
        derived = [
            row
            for row in scenarios
            if isinstance(row, dict)
            and row.get("classification_role") == "derived_reuse_experiment"
        ]
        if len(derived) != 1:
            raise SummaryError(f"{case_id} requires exactly one derived-reuse scenario")
        scenario = derived[0]
        if scenario.get("scenario_id") != f"{case_id}-derived-reuse":
            raise SummaryError(f"derived scenario mismatch for {case_id}")
        annotation = scenario.get("type_isolation_edge_annotation")
        if not isinstance(annotation, dict) or annotation.get("schema_version") != 1:
            raise SummaryError(f"invalid edge annotation for {case_id}")
        if annotation.get("kind") != "manual_exact_vulnerability_edge_identity":
            raise SummaryError(f"invalid edge annotation kind for {case_id}")
        layout = annotation.get("expected_layout")
        if not isinstance(layout, dict):
            raise SummaryError(f"missing expected layout for {case_id}")
        positive_int(layout.get("size"), f"{case_id} expected size")
        positive_int(layout.get("align"), f"{case_id} expected alignment")
        positive_int(annotation.get("victim_type_id"), f"{case_id} victim type id")
        positive_int(annotation.get("victim_module_id"), f"{case_id} victim module id")
        verify_source(scenario, path_key="source_path", hash_key="source_sha256")
        patched_source = scenario.get("patched_source")
        if not isinstance(patched_source, dict):
            raise SummaryError(f"missing patched source for {case_id}")
        verify_source(
            patched_source, path_key="source_path", hash_key="source_sha256"
        )
        result[case_id] = (case, scenario)
    counts = catalog.get("counts")
    if not isinstance(counts, dict) or any(
        counts.get(key) != expected
        for key, expected in profile.catalog_count_contract.items()
    ):
        raise SummaryError("catalog count contract mismatch")
    return result


def arm_summary(arm: dict[str, Any], repetitions: int) -> dict[str, Any]:
    repetition = arm.get("repetition_summary")
    oracle = arm.get("oracle_validation")
    denial = arm.get("reuse_denial_evidence")
    if not isinstance(repetition, dict) or not isinstance(oracle, dict):
        raise SummaryError("arm lacks repetition or oracle evidence")
    if not isinstance(denial, dict):
        raise SummaryError("arm lacks reuse-denial evidence")
    executed = nonnegative_int(repetition.get("executed"), "executed repetitions")
    requested = nonnegative_int(repetition.get("requested"), "requested repetitions")
    exit_codes = repetition.get("exit_codes")
    if requested != repetitions or executed != repetitions:
        raise SummaryError("arm repetition count mismatch")
    if not isinstance(exit_codes, list) or len(exit_codes) != repetitions or not all(
        isinstance(code, int) and not isinstance(code, bool) for code in exit_codes
    ):
        raise SummaryError("arm exit-code count mismatch")
    if nonnegative_int(repetition.get("timed_out_count"), "timed-out count") != 0:
        raise SummaryError("arm contains timed-out repetitions")
    require_false(repetition.get("mitigation_inferred"), "arm mitigation inference")
    expected = oracle.get("expected_oracle_observed")
    if not isinstance(expected, bool):
        raise SummaryError("arm lacks expected-oracle result")
    return {
        "address_reuse_observation_count": nonnegative_int(
            repetition.get("address_reuse_observation_count"),
            "address-reuse count",
        ),
        "allocator_variant": arm["allocator_variant"],
        "archive_variant": arm["archive_variant"],
        "bound_replacement_site_count": nonnegative_int(
            denial.get("bound_replacement_site_count", 0),
            "bound replacement-site count",
        ),
        "claim_grade": False,
        "execution_status": arm["execution_status"],
        "exit_codes": list(exit_codes),
        "expected_oracle_observed": expected,
        "matching_reuse_denial_event_count": nonnegative_int(
            denial.get("matching_reuse_denial_event_count", 0),
            "matching reuse-denial count",
        ),
        "mitigation_inferred": False,
    }


def validate_experiment(
    case_id: str,
    raw: dict[str, Any],
    case: dict[str, Any],
    scenario: dict[str, Any],
    *,
    expected_variants: tuple[str, ...],
    typed_allocator_expected_oracle: bool = True,
    expected_repetitions: int = 3,
) -> list[dict[str, Any]]:
    scenario_id = f"{case_id}-derived-reuse"
    if raw.get("schema_version") != 1 or raw.get("source") != (
        "unialloc-rustsec-heap-experiment-matrix"
    ):
        raise SummaryError(f"unsupported experiment for {case_id}")
    if raw.get("scenario_id") != scenario_id:
        raise SummaryError(f"experiment scenario mismatch for {case_id}")
    if raw.get("orchestration_success") is not True:
        raise SummaryError(f"experiment orchestration failed for {case_id}")
    if raw.get("unexpected_arm_count") != 0:
        raise SummaryError(f"experiment has unexpected arms for {case_id}")
    require_false(raw.get("claim_grade"), f"{case_id} claim grade")
    repetitions = positive_int(raw.get("repetitions_requested"), "repetitions")
    if repetitions != expected_repetitions:
        raise SummaryError(
            f"{case_id} requires exactly {expected_repetitions} repetitions"
        )
    archives = raw.get("archive_variants")
    variants = raw.get("variants")
    if archives != list(ARCHIVES):
        raise SummaryError(f"archive variants mismatch for {case_id}")
    if variants != list(expected_variants) or any(
        value not in ALLOCATOR_ORDER for value in expected_variants
    ):
        raise SummaryError(f"allocator variants mismatch for {case_id}")
    arms = raw.get("arms")
    if not isinstance(arms, list):
        raise SummaryError(f"experiment arms missing for {case_id}")
    expected_keys = {(archive, variant) for archive in ARCHIVES for variant in variants}
    arm_index: dict[tuple[str, str], dict[str, Any]] = {}
    annotation = scenario["type_isolation_edge_annotation"]
    for arm in arms:
        if not isinstance(arm, dict):
            raise SummaryError(f"invalid arm for {case_id}")
        if arm.get("case_id") != case_id or arm.get("scenario_id") != scenario_id:
            raise SummaryError(f"arm case/scenario mismatch for {case_id}")
        if arm.get("crate") != case.get("crate"):
            raise SummaryError(f"arm crate mismatch for {case_id}")
        key = (arm.get("archive_variant"), arm.get("allocator_variant"))
        if key not in expected_keys or key in arm_index:
            raise SummaryError(f"unexpected or duplicate arm for {case_id}: {key}")
        if arm.get("execution_status") != "completed":
            raise SummaryError(f"incomplete arm for {case_id}: {key}")
        if arm.get("repetitions_requested") != repetitions:
            raise SummaryError(f"arm repetition request mismatch for {case_id}: {key}")
        require_false(arm.get("claim_grade"), f"{case_id} arm claim grade")
        payload = arm.get("fingerprint_payload")
        fingerprint = arm.get("fingerprint")
        if not isinstance(payload, dict) or canonical_sha256(payload) != fingerprint:
            raise SummaryError(f"arm fingerprint mismatch for {case_id}: {key}")
        expected_source = (
            scenario["source_sha256"]
            if key[0] == "vulnerable"
            else scenario["patched_source"]["source_sha256"]
        )
        if payload.get("source_sha256") != expected_source:
            raise SummaryError(f"arm source hash mismatch for {case_id}: {key}")
        materialization = arm.get("materialization")
        if not isinstance(materialization, dict) or materialization.get(
            "source_sha256"
        ) != expected_source:
            raise SummaryError(
                f"materialized source hash mismatch for {case_id}: {key}"
            )
        build = arm.get("build")
        runs = arm.get("runs")
        if (
            not isinstance(build, dict)
            or not isinstance(runs, list)
            or len(runs) != repetitions
        ):
            raise SummaryError(f"arm build/run artifacts missing for {case_id}: {key}")
        verify_inline_artifact(build, f"{case_id} {key} build")
        for index, run in enumerate(runs, 1):
            if not isinstance(run, dict):
                raise SummaryError(f"invalid run artifact for {case_id}: {key}")
            verify_inline_artifact(run, f"{case_id} {key} run {index}")
        denial = arm.get("reuse_denial_evidence")
        if not isinstance(denial, dict) or denial.get("annotation") != annotation:
            raise SummaryError(f"reuse annotation mismatch for {case_id}: {key}")
        arm_index[key] = arm
    if set(arm_index) != expected_keys or len(arms) != len(expected_keys):
        raise SummaryError(f"required arms missing for {case_id}")

    summaries = {
        key: arm_summary(arm, repetitions) for key, arm in arm_index.items()
    }
    for key, summary in summaries.items():
        archive, variant = key
        denial_edge = archive == "vulnerable" and variant == "typeiso"
        expected_reuse = 0 if denial_edge else repetitions
        expected_denials = repetitions if denial_edge else 0
        expected_oracle = (
            not denial_edge
            and (variant == "system" or typed_allocator_expected_oracle)
        )
        if (
            summary["address_reuse_observation_count"] != expected_reuse
            or summary["matching_reuse_denial_event_count"] != expected_denials
            or summary["bound_replacement_site_count"] != expected_denials
            or summary["expected_oracle_observed"] is not expected_oracle
        ):
            raise SummaryError(
                f"arm causal contract does not validate {case_id}: {key}"
            )

    evaluation = raw.get("type_isolation_reuse_edge_evaluation")
    required_true = (
        "validated",
        "baseline_vulnerability_and_address_reuse_reproduced",
        "typed_plain_address_reuse_ablation_reproduced",
        "typeiso_reuse_edge_blocked_and_reported",
        "patched_system_control_reproduced",
        "manual_victim_identity_annotation",
        "vulnerability_specific_detection_signal",
    )
    required_false = (
        "claim_grade",
        "compiler_automatic_victim_coverage",
        "source_vulnerability_detection_validated",
    )
    if not isinstance(evaluation, dict) or any(
        evaluation.get(key) is not True for key in required_true
    ) or any(evaluation.get(key) is not False for key in required_false):
        raise SummaryError(f"invalid edge evaluation for {case_id}")
    if (
        evaluation.get("status") != "cross_identity_reuse_edge_blocked_and_reported"
        or evaluation.get("missing_required_arms") != []
        or evaluation.get("patched_typeiso_matching_denial_count") != 0
        or evaluation.get("annotation") != annotation
        or evaluation.get("claim_scope") != annotation.get("claim_scope")
    ):
        raise SummaryError(f"edge evaluation contract mismatch for {case_id}")
    return [
        summaries[key]
        for key in sorted(
            summaries,
            key=lambda value: (ARCHIVES.index(value[0]), ALLOCATOR_ORDER[value[1]]),
        )
    ]


def build_summary(
    catalog: dict[str, Any],
    catalog_artifact: dict[str, Any],
    experiments: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    *,
    profile_name: str = "expansion",
) -> dict[str, Any]:
    try:
        profile = PROFILES[profile_name]
    except KeyError as error:
        raise SummaryError(f"unknown summary profile: {profile_name}") from error
    if set(experiments) != set(profile.case_ids):
        expected = ", ".join(profile.case_ids)
        raise SummaryError(f"experiments must contain exactly {expected}")
    verify_json_artifact(catalog, catalog_artifact, "catalog")
    selected = catalog_inputs(catalog, profile)
    scenarios = []
    raw_inputs = []
    for case_id in profile.case_ids:
        case, scenario = selected[case_id]
        raw, raw_artifact = experiments[case_id]
        verify_json_artifact(raw, raw_artifact, f"{case_id} experiment")
        arms = validate_experiment(
            case_id,
            raw,
            case,
            scenario,
            expected_variants=profile.expected_variants[case_id],
            typed_allocator_expected_oracle=(
                profile.typed_allocator_expected_oracle[case_id]
            ),
        )
        raw_record = {**raw_artifact, "case_id": case_id}
        raw_inputs.append(raw_record)
        scenarios.append(
            {
                "advisory_id": case.get(
                    "advisory_id", profile.advisory_ids[case_id]
                ),
                "annotation": scenario["type_isolation_edge_annotation"],
                "arms": arms,
                "case_id": case_id,
                "crate": case["crate"],
                "edge_evaluation": raw["type_isolation_reuse_edge_evaluation"],
                "orchestration_success": True,
                "patched_scenario_source": scenario["patched_source"],
                "raw_experiment": raw_record,
                "repetitions_requested": raw["repetitions_requested"],
                "scenario_id": scenario["scenario_id"],
                "scenario_source": {
                    "path": scenario["source_path"],
                    "sha256": scenario["source_sha256"],
                },
                "typed_allocator_expected_oracle": (
                    profile.typed_allocator_expected_oracle[case_id]
                ),
                "unexpected_arm_count": 0,
            }
        )
    return {
        "boundary": profile.boundary,
        "catalog": catalog_artifact,
        "claim_grade": False,
        "counts": {
            "compiler_automatic_victim_coverage_count": 0,
            "derived_reuse_scenario_count": len(scenarios),
            "source_vulnerability_detection_validated_count": 0,
            "source_vulnerability_mitigation_inferred_count": 0,
            "validated_cross_identity_reuse_edge_count": len(scenarios),
        },
        "mitigation_inferred": False,
        "raw_experiment_inputs": raw_inputs,
        "scenarios": scenarios,
        "schema_version": 1,
        "source": profile.source,
    }


def experiment_selections(
    profile: SummaryProfile, values: Sequence[str] | None
) -> dict[str, pathlib.Path]:
    """Resolve CASE=PATH selections, with PATH shorthand for single-case profiles."""

    if not values:
        return dict(profile.default_experiments)
    result: dict[str, pathlib.Path] = {}
    for value in values:
        if "=" in value:
            case_id, raw_path = value.split("=", 1)
        elif len(profile.case_ids) == 1:
            case_id, raw_path = profile.case_ids[0], value
        else:
            raise SummaryError(
                "multi-case --experiment selections must use CASE=PATH"
            )
        if case_id not in profile.case_ids or not raw_path:
            raise SummaryError(f"invalid experiment selection: {value}")
        if case_id in result:
            raise SummaryError(f"duplicate experiment case selection: {case_id}")
        result[case_id] = pathlib.Path(raw_path).expanduser().resolve()
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", choices=tuple(PROFILES), default="expansion"
    )
    parser.add_argument("--catalog", type=pathlib.Path)
    parser.add_argument(
        "--experiment",
        action="append",
        metavar="CASE=PATH|PATH",
        help=(
            "experiment input; single-case profiles accept PATH, while expansion "
            "uses one CASE=PATH selection per case"
        ),
    )
    parser.add_argument("--output", type=pathlib.Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        profile = PROFILES[args.profile]
        selections = experiment_selections(profile, args.experiment)
        catalog_path = (args.catalog or profile.catalog_path).expanduser().resolve()
        experiments = {
            case_id: load_json_artifact(path, case_id)
            for case_id, path in selections.items()
        }
        catalog, catalog_artifact = load_json_artifact(catalog_path, "catalog")
        summary = build_summary(
            catalog,
            catalog_artifact,
            experiments,
            profile_name=args.profile,
        )
        rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            args.output.write_text(rendered, encoding="utf-8")
    except (OSError, SummaryError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
