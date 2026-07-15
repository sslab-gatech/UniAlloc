#!/usr/bin/env python3
"""Summarize RustSec heap-expansion experiments without inferring mitigation.

The input experiment files are the raw ``experiment.json`` artifacts emitted by
``run_rustsec_heap_experiment.py``.  Multiple files may be assigned to one case
so system ground-truth arms and typed allocator arms can be run independently
and merged afterwards.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import re
import signal
import sys
from typing import Any, Iterable


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_rustsec_heap_experiment as experiment_runner  # noqa: E402


DEFAULT_CATALOG = (
    ROOT / "evaluation" / "config" / "rustsec_heap_expansion_harnesses.json"
)
DEFAULT_CORPUS = ROOT / "evaluation" / "config" / "rustsec_heap_security_corpus.json"
CASE_RE = re.compile(r"^RSH-\d{3}$")
ALLOCATOR_ORDER = {
    "system": 0,
    "unialloc": 1,
    "reclaim_plain": 2,
    "reclaim_checks": 3,
    "typed_plain": 4,
    "typeiso": 5,
}
ARCHIVE_ORDER = {"vulnerable": 0, "patched": 1}
CLASSIFICATION_ORDER = {
    "insufficient_evidence": 0,
    "direct_effect_absent": 1,
    "baseline_reproduced": 2,
    "allocator_signal_observed": 3,
    "typeiso_specific_signal": 4,
}
ALLOCATOR_NATIVE_DIAGNOSTICS = frozenset(
    {
        "unialloc_pointer_already_released_check",
    }
)
SYNCHRONOUS_NATIVE_SIGNAL_NAMES = (
    "SIGABRT",
    "SIGBUS",
    "SIGFPE",
    "SIGILL",
    "SIGSEGV",
    "SIGTRAP",
)
RUST_PANIC_HEADER_RE = re.compile(
    r"(?m)^thread '[^'\r\n]+'(?: \([^\r\n)]*\))? panicked at "
    r"(?P<source>[^\r\n]+?):[1-9][0-9]*(?::[1-9][0-9]*)?:\r?$"
)
HEX_ADDRESS_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
RUST_SOURCE_LOCATION_RE = re.compile(r"(?P<path>\S+\.rs):[1-9][0-9]*:[1-9][0-9]*")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PANIC_MESSAGE_TERMINATORS = ("note:", "stack backtrace:")
DESTRUCTOR_CLEANUP_ABORT_CHECKPOINT = (
    "core/src/panicking.rs",
    "panic in a destructor during cleanup\n"
    "thread caused non-unwinding panic. aborting.",
)
EXPLICIT_CHECKPOINT_FIELDS = (
    "critical_checkpoint",
    "critical_site_checkpoint",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIRECT_CARGO_ATTESTATION_ALLOCATORS = frozenset(
    {"unialloc", "reclaim_plain", "reclaim_checks"}
)
UNIALLOC_MANIFEST = (ROOT / "unialloc" / "Cargo.toml").resolve()
UNIALLOC_PACKAGE_ID_PREFIX = f"path+{UNIALLOC_MANIFEST.parent.as_uri()}#"
RECLAIM_FEATURE_CONTRACTS = {
    "reclaim_plain": ["stats"],
    "reclaim_checks": ["reclaim_checks", "stats"],
}
CARGO_ATTESTATION_FIELDS = frozenset(
    {
        "schema_version",
        "cargo_metadata_stdout_sha256",
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
)
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


class SummaryError(RuntimeError):
    """Raised when an input cannot support a fail-closed summary."""


def artifact_record(path: pathlib.Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise SummaryError(f"cannot read experiment artifact {path}: {error}") from error
    try:
        recorded_path = str(resolved.relative_to(ROOT))
    except ValueError:
        recorded_path = str(resolved)
    return {
        "path": recorded_path,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def load_json(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SummaryError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise SummaryError(f"{label} must contain a JSON object: {path}")
    return value


def load_catalog(path: pathlib.Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    catalog = load_json(path, "catalog")
    if catalog.get("schema_version") != 1:
        raise SummaryError("unsupported expansion catalog schema")
    raw_cases = catalog.get("cases")
    if not isinstance(raw_cases, list):
        raise SummaryError("expansion catalog cases must be a list")
    cases: dict[str, dict[str, Any]] = {}
    for case in raw_cases:
        if not isinstance(case, dict):
            raise SummaryError("expansion catalog case must be an object")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not CASE_RE.fullmatch(case_id):
            raise SummaryError(f"invalid expansion case id: {case_id!r}")
        if case_id in cases:
            raise SummaryError(f"duplicate expansion case id: {case_id}")
        scenarios = case.get("scenarios")
        if not isinstance(scenarios, list) or not scenarios:
            raise SummaryError(f"missing scenarios for {case_id}")
        scenario_ids: set[str] = set()
        for scenario in scenarios:
            if not isinstance(scenario, dict) or not isinstance(
                scenario.get("scenario_id"), str
            ):
                raise SummaryError(f"invalid scenario for {case_id}")
            scenario_ids.add(scenario["scenario_id"])
        case = dict(case)
        case["_scenario_ids"] = scenario_ids
        cases[case_id] = case
    return catalog, cases


def corpus_advisory_index(path: pathlib.Path) -> dict[str, str]:
    corpus = load_json(path, "corpus")
    raw_cases = corpus.get("cases")
    if not isinstance(raw_cases, list):
        raise SummaryError("corpus cases must be a list")
    result: dict[str, str] = {}
    for case in raw_cases:
        if not isinstance(case, dict):
            raise SummaryError("corpus case must be an object")
        case_id = case.get("case_id")
        advisory_id = case.get("advisory_id")
        if not isinstance(case_id, str) or not isinstance(advisory_id, str):
            continue
        previous = result.get(case_id)
        if previous is not None and previous != advisory_id:
            raise SummaryError(f"conflicting corpus advisory for {case_id}")
        result[case_id] = advisory_id
    return result


def parse_experiment_spec(text: str) -> tuple[str, pathlib.Path]:
    if "=" not in text:
        raise SummaryError("--experiment must use CASE=path")
    case_id, path_text = text.split("=", 1)
    if not CASE_RE.fullmatch(case_id) or not path_text:
        raise SummaryError(f"invalid --experiment value: {text!r}")
    return case_id, pathlib.Path(path_text).expanduser().resolve()


def parse_experiment_allocator_spec(
    text: str,
) -> tuple[str, frozenset[str], pathlib.Path]:
    if "=" not in text:
        raise SummaryError("--experiment-allocator must use CASE:ALLOCATOR[,ALLOCATOR]=path")
    selector, path_text = text.split("=", 1)
    if ":" not in selector:
        raise SummaryError("--experiment-allocator must include an allocator selection")
    case_id, allocator_text = selector.split(":", 1)
    raw_allocators = allocator_text.split(",") if allocator_text else []
    if (
        not CASE_RE.fullmatch(case_id)
        or not path_text
        or not raw_allocators
        or any(not value for value in raw_allocators)
    ):
        raise SummaryError(f"invalid --experiment-allocator value: {text!r}")
    unknown = sorted(set(raw_allocators) - set(ALLOCATOR_ORDER))
    if unknown:
        raise SummaryError(f"unknown allocator selection: {', '.join(unknown)}")
    if len(raw_allocators) != len(set(raw_allocators)):
        raise SummaryError("duplicate allocator in experiment selection")
    return (
        case_id,
        frozenset(raw_allocators),
        pathlib.Path(path_text).expanduser().resolve(),
    )


def string_values(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SummaryError(f"{label} must be a list of strings")
    return list(value)


def count_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SummaryError(f"{label} must be a non-negative integer")
    return value


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sorted_unique_strings(value: object, label: str) -> list[str]:
    values = string_values(value, label)
    if values != sorted(values) or len(values) != len(set(values)):
        raise SummaryError(f"{label} must contain sorted unique strings")
    return values


def normalized_cargo_metadata_attestation(
    arm: dict[str, Any],
) -> tuple[str, dict[str, Any], str] | None:
    allocator = arm.get("allocator_variant")
    provenance = arm.get("allocator_provenance")
    if provenance is None:
        if allocator in DIRECT_CARGO_ATTESTATION_ALLOCATORS:
            raise SummaryError("missing allocator_provenance for direct UniAlloc arm")
        return None
    if not isinstance(provenance, dict):
        raise SummaryError("allocator_provenance must be an object")
    raw = provenance.get("cargo_metadata_attestation")
    if raw is None:
        if allocator in DIRECT_CARGO_ATTESTATION_ALLOCATORS:
            raise SummaryError("missing cargo metadata attestation")
        return None
    if allocator not in DIRECT_CARGO_ATTESTATION_ALLOCATORS:
        raise SummaryError("unexpected cargo metadata attestation for non-Cargo arm")
    if not isinstance(raw, dict):
        raise SummaryError("cargo metadata attestation must be an object")
    if set(raw) != CARGO_ATTESTATION_FIELDS:
        raise SummaryError("cargo metadata attestation fields do not match schema 1")
    if raw.get("schema_version") != 1:
        raise SummaryError("unsupported cargo metadata attestation schema")
    stdout_sha256 = raw.get("cargo_metadata_stdout_sha256")
    token = raw.get("feature_contract_sha256")
    if not isinstance(stdout_sha256, str) or not SHA256_RE.fullmatch(stdout_sha256):
        raise SummaryError("invalid cargo metadata stdout sha256")
    if not isinstance(token, str) or not SHA256_RE.fullmatch(token):
        raise SummaryError("invalid cargo feature contract sha256")
    if raw.get("package_count") != 1 or raw.get("resolve_node_count") != 1:
        raise SummaryError("cargo metadata must resolve one UniAlloc package and node")
    package_id = raw.get("package_id")
    if not isinstance(package_id, str) or not package_id.strip():
        raise SummaryError("cargo metadata package id must be a non-empty string")
    manifest_path = raw.get("manifest_path")
    if not isinstance(manifest_path, str):
        raise SummaryError("cargo metadata manifest path must be a string")
    manifest_path_input = pathlib.Path(manifest_path)
    if (
        not manifest_path_input.is_absolute()
        or manifest_path_input.resolve() != UNIALLOC_MANIFEST
    ):
        raise SummaryError("cargo metadata manifest does not bind repository UniAlloc")
    if raw.get("package_source") is not None:
        raise SummaryError("cargo metadata UniAlloc package must be a path dependency")
    if not package_id.startswith(UNIALLOC_PACKAGE_ID_PREFIX) or len(
        package_id
    ) == len(UNIALLOC_PACKAGE_ID_PREFIX):
        raise SummaryError(
            "cargo metadata package id does not bind repository UniAlloc"
        )
    expected_features = sorted_unique_strings(
        raw.get("expected_features"), "cargo metadata expected_features"
    )
    resolved_features = sorted_unique_strings(
        raw.get("resolved_features"), "cargo metadata resolved_features"
    )
    if resolved_features != expected_features:
        raise SummaryError("resolved UniAlloc features differ from expected features")
    default_expected = raw.get("default_features_expected")
    default_resolved = raw.get("default_feature_resolved")
    if not isinstance(default_expected, bool) or not isinstance(default_resolved, bool):
        raise SummaryError("cargo metadata default feature fields must be booleans")
    if default_expected != default_resolved:
        raise SummaryError("resolved UniAlloc default feature state differs from expected")
    required_features = RECLAIM_FEATURE_CONTRACTS.get(str(allocator))
    if required_features is not None and (
        expected_features != required_features or default_expected
    ):
        raise SummaryError(f"invalid {allocator} Cargo feature contract")
    contract = {
        key: raw[key]
        for key in sorted(CARGO_ATTESTATION_FIELDS)
        if key not in {"cargo_metadata_stdout_sha256", "feature_contract_sha256"}
    }
    if canonical_sha256(contract) != token:
        raise SummaryError("cargo feature contract sha256 does not match attestation")
    normalized = {
        **contract,
        "manifest_path": str(UNIALLOC_MANIFEST.relative_to(ROOT)),
        "feature_contract_sha256": token,
    }
    return token, normalized, stdout_sha256


def normalized_subject_cargo_features(
    raw: object, *, allocator: str
) -> dict[str, Any]:
    """Normalize the runner's effective subject dependency feature record."""

    if not isinstance(raw, dict) or set(raw) != SUBJECT_CARGO_FEATURE_FIELDS:
        raise SummaryError("subject Cargo feature fields do not match schema 1")
    if raw.get("allocator_variant") != allocator:
        raise SummaryError("subject Cargo feature allocator does not match arm")
    crate = raw.get("crate")
    if not isinstance(crate, str) or not crate.strip():
        raise SummaryError("subject Cargo feature crate must be a non-empty string")

    def feature_set(field: str) -> list[str]:
        values = string_values(raw.get(field), f"subject_cargo_features.{field}")
        if any(not value for value in values) or len(values) != len(set(values)):
            raise SummaryError(
                f"subject_cargo_features.{field} must contain unique non-empty strings"
            )
        return sorted(values)

    default_features_enabled = raw.get("default_features_enabled")
    override_applied = raw.get("override_applied")
    if not isinstance(default_features_enabled, bool) or not isinstance(
        override_applied, bool
    ):
        raise SummaryError("subject Cargo feature state fields must be booleans")
    override_allocator = raw.get("override_allocator_variant")
    override_source = raw.get("override_source")
    if override_applied:
        if not isinstance(override_allocator, str) or not override_allocator:
            raise SummaryError(
                "applied subject Cargo feature override lacks allocator"
            )
        if override_source != "scenario.allocator_cargo_feature_overrides":
            raise SummaryError("subject Cargo feature override source is invalid")
    elif override_allocator is not None or override_source is not None:
        raise SummaryError("inactive subject Cargo feature override has metadata")
    return {
        "allocator_variant": allocator,
        "crate": crate,
        "catalog_features": feature_set("catalog_features"),
        "effective_features": feature_set("effective_features"),
        "default_features_enabled": default_features_enabled,
        "override_applied": override_applied,
        "override_allocator_variant": override_allocator,
        "override_source": override_source,
    }


