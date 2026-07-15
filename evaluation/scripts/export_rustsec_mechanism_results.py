#!/usr/bin/env python3
"""Export fail-closed allocator-mechanism results from matched RustSec runs.

The input is a summary emitted by ``summarize_rustsec_heap_expansion.py``.
An advisory is reported as detected only when a repeated vulnerable baseline,
an exact allocator-native diagnostic, and a signal-free patched control are all
present.  A derived Type-Isolation edge is reported as mitigated only when the
feature blocks and reports that matched exploit-enabling edge.  Executed
feature-matched negatives remain explicit ``no_signal`` results; incomplete
evidence remains ``inconclusive``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
EXACT_RECLAIM_SIGNATURES = frozenset(
    {"unialloc_pointer_already_released_check"}
)
NORMALIZED_SYNCHRONOUS_NATIVE_SIGNALS = frozenset(
    {
        "posix_signal:SIGABRT",
        "posix_signal:SIGBUS",
        "posix_signal:SIGFPE",
        "posix_signal:SIGILL",
        "posix_signal:SIGSEGV",
        "posix_signal:SIGTRAP",
    }
)
UNIALLOC_MANIFEST = (ROOT / "unialloc/Cargo.toml").resolve()
UNIALLOC_PACKAGE_ID_PREFIX = f"path+{UNIALLOC_MANIFEST.parent.as_uri()}#"
RECLAIM_PLAIN_FEATURES = ["stats"]
RECLAIM_CHECKS_FEATURES = ["reclaim_checks", "stats"]
SUBJECT_CARGO_FEATURE_FIELDS = frozenset(
    {
        "allocator_variant",
        "crate",
        "catalog_features",
        "effective_features",
        "default_features_enabled",
        "override_applied",
        "override_allocator_variant",
        "override_source",
    }
)
INCOMPLETE_ORACLE_STATUS_MARKERS = (
    "abnormal",
    "timeout",
    "timed_out",
    "build_failed",
    "build_not_performed",
    "run_failed",
    "run_not_performed",
    "not_run",
)
SYNTHETIC_TYPEISO_REDUCTION_MARKERS = {
    "RSH-065": "manually modeled",
    "RSH-069": "synthetically groomed",
}


class ResultError(RuntimeError):
    """Raised when the input cannot support a fail-closed result."""


def load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResultError(f"cannot read summary {path}: {error}") from error
    if not isinstance(value, dict):
        raise ResultError("summary must be a JSON object")
    return value


def artifact(path: pathlib.Path) -> dict[str, Any]:
    resolved = path.resolve()
    payload = resolved.read_bytes()
    try:
        display = str(resolved.relative_to(ROOT))
    except ValueError:
        display = str(resolved)
    return {
        "path": display,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def arm_index(case: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    arms = case.get("arms", [])
    if not isinstance(arms, list):
        raise ResultError("case arms must be a list")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for arm in arms:
        if not isinstance(arm, dict):
            raise ResultError("case arm must be an object")
        key = (arm.get("archive_variant"), arm.get("allocator_variant"))
        if not all(isinstance(value, str) for value in key):
            raise ResultError("case arm requires archive and allocator variants")
        if key in result:
            raise ResultError(f"duplicate summarized arm: {key}")
        result[key] = arm
    return result


def nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResultError(f"{label} must be a non-negative integer")
    return value


def signature_counts(arm: dict[str, Any], field: str) -> dict[str, int]:
    raw = arm.get(field, {})
    if not isinstance(raw, dict):
        raise ResultError(f"{field} must be an object")
    return {
        str(name): nonnegative_int(count, f"{field}.{name}")
        for name, count in raw.items()
    }


def completed(arm: dict[str, Any]) -> bool:
    statuses = arm.get("execution_statuses", [])
    return isinstance(statuses, list) and statuses == ["completed"]


def unique_scenario_binding(
    case: dict[str, Any], arms: dict[tuple[str, str], dict[str, Any]]
) -> tuple[str | None, list[str], str | None]:
    """Resolve one scenario identity without silently falling back to a case id."""

    observed: set[str] = set()

    def collect(record: dict[str, Any], label: str) -> None:
        scenario_id = record.get("scenario_id")
        if scenario_id is not None:
            if not isinstance(scenario_id, str) or not scenario_id:
                raise ResultError(f"{label}.scenario_id must be a non-empty string")
            observed.add(scenario_id)
        scenario_ids = record.get("scenario_ids")
        if scenario_ids is not None:
            if not isinstance(scenario_ids, list) or not all(
                isinstance(value, str) and value for value in scenario_ids
            ):
                raise ResultError(
                    f"{label}.scenario_ids must be a list of non-empty strings"
                )
            observed.update(scenario_ids)

    collect(case, "case")
    for (archive, allocator), arm in sorted(arms.items()):
        collect(arm, f"arm[{archive},{allocator}]")
    values = sorted(observed)
    if len(values) == 1:
        return values[0], values, None
    if not values:
        return None, values, "missing_scenario_binding"
    return None, values, "ambiguous_scenario_binding"


def one_signature_in_every_repetition(
    counts: dict[str, int], repetitions: int
) -> bool:
    """Require one stable observation class with no mixed or extra signatures."""

    return len(counts) == 1 and next(iter(counts.values())) == repetitions


def optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def incomplete_oracle_statuses(arm: dict[str, Any]) -> list[str]:
    statuses = signature_counts(arm, "oracle_statuses")
    return sorted(
        status
        for status, count in statuses.items()
        if count > 0
        and any(marker in status.lower() for marker in INCOMPLETE_ORACLE_STATUS_MARKERS)
    )


def stable_native_abnormal_signal(
    arm: dict[str, Any], repetitions: int
) -> tuple[bool, dict[str, int]]:
    """Validate a normalized synchronous fault in every native repetition.

    The raw exit code must be normalized upstream to a named synchronous
    signal.  Generic abnormal exits, timeouts, and parser-failure classes do
    not satisfy this evidence contract.
    """

    signals = signature_counts(arm, "native_abnormal_signal_signatures")
    diagnostics = signature_counts(arm, "native_diagnostic_signatures")
    fingerprints = signature_counts(arm, "native_outcome_fingerprints")
    fingerprint_failures = signature_counts(
        arm, "native_outcome_fingerprint_failures"
    )
    oracle_statuses = signature_counts(arm, "oracle_statuses")
    valid = (
        one_signature_in_every_repetition(signals, repetitions)
        and set(signals).issubset(NORMALIZED_SYNCHRONOUS_NATIVE_SIGNALS)
        and not diagnostics
        and (
            (
                one_signature_in_every_repetition(fingerprints, repetitions)
                and not fingerprint_failures
            )
            or (
                not fingerprints
                and (
                    not fingerprint_failures
                    or one_signature_in_every_repetition(
                        fingerprint_failures, repetitions
                    )
                )
            )
        )
        and set(oracle_statuses) == {"native_abnormal_exit_inconclusive"}
        and oracle_statuses["native_abnormal_exit_inconclusive"] > 0
    )
    return valid, signals


def sha256_hex(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_cargo_metadata_attestation(
    arm: dict[str, Any],
    *,
    label: str,
    expected_features: list[str],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate the summarized Cargo record without trusting its valid bit."""

    failures: list[str] = []
    attestation = arm.get("cargo_metadata_attestation")
    if not isinstance(attestation, dict):
        return None, [f"{label}.cargo_metadata_attestation:missing_or_invalid"]
    if attestation.get("valid") is not True:
        failures.append(f"{label}.cargo_metadata_attestation.valid:not_true")
    if attestation.get("required") is not True:
        failures.append(f"{label}.cargo_metadata_attestation.required:not_true")
    observation_count = optional_nonnegative_int(
        attestation.get("observation_count")
    )
    if observation_count is None or observation_count == 0:
        failures.append(
            f"{label}.cargo_metadata_attestation.observation_count:not_positive"
        )
    experiment_count = optional_nonnegative_int(arm.get("experiment_count"))
    if experiment_count is None or observation_count != experiment_count:
        failures.append(
            f"{label}.cargo_metadata_attestation.observation_count:"
            "experiment_count_mismatch"
        )
    failure_counts = attestation.get("failure_counts")
    if not isinstance(failure_counts, dict) or failure_counts:
        failures.append(
            f"{label}.cargo_metadata_attestation.failure_counts:not_empty"
        )
    token = attestation.get("token")
    if not sha256_hex(token):
        failures.append(f"{label}.cargo_metadata_attestation.token:invalid_sha256")
    safe_token = token if isinstance(token, str) else "<invalid-token>"

    record = attestation.get("record")
    if not isinstance(record, dict):
        failures.append(f"{label}.cargo_metadata_attestation.record:missing_or_invalid")
        return None, failures
    record_fields = {
        "schema_version",
        "package_count",
        "resolve_node_count",
        "package_id",
        "manifest_path",
        "package_source",
        "expected_features",
        "resolved_features",
        "default_features_expected",
        "default_feature_resolved",
        "feature_contract_sha256",
    }
    if set(record) != record_fields:
        failures.append(
            f"{label}.cargo_metadata_attestation.record:unexpected_schema"
        )
    expected_values: dict[str, object] = {
        "schema_version": 1,
        "package_count": 1,
        "resolve_node_count": 1,
        "package_source": None,
        "expected_features": expected_features,
        "resolved_features": expected_features,
        "default_features_expected": False,
        "default_feature_resolved": False,
    }
    for field, expected in expected_values.items():
        if record.get(field) != expected:
            failures.append(
                f"{label}.cargo_metadata_attestation.record.{field}:mismatch"
            )
    manifest_path = record.get("manifest_path")
    expected_relative_manifest = str(UNIALLOC_MANIFEST.relative_to(ROOT))
    if manifest_path != expected_relative_manifest:
        failures.append(
            f"{label}.cargo_metadata_attestation.record.manifest_path:mismatch"
        )
    package_id = record.get("package_id")
    if (
        not isinstance(package_id, str)
        or not package_id.startswith(UNIALLOC_PACKAGE_ID_PREFIX)
        or len(package_id) == len(UNIALLOC_PACKAGE_ID_PREFIX)
    ):
        failures.append(
            f"{label}.cargo_metadata_attestation.record.package_id:non_repo_identity"
        )
    stdout_hashes = attestation.get("cargo_metadata_stdout_sha256")
    if (
        not isinstance(stdout_hashes, dict)
        or len(stdout_hashes) != 1
        or not all(
            sha256_hex(value)
            and optional_nonnegative_int(count) is not None
            and count > 0
            for value, count in stdout_hashes.items()
        )
        or sum(stdout_hashes.values()) != observation_count
    ):
        failures.append(
            f"{label}.cargo_metadata_attestation."
            "cargo_metadata_stdout_sha256:invalid_or_mixed"
        )
    contract_sha256 = record.get("feature_contract_sha256")
    if not sha256_hex(contract_sha256):
        failures.append(
            f"{label}.cargo_metadata_attestation.record."
            "feature_contract_sha256:invalid_sha256"
        )
    if safe_token != contract_sha256:
        failures.append(
            f"{label}.cargo_metadata_attestation.token:contract_mismatch"
        )
    observed_tokens = attestation.get("observed_feature_contract_tokens")
    if observed_tokens != {safe_token: observation_count}:
        failures.append(
            f"{label}.cargo_metadata_attestation."
            "observed_feature_contract_tokens:mismatch"
        )
    observed_records = attestation.get("observed_records")
    if observed_records != {safe_token: record}:
        failures.append(
            f"{label}.cargo_metadata_attestation.observed_records:mismatch"
        )
    contract = {
        key: value
        for key, value in record.items()
        if key != "feature_contract_sha256"
    }
    contract["manifest_path"] = str(UNIALLOC_MANIFEST)
    if canonical_sha256(contract) != contract_sha256:
        failures.append(
            f"{label}.cargo_metadata_attestation."
            "feature_contract_sha256:content_mismatch"
        )
    return record, failures


