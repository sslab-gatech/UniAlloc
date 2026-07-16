#!/usr/bin/env python3
"""Load versioned Type Isolation macro-evaluation suite contracts."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from evaluation.scripts import immutable_evidence


ROOT = Path(__file__).resolve().parents[2]
CURRENT_SUITE_PATH = (
    ROOT / "evaluation/config/type_isolation_primary_suite_v6_ce8af7b.json"
)
HISTORICAL_V1_SHA256 = (
    "ba386a779e45e0884e02cf8825c69985d1b5cf68d57366af78e7f77cfa8f6ccb"
)
_NAMESPACE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
HOST_PRIMARY_MEASUREMENT_LOCK = Path("/tmp/unialloc-primary-measurement.lock")
RUNTIME_ENVIRONMENT_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "TZ",
)
RUNTIME_ENVIRONMENT_POLICY = "allowlisted-production-runtime-v1"
PRODUCTION_RSEQ_POLICY = "libc_default"
TYPE_ISOLATION_PROTOCOL_FILES = (
    "evaluation/scripts/immutable_evidence.py",
    "evaluation/scripts/type_isolation_suite_contract.py",
    "evaluation/scripts/realworld_type_isolation_matrix.py",
    "evaluation/scripts/type_isolation_primary_collections_oxipng.py",
    "evaluation/scripts/type_isolation_redb_actix_campaign.py",
    "evaluation/scripts/polars_swc_rustpython_primary_campaign.py",
    "evaluation/scripts/assemble_type_isolation_primary_results.py",
)


class SuiteContractError(ValueError):
    """Raised when a suite cannot safely select a measurement campaign."""


@dataclass(frozen=True)
class SuiteContract:
    path: Path
    manifest: Mapping[str, Any]
    manifest_sha256: str
    suite_id: str
    implementation_revision: str
    implementation_sha256: str
    implementation_file_count: int
    implementation_size_bytes: int
    warmup_rounds: int
    measured_rounds: int
    publication_namespace: str
    result_namespace: str
    preregistration_manifest_sha256: str

    @property
    def publication_root(self) -> Path:
        return ROOT / "evaluation" / "raw" / self.publication_namespace

    @property
    def target_results_dir(self) -> Path:
        return self.publication_root / "targets"

    @property
    def measurement_lock(self) -> Path:
        return HOST_PRIMARY_MEASUREMENT_LOCK

    @property
    def bound_suite_manifest(self) -> Path:
        return self.publication_root / "suite-manifest.json"

    @property
    def assembled_result(self) -> Path:
        return ROOT / "benchmark-results" / f"{self.result_namespace}.json"

    def runner_raw_dir(self, runner_namespace: str) -> Path:
        _validate_namespace(runner_namespace, "runner namespace")
        return self.publication_root / "campaigns" / runner_namespace


def verify_protocol_files(contract: SuiteContract) -> dict[str, str]:
    raw_protocol = contract.manifest.get("protocol_files")
    requires_protocol = bool(
        re.fullmatch(r"type-isolation-primary-v(?:[2-9]|[1-9][0-9]+)-.+", contract.suite_id)
    )
    if raw_protocol is None and not requires_protocol:
        return {}
    if not isinstance(raw_protocol, dict):
        raise SuiteContractError("protocol_files must be an object")
    if set(raw_protocol) != set(TYPE_ISOLATION_PROTOCOL_FILES):
        raise SuiteContractError(
            "protocol_files must exactly cover the Type Isolation execution protocol"
        )
    verified: dict[str, str] = {}
    for relative in TYPE_ISOLATION_PROTOCOL_FILES:
        expected = _sha256(raw_protocol.get(relative), f"protocol_files.{relative}")
        path = (ROOT / relative).resolve()
        if not path.is_file() or immutable_evidence.sha256_file(path) != expected:
            raise SuiteContractError(f"protocol file digest mismatch: {relative}")
        verified[relative] = expected
    return verified


def bind_suite_manifest(
    contract: SuiteContract, *, destination: Path | None = None
) -> dict[str, Any]:
    verify_protocol_files(contract)
    path = destination or contract.bound_suite_manifest
    try:
        committed = immutable_evidence.bind_immutable_file(
            contract.path,
            path,
            expected_sha256=contract.manifest_sha256,
        )
    except immutable_evidence.ImmutableEvidenceError as error:
        raise SuiteContractError(str(error)) from error
    return {
        "path": str(committed.path),
        "sha256": committed.record_sha256,
        "bytes": committed.path.stat().st_size,
        "reused": committed.reused,
    }


def verify_suite_manifest_binding(
    contract: SuiteContract, *, path: Path | None = None
) -> dict[str, Any]:
    verify_protocol_files(contract)
    bound = (path or contract.bound_suite_manifest).expanduser().resolve()
    if not bound.is_file():
        raise SuiteContractError(f"bound suite manifest is missing: {bound}")
    digest = immutable_evidence.sha256_file(bound)
    if digest != contract.manifest_sha256 or bound.read_bytes() != contract.path.read_bytes():
        raise SuiteContractError(f"bound suite manifest differs from {contract.path}")
    return {
        "path": str(bound),
        "sha256": digest,
        "bytes": bound.stat().st_size,
    }


def clean_runtime_environment(
    temporary_dir: Path | str,
    *,
    source: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a timed-child environment without inherited experiment state."""

    inherited = os.environ if source is None else source
    environment = {
        name: str(inherited[name])
        for name in RUNTIME_ENVIRONMENT_ALLOWLIST
        if name in inherited and inherited[name]
    }
    runtime_tmp = Path(temporary_dir).expanduser().resolve()
    runtime_tmp.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": str(runtime_tmp),
        }
    )
    for name, value in (overrides or {}).items():
        if not isinstance(name, str) or not isinstance(value, str) or not name:
            raise SuiteContractError("runtime environment overrides must be strings")
        if name == "GLIBC_TUNABLES":
            raise SuiteContractError(
                "production runtime overrides cannot change libc rseq policy"
            )
        environment[name] = value
    return environment