def normalized_direct_allocator_provenance(
    arm: dict[str, Any],
) -> tuple[str, str, dict[str, Any]] | None:
    """Revalidate direct-UniAlloc provenance against the arm fingerprint."""

    allocator = arm.get("allocator_variant")
    if allocator not in DIRECT_CARGO_ATTESTATION_ALLOCATORS:
        return None
    provenance = arm.get("allocator_provenance")
    fingerprint_payload = arm.get("fingerprint_payload")
    fingerprint = arm.get("fingerprint")
    if not isinstance(provenance, dict):
        raise SummaryError("missing direct allocator provenance")
    if not isinstance(fingerprint_payload, dict):
        raise SummaryError("missing direct allocator fingerprint payload")
    if not isinstance(fingerprint, str) or not SHA256_RE.fullmatch(fingerprint):
        raise SummaryError("invalid direct allocator fingerprint")
    if canonical_sha256(fingerprint_payload) != fingerprint:
        raise SummaryError("direct allocator fingerprint does not match payload")
    if fingerprint_payload.get("allocator_variant") != allocator:
        raise SummaryError("direct allocator fingerprint variant does not match arm")
    if fingerprint_payload.get("archive_variant") != arm.get("archive_variant"):
        raise SummaryError("direct allocator fingerprint archive does not match arm")

    provenance_digest = provenance.get("unialloc_implementation_sha256")
    fingerprint_digest = fingerprint_payload.get(
        "unialloc_implementation_sha256"
    )
    if not isinstance(provenance_digest, str) or not SHA256_RE.fullmatch(
        provenance_digest
    ):
        raise SummaryError("invalid UniAlloc implementation sha256")
    if provenance_digest != fingerprint_digest:
        raise SummaryError(
            "UniAlloc implementation sha256 differs from fingerprint"
        )

    provenance_subject = provenance.get("subject_cargo_features")
    fingerprint_subject = fingerprint_payload.get("subject_cargo_features")
    if provenance_subject != fingerprint_subject:
        raise SummaryError("subject Cargo features differ from fingerprint")
    subject = normalized_subject_cargo_features(
        provenance_subject, allocator=str(allocator)
    )
    return provenance_digest, canonical_sha256(subject), subject