def validate_subject_cargo_features(
    value: object, *, label: str, expected_allocator: str
) -> tuple[dict[str, Any] | None, list[str]]:
    failures: list[str] = []
    if not isinstance(value, dict) or set(value) != SUBJECT_CARGO_FEATURE_FIELDS:
        return None, [f"{label}.subject_cargo_features:missing_or_invalid"]
    if value.get("allocator_variant") != expected_allocator:
        failures.append(f"{label}.subject_cargo_features.allocator_variant:mismatch")
    crate = value.get("crate")
    if not isinstance(crate, str) or not crate:
        failures.append(f"{label}.subject_cargo_features.crate:invalid")
    for field in ("catalog_features", "effective_features"):
        features = value.get(field)
        if (
            not isinstance(features, list)
            or not all(isinstance(item, str) and item for item in features)
            or features != sorted(features)
            or len(features) != len(set(features))
        ):
            failures.append(
                f"{label}.subject_cargo_features.{field}:not_sorted_unique_strings"
            )
    default_features = value.get("default_features_enabled")
    override_applied = value.get("override_applied")
    if not isinstance(default_features, bool):
        failures.append(
            f"{label}.subject_cargo_features.default_features_enabled:invalid"
        )
    if not isinstance(override_applied, bool):
        failures.append(f"{label}.subject_cargo_features.override_applied:invalid")
    override_allocator = value.get("override_allocator_variant")
    override_source = value.get("override_source")
    if override_applied is True:
        if not isinstance(override_allocator, str) or not override_allocator:
            failures.append(
                f"{label}.subject_cargo_features.override_allocator_variant:invalid"
            )
        if override_source != "scenario.allocator_cargo_feature_overrides":
            failures.append(
                f"{label}.subject_cargo_features.override_source:invalid"
            )
    elif override_applied is False and (
        override_allocator is not None or override_source is not None
    ):
        failures.append(
            f"{label}.subject_cargo_features.inactive_override:has_metadata"
        )
    return dict(value), failures