def runtime_environment_record(environment: Mapping[str, str]) -> dict[str, Any]:
    """Return exact, digest-bound runtime policy evidence for one child."""

    normalized = {str(name): str(value) for name, value in environment.items()}
    if normalized.get("LANG") != "C" or normalized.get("LC_ALL") != "C":
        raise SuiteContractError("runtime locale must be LANG=C and LC_ALL=C")
    if "GLIBC_TUNABLES" in normalized:
        raise SuiteContractError(
            "production runtime must retain libc-default rseq registration"
        )
    temporary_dir = normalized.get("TMPDIR")
    if not temporary_dir or not Path(temporary_dir).is_dir():
        raise SuiteContractError("runtime TMPDIR must name an existing directory")
    payload = json.dumps(
        normalized, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "policy": RUNTIME_ENVIRONMENT_POLICY,
        "inherited_allowlist": list(RUNTIME_ENVIRONMENT_ALLOWLIST),
        "rseq_policy": PRODUCTION_RSEQ_POLICY,
        "glibc_tunables_present": False,
        "environment": normalized,
        "environment_sha256": hashlib.sha256(payload).hexdigest(),
    }


@contextlib.contextmanager
def primary_measurement_lock(
    *,
    runner: str,
    raw_root: Path | str,
    target_id: str | None = None,
    phase: str = "warmup-and-measured",
) -> Iterator[dict[str, Any]]:
    """Serialize every UniAlloc warmup and timed sample on this host."""

    path = HOST_PRIMARY_MEASUREMENT_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        waiting_at = time.time()
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        acquired_at = time.time()
        metadata: dict[str, Any] = {
            "lock_path": str(path),
            "runner": runner,
            "raw_root": str(Path(raw_root).expanduser().resolve()),
            "target_id": target_id,
            "phase": phase,
            "pid": os.getpid(),
            "wait_seconds": acquired_at - waiting_at,
            "acquired_unix": acquired_at,
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(metadata, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        try:
            yield metadata
        finally:
            metadata["released_unix"] = time.time()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_namespace(value: object, context: str) -> str:
    if not isinstance(value, str) or _NAMESPACE.fullmatch(value) is None:
        raise SuiteContractError(f"{context} must be a lowercase slug")
    return value


def _positive_integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SuiteContractError(f"{context} must be a positive integer")
    return value


def _sha256(value: object, context: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SuiteContractError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _revision(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise SuiteContractError(
            "implementation.git_revision must be a full lowercase Git commit"
        )
    return value


def _legacy_contract(
    manifest: Mapping[str, Any], manifest_sha256: str
) -> tuple[int, int, str, str]:
    if (
        manifest.get("suite_id") != "type-isolation-primary-v1"
        or manifest_sha256 != HISTORICAL_V1_SHA256
    ):
        raise SuiteContractError(
            "suite lacks a measurement/publication contract and is not the exact "
            "historical v1 manifest"
        )
    return 1, 5, "type-isolation-primary-v1", "type-isolation-primary-v1"


def _sampling_and_publication(
    manifest: Mapping[str, Any], manifest_sha256: str
) -> tuple[int, int, str, str]:
    measurement = manifest.get("measurement_contract")
    publication = manifest.get("publication")
    if measurement is None and publication is None:
        return _legacy_contract(manifest, manifest_sha256)
    if not isinstance(measurement, dict):
        raise SuiteContractError("measurement_contract must be an object")
    if not isinstance(publication, dict):
        raise SuiteContractError("publication must be an object")
    warmups = _positive_integer(
        measurement.get("warmup_rounds"), "measurement_contract.warmup_rounds"
    )
    measured = _positive_integer(
        measurement.get("measured_rounds"), "measurement_contract.measured_rounds"
    )
    namespace = _validate_namespace(
        publication.get("namespace"), "publication.namespace"
    )
    result_namespace = _validate_namespace(
        publication.get("result_namespace"), "publication.result_namespace"
    )
    return warmups, measured, namespace, result_namespace


def _preregistration_digest(
    manifest: Mapping[str, Any], manifest_sha256: str
) -> str:
    amendments = manifest.get("analysis_amendments", [])
    if not isinstance(amendments, list):
        raise SuiteContractError("analysis_amendments must be a list")
    if not amendments:
        return manifest_sha256
    first = amendments[0]
    if not isinstance(first, dict):
        raise SuiteContractError("analysis_amendments entries must be objects")
    return _sha256(
        first.get("original_campaign_manifest_sha256"),
        "analysis_amendments original campaign manifest",
    )


def load_suite_contract(path: Path | str = CURRENT_SUITE_PATH) -> SuiteContract:
    suite_path = Path(path).expanduser().resolve()
    try:
        payload = suite_path.read_bytes()
        manifest = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        raise SuiteContractError(f"cannot load suite manifest: {suite_path}") from error
    if not isinstance(manifest, dict):
        raise SuiteContractError("suite manifest must be a JSON object")
    manifest_sha256 = hashlib.sha256(payload).hexdigest()
    suite_id = _validate_namespace(manifest.get("suite_id"), "suite_id")
    implementation = manifest.get("implementation")
    if not isinstance(implementation, dict):
        raise SuiteContractError("implementation must be an object")
    targets = manifest.get("targets")
    if not isinstance(targets, list) or not targets:
        raise SuiteContractError("targets must be a non-empty list")
    variants = manifest.get("variant_order")
    if variants != ["unialloc", "typed_plain", "typeiso_perf"]:
        raise SuiteContractError("variant_order does not match the primary contract")
    warmups, measured, namespace, result_namespace = _sampling_and_publication(
        manifest, manifest_sha256
    )
    return SuiteContract(
        path=suite_path,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        suite_id=suite_id,
        implementation_revision=_revision(implementation.get("git_revision")),
        implementation_sha256=_sha256(
            implementation.get("canonical_sha256"),
            "implementation.canonical_sha256",
        ),
        implementation_file_count=_positive_integer(
            implementation.get("canonical_file_count"),
            "implementation.canonical_file_count",
        ),
        implementation_size_bytes=_positive_integer(
            implementation.get("canonical_size_bytes"),
            "implementation.canonical_size_bytes",
        ),
        warmup_rounds=warmups,
        measured_rounds=measured,
        publication_namespace=namespace,
        result_namespace=result_namespace,
        preregistration_manifest_sha256=_preregistration_digest(
            manifest, manifest_sha256
        ),
    )