def normalized_panic_source(raw: str) -> str:
    source = raw.strip().replace("\\", "/")
    for marker, label in (
        ("/work/subject/", "subject/"),
        ("/work/project/", "project/"),
    ):
        if marker in source:
            return label + source.split(marker, 1)[1]
    if source.startswith("./"):
        source = source[2:]
    if not source.startswith("/"):
        return source
    if "/src/" in source:
        prefix, suffix = source.rsplit("/src/", 1)
        owner = pathlib.PurePosixPath(prefix).name
        return f"{owner}/src/{suffix}" if owner else f"src/{suffix}"
    return pathlib.PurePosixPath(source).name


def normalized_panic_message(lines: list[str]) -> str | None:
    normalized: list[str] = []
    for raw_line in lines:
        line = ANSI_ESCAPE_RE.sub("", raw_line).strip()
        if not line:
            continue
        if line.casefold().startswith(PANIC_MESSAGE_TERMINATORS):
            break
        line = HEX_ADDRESS_RE.sub("<address>", line)
        line = RUST_SOURCE_LOCATION_RE.sub(r"\g<path>", line)
        line = " ".join(line.split())
        if line:
            normalized.append(line)
    if not normalized:
        return None
    return "\n".join(normalized)


def explicit_critical_checkpoint(record: dict[str, Any]) -> str | None:
    values: list[str] = []
    for field in EXPLICIT_CHECKPOINT_FIELDS:
        value = record.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise SummaryError(f"{field} must be a non-empty string")
        normalized = RUST_SOURCE_LOCATION_RE.sub(
            lambda match: normalized_panic_source(match.group("path")), value
        )
        normalized = " ".join(
            HEX_ADDRESS_RE.sub("<address>", normalized).split()
        )
        values.append(normalized)
    if len(set(values)) > 1:
        raise SummaryError("conflicting explicit critical checkpoints")
    return values[0] if values else None