def validate_direct_allocator_provenance(
    arm: dict[str, Any], *, label: str, expected_allocator: str
) -> tuple[str | None, dict[str, Any] | None, list[str]]:
    """Validate the summarizer's fingerprint-bound direct allocator record."""

    failures: list[str] = []
    digest = arm.get("unialloc_implementation_sha256")
    subject_value = arm.get("subject_cargo_features")
    subject, subject_failures = validate_subject_cargo_features(
        subject_value,
        label=label,
        expected_allocator=expected_allocator,
    )
    failures.extend(subject_failures)
    if not sha256_hex(digest):
        failures.append(f"{label}.unialloc_implementation_sha256:invalid")
    safe_digest = digest if isinstance(digest, str) else "<invalid-digest>"

    state = arm.get("direct_allocator_provenance")
    expected_state_fields = {
        "required",
        "valid",
        "observation_count",
        "failure_counts",
        "unialloc_implementation_sha256",
        "subject_cargo_features_sha256",
        "subject_cargo_features",
        "observed_unialloc_implementation_sha256",
        "observed_subject_cargo_feature_tokens",
        "observed_subject_cargo_features",
    }
    if not isinstance(state, dict) or set(state) != expected_state_fields:
        return None, None, failures + [
            f"{label}.direct_allocator_provenance:missing_or_invalid"
        ]
    if state.get("required") is not True:
        failures.append(f"{label}.direct_allocator_provenance.required:not_true")
    if state.get("valid") is not True:
        failures.append(f"{label}.direct_allocator_provenance.valid:not_true")
    observation_count = optional_nonnegative_int(state.get("observation_count"))
    experiment_count = optional_nonnegative_int(arm.get("experiment_count"))
    if (
        observation_count is None
        or observation_count == 0
        or observation_count != experiment_count
    ):
        failures.append(
            f"{label}.direct_allocator_provenance.observation_count:mismatch"
        )
    if state.get("failure_counts") != {}:
        failures.append(
            f"{label}.direct_allocator_provenance.failure_counts:not_empty"
        )
    if state.get("unialloc_implementation_sha256") != digest:
        failures.append(
            f"{label}.direct_allocator_provenance.implementation:mismatch"
        )
    if state.get("subject_cargo_features") != subject_value:
        failures.append(
            f"{label}.direct_allocator_provenance.subject_features:mismatch"
        )
    subject_token = state.get("subject_cargo_features_sha256")
    if not sha256_hex(subject_token) or (
        subject is not None and canonical_sha256(subject) != subject_token
    ):
        failures.append(
            f"{label}.direct_allocator_provenance.subject_features_sha256:mismatch"
        )
    safe_subject_token = (
        subject_token if isinstance(subject_token, str) else "<invalid-token>"
    )
    if state.get("observed_unialloc_implementation_sha256") != {
        safe_digest: observation_count
    }:
        failures.append(
            f"{label}.direct_allocator_provenance.observed_implementations:mismatch"
        )
    if state.get("observed_subject_cargo_feature_tokens") != {
        safe_subject_token: observation_count
    }:
        failures.append(
            f"{label}.direct_allocator_provenance.observed_subject_tokens:mismatch"
        )
    if state.get("observed_subject_cargo_features") != {
        safe_subject_token: subject_value
    }:
        failures.append(
            f"{label}.direct_allocator_provenance.observed_subject_records:mismatch"
        )
    return (
        digest if sha256_hex(digest) else None,
        subject,
        failures,
    )


def validate_reclaim_feature_provenance(
    required: dict[str, dict[str, Any] | None],
) -> tuple[bool, list[str], dict[str, Any]]:
    specs = (
        ("vulnerable_reclaim_plain", RECLAIM_PLAIN_FEATURES),
        ("patched_reclaim_plain", RECLAIM_PLAIN_FEATURES),
        ("vulnerable_reclaim_checks", RECLAIM_CHECKS_FEATURES),
        ("patched_reclaim_checks", RECLAIM_CHECKS_FEATURES),
    )
    records: dict[str, dict[str, Any]] = {}
    implementation_digests: dict[str, str] = {}
    subject_feature_records: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for label, expected_features in specs:
        arm = required.get(label)
        if arm is None:
            failures.append(f"{label}:missing_arm")
            continue
        record, arm_failures = validate_cargo_metadata_attestation(
            arm,
            label=label,
            expected_features=expected_features,
        )
        failures.extend(arm_failures)
        if record is not None:
            records[label] = record
        expected_allocator = (
            "reclaim_plain" if label.endswith("reclaim_plain") else "reclaim_checks"
        )
        digest, subject, direct_failures = validate_direct_allocator_provenance(
            arm,
            label=label,
            expected_allocator=expected_allocator,
        )
        failures.extend(direct_failures)
        if digest is not None:
            implementation_digests[label] = digest
        if subject is not None:
            subject_feature_records[label] = subject

    if len(records) == len(specs):
        package_ids = {record.get("package_id") for record in records.values()}
        manifests = {
            (
                str(UNIALLOC_MANIFEST)
                if record.get("manifest_path")
                == str(UNIALLOC_MANIFEST.relative_to(ROOT))
                else record.get("manifest_path")
            )
            for record in records.values()
        }
        if len(package_ids) != 1:
            failures.append("reclaim_attestations.package_id:mixed")
        if manifests != {str(UNIALLOC_MANIFEST)}:
            failures.append("reclaim_attestations.manifest_path:mixed_or_non_repo")
        plain_tokens = {
            records[label].get("feature_contract_sha256")
            for label in ("vulnerable_reclaim_plain", "patched_reclaim_plain")
        }
        checks_tokens = {
            records[label].get("feature_contract_sha256")
            for label in (
                "vulnerable_reclaim_checks",
                "patched_reclaim_checks",
            )
        }
        if len(plain_tokens) != 1:
            failures.append("reclaim_plain_attestations.feature_contract:mixed")
        if len(checks_tokens) != 1:
            failures.append("reclaim_checks_attestations.feature_contract:mixed")
        if plain_tokens == checks_tokens:
            failures.append("reclaim_attestations.feature_contract:missing_delta")

    if len(implementation_digests) == len(specs):
        if len(set(implementation_digests.values())) != 1:
            failures.append(
                "reclaim_attestations.unialloc_implementation_sha256:mixed"
            )

    def matched_subject_contract(record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key != "allocator_variant"
        }

    if len(subject_feature_records) == len(specs):
        for archive in ("vulnerable", "patched"):
            plain_label = f"{archive}_reclaim_plain"
            checks_label = f"{archive}_reclaim_checks"
            if matched_subject_contract(
                subject_feature_records[plain_label]
            ) != matched_subject_contract(subject_feature_records[checks_label]):
                failures.append(
                    f"{archive}_reclaim_subject_cargo_features:mismatch"
                )

    evidence = {
        label: {
            "manifest_path": record.get("manifest_path"),
            "package_id": record.get("package_id"),
            "resolved_features": record.get("resolved_features"),
            "feature_contract_sha256": record.get("feature_contract_sha256"),
            "unialloc_implementation_sha256": implementation_digests.get(label),
            "subject_cargo_features": subject_feature_records.get(label),
        }
        for label, record in sorted(records.items())
    }
    return not failures, sorted(set(failures)), evidence


