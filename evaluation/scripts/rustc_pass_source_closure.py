#!/usr/bin/env python3
"""Source-closure identity and materialization for the MIR rewrite driver.

The historical entrypoint is now a thin module shim.  A source snapshot is
compilable only when every relative module it names is retained beside it.
Older revisions that still contain the monolithic entrypoint remain a valid
one-file closure.
"""

from __future__ import annotations

import hashlib
import pathlib
import shutil
import subprocess
from typing import Any, Iterable


PASS_DIRECTORY = pathlib.PurePosixPath("tools/unialloc-rustc-pass")
PASS_ENTRYPOINT_RELATIVE_PATH = PASS_DIRECTORY / "unialloc-rustc-mir-rewrite-dry-run.rs"
PASS_ENGINE_RELATIVE_PATH = PASS_DIRECTORY / "unialloc-rustc-driver-engine.rs"
PASS_LIFETIME_POLICY_RELATIVE_PATH = PASS_DIRECTORY / "lifetime_aware.rs"
PASS_SOURCE_CANDIDATE_RELATIVE_PATHS = (
    PASS_ENTRYPOINT_RELATIVE_PATH,
    PASS_ENGINE_RELATIVE_PATH,
    PASS_LIFETIME_POLICY_RELATIVE_PATH,
)


class PassSourceClosureError(RuntimeError):
    """A pass source closure is missing or internally inconsistent."""


def _required_paths_from_payloads(
    entrypoint: bytes,
    *,
    engine: bytes | None,
) -> tuple[pathlib.PurePosixPath, ...]:
    paths = [PASS_ENTRYPOINT_RELATIVE_PATH]
    entrypoint_text = entrypoint.decode("utf-8", errors="replace")
    engine_name = PASS_ENGINE_RELATIVE_PATH.name
    if engine_name in entrypoint_text:
        if engine is None:
            raise PassSourceClosureError(
                f"pass entrypoint references missing module {engine_name}"
            )
        paths.append(PASS_ENGINE_RELATIVE_PATH)
        engine_text = engine.decode("utf-8", errors="replace")
        if (
            "mod lifetime_aware;" in engine_text
            or PASS_LIFETIME_POLICY_RELATIVE_PATH.name in engine_text
        ):
            paths.append(PASS_LIFETIME_POLICY_RELATIVE_PATH)
    return tuple(paths)


def source_closure_relative_paths(root: pathlib.Path) -> tuple[pathlib.PurePosixPath, ...]:
    """Return and validate the relative files required to compile the pass."""

    entrypoint = root / PASS_ENTRYPOINT_RELATIVE_PATH
    if not entrypoint.is_file():
        raise PassSourceClosureError(f"missing pass entrypoint: {entrypoint}")
    engine_path = root / PASS_ENGINE_RELATIVE_PATH
    paths = _required_paths_from_payloads(
        entrypoint.read_bytes(),
        engine=engine_path.read_bytes() if engine_path.is_file() else None,
    )
    missing = [str(path) for path in paths if not (root / path).is_file()]
    if missing:
        raise PassSourceClosureError(
            "pass source closure is missing required files: " + ",".join(missing)
        )
    return paths


def git_source_closure_relative_paths(
    repository: pathlib.Path, revision: str
) -> tuple[pathlib.PurePosixPath, ...]:
    """Resolve a monolithic or module-based pass closure at one Git revision."""

    def git_blob(relative: pathlib.PurePosixPath, *, required: bool) -> bytes | None:
        result = subprocess.run(
            ["git", "show", f"{revision}:{relative}"],
            cwd=repository,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            return result.stdout
        if required:
            detail = result.stderr.decode("utf-8", errors="replace")[-2000:]
            raise PassSourceClosureError(
                f"failed to read pass source {revision}:{relative}: {detail}"
            )
        return None

    entrypoint = git_blob(PASS_ENTRYPOINT_RELATIVE_PATH, required=True)
    assert entrypoint is not None
    engine = git_blob(PASS_ENGINE_RELATIVE_PATH, required=False)
    paths = _required_paths_from_payloads(entrypoint, engine=engine)
    for path in paths:
        git_blob(path, required=True)
    return paths


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def closure_digest(
    root: pathlib.Path,
    relative_paths: Iterable[pathlib.PurePosixPath] | None = None,
) -> tuple[str, int, int]:
    paths = tuple(relative_paths or source_closure_relative_paths(root))
    digest = hashlib.sha256()
    size_bytes = 0
    for relative in sorted(set(paths), key=str):
        payload = (root / relative).read_bytes()
        digest.update(str(relative).encode())
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        size_bytes += len(payload)
    return digest.hexdigest(), len(set(paths)), size_bytes


def source_closure_provenance(root: pathlib.Path) -> dict[str, Any]:
    paths = source_closure_relative_paths(root)
    digest, file_count, size_bytes = closure_digest(root, paths)
    entrypoint = root / PASS_ENTRYPOINT_RELATIVE_PATH
    return {
        # Retain the historical per-entrypoint fields for artifact readers.
        "pass_source": str(entrypoint.resolve()),
        "pass_source_sha256": sha256_file(entrypoint),
        # Cache and drift identities bind the full compilable source closure.
        "pass_source_closure_sha256": digest,
        "pass_source_closure_file_count": file_count,
        "pass_source_closure_size_bytes": size_bytes,
        "pass_source_files": [
            {
                "relative_path": str(relative),
                "sha256": sha256_file(root / relative),
                "size_bytes": (root / relative).stat().st_size,
            }
            for relative in paths
        ],
    }


def source_closure_provenance_for_entrypoint(entrypoint: pathlib.Path) -> dict[str, Any]:
    """Bind a repository pass entrypoint, with a one-file test/tool fallback."""

    expected_suffix = pathlib.Path(PASS_ENTRYPOINT_RELATIVE_PATH)
    if len(entrypoint.parents) >= 3:
        candidate_root = entrypoint.parents[2]
        if candidate_root / expected_suffix == entrypoint:
            return source_closure_provenance(candidate_root)
    digest = sha256_file(entrypoint)
    size_bytes = entrypoint.stat().st_size
    return {
        "pass_source": str(entrypoint.resolve()),
        "pass_source_sha256": digest,
        "pass_source_closure_sha256": digest,
        "pass_source_closure_file_count": 1,
        "pass_source_closure_size_bytes": size_bytes,
        "pass_source_files": [
            {
                "relative_path": entrypoint.name,
                "sha256": digest,
                "size_bytes": size_bytes,
            }
        ],
    }


def copy_source_closure(
    source_root: pathlib.Path, destination_root: pathlib.Path
) -> tuple[pathlib.PurePosixPath, ...]:
    """Copy the pass closure without flattening its relative module paths."""

    paths = source_closure_relative_paths(source_root)
    for relative in paths:
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, destination)
    # Validate the materialized closure before a caller publishes its digest.
    source_closure_relative_paths(destination_root)
    return paths