def normalized_native_outcome(
    record: dict[str, Any],
    *,
    diagnostic_signature: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Normalize one source-bound Rust panic without ephemeral run details."""

    returncode = record.get("exit_code")
    timed_out = record.get("timed_out")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        return None, "missing_returncode"
    if not isinstance(timed_out, bool):
        return None, "missing_timeout_status"
    if timed_out:
        return None, "timed_out"
    if returncode == 0:
        return None, "clean_exit"
    stderr = record.get("stderr")
    if not isinstance(stderr, str):
        return None, "missing_stderr"
    matches = list(RUST_PANIC_HEADER_RE.finditer(stderr))
    candidates: list[tuple[re.Match[str], str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(stderr)
        message = normalized_panic_message(stderr[match.end() : end].splitlines())
        if message is not None:
            candidates.append((match, message))
    if returncode == -signal.SIGABRT and len(candidates) >= 3:
        cleanup_match, cleanup_message = candidates[-1]
        cleanup_checkpoint = (
            normalized_panic_source(cleanup_match.group("source")),
            cleanup_message,
        )
        repeated = candidates[:-1]
        repeated_checkpoints = {
            (normalized_panic_source(match.group("source")), message)
            for match, message in repeated
        }
        if (
            cleanup_checkpoint == DESTRUCTOR_CLEANUP_ABORT_CHECKPOINT
            and len(repeated) >= 2
            and len(repeated_checkpoints) == 1
        ):
            candidates = [repeated[0]]
    if diagnostic_signature == "unialloc_pointer_already_released_check":
        candidates = [
            (match, message)
            for match, message in candidates
            if normalized_panic_source(match.group("source")).endswith(
                "unialloc/src/alloc_api/type_isolation.rs"
            )
            and message == "pointer already released"
        ]
    if len(candidates) != 1:
        return None, "missing_or_ambiguous_panic_checkpoint"
    match, message = candidates[0]
    outcome: dict[str, Any] = {
        "kind": "rust_panic",
        "returncode": returncode,
        "timed_out": timed_out,
        "critical_checkpoint": {
            "panic_source": normalized_panic_source(match.group("source")),
            "panic_message": message,
        },
    }
    reported_checkpoint = explicit_critical_checkpoint(record)
    if reported_checkpoint is not None:
        outcome["reported_critical_checkpoint"] = reported_checkpoint
    return outcome, None


def outcome_fingerprint(outcome: dict[str, Any]) -> str:
    canonical = json.dumps(
        outcome, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def normalized_native_abnormal_signal(record: dict[str, Any]) -> str | None:
    """Return a bounded POSIX signal class for a direct subprocess death.

    Python reports direct signal termination as a negative return code.  Shell
    encodings such as 128+signal are deliberately left unclassified because
    they are ambiguous without an additional execution contract.
    """

    returncode = record.get("exit_code")
    timed_out = record.get("timed_out")
    if (
        isinstance(returncode, bool)
        or not isinstance(returncode, int)
        or not isinstance(timed_out, bool)
        or timed_out
        or returncode >= 0
    ):
        return None
    signum = -returncode
    for name in SYNCHRONOUS_NATIVE_SIGNAL_NAMES:
        if getattr(signal, name, None) == signum:
            return f"posix_signal:{name}"
    return None


def repetition_runs(arm: dict[str, Any]) -> list[dict[str, Any]]:
    raw = arm.get("runs")
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise SummaryError("runs must be a list of objects")
    return list(raw)


def new_aggregate(case_id: str, archive: str, allocator: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "archive_variant": archive,
        "allocator_variant": allocator,
        "scenario_ids": set(),
        "experiment_count": 0,
        "execution_statuses": set(),
        "repetition_count": 0,
        "baseline_findings": collections.Counter(),
        "oracle_statuses": collections.Counter(),
        "expected_oracle_observation_count": 0,
        "matched_compile_error_codes": collections.Counter(),
        "clean_exit_count": 0,
        "patched_clean_count": 0,
        "native_diagnostics": collections.Counter(),
        "allocator_signals": collections.Counter(),
        "native_parser_signatures": collections.Counter(),
        "native_revalidation_observation_count": 0,
        "native_revalidation_run_count": 0,
        "native_revalidation_match_count": 0,
        "native_revalidation_failures": collections.Counter(),
        "native_outcome_fingerprints": collections.Counter(),
        "native_outcome_fingerprint_records": {},
        "native_outcome_fingerprint_failures": collections.Counter(),
        "native_abnormal_signal_signatures": collections.Counter(),
        "cargo_attestation_observation_count": 0,
        "cargo_attestation_tokens": collections.Counter(),
        "cargo_attestation_records": {},
        "cargo_metadata_stdout_hashes": collections.Counter(),
        "cargo_attestation_failures": collections.Counter(),
        "direct_provenance_observation_count": 0,
        "unialloc_implementation_hashes": collections.Counter(),
        "subject_cargo_feature_tokens": collections.Counter(),
        "subject_cargo_feature_records": {},
        "direct_provenance_failures": collections.Counter(),
        "typed_allocation_count": 0,
        "fallback_allocation_count": 0,
        "allocation_stats_observation_count": 0,
        "compiler_rewrite_counts": collections.Counter(),
        "compiler_audit_observation_count": 0,
        "eligibility_blockers": set(),
    }


def collect_cargo_metadata_attestation(
    target: dict[str, Any], arm: dict[str, Any]
) -> None:
    try:
        attestation = normalized_cargo_metadata_attestation(arm)
    except SummaryError as error:
        target["cargo_attestation_failures"][str(error)] += 1
        return
    if attestation is None:
        return
    token, record, stdout_sha256 = attestation
    target["cargo_attestation_observation_count"] += 1
    target["cargo_attestation_tokens"][token] += 1
    target["cargo_metadata_stdout_hashes"][stdout_sha256] += 1
    previous = target["cargo_attestation_records"].setdefault(token, record)
    if previous != record:
        target["cargo_attestation_failures"][
            "feature contract token maps to conflicting records"
        ] += 1


def cargo_metadata_attestation_state(arm: dict[str, Any]) -> dict[str, Any]:
    required = arm["allocator_variant"] in DIRECT_CARGO_ATTESTATION_ALLOCATORS
    failures = collections.Counter(arm["cargo_attestation_failures"])
    if len(arm["cargo_attestation_tokens"]) > 1:
        failures["mixed cargo feature contract tokens"] += 1
    valid = (
        not failures
        and (
            not required
            or (
                arm["experiment_count"] > 0
                and arm["cargo_attestation_observation_count"]
                == arm["experiment_count"]
                and len(arm["cargo_attestation_tokens"]) == 1
                and len(arm["cargo_attestation_records"]) == 1
            )
        )
    )
    token = (
        next(iter(arm["cargo_attestation_tokens"]))
        if valid and required
        else None
    )
    record = arm["cargo_attestation_records"].get(token) if token else None
    return {
        "required": required,
        "valid": valid,
        "token": token,
        "observation_count": arm["cargo_attestation_observation_count"],
        "failure_counts": dict(sorted(failures.items())),
        "record": record,
        "observed_feature_contract_tokens": dict(
            sorted(arm["cargo_attestation_tokens"].items())
        ),
        "observed_records": {
            key: arm["cargo_attestation_records"][key]
            for key in sorted(arm["cargo_attestation_records"])
        },
        "cargo_metadata_stdout_sha256": dict(
            sorted(arm["cargo_metadata_stdout_hashes"].items())
        ),
    }


def collect_direct_allocator_provenance(
    target: dict[str, Any], arm: dict[str, Any]
) -> None:
    try:
        normalized = normalized_direct_allocator_provenance(arm)
    except SummaryError as error:
        target["direct_provenance_failures"][str(error)] += 1
        return
    if normalized is None:
        return
    implementation_sha256, subject_token, subject_record = normalized
    target["direct_provenance_observation_count"] += 1
    target["unialloc_implementation_hashes"][implementation_sha256] += 1
    target["subject_cargo_feature_tokens"][subject_token] += 1
    previous = target["subject_cargo_feature_records"].setdefault(
        subject_token, subject_record
    )
    if previous != subject_record:
        target["direct_provenance_failures"][
            "subject Cargo feature token maps to conflicting records"
        ] += 1


def direct_allocator_provenance_state(arm: dict[str, Any]) -> dict[str, Any]:
    required = arm["allocator_variant"] in DIRECT_CARGO_ATTESTATION_ALLOCATORS
    failures = collections.Counter(arm["direct_provenance_failures"])
    if len(arm["unialloc_implementation_hashes"]) > 1:
        failures["mixed UniAlloc implementation sha256 values"] += 1
    if len(arm["subject_cargo_feature_tokens"]) > 1:
        failures["mixed subject Cargo feature records"] += 1
    valid = (
        not failures
        and (
            not required
            or (
                arm["experiment_count"] > 0
                and arm["direct_provenance_observation_count"]
                == arm["experiment_count"]
                and len(arm["unialloc_implementation_hashes"]) == 1
                and len(arm["subject_cargo_feature_tokens"]) == 1
                and len(arm["subject_cargo_feature_records"]) == 1
            )
        )
    )
    implementation_sha256 = (
        next(iter(arm["unialloc_implementation_hashes"]))
        if valid and required
        else None
    )
    subject_token = (
        next(iter(arm["subject_cargo_feature_tokens"]))
        if valid and required
        else None
    )
    subject_record = (
        arm["subject_cargo_feature_records"].get(subject_token)
        if subject_token
        else None
    )
    return {
        "required": required,
        "valid": valid,
        "observation_count": arm["direct_provenance_observation_count"],
        "failure_counts": dict(sorted(failures.items())),
        "unialloc_implementation_sha256": implementation_sha256,
        "subject_cargo_features_sha256": subject_token,
        "subject_cargo_features": subject_record,
        "observed_unialloc_implementation_sha256": dict(
            sorted(arm["unialloc_implementation_hashes"].items())
        ),
        "observed_subject_cargo_feature_tokens": dict(
            sorted(arm["subject_cargo_feature_tokens"].items())
        ),
        "observed_subject_cargo_features": {
            key: arm["subject_cargo_feature_records"][key]
            for key in sorted(arm["subject_cargo_feature_records"])
        },
    }


def observations(arm: dict[str, Any]) -> list[dict[str, Any]]:
    oracle = arm.get("oracle_validation")
    if oracle is None:
        return []
    if not isinstance(oracle, dict):
        raise SummaryError("oracle_validation must be an object")
    raw = oracle.get("repetition_observations", [])
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise SummaryError("repetition_observations must be a list of objects")
    return raw


def revalidate_native_observation(
    *,
    arm: dict[str, Any],
    observation: dict[str, Any],
    run: dict[str, Any] | None,
    repetition: int,
) -> tuple[bool, str | None, str | None, str | None]:
    """Re-run the current diagnostic parser and bind it to one raw run."""

    if run is None:
        return False, None, None, "missing_raw_run"
    run_exit = run.get("exit_code")
    observation_exit = observation.get("exit_code")
    run_timeout = run.get("timed_out")
    observation_timeout = observation.get("timed_out")
    observation_repetition = observation.get("repetition")
    run_clean = (
        isinstance(run_exit, int)
        and not isinstance(run_exit, bool)
        and run_exit == 0
        and run_timeout is False
    )
    if (
        isinstance(run_exit, bool)
        or not isinstance(run_exit, int)
        or observation_exit != run_exit
        or not isinstance(run_timeout, bool)
        or observation_timeout is not run_timeout
        or observation_repetition != repetition
        or observation.get("clean_exit") is not run_clean
    ):
        return False, None, None, "run_observation_metadata_mismatch"

    parsed_signature = experiment_runner.native_diagnostic_signature(run)
    mode = arm.get("execution_mode")
    if not isinstance(mode, str):
        return False, parsed_signature, None, "missing_execution_mode"
    effective_signature = parsed_signature if mode == "native_diagnostic" else None
    recorded_signature = observation.get("native_diagnostic_signature")
    if recorded_signature is not None and not isinstance(recorded_signature, str):
        raise SummaryError("native diagnostic signature must be a string")
    if recorded_signature != effective_signature:
        return (
            False,
            parsed_signature,
            effective_signature,
            "native_diagnostic_signature_mismatch",
        )
    return True, parsed_signature, effective_signature, None


def collect_runtime_stats(arm: dict[str, Any]) -> list[dict[str, Any]]:
    by_repetition = arm.get("runtime_stats_by_repetition")
    if by_repetition is not None:
        if not isinstance(by_repetition, list):
            raise SummaryError("runtime_stats_by_repetition must be a list")
        result: list[dict[str, Any]] = []
        for record in by_repetition:
            if not isinstance(record, dict):
                raise SummaryError("runtime stats record must be an object")
            stats = record.get("stats")
            if stats is not None:
                if not isinstance(stats, dict):
                    raise SummaryError("runtime stats must be an object")
                result.append(stats)
        return result
    stats = arm.get("runtime_stats")
    if stats is None:
        return []
    if not isinstance(stats, dict):
        raise SummaryError("runtime_stats must be an object")
    return [stats]


def collect_blockers(arm: dict[str, Any]) -> set[str]:
    result = set(string_values(arm.get("efficacy_blockers"), "efficacy_blockers"))
    for field in (
        "pass_audit_validation",
        "runtime_stats_validation",
        "reuse_denial_evidence",
    ):
        value = arm.get(field)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise SummaryError(f"{field} must be an object")
        result.update(
            string_values(value.get("efficacy_blockers"), f"{field}.efficacy_blockers")
        )
    return result


def merge_arm(
    target: dict[str, Any], arm: dict[str, Any], *, scenario_id: str
) -> None:
    target["scenario_ids"].add(scenario_id)
    target["experiment_count"] += 1
    collect_cargo_metadata_attestation(target, arm)
    collect_direct_allocator_provenance(target, arm)
    status = arm.get("execution_status")
    if isinstance(status, str):
        target["execution_statuses"].add(status)
    elif status is not None:
        raise SummaryError("execution_status must be a string")

    arm_observations = observations(arm)
    arm_runs = repetition_runs(arm)
    target["repetition_count"] += len(arm_observations)
    target["native_revalidation_observation_count"] += len(arm_observations)
    target["native_revalidation_run_count"] += len(arm_runs)
    if len(arm_runs) != len(arm_observations):
        target["native_revalidation_failures"][
            "run_observation_count_mismatch"
        ] += 1
    oracle = arm.get("oracle_validation")
    if isinstance(oracle, dict):
        oracle_status = oracle.get("status")
        if isinstance(oracle_status, str):
            target["oracle_statuses"][oracle_status] += 1
        elif oracle_status is not None:
            raise SummaryError("oracle_validation.status must be a string")
        if oracle.get("expected_oracle_observed") is True:
            target["expected_oracle_observation_count"] += max(
                1, len(arm_observations)
            )
        for code in string_values(
            oracle.get("matched_compile_error_codes"),
            "oracle_validation.matched_compile_error_codes",
        ):
            target["matched_compile_error_codes"][code] += 1
    for repetition, observation in enumerate(arm_observations, start=1):
        run = arm_runs[repetition - 1] if repetition <= len(arm_runs) else None
        (
            revalidation_valid,
            parsed_native_signature,
            effective_native_signature,
            revalidation_failure,
        ) = revalidate_native_observation(
            arm=arm,
            observation=observation,
            run=run,
            repetition=repetition,
        )
        if parsed_native_signature is not None:
            target["native_parser_signatures"][parsed_native_signature] += 1
        if revalidation_valid:
            target["native_revalidation_match_count"] += 1
        else:
            target["native_revalidation_failures"][
                revalidation_failure or "unknown_revalidation_failure"
            ] += 1

        tool_signature = observation.get("tool_finding_signature")
        if tool_signature is not None:
            if not isinstance(tool_signature, str):
                raise SummaryError("tool finding signature must be a string")
            target["baseline_findings"][tool_signature] += 1
        if revalidation_valid and effective_native_signature is not None:
            target["native_diagnostics"][effective_native_signature] += 1
            if effective_native_signature in ALLOCATOR_NATIVE_DIAGNOSTICS:
                target["allocator_signals"][effective_native_signature] += 1

        clean = bool(
            revalidation_valid
            and run is not None
            and run.get("exit_code") == 0
            and run.get("timed_out") is False
        )
        if clean:
            target["clean_exit_count"] += 1
        if target["archive_variant"] == "patched" and revalidation_valid:
            sanitizer = observation.get("sanitizer_finding_observed") is True
            if clean and not sanitizer and tool_signature is None:
                target["patched_clean_count"] += 1

        if run is not None and revalidation_valid:
            fingerprint_input = dict(run)
            for field in EXPLICIT_CHECKPOINT_FIELDS:
                checkpoint = observation.get(field)
                if checkpoint is None:
                    continue
                if field in fingerprint_input and fingerprint_input[field] != checkpoint:
                    raise SummaryError(f"conflicting {field} between run and observation")
                fingerprint_input[field] = checkpoint
            outcome, outcome_failure = normalized_native_outcome(
                fingerprint_input,
                diagnostic_signature=parsed_native_signature,
            )
            abnormal_signal = (
                normalized_native_abnormal_signal(fingerprint_input)
                if outcome is None
                else None
            )
            if parsed_native_signature is not None and outcome is not None:
                fingerprint = outcome_fingerprint(outcome)
                target["native_outcome_fingerprints"][fingerprint] += 1
                previous = target["native_outcome_fingerprint_records"].setdefault(
                    fingerprint, outcome
                )
                if previous != outcome:
                    raise SummaryError("native outcome fingerprint collision")
            elif abnormal_signal is not None:
                target["native_abnormal_signal_signatures"][abnormal_signal] += 1
                target["native_outcome_fingerprint_failures"][
                    "non_panic_native_signal"
                ] += 1
            elif run.get("exit_code") != 0 or run.get("timed_out") is True:
                target["native_outcome_fingerprint_failures"][
                    outcome_failure or "current_parser_no_panic_signature"
                ] += 1
        elif run is not None and (
            run.get("exit_code") != 0 or run.get("timed_out") is True
        ):
            target["native_outcome_fingerprint_failures"][
                "native_revalidation_failed"
            ] += 1

    explicit_denial_count = 0
    denial = arm.get("reuse_denial_evidence")
    if denial is not None:
        if not isinstance(denial, dict):
            raise SummaryError("reuse_denial_evidence must be an object")
        explicit_denial_count = denial.get(
            "matching_reuse_denial_event_count",
            denial.get("reuse_denial_event_count", 0),
        )
        explicit_denial_count = count_value(
            explicit_denial_count or 0, "reuse denial event count"
        )
    if explicit_denial_count:
        target["native_diagnostics"][
            "cross_identity_reuse_denial"
        ] += explicit_denial_count
        target["allocator_signals"][
            "cross_identity_reuse_denial"
        ] += explicit_denial_count

    stats_records = collect_runtime_stats(arm)
    target["allocation_stats_observation_count"] += len(stats_records)
    stats_denial_count = 0
    for stats in stats_records:
        target["typed_allocation_count"] += count_value(
            stats.get("typed_allocations", 0), "typed_allocations"
        )
        target["fallback_allocation_count"] += count_value(
            stats.get("fallback_allocations", 0), "fallback_allocations"
        )
        stats_denial_count += count_value(
            stats.get("typed_cache_wrong_identity_denials", 0),
            "typed_cache_wrong_identity_denials",
        )
    if stats_denial_count and explicit_denial_count == 0:
        target["native_diagnostics"][
            "typed_cache_wrong_identity_denial"
        ] += stats_denial_count
        target["allocator_signals"][
            "typed_cache_wrong_identity_denial"
        ] += stats_denial_count

    audit = arm.get("pass_audit_summary")
    if audit is not None:
        if not isinstance(audit, dict):
            raise SummaryError("pass_audit_summary must be an object")
        target["compiler_audit_observation_count"] += 1
        for name, value in audit.items():
            if name.endswith("_rewrites_applied"):
                target["compiler_rewrite_counts"][name] += count_value(
                    value, f"pass_audit_summary.{name}"
                )

    target["eligibility_blockers"].update(collect_blockers(arm))
    if not cargo_metadata_attestation_state(target)["valid"]:
        target["eligibility_blockers"].add(
            "cargo_metadata_attestation_invalid"
        )
    if not direct_allocator_provenance_state(target)["valid"]:
        target["eligibility_blockers"].add(
            "direct_allocator_provenance_invalid"
        )
    if target["native_revalidation_failures"]:
        target["eligibility_blockers"].add(
            "native_diagnostic_source_revalidation_failed"
        )
    if status not in (None, "completed"):
        target["eligibility_blockers"].add("execution_not_completed")


def validate_and_merge_experiment(
    *,
    case_id: str,
    path: pathlib.Path,
    case: dict[str, Any],
    aggregates: dict[tuple[str, str], dict[str, Any]],
    selected_allocators: frozenset[str] | None = None,
) -> list[str]:
    experiment = load_json(path, "experiment")
    if experiment.get("schema_version") != 1:
        raise SummaryError(f"unsupported experiment schema: {path}")
    scenario_id = experiment.get("scenario_id")
    if scenario_id not in case["_scenario_ids"]:
        raise SummaryError(
            f"experiment scenario {scenario_id!r} does not belong to {case_id}"
        )
    raw_arms = experiment.get("arms")
    if not isinstance(raw_arms, list):
        raise SummaryError(f"experiment arms must be a list: {path}")
    matched = 0
    observed_allocators: set[str] = set()
    merged_allocators: set[str] = set()
    for arm in raw_arms:
        if not isinstance(arm, dict):
            raise SummaryError("experiment arm must be an object")
        arm_case = arm.get("case_id")
        if arm_case != case_id:
            raise SummaryError(
                f"experiment arm case {arm_case!r} does not match assignment {case_id}"
            )
        arm_scenario = arm.get("scenario_id")
        if arm_scenario is not None and arm_scenario != scenario_id:
            raise SummaryError(
                f"experiment arm scenario {arm_scenario!r} does not match "
                f"experiment {scenario_id!r}"
            )
        archive = arm.get("archive_variant")
        allocator = arm.get("allocator_variant")
        if archive not in ARCHIVE_ORDER:
            raise SummaryError(f"invalid archive variant: {archive!r}")
        if allocator not in ALLOCATOR_ORDER:
            raise SummaryError(f"invalid allocator variant: {allocator!r}")
        observed_allocators.add(allocator)
        if selected_allocators is not None and allocator not in selected_allocators:
            continue
        key = (archive, allocator)
        target = aggregates.setdefault(key, new_aggregate(case_id, archive, allocator))
        merge_arm(target, arm, scenario_id=scenario_id)
        merged_allocators.add(allocator)
        matched += 1
    if selected_allocators is not None:
        missing = sorted(selected_allocators - observed_allocators)
        if missing:
            raise SummaryError(
                f"selected allocator absent from experiment {path}: {', '.join(missing)}"
            )
    if matched == 0:
        raise SummaryError(f"experiment contains no arms: {path}")
    return sorted(merged_allocators, key=ALLOCATOR_ORDER.__getitem__)


def total(counter: collections.Counter[str]) -> int:
    return sum(counter.values())


def classify_arm(
    arm: dict[str, Any],
    aggregates: dict[tuple[str, str], dict[str, Any]],
) -> str:
    archive = arm["archive_variant"]
    allocator = arm["allocator_variant"]
    findings = total(arm["baseline_findings"])
    signals = total(arm["allocator_signals"])
    if not cargo_metadata_attestation_state(arm)["valid"]:
        signals = 0
    if not direct_allocator_provenance_state(arm)["valid"]:
        signals = 0

    if archive == "vulnerable" and allocator == "system" and findings > 0:
        return "baseline_reproduced"
    if archive == "vulnerable" and allocator != "system" and signals > 0:
        if allocator == "typeiso":
            ablation = aggregates.get(("vulnerable", "typed_plain"))
            if (
                ablation is not None
                and ablation["repetition_count"] > 0
                and total(ablation["allocator_signals"]) == 0
            ):
                return "typeiso_specific_signal"
        return "allocator_signal_observed"
    if archive == "vulnerable" and allocator != "system":
        baseline = aggregates.get(("vulnerable", "system"))
        if (
            arm["repetition_count"] > 0
            and baseline is not None
            and total(baseline["baseline_findings"]) > 0
        ):
            return "direct_effect_absent"
    return "insufficient_evidence"


def render_arm(
    arm: dict[str, Any],
    aggregates: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    classification = classify_arm(arm, aggregates)
    cargo_attestation = cargo_metadata_attestation_state(arm)
    direct_provenance = direct_allocator_provenance_state(arm)
    effective_allocator_signals = (
        arm["allocator_signals"]
        if cargo_attestation["valid"] and direct_provenance["valid"]
        else collections.Counter()
    )
    scenario_ids = sorted(arm["scenario_ids"])
    native_outcome_fingerprints = dict(
        sorted(arm["native_outcome_fingerprints"].items())
    )
    fingerprint_records = {
        fingerprint: {
            "count": native_outcome_fingerprints[fingerprint],
            "normalized_outcome": arm["native_outcome_fingerprint_records"][
                fingerprint
            ],
        }
        for fingerprint in native_outcome_fingerprints
    }
    revalidation_valid = (
        arm["native_revalidation_run_count"]
        == arm["native_revalidation_observation_count"]
        == arm["native_revalidation_match_count"]
        and not arm["native_revalidation_failures"]
    )
    return {
        "archive_variant": arm["archive_variant"],
        "allocator_variant": arm["allocator_variant"],
        "scenario_id": scenario_ids[0] if len(scenario_ids) == 1 else None,
        "scenario_ids": scenario_ids,
        "experiment_count": arm["experiment_count"],
        "execution_statuses": sorted(arm["execution_statuses"]),
        "repetition_count": arm["repetition_count"],
        "baseline_finding_count": total(arm["baseline_findings"]),
        "baseline_finding_signatures": dict(sorted(arm["baseline_findings"].items())),
        "oracle_statuses": dict(sorted(arm["oracle_statuses"].items())),
        "expected_oracle_observation_count": arm[
            "expected_oracle_observation_count"
        ],
        "matched_compile_error_codes": dict(
            sorted(arm["matched_compile_error_codes"].items())
        ),
        "clean_exit_count": arm["clean_exit_count"],
        "patched_clean_count": arm["patched_clean_count"],
        "native_diagnostic_count": total(arm["native_diagnostics"]),
        "native_diagnostic_signatures": dict(
            sorted(arm["native_diagnostics"].items())
        ),
        "allocator_signal_count": total(effective_allocator_signals),
        "allocator_signal_signatures": dict(
            sorted(effective_allocator_signals.items())
        ),
        "source_revalidated_allocator_signal_signatures": dict(
            sorted(arm["allocator_signals"].items())
        ),
        "cargo_metadata_attestation": cargo_attestation,
        "direct_allocator_provenance": direct_provenance,
        "unialloc_implementation_sha256": direct_provenance[
            "unialloc_implementation_sha256"
        ],
        "subject_cargo_features": direct_provenance[
            "subject_cargo_features"
        ],
        "native_diagnostic_revalidation": {
            "valid": revalidation_valid,
            "observation_count": arm["native_revalidation_observation_count"],
            "raw_run_count": arm["native_revalidation_run_count"],
            "matched_observation_count": arm["native_revalidation_match_count"],
            "failure_counts": dict(
                sorted(arm["native_revalidation_failures"].items())
            ),
            "current_parser_signatures": dict(
                sorted(arm["native_parser_signatures"].items())
            ),
        },
        "native_outcome_fingerprints": native_outcome_fingerprints,
        "native_outcome_fingerprint_records": fingerprint_records,
        "native_outcome_fingerprint_failures": dict(
            sorted(arm["native_outcome_fingerprint_failures"].items())
        ),
        "native_abnormal_signal_signatures": dict(
            sorted(arm["native_abnormal_signal_signatures"].items())
        ),
        "patched_control_fingerprints": (
            native_outcome_fingerprints
            if arm["archive_variant"] == "patched"
            else {}
        ),
        "typed_allocation_count": arm["typed_allocation_count"],
        "fallback_allocation_count": arm["fallback_allocation_count"],
        "allocation_stats_observation_count": arm[
            "allocation_stats_observation_count"
        ],
        "compiler_rewrite_counts": dict(
            sorted(arm["compiler_rewrite_counts"].items())
        ),
        "compiler_audit_observation_count": arm[
            "compiler_audit_observation_count"
        ],
        "eligibility_blockers": sorted(arm["eligibility_blockers"]),
        "classification": classification,
        "mitigation_inferred": False,
    }


def strongest_classification(arms: Iterable[dict[str, Any]]) -> str:
    values = [arm["classification"] for arm in arms]
    if not values:
        return "insufficient_evidence"
    return max(values, key=CLASSIFICATION_ORDER.__getitem__)


def summarize(
    catalog_path: pathlib.Path,
    experiment_specs: Iterable[str],
    *,
    experiment_allocator_specs: Iterable[str] = (),
    corpus_path: pathlib.Path | None = DEFAULT_CORPUS,
) -> dict[str, Any]:
    catalog_path = catalog_path.expanduser().resolve()
    _, cases = load_catalog(catalog_path)
    corpus_input: dict[str, Any] | None = None
    if corpus_path is not None:
        corpus_path = corpus_path.expanduser().resolve()
        advisory_by_case = corpus_advisory_index(corpus_path)
        corpus_input = artifact_record(corpus_path)
        for case_id, case in cases.items():
            advisory_id = case.get("advisory_id")
            if isinstance(advisory_id, str) and advisory_id:
                continue
            resolved = advisory_by_case.get(case_id)
            if resolved is None:
                raise SummaryError(
                    f"catalog case has no advisory id and corpus has no mapping: {case_id}"
                )
            case["advisory_id"] = resolved
    assignments: dict[
        str,
        list[
            tuple[
                pathlib.Path,
                frozenset[str] | None,
                dict[str, Any],
            ]
        ],
    ] = {case_id: [] for case_id in cases}
    observed_paths: set[pathlib.Path] = set()
    experiment_inputs: list[dict[str, Any]] = []
    for text in experiment_specs:
        case_id, path = parse_experiment_spec(text)
        if case_id not in cases:
            raise SummaryError(f"experiment references unknown catalog case: {case_id}")
        if path in observed_paths:
            raise SummaryError(f"duplicate experiment path: {path}")
        observed_paths.add(path)
        input_record = {
            "case_id": case_id,
            **artifact_record(path),
            "allocator_selection": "all",
            "requested_allocators": None,
            "selected_allocators": [],
        }
        assignments[case_id].append((path, None, input_record))
        experiment_inputs.append(input_record)
    for text in experiment_allocator_specs:
        case_id, selected_allocators, path = parse_experiment_allocator_spec(text)
        if case_id not in cases:
            raise SummaryError(f"experiment references unknown catalog case: {case_id}")
        if path in observed_paths:
            raise SummaryError(f"duplicate experiment path: {path}")
        observed_paths.add(path)
        requested = sorted(selected_allocators, key=ALLOCATOR_ORDER.__getitem__)
        input_record = {
            "case_id": case_id,
            **artifact_record(path),
            "allocator_selection": "explicit",
            "requested_allocators": requested,
            "selected_allocators": [],
        }
        assignments[case_id].append((path, selected_allocators, input_record))
        experiment_inputs.append(input_record)

    rendered_cases: list[dict[str, Any]] = []
    classification_counts: collections.Counter[str] = collections.Counter()
    for case_id, case in sorted(cases.items()):
        aggregates: dict[tuple[str, str], dict[str, Any]] = {}
        for path, selected_allocators, input_record in assignments[case_id]:
            input_record["selected_allocators"] = validate_and_merge_experiment(
                case_id=case_id,
                path=path,
                case=case,
                aggregates=aggregates,
                selected_allocators=selected_allocators,
            )
        rendered_arms = [
            render_arm(aggregates[key], aggregates)
            for key in sorted(
                aggregates,
                key=lambda item: (ARCHIVE_ORDER[item[0]], ALLOCATOR_ORDER[item[1]]),
            )
        ]
        case_scenario_ids = sorted(
            {
                scenario_id
                for aggregate in aggregates.values()
                for scenario_id in aggregate["scenario_ids"]
            }
        )
        classification = strongest_classification(rendered_arms)
        classification_counts[classification] += 1
        rendered_cases.append(
            {
                "case_id": case_id,
                "advisory_id": case.get("advisory_id"),
                "crate": case.get("crate"),
                "scenario_id": (
                    case_scenario_ids[0] if len(case_scenario_ids) == 1 else None
                ),
                "scenario_ids": case_scenario_ids,
                "experiment_count": len(assignments[case_id]),
                "classification": classification,
                "arms": rendered_arms,
            }
        )

    return {
        "schema_version": 1,
        "source": "unialloc-rustsec-heap-expansion-summary",
        "claim_grade": False,
        "mitigation_inferred": False,
        "boundary": (
            "Classifications report reproduced findings and allocator signals only; "
            "clean exits never establish mitigation."
        ),
        "catalog_path": artifact_record(catalog_path)["path"],
        "catalog_sha256": artifact_record(catalog_path)["sha256"],
        "corpus_input": corpus_input,
        "catalog_case_count": len(cases),
        "experiment_file_count": len(observed_paths),
        "experiment_inputs": experiment_inputs,
        "classification_counts": dict(sorted(classification_counts.items())),
        "cases": rendered_cases,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=pathlib.Path, default=DEFAULT_CATALOG)
    parser.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--experiment",
        action="append",
        default=[],
        metavar="CASE=PATH",
        help="assign one raw experiment.json file to a catalog case; repeatable",
    )
    parser.add_argument(
        "--experiment-allocator",
        action="append",
        default=[],
        metavar="CASE:ALLOCATOR[,ALLOCATOR]=PATH",
        help=(
            "assign selected allocator arms from one raw experiment; repeatable "
            "and mutually path-unique with --experiment"
        ),
    )
    parser.add_argument("--output", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = summarize(
            args.catalog,
            args.experiment,
            experiment_allocator_specs=args.experiment_allocator,
            corpus_path=args.corpus,
        )
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    except SummaryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