def valid_patched_treatment(
    arm: dict[str, Any],
    repetitions: int,
    *,
    expected_runtime_mode: str | None = None,
    expected_compile_errors: dict[str, int] | None = None,
    expected_control_fingerprints: dict[str, int] | None = None,
) -> tuple[bool, str]:
    treatment_repetitions = nonnegative_int(
        arm.get("repetition_count"), "patched treatment repetition_count"
    )
    if not completed(arm):
        return False, "patched_treatment_incomplete"
    if sum(signature_counts(arm, "allocator_signal_signatures").values()) != 0:
        return False, "patched_treatment_allocator_signal"
    clean_exit = (
        treatment_repetitions == repetitions
        and nonnegative_int(arm.get("patched_clean_count"), "patched_clean_count")
        == repetitions
    )
    safe_panic = (
        treatment_repetitions == repetitions
        and signature_counts(arm, "native_diagnostic_signatures")
        == {"rust_panic_observed": repetitions}
    )
    if expected_runtime_mode == "clean" and clean_exit:
        return True, "matched_clean_exit"
    if expected_runtime_mode == "nonclean":
        if not expected_control_fingerprints:
            return False, "patched_system_control_fingerprint_missing"
        treatment_control_fingerprints = signature_counts(
            arm, "patched_control_fingerprints"
        )
        if (
            safe_panic
            and one_signature_in_every_repetition(
                expected_control_fingerprints, repetitions
            )
            and treatment_control_fingerprints == expected_control_fingerprints
        ):
            return True, "matched_safe_panic_fingerprint"
        return False, "patched_treatment_control_fingerprint_mismatch"
    compile_errors = signature_counts(arm, "matched_compile_error_codes")
    if (
        treatment_repetitions == 0
        and expected_compile_errors
        and compile_errors == expected_compile_errors
    ):
        return True, "matched_compile_rejection"
    if expected_runtime_mode is not None:
        return False, "patched_treatment_outcome_mismatch"
    return False, "patched_treatment_unmatched_outcome"


