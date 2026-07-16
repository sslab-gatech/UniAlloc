#!/usr/bin/env python3
"""Dependency-free immutable publication for evaluation evidence.

Collectors write every process artifact below a unique attempt directory.  A
fully validated JSON record becomes visible through one hard-link operation,
which provides no-replace publication on the same filesystem.  Interrupted
attempts remain outside the committed namespace and cannot block a retry.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any, Callable, Mapping


SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ImmutableEvidenceError(RuntimeError):
    """Evidence is incomplete, divergent, corrupt, or already committed."""


@dataclass(frozen=True)
class CommittedRecord:
    path: Path
    record_sha256: str
    value: Any
    reused: bool

    @property
    def sha256(self) -> str:
        return self.record_sha256


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, payload: bytes) -> Path:
    """Atomically replace an attempt-local or explicitly mutable file."""

    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def begin_attempt(commit_path: Path) -> Path:
    """Create one unique attempt directory adjacent to a committed record."""

    commit = commit_path.expanduser().resolve()
    root = commit.parent / "attempts" / commit.stem
    root.mkdir(parents=True, exist_ok=True)
    attempt = root / (
        f"{time.time_ns()}-{os.getpid()}-{secrets.token_hex(8)}"
    )
    attempt.mkdir(mode=0o700)
    _fsync_directory(root)
    return attempt


def artifact_ref(path: Path) -> dict[str, Any]:
    artifact = path.expanduser().resolve()
    if not artifact.is_file():
        raise ImmutableEvidenceError(f"artifact is missing: {artifact}")
    return {
        "path": str(artifact),
        "sha256": sha256_file(artifact),
        "bytes": artifact.stat().st_size,
    }


def validate_artifact_ref(reference: Any, *, context: str = "artifact") -> Path:
    if not isinstance(reference, dict):
        raise ImmutableEvidenceError(f"{context} reference must be an object")
    path_value = reference.get("path")
    digest = reference.get("sha256")
    size = reference.get("bytes")
    if not isinstance(path_value, str) or not path_value:
        raise ImmutableEvidenceError(f"{context} path is invalid")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise ImmutableEvidenceError(f"{context} digest is invalid")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ImmutableEvidenceError(f"{context} byte count is invalid")
    path = Path(path_value)
    if not path.is_file():
        raise ImmutableEvidenceError(f"{context} is missing: {path}")
    if path.stat().st_size != size or sha256_file(path) != digest:
        raise ImmutableEvidenceError(f"{context} content differs from its attestation")
    return path.resolve()


def validate_measurement_record(
    value: Any,
    *,
    expected_identity: Mapping[str, Any],
    expected_metrics: Mapping[str, Any] | None = None,
) -> None:
    if not isinstance(value, dict) or value.get("evidence_schema_version") != 1:
        raise ImmutableEvidenceError("measurement evidence schema is invalid")
    identity = value.get("identity")
    if not isinstance(identity, dict):
        raise ImmutableEvidenceError("measurement identity must be an object")
    for field, expected in expected_identity.items():
        if identity.get(field) != expected:
            raise ImmutableEvidenceError(
                f"measurement identity mismatch for {field}: "
                f"expected {expected!r}, got {identity.get(field)!r}"
            )
    metrics = value.get("metrics")
    if not isinstance(metrics, dict):
        raise ImmutableEvidenceError("measurement metrics must be an object")
    for field in ("performance", "peak_rss_mib"):
        metric = metrics.get(field)
        if (
            isinstance(metric, bool)
            or not isinstance(metric, (int, float))
            or not math.isfinite(float(metric))
            or float(metric) <= 0.0
        ):
            raise ImmutableEvidenceError(f"measurement {field} is invalid")
    unit = metrics.get("performance_unit")
    if not isinstance(unit, str) or not unit:
        raise ImmutableEvidenceError("measurement performance unit is invalid")
    for field, expected in (expected_metrics or {}).items():
        actual = metrics.get(field)
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            if (
                isinstance(actual, bool)
                or not isinstance(actual, (int, float))
                or float(actual) != float(expected)
            ):
                raise ImmutableEvidenceError(
                    f"measurement metric mismatch for {field}"
                )
        elif actual != expected:
            raise ImmutableEvidenceError(
                f"measurement metric mismatch for {field}"
            )
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ImmutableEvidenceError("measurement artifacts must be a non-empty object")
    for name, reference in artifacts.items():
        if not isinstance(name, str) or not name:
            raise ImmutableEvidenceError("measurement artifact name is invalid")
        validate_artifact_ref(reference, context=f"artifact {name}")


def _read_json_record(path: Path) -> tuple[bytes, Any]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as error:
        raise ImmutableEvidenceError(f"committed record is unreadable: {path}") from error
    return payload, value


def _fsync_attempt(attempt: Path) -> None:
    for path in sorted(attempt.rglob("*")):
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    directories = [path for path in attempt.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(attempt)


def _make_read_only(path: Path) -> None:
    path.chmod(path.stat().st_mode & ~0o222)


def _validate_committed_inode(path: Path) -> None:
    metadata = path.stat()
    if metadata.st_nlink != 1:
        deadline = time.monotonic() + 1.0
        while metadata.st_nlink != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
            metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ImmutableEvidenceError(f"committed evidence is not a file: {path}")
    if metadata.st_nlink != 1:
        raise ImmutableEvidenceError(
            f"committed evidence has an unsafe alternate hard link: {path}"
        )
    if metadata.st_mode & 0o222:
        raise ImmutableEvidenceError(
            f"committed evidence must be read-only: {path}"
        )


def validate_committed_file(path: Path) -> Path:
    committed = path.expanduser().resolve()
    _validate_committed_inode(committed)
    return committed


def _publish_candidate(candidate: Path, commit: Path) -> None:
    """Publish one read-only inode and remove its writable attempt alias."""

    _make_read_only(candidate)
    os.link(candidate, commit)
    candidate.unlink()
    _fsync_directory(candidate.parent)
    _fsync_directory(commit.parent)
    _validate_committed_inode(commit)


def run_or_reuse_json(
    commit_path: Path,
    collect: Callable[[Path], Mapping[str, Any]],
    validate: Callable[[Mapping[str, Any]], None],
) -> CommittedRecord:
    """Reuse one valid cell or atomically publish one completed attempt."""

    commit = commit_path.expanduser().resolve()
    if commit.exists():
        _validate_committed_inode(commit)
        payload, value = _read_json_record(commit)
        if not isinstance(value, dict):
            raise ImmutableEvidenceError(f"committed record is not an object: {commit}")
        validate(value)
        return CommittedRecord(commit, sha256_bytes(payload), value, True)

    attempt = begin_attempt(commit)
    value = collect(attempt)
    if not isinstance(value, dict):
        raise ImmutableEvidenceError("collector must return a JSON object")
    validate(value)
    candidate = attempt / "candidate-record.json"
    payload = canonical_json_bytes(value)
    atomic_write_bytes(candidate, payload)
    _fsync_attempt(attempt)
    commit.parent.mkdir(parents=True, exist_ok=True)
    try:
        _publish_candidate(candidate, commit)
        return CommittedRecord(commit, sha256_bytes(payload), value, False)
    except FileExistsError:
        _validate_committed_inode(commit)
        winner_payload, winner = _read_json_record(commit)
        if not isinstance(winner, dict):
            raise ImmutableEvidenceError(
                f"racing committed record is not an object: {commit}"
            )
        validate(winner)
        if winner_payload != payload:
            raise ImmutableEvidenceError(
                f"immutable record collision with different content: {commit}"
            )
        return CommittedRecord(commit, sha256_bytes(winner_payload), winner, True)
    except OSError as error:
        if error.errno == errno.EXDEV:
            raise ImmutableEvidenceError(
                "attempt and committed record must share one filesystem"
            ) from error
        raise


def persist_immutable_bytes(path: Path, payload: bytes) -> CommittedRecord:
    commit = path.expanduser().resolve()
    if commit.exists():
        _validate_committed_inode(commit)
        current = commit.read_bytes()
        if current != payload:
            raise ImmutableEvidenceError(
                f"immutable file collision with different content: {commit}"
            )
        return CommittedRecord(commit, sha256_bytes(current), current, True)
    attempt = begin_attempt(commit)
    candidate = attempt / "candidate-bytes"
    atomic_write_bytes(candidate, payload)
    _fsync_attempt(attempt)
    commit.parent.mkdir(parents=True, exist_ok=True)
    try:
        _publish_candidate(candidate, commit)
        return CommittedRecord(commit, sha256_bytes(payload), payload, False)
    except FileExistsError:
        _validate_committed_inode(commit)
        current = commit.read_bytes()
        if current != payload:
            raise ImmutableEvidenceError(
                f"immutable file collision with different content: {commit}"
            )
        return CommittedRecord(commit, sha256_bytes(current), current, True)


def persist_immutable_json(path: Path, value: Mapping[str, Any]) -> CommittedRecord:
    payload = canonical_json_bytes(value)
    committed = persist_immutable_bytes(path, payload)
    return CommittedRecord(
        committed.path, committed.record_sha256, dict(value), committed.reused
    )


def bind_immutable_file(
    source: Path, destination: Path, *, expected_sha256: str
) -> CommittedRecord:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise ImmutableEvidenceError("expected immutable file digest is invalid")
    source_path = source.expanduser().resolve()
    if not source_path.is_file() or sha256_file(source_path) != expected_sha256:
        raise ImmutableEvidenceError(
            f"immutable source does not match expected digest: {source_path}"
        )
    committed = persist_immutable_bytes(destination, source_path.read_bytes())
    if committed.record_sha256 != expected_sha256:
        raise ImmutableEvidenceError(
            f"immutable destination digest mismatch: {committed.path}"
        )
    return committed