def evaluate_reclaim_case(
    case: dict[str, Any], *, minimum_repetitions: int
) -> dict[str, Any] | None:
    advisory_id = case.get("advisory_id")
    case_id = case.get("case_id")
    if not isinstance(advisory_id, str) or not isinstance(case_id, str):
        raise ResultError("case requires advisory_id and case_id")
    arms = arm_index(case)
    treatment = arms.get(("vulnerable", "reclaim_checks"))
    if treatment is None:
        return None

    required = {
        "vulnerable_system": arms.get(("vulnerable", "system")),
        "vulnerable_reclaim_plain": arms.get(("vulnerable", "reclaim_plain")),
        "vulnerable_reclaim_checks": treatment,
        "patched_system": arms.get(("patched", "system")),
        "patched_reclaim_plain": arms.get(("patched", "reclaim_plain")),
        "patched_reclaim_checks": arms.get(("patched", "reclaim_checks")),
    }
    missing = sorted(name for name, arm in required.items() if arm is None)
    common = {
        "advisory_id": advisory_id,
        "case_id": case_id,
        "mechanism": "reclaim_checks",
    }
    inconclusive_semantics = {
        "true_positive": False,
        "result_semantics": "evidence_gap",
        "positive_scope": None,
        "negative_boundary": None,
    }
    scenario_id, scenario_ids, binding_error = unique_scenario_binding(case, arms)
    if binding_error is not None:
        return {
            **common,
            **inconclusive_semantics,
            "outcome": "inconclusive",
            "reason": binding_error,
            "scenario_ids": scenario_ids,
            "checks": {"unique_scenario_binding": False},
        }
    assert scenario_id is not None
    common["scenario_id"] = scenario_id
    if missing:
        return {
            **common,
            **inconclusive_semantics,
            "outcome": "inconclusive",
            "reason": "missing_matched_arms",
            "missing_arms": missing,
        }

    vulnerable_system = required["vulnerable_system"]
    vulnerable_plain = required["vulnerable_reclaim_plain"]
    patched_system = required["patched_system"]
    patched_plain = required["patched_reclaim_plain"]
    patched_treatment = required["patched_reclaim_checks"]
    assert vulnerable_system is not None
    assert vulnerable_plain is not None
    assert patched_system is not None
    assert patched_plain is not None
    assert patched_treatment is not None

    repetitions = nonnegative_int(
        treatment.get("repetition_count"), "treatment repetition_count"
    )
    baseline_repetitions = nonnegative_int(
        vulnerable_system.get("repetition_count"), "baseline repetition_count"
    )
    baseline_findings = signature_counts(
        vulnerable_system, "baseline_finding_signatures"
    )
    baseline_expected_observations = nonnegative_int(
        vulnerable_system.get("expected_oracle_observation_count"),
        "baseline expected oracle count",
    )
    baseline_valid = (
        completed(vulnerable_system)
        and repetitions >= minimum_repetitions
        and baseline_repetitions == repetitions
        and baseline_expected_observations == repetitions
        and one_signature_in_every_repetition(baseline_findings, repetitions)
    )
    patched_system_repetitions = nonnegative_int(
        patched_system.get("repetition_count"), "patched baseline repetition_count"
    )
    patched_system_compile_errors = signature_counts(
        patched_system, "matched_compile_error_codes"
    )
    patched_system_clean_count = nonnegative_int(
        patched_system.get("patched_clean_count"),
        "patched baseline clean count",
    )
    patched_system_expected_observations = nonnegative_int(
        patched_system.get("expected_oracle_observation_count"),
        "patched baseline expected oracle count",
    )
    patched_system_control_fingerprints = signature_counts(
        patched_system, "patched_control_fingerprints"
    )
    patched_system_clean_valid = (
        patched_system_repetitions == repetitions
        and patched_system_clean_count == repetitions
    )
    patched_system_nonclean_valid = (
        patched_system_repetitions == repetitions
        and patched_system_clean_count == 0
        and patched_system_expected_observations == repetitions
        and one_signature_in_every_repetition(
            patched_system_control_fingerprints, repetitions
        )
    )
    patched_system_runtime_valid = (
        patched_system_clean_valid or patched_system_nonclean_valid
    )
    patched_system_compile_valid = (
        patched_system_repetitions == 0
        and bool(patched_system_compile_errors)
        and nonnegative_int(
            patched_system.get("expected_oracle_observation_count"),
            "patched baseline expected oracle count",
        )
        >= 1
    )
    patched_system_valid = (
        completed(patched_system)
        and (patched_system_runtime_valid or patched_system_compile_valid)
        and sum(
            signature_counts(
                patched_system, "allocator_signal_signatures"
            ).values()
        )
        == 0
    )
    if patched_system_compile_valid:
        patched_system_runtime_mode = None
    elif patched_system_clean_valid:
        patched_system_runtime_mode = "clean"
    else:
        patched_system_runtime_mode = "nonclean"

    plain_repetitions = nonnegative_int(
        vulnerable_plain.get("repetition_count"),
        "vulnerable reclaim_plain repetition_count",
    )
    plain_signatures = signature_counts(
        vulnerable_plain, "allocator_signal_signatures"
    )
    plain_native_diagnostics = signature_counts(
        vulnerable_plain, "native_diagnostic_signatures"
    )
    plain_outcome_fingerprints = signature_counts(
        vulnerable_plain, "native_outcome_fingerprints"
    )
    plain_fingerprint_failures = signature_counts(
        vulnerable_plain, "native_outcome_fingerprint_failures"
    )
    plain_clean_exit_count = optional_nonnegative_int(
        vulnerable_plain.get("clean_exit_count")
    )
    plain_incomplete_statuses = incomplete_oracle_statuses(vulnerable_plain)
    plain_stable_abnormal, plain_abnormal_signatures = (
        stable_native_abnormal_signal(vulnerable_plain, repetitions)
    )
    plain_blocking_statuses = [
        status
        for status in plain_incomplete_statuses
        if not (
            plain_stable_abnormal
            and status == "native_abnormal_exit_inconclusive"
        )
    ]
    plain_status_valid = (
        completed(vulnerable_plain)
        and repetitions >= minimum_repetitions
        and plain_repetitions == repetitions
        and not plain_blocking_statuses
    )
    plain_signal_free = not plain_signatures
    plain_stable_clean = (
        plain_status_valid
        and plain_clean_exit_count == repetitions
        and plain_signal_free
        and not plain_native_diagnostics
        and not plain_outcome_fingerprints
        and not plain_fingerprint_failures
    )
    plain_stable_panic = (
        plain_status_valid
        and plain_signal_free
        and plain_clean_exit_count == 0
        and plain_native_diagnostics == {"rust_panic_observed": repetitions}
        and one_signature_in_every_repetition(
            plain_outcome_fingerprints, repetitions
        )
        and not plain_fingerprint_failures
    )
    plain_stable_native_abnormal = (
        plain_status_valid
        and plain_clean_exit_count == 0
        and plain_signal_free
        and plain_stable_abnormal
        and one_signature_in_every_repetition(
            plain_outcome_fingerprints, repetitions
        )
        and not plain_fingerprint_failures
    )
    plain_execution_valid = (
        plain_stable_clean
        or plain_stable_panic
        or plain_stable_native_abnormal
    )
    if plain_stable_clean:
        plain_mode = "stable_clean_exit"
    elif plain_stable_panic:
        plain_mode = "stable_safe_panic_fingerprint"
    elif plain_stable_native_abnormal:
        plain_mode = "stable_normalized_native_abnormal_signal"
    elif plain_blocking_statuses:
        plain_mode = "incomplete_or_abnormal_oracle_status"
    else:
        plain_mode = "outcome_completeness_not_established"

    treatment_signatures = signature_counts(
        treatment, "allocator_signal_signatures"
    )
    treatment_native_diagnostics = signature_counts(
        treatment, "native_diagnostic_signatures"
    )
    treatment_outcome_fingerprints = signature_counts(
        treatment, "native_outcome_fingerprints"
    )
    treatment_fingerprint_failures = signature_counts(
        treatment, "native_outcome_fingerprint_failures"
    )
    treatment_clean_exit_count = optional_nonnegative_int(
        treatment.get("clean_exit_count")
    )
    treatment_incomplete_statuses = incomplete_oracle_statuses(treatment)
    treatment_stable_abnormal, treatment_abnormal_signatures = (
        stable_native_abnormal_signal(treatment, repetitions)
    )
    treatment_blocking_statuses = [
        status
        for status in treatment_incomplete_statuses
        if not (
            treatment_stable_abnormal
            and status == "native_abnormal_exit_inconclusive"
        )
    ]
    treatment_status_valid = (
        completed(treatment) and repetitions >= minimum_repetitions
        and not treatment_blocking_statuses
    )
    treatment_valid = (
        treatment_status_valid
        and set(treatment_signatures).issubset(EXACT_RECLAIM_SIGNATURES)
        and sum(treatment_signatures.values()) == repetitions
    )
    treatment_stable_clean = (
        treatment_status_valid
        and treatment_clean_exit_count == repetitions
        and not treatment_signatures
        and not treatment_native_diagnostics
        and not treatment_outcome_fingerprints
        and not treatment_fingerprint_failures
    )
    patched_treatment_control_fingerprints = signature_counts(
        patched_treatment, "patched_control_fingerprints"
    )
    treatment_safe_panic = (
        treatment_status_valid
        and not treatment_signatures
        and treatment_clean_exit_count == 0
        and treatment_native_diagnostics == {"rust_panic_observed": repetitions}
        and one_signature_in_every_repetition(
            treatment_outcome_fingerprints, repetitions
        )
        and treatment_outcome_fingerprints
        == patched_system_control_fingerprints
        == patched_treatment_control_fingerprints
        and not treatment_fingerprint_failures
    )
    treatment_matched_vulnerable_panic = (
        treatment_status_valid
        and plain_stable_panic
        and not treatment_signatures
        and treatment_clean_exit_count == 0
        and treatment_native_diagnostics == {"rust_panic_observed": repetitions}
        and treatment_outcome_fingerprints == plain_outcome_fingerprints
        and not treatment_fingerprint_failures
    )
    treatment_matched_native_abnormal = (
        treatment_status_valid
        and plain_stable_native_abnormal
        and not treatment_signatures
        and treatment_clean_exit_count == 0
        and treatment_stable_abnormal
        and treatment_abnormal_signatures == plain_abnormal_signatures
        and one_signature_in_every_repetition(
            treatment_outcome_fingerprints, repetitions
        )
        and treatment_outcome_fingerprints == plain_outcome_fingerprints
        and not treatment_fingerprint_failures
    )
    treatment_execution_valid = (
        treatment_valid
        or treatment_stable_clean
        or treatment_safe_panic
        or treatment_matched_vulnerable_panic
        or treatment_matched_native_abnormal
    )
    if treatment_valid:
        treatment_mode = "exact_allocator_signal"
    elif treatment_stable_clean:
        treatment_mode = "stable_clean_exit"
    elif treatment_safe_panic:
        treatment_mode = "matched_safe_panic_control_fingerprint"
    elif treatment_matched_vulnerable_panic:
        treatment_mode = "matched_vulnerable_plain_panic_fingerprint"
    elif treatment_matched_native_abnormal:
        treatment_mode = "matched_vulnerable_plain_native_abnormal_signal"
    elif treatment_blocking_statuses:
        treatment_mode = "incomplete_or_abnormal_oracle_status"
    else:
        treatment_mode = "outcome_completeness_not_established"
    patched_plain_valid, patched_plain_mode = valid_patched_treatment(
        patched_plain,
        repetitions,
        expected_runtime_mode=patched_system_runtime_mode,
        expected_compile_errors=patched_system_compile_errors,
        expected_control_fingerprints=patched_system_control_fingerprints,
    )
    patched_valid, patched_mode = valid_patched_treatment(
        patched_treatment,
        repetitions,
        expected_runtime_mode=patched_system_runtime_mode,
        expected_compile_errors=patched_system_compile_errors,
        expected_control_fingerprints=patched_system_control_fingerprints,
    )
    feature_provenance_valid, feature_provenance_failures, feature_provenance = (
        validate_reclaim_feature_provenance(required)
    )

    checks = {
        "baseline_reproduced_in_all_repetitions": baseline_valid,
        "vulnerable_reclaim_plain_completed_in_all_repetitions": (
            plain_execution_valid
        ),
        "vulnerable_reclaim_plain_exact_signal_absent": plain_signal_free,
        "vulnerable_treatment_status_valid": treatment_status_valid,
        "vulnerable_treatment_completed_in_all_repetitions": (
            treatment_execution_valid
        ),
        "exact_allocator_signal_in_all_repetitions": treatment_valid,
        "patched_system_control_valid": patched_system_valid,
        "patched_reclaim_plain_control_valid": patched_plain_valid,
        "patched_treatment_signal_free": patched_valid,
        "feature_matched_reclaim_ablation_valid": feature_provenance_valid,
    }
    detected = (
        baseline_valid
        and plain_execution_valid
        and plain_signal_free
        and treatment_valid
        and patched_system_valid
        and patched_plain_valid
        and patched_valid
        and feature_provenance_valid
    )
    no_signal = (
        baseline_valid
        and plain_execution_valid
        and plain_signal_free
        and treatment_execution_valid
        and not treatment_valid
        and patched_system_valid
        and patched_plain_valid
        and patched_valid
        and feature_provenance_valid
    )
    if detected:
        outcome = "detected"
        reason = "feature_matched_exact_allocator_diagnostic"
        true_positive = True
        result_semantics = "exact_diagnostic_true_positive"
        positive_scope = "duplicate_reclaim_event_in_integrated_witness"
        negative_boundary = None
    elif no_signal:
        outcome = "no_signal"
        reason = "feature_matched_execution_without_exact_allocator_diagnostic"
        true_positive = False
        result_semantics = "matched_negative_without_exact_reclaim_diagnostic"
        positive_scope = None
        negative_boundary = (
            "no_exact_reclaim_diagnostic_in_feature_matched_integrated_witness"
        )
    else:
        outcome = "inconclusive"
        reason = "matched_evidence_requirements_not_met"
        true_positive = False
        result_semantics = "evidence_gap"
        positive_scope = None
        negative_boundary = None
    return {
        **common,
        "outcome": outcome,
        "reason": reason,
        "true_positive": true_positive,
        "result_semantics": result_semantics,
        "positive_scope": positive_scope,
        "negative_boundary": negative_boundary,
        "repetitions": repetitions,
        "baseline_finding_signatures": baseline_findings,
        "vulnerable_reclaim_plain_mode": plain_mode,
        "vulnerable_reclaim_plain_allocator_signal_signatures": (
            plain_signatures
        ),
        "vulnerable_reclaim_plain_native_abnormal_signal_signatures": (
            plain_abnormal_signatures
        ),
        "allocator_signal_signatures": treatment_signatures,
        "vulnerable_treatment_mode": treatment_mode,
        "vulnerable_treatment_clean_exit_count": treatment_clean_exit_count,
        "vulnerable_treatment_native_outcome_fingerprints": (
            treatment_outcome_fingerprints
        ),
        "vulnerable_treatment_native_abnormal_signal_signatures": (
            treatment_abnormal_signatures
        ),
        "vulnerable_treatment_incomplete_oracle_statuses": (
            treatment_incomplete_statuses
        ),
        "feature_provenance": feature_provenance,
        "feature_provenance_failures": feature_provenance_failures,
        "patched_reclaim_plain_control_mode": patched_plain_mode,
        "patched_control_mode": patched_mode,
        "checks": checks,
        "claim_scope": "exact duplicate-reclaim detection in the integrated witness",
    }


def evaluate_type_isolation_edge(
    scenario: dict[str, Any], *, minimum_repetitions: int
) -> dict[str, Any]:
    advisory_id = scenario.get("advisory_id")
    case_id = scenario.get("case_id")
    scenario_id = scenario.get("scenario_id")
    if not all(isinstance(value, str) for value in (advisory_id, case_id, scenario_id)):
        raise ResultError(
            "derived reuse scenario requires advisory, case, and scenario ids"
        )
    evidence_gaps: list[str] = []
    raw_repetitions = scenario.get("repetitions_requested")
    if (
        isinstance(raw_repetitions, bool)
        or not isinstance(raw_repetitions, int)
        or raw_repetitions < 0
    ):
        repetitions = 0
        evidence_gaps.append(
            "scenario.repetitions_requested:missing_or_invalid_nonnegative_integer"
        )
    else:
        repetitions = raw_repetitions
    edge_value = scenario.get("edge_evaluation")
    edge = edge_value if isinstance(edge_value, dict) else {}
    automatic_probe_value = scenario.get("automatic_edge_identity_probe", False)
    automatic_probe_valid = isinstance(automatic_probe_value, bool)
    if not automatic_probe_valid:
        evidence_gaps.append(
            "scenario.automatic_edge_identity_probe:invalid_boolean"
        )
    automatic_probe = (
        automatic_probe_value if automatic_probe_valid else False
    )
    raw_arms = scenario.get("arms", [])
    if not isinstance(raw_arms, list):
        raise ResultError("derived reuse arms must be a list")
    arms: dict[tuple[str, str], dict[str, Any]] = {}
    for arm in raw_arms:
        if not isinstance(arm, dict):
            raise ResultError("derived reuse arm must be an object")
        key = (arm.get("archive_variant"), arm.get("allocator_variant"))
        if not all(isinstance(value, str) for value in key):
            raise ResultError(
                "derived reuse arm requires archive and allocator variants"
            )
        if key in arms:
            raise ResultError(f"duplicate derived reuse arm: {key}")
        arms[key] = arm

    required: dict[str, dict[str, Any] | None] = {
        "vulnerable_system": arms.get(("vulnerable", "system")),
        "vulnerable_typed_plain": arms.get(("vulnerable", "typed_plain")),
        "vulnerable_typeiso": arms.get(("vulnerable", "typeiso")),
        "patched_system": arms.get(("patched", "system")),
        "patched_typed_plain": arms.get(("patched", "typed_plain")),
        "patched_typeiso": arms.get(("patched", "typeiso")),
    }
    missing = sorted(name for name, arm in required.items() if arm is None)

    def count(
        label: str, arm: dict[str, Any] | None, field: str
    ) -> int | None:
        if arm is None:
            return None
        if field not in arm:
            evidence_gaps.append(f"{label}.{field}:missing")
            return None
        value = arm[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            evidence_gaps.append(
                f"{label}.{field}:invalid_nonnegative_integer"
            )
            return None
        return value

    def true_field(
        label: str, arm: dict[str, Any] | None, field: str
    ) -> bool:
        if arm is None:
            return False
        if field not in arm:
            evidence_gaps.append(f"{label}.{field}:missing")
            return False
        value = arm[field]
        if not isinstance(value, bool):
            evidence_gaps.append(f"{label}.{field}:invalid_boolean")
            return False
        return value

    def boolean_field(
        label: str, arm: dict[str, Any] | None, field: str
    ) -> bool | None:
        if arm is None:
            return None
        if field not in arm:
            evidence_gaps.append(f"{label}.{field}:missing")
            return None
        value = arm[field]
        if not isinstance(value, bool):
            evidence_gaps.append(f"{label}.{field}:invalid_boolean")
            return None
        return value

    def completed_repetitions(
        label: str,
        arm: dict[str, Any] | None,
        *,
        require_clean_exit: bool,
    ) -> bool:
        if arm is None:
            return False
        if "execution_status" not in arm:
            evidence_gaps.append(f"{label}.execution_status:missing")
            status_valid = False
        else:
            status = arm["execution_status"]
            if not isinstance(status, str):
                evidence_gaps.append(f"{label}.execution_status:invalid_string")
                status_valid = False
            else:
                status_valid = status == "completed"
        if "exit_codes" not in arm:
            evidence_gaps.append(f"{label}.exit_codes:missing")
            return False
        exit_codes = arm["exit_codes"]
        if not isinstance(exit_codes, list) or not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in exit_codes
        ):
            evidence_gaps.append(f"{label}.exit_codes:invalid_integer_list")
            return False
        repetitions_valid = len(exit_codes) == repetitions
        clean_valid = not require_clean_exit or all(value == 0 for value in exit_codes)
        return status_valid and repetitions_valid and clean_valid

    vulnerable_system = required["vulnerable_system"]
    vulnerable_typed_plain = required["vulnerable_typed_plain"]
    vulnerable_typeiso = required["vulnerable_typeiso"]
    patched_system = required["patched_system"]
    patched_typed_plain = required["patched_typed_plain"]
    patched_typeiso = required["patched_typeiso"]

    system_reuse = count(
        "vulnerable_system", vulnerable_system, "address_reuse_observation_count"
    )
    system_denials = count(
        "vulnerable_system",
        vulnerable_system,
        "matching_reuse_denial_event_count",
    )
    ablation_reuse = count(
        "vulnerable_typed_plain",
        vulnerable_typed_plain,
        "address_reuse_observation_count",
    )
    ablation_denials = count(
        "vulnerable_typed_plain",
        vulnerable_typed_plain,
        "matching_reuse_denial_event_count",
    )
    typeiso_reuse = count(
        "vulnerable_typeiso",
        vulnerable_typeiso,
        "address_reuse_observation_count",
    )
    typeiso_denials = count(
        "vulnerable_typeiso",
        vulnerable_typeiso,
        "matching_reuse_denial_event_count",
    )
    typeiso_bound_reports = count(
        "vulnerable_typeiso",
        vulnerable_typeiso,
        "bound_replacement_site_count",
    )
    patched_system_denials = count(
        "patched_system", patched_system, "matching_reuse_denial_event_count"
    )
    patched_ablation_denials = count(
        "patched_typed_plain",
        patched_typed_plain,
        "matching_reuse_denial_event_count",
    )
    patched_typeiso_denials = count(
        "patched_typeiso",
        patched_typeiso,
        "matching_reuse_denial_event_count",
    )
    patched_typeiso_bound_reports = count(
        "patched_typeiso", patched_typeiso, "bound_replacement_site_count"
    )
    patched_system_reuse = count(
        "patched_system", patched_system, "address_reuse_observation_count"
    )
    patched_ablation_reuse = count(
        "patched_typed_plain",
        patched_typed_plain,
        "address_reuse_observation_count",
    )
    patched_typeiso_reuse = count(
        "patched_typeiso", patched_typeiso, "address_reuse_observation_count"
    )

    claim_scope = edge.get("claim_scope")
    if not isinstance(claim_scope, str) or not claim_scope.strip():
        annotation = scenario.get("annotation")
        claim_scope = (
            annotation.get("claim_scope")
            if isinstance(annotation, dict)
            else None
        )
    claim_scope_present = isinstance(claim_scope, str) and bool(claim_scope.strip())
    if not claim_scope_present:
        evidence_gaps.append("scenario.claim_scope:missing_or_empty")
    synthetic_reduction = case_id in SYNTHETIC_TYPEISO_REDUCTION_MARKERS
    synthetic_reduction_marker_present = (
        not synthetic_reduction
        or SYNTHETIC_TYPEISO_REDUCTION_MARKERS[case_id] in str(claim_scope)
    )
    if not synthetic_reduction_marker_present:
        evidence_gaps.append("scenario.claim_scope:synthetic_reduction_marker_missing")

    annotation_value = scenario.get("annotation")
    annotation = annotation_value if isinstance(annotation_value, dict) else {}
    automatic_coverage_contract = annotation.get(
        "automatic_compiler_coverage_contract"
    )
    vulnerability_specific_detection_boundary_valid = edge.get(
        "vulnerability_specific_detection_signal"
    ) in (None, False)
    if not vulnerability_specific_detection_boundary_valid:
        evidence_gaps.append(
            "scenario.vulnerability_specific_detection_signal:unsupported"
        )
    legacy_typeiso_semantics_absent = "causal_mitigation_true_positive" not in edge
    if not legacy_typeiso_semantics_absent:
        evidence_gaps.append("scenario.legacy_typeiso_true_positive_semantics:present")
    if automatic_probe:
        edge_identity_contract_valid = (
            edge.get("manual_victim_identity_annotation") is False
            and edge.get("compiler_automatic_victim_coverage") is True
            and edge.get("source_vulnerability_detection_validated") is False
            and edge.get(
                "causal_compiler_bound_reuse_edge_mitigation"
            )
            is True
            and edge.get("result_semantics")
            == "causal_compiler_bound_reuse_edge_mitigation"
            and isinstance(automatic_coverage_contract, dict)
            and automatic_coverage_contract.get("schema_version") == 1
            and isinstance(
                automatic_coverage_contract.get("coverage_scope"), str
            )
            and bool(automatic_coverage_contract["coverage_scope"].strip())
        )
    else:
        edge_identity_contract_valid = (
            edge.get("manual_victim_identity_annotation") is True
            and edge.get("compiler_automatic_victim_coverage") is False
            and edge.get("source_vulnerability_detection_validated") is False
        )
    if not edge_identity_contract_valid:
        evidence_gaps.append("scenario.edge_identity_contract:invalid")

    typed_oracle_policy = scenario.get("typed_allocator_expected_oracle", True)
    typed_oracle_policy_valid = isinstance(typed_oracle_policy, bool)
    if not typed_oracle_policy_valid:
        evidence_gaps.append(
            "scenario.typed_allocator_expected_oracle:invalid_boolean"
        )
        typed_oracle_policy = True
    vulnerable_typed_plain_oracle = boolean_field(
        "vulnerable_typed_plain",
        vulnerable_typed_plain,
        "expected_oracle_observed",
    )
    patched_typed_plain_oracle = boolean_field(
        "patched_typed_plain",
        patched_typed_plain,
        "expected_oracle_observed",
    )
    patched_typeiso_oracle = boolean_field(
        "patched_typeiso",
        patched_typeiso,
        "expected_oracle_observed",
    )

    checks = {
        "automatic_edge_identity_probe_mode_valid": automatic_probe_valid,
        "edge_identity_contract_valid": edge_identity_contract_valid,
        "vulnerability_specific_detection_boundary_valid": (
            vulnerability_specific_detection_boundary_valid
        ),
        "legacy_typeiso_true_positive_semantics_absent": (
            legacy_typeiso_semantics_absent
        ),
        "minimum_repetitions_met": repetitions >= minimum_repetitions,
        "orchestration_success": scenario.get("orchestration_success") is True
        and scenario.get("unexpected_arm_count") == 0,
        "matched_arms_present": not missing,
        "claim_scope_present": claim_scope_present,
        "synthetic_reduction_marker_present": (
            synthetic_reduction_marker_present
        ),
        "typed_allocator_oracle_policy_valid": typed_oracle_policy_valid,
        "vulnerable_system_completed_in_all_repetitions": completed_repetitions(
            "vulnerable_system", vulnerable_system, require_clean_exit=False
        ),
        "vulnerable_system_oracle_observed": true_field(
            "vulnerable_system", vulnerable_system, "expected_oracle_observed"
        ),
        "system_reuse_observed_in_all_repetitions": system_reuse == repetitions,
        "system_has_no_reuse_denial": system_denials == 0,
        "vulnerable_typed_plain_completed_in_all_repetitions": completed_repetitions(
            "vulnerable_typed_plain",
            vulnerable_typed_plain,
            require_clean_exit=True,
        ),
        "vulnerable_typed_plain_oracle_contract_satisfied": (
            vulnerable_typed_plain_oracle is typed_oracle_policy
        ),
        "typed_plain_reuse_observed_in_all_repetitions": (
            ablation_reuse == repetitions
        ),
        "typed_plain_has_no_reuse_denial": ablation_denials == 0,
        "vulnerable_typeiso_completed_in_all_repetitions": completed_repetitions(
            "vulnerable_typeiso", vulnerable_typeiso, require_clean_exit=False
        ),
        "typeiso_address_reuse_blocked_in_all_repetitions": typeiso_reuse == 0,
        "typeiso_denial_observed_in_all_repetitions": (
            typeiso_denials == repetitions
        ),
        "typeiso_report_bound_in_all_repetitions": (
            typeiso_bound_reports == repetitions
        ),
        "patched_system_control_valid": (
            completed_repetitions(
                "patched_system", patched_system, require_clean_exit=True
            )
            and true_field(
                "patched_system", patched_system, "expected_oracle_observed"
            )
            and patched_system_reuse == repetitions
            and patched_system_denials == 0
        ),
        "patched_typed_plain_control_valid": (
            completed_repetitions(
                "patched_typed_plain",
                patched_typed_plain,
                require_clean_exit=True,
            )
            and patched_typed_plain_oracle is typed_oracle_policy
            and patched_ablation_reuse == repetitions
            and patched_ablation_denials == 0
        ),
        "patched_typeiso_control_valid": (
            completed_repetitions(
                "patched_typeiso", patched_typeiso, require_clean_exit=True
            )
            and patched_typeiso_oracle is typed_oracle_policy
            and patched_typeiso_reuse == repetitions
            and patched_typeiso_denials == 0
            and patched_typeiso_bound_reports == 0
        ),
    }
    validated = all(checks.values())
    failed_checks = sorted(name for name, value in checks.items() if not value)
    if validated:
        reason = "matched_cross_identity_reuse_edge_blocked_and_reported"
    elif missing:
        reason = "missing_matched_arms"
    elif evidence_gaps:
        reason = "derived_reuse_arm_evidence_missing_or_invalid"
    else:
        reason = "derived_reuse_evidence_requirements_not_met"
    return {
        "advisory_id": advisory_id,
        "case_id": case_id,
        "scenario_id": scenario_id,
        "mechanism": "type_isolation",
        "outcome": "mitigated" if validated else "inconclusive",
        "reason": reason,
        "validated_mitigation": validated,
        "result_semantics": (
            "causal_compiler_bound_reuse_edge_mitigation"
            if validated
            else "evidence_gap"
        ),
        "positive_scope": (
            "exploit_enabling_cross_identity_reuse_edge" if validated else None
        ),
        "negative_boundary": None,
        "repetitions": repetitions,
        "checks": checks,
        "missing_arms": missing,
        "evidence_gaps": sorted(set(evidence_gaps)),
        "failed_checks": failed_checks,
        "claim_scope": claim_scope,
        "evidence_scope": "exploit_enabling_cross_identity_reuse_edge",
        "automatic_edge_identity_probe": automatic_probe,
        "manual_victim_identity_annotation": edge.get(
            "manual_victim_identity_annotation"
        ),
        "compiler_automatic_victim_coverage": edge.get(
            "compiler_automatic_victim_coverage"
        ),
        "source_vulnerability_detection_validated": edge.get(
            "source_vulnerability_detection_validated"
        ),
        "vulnerability_specific_detection_signal": False,
        "full_source_vulnerability_detection": False,
        "causal_compiler_bound_reuse_edge_mitigation": validated,
        "automatic_source_coverage": False,
        "claim_grade": False,
        "reduction_fidelity": (
            "compiler_automatic_synthetic_reduction"
            if automatic_probe and synthetic_reduction
            else "compiler_automatic_derived_reduction"
            if automatic_probe
            else "synthetic_manual_reduction"
            if synthetic_reduction
            else "manual_derived_reduction"
        ),
        "synthetic_reduction": synthetic_reduction,
    }


def export(
    summary: dict[str, Any] | None,
    *,
    summary_artifact: dict[str, Any] | None,
    minimum_repetitions: int,
    derived_reuse_summary: dict[str, Any] | None = None,
    derived_reuse_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if summary is None and derived_reuse_summary is None:
        raise ResultError(
            "at least one of summary or derived reuse summary is required"
        )

    results = []
    if summary is not None:
        if summary.get("schema_version") != 1:
            raise ResultError("unsupported summary schema")
        cases = summary.get("cases")
        if not isinstance(cases, list):
            raise ResultError("summary cases must be a list")
        for case in cases:
            if not isinstance(case, dict):
                raise ResultError("summary case must be an object")
            result = evaluate_reclaim_case(
                case, minimum_repetitions=minimum_repetitions
            )
            if result is not None:
                results.append({**result, "evidence": summary_artifact})
    if derived_reuse_summary is not None:
        if derived_reuse_summary.get("schema_version") != 1:
            raise ResultError("unsupported derived reuse summary schema")
        scenarios = derived_reuse_summary.get("scenarios")
        if not isinstance(scenarios, list):
            raise ResultError("derived reuse summary scenarios must be a list")
        if derived_reuse_artifact is None:
            raise ResultError("derived reuse artifact metadata is required")
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                raise ResultError("derived reuse scenario must be an object")
            results.append(
                {
                    **evaluate_type_isolation_edge(
                        scenario, minimum_repetitions=minimum_repetitions
                    ),
                    "evidence": derived_reuse_artifact,
                }
            )
    results.sort(key=lambda row: (row["advisory_id"], row["case_id"]))
    counts: dict[str, int] = {}
    for result in results:
        outcome = result["outcome"]
        counts[outcome] = counts.get(outcome, 0) + 1
    output = {
        "schema_version": 1,
        "source": "unialloc-rustsec-mechanism-results",
        "claim_grade": False,
        "boundary": (
            "Detector true positives reproduce an exact allocator diagnostic in "
            "every vulnerable repetition with matched baseline and patched controls. "
            "Type Isolation validated mitigations causally block and report measured "
            "compiler-bound cross-identity reuse edges. These rows carry "
            "zero credit for vulnerability-specific or full-source detection. "
            "No-signal rows are "
            "feature-matched negatives for the integrated witness only."
        ),
        "minimum_repetitions": minimum_repetitions,
        "derived_reuse_input": derived_reuse_artifact,
        "counts": dict(sorted(counts.items())),
        "results": results,
    }
    if summary is not None:
        output["summary_input"] = summary_artifact
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=pathlib.Path)
    parser.add_argument("--derived-reuse-summary", type=pathlib.Path)
    parser.add_argument("--minimum-repetitions", type=int, default=3)
    parser.add_argument("--output", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.minimum_repetitions <= 0:
            raise ResultError("minimum repetitions must be positive")
        summary_path = (
            args.summary.expanduser().resolve()
            if args.summary is not None
            else None
        )
        derived_path = (
            args.derived_reuse_summary.expanduser().resolve()
            if args.derived_reuse_summary is not None
            else None
        )
        if summary_path is None and derived_path is None:
            raise ResultError(
                "at least one of --summary or --derived-reuse-summary is required"
            )
        result = export(
            load_json(summary_path) if summary_path else None,
            summary_artifact=artifact(summary_path) if summary_path else None,
            minimum_repetitions=args.minimum_repetitions,
            derived_reuse_summary=load_json(derived_path) if derived_path else None,
            derived_reuse_artifact=artifact(derived_path) if derived_path else None,
        )
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    except (OSError, ResultError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
