#!/usr/bin/env python3
"""Run exact-pin redb and Actix Web Type Isolation campaigns.

The adapter fails closed on source pins, allocator activation, actual-MIR
compiler provenance, correctness, or incomplete primary three-round pairing. Raw build,
audit, command, stdout, stderr, and GNU time artifacts remain under the selected
raw directory. A compiler-route miss retains the complete target as an explicit
attribution limit rather than discarding otherwise eligible measurements.
Each campaign runs one warmup plus three paired measured rounds and reports
median point estimates.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import realworld_type_isolation_matrix as matrix  # noqa: E402
import rustc_pass_source_closure as pass_closure  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_RAW_DIR = ROOT / "evaluation" / "raw" / "type-isolation-redb-actix-f5d0c19"
DEFAULT_CHECKOUT_ROOT = ROOT / "evaluation" / "external" / "_checkouts"
PINNED_IMPLEMENTATION_REVISION = "f5d0c19c1cc5b56fdac3282d69333dd8c85d4cf2"
PINNED_IMPLEMENTATION_SHA256 = (
    "cab1e580c08e2b16308bae75501716049ba428b040fb04bf305269c9ba9eaf01"
)
PINNED_IMPLEMENTATION_FILE_COUNT = 131
PINNED_IMPLEMENTATION_SIZE_BYTES = 4_426_670
PASS_RELATIVE_PATH = pass_closure.PASS_ENTRYPOINT_RELATIVE_PATH
ASSEMBLER_TARGET_DIR = (
    ROOT / "evaluation" / "raw" / "type-isolation-primary-v1" / "targets"
)
MEASUREMENT_LOCK_PATH = (
    ROOT
    / "evaluation"
    / "raw"
    / "type-isolation-primary-v1"
    / "primary-measurement.lock"
)
VARIANTS = ("unialloc", "typed_plain", "typeiso_perf")
REQUIRED_GATES = (
    "build_success",
    "correctness",
    "allocator_activation",
    "actual_mir_provenance",
    "stats_disabled",
    "compiler_route_equivalent",
    "source_audit_retained",
)
CORE_REQUIRED_GATES = tuple(
    gate for gate in REQUIRED_GATES if gate != "compiler_route_equivalent"
)
RESULT_PREFIX = "UNIALLOC_REDB_ACTIX_RESULT="
ROUNDS = 3
# Artifact readers retain compatibility with completed five-round campaigns;
# every scheduling path below accepts ROUNDS only.
LEGACY_ROUNDS = 5
PUBLISHABLE_ROUND_COUNTS = frozenset((ROUNDS, LEGACY_ROUNDS))
BASELINE_FORCE_WRAPPER_SOURCE = r'''#!/usr/bin/env python3
"""Force-load a baseline UniAlloc rlib into selected Cargo rustc invocations."""

import os
import pathlib
import sys


def fail(message: str) -> "None":
    print(f"unialloc baseline force-load wrapper: {message}", file=sys.stderr)
    raise SystemExit(2)


def normalized(value: str) -> str:
    return value.strip().replace("-", "_")


arguments = sys.argv[1:]
if not arguments:
    fail("missing Cargo rustc invocation")
rustc = pathlib.Path(arguments.pop(0))
if not rustc.is_file():
    fail(f"missing rustc executable: {rustc}")

crate = ""
for index, value in enumerate(arguments):
    if value == "--crate-name" and index + 1 < len(arguments):
        crate = normalized(arguments[index + 1])
        break
    if value.startswith("--crate-name="):
        crate = normalized(value.split("=", 1)[1])
        break

targets = {
    normalized(value)
    for value in os.environ.get("UNIALLOC_RUSTC_TARGET_CRATES", "").split(",")
    if normalized(value)
}
rlib = pathlib.Path(os.environ.get("UNIALLOC_FORCE_LOAD_RLIB", ""))
dependency_dir = pathlib.Path(
    os.environ.get("UNIALLOC_FORCE_LOAD_DEPENDENCY_DIR", "")
)
if not rlib.is_file() or rlib.suffix != ".rlib":
    fail(f"missing baseline UniAlloc rlib: {rlib}")
if not dependency_dir.is_dir():
    fail(f"missing baseline dependency directory: {dependency_dir}")
arguments.extend(["-L", f"dependency={dependency_dir}"])
if crate in targets:
    arguments.extend(
        [
            "-Z",
            "unstable-options",
            "--extern",
            f"force:unialloc={rlib}",
        ]
    )

os.execv(str(rustc), [str(rustc), *arguments])
'''


class CampaignError(RuntimeError):
    """A fail-closed source, build, provenance, or measurement error."""


class HarnessCorrectnessError(CampaignError):
    """A benchmark completed at the process level but failed its workload."""

    def __init__(self, harness_id: str, message: str) -> None:
        super().__init__(message)
        self.harness_id = harness_id


@dataclasses.dataclass(frozen=True)
class HarnessSpec:
    id: str
    metric_direction: str = "lower_is_better"


@dataclasses.dataclass(frozen=True)
class TargetSpec:
    id: str
    label: str
    repository: str
    source_ref: str
    source_commit: str
    checkout_name: str
    harnesses: tuple[HarnessSpec, ...]
    target_crates: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ImplementationSnapshot:
    revision: str
    sha256: str
    path: pathlib.Path
    manifest_path: pathlib.Path
    file_count: int
    size_bytes: int
    repository: pathlib.Path


TARGETS = {
    "redb": TargetSpec(
        id="redb",
        label="redb",
        repository="https://github.com/cberner/redb.git",
        source_ref="v4.1.0",
        source_commit="6ed1f981ba4deab0b2adbdd7bccb46ec409b2191",
        checkout_name="type-isolation-redb-actix-redb",
        harnesses=tuple(
            HarnessSpec(value)
            for value in (
                "bulk_small",
                "transaction_churn",
                "delete_reinsert",
                "large_values",
            )
        ),
        target_crates=("unialloc_redb_actix_runner", "redb"),
    ),
    "actix_web": TargetSpec(
        id="actix_web",
        label="Actix Web",
        repository="https://github.com/actix/actix-web.git",
        source_ref="web-v4.14.0",
        source_commit="696b1fed9c5b0147c37c70e0808cae5f63a5a4a0",
        checkout_name="type-isolation-redb-actix-actix-web",
        harnesses=tuple(
            HarnessSpec(value)
            for value in (
                "async_service_direct",
                "async_web_service_direct",
                "get_body_async_burst",
                "compression_gzip",
                "router_actix",
            )
        ),
        target_crates=(
            "service",
            "server",
            "response_body_compression",
            "router",
            "actix_web",
            "actix_http",
            "actix_router",
        ),
    ),
}


REDB_RUNNER_SOURCE = r"""use redb::{
    Database, Durability, ReadableDatabase, ReadableTable, ReadableTableMetadata,
    Table, TableDefinition,
};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

#[global_allocator]
static ALLOCATOR: unialloc::UniAlloc = unialloc::UniAlloc;

#[unsafe(no_mangle)]
pub static UNIALLOC_REDB_ACTIX_ALLOCATOR_MARKER: u8 = 0xa5;

#[inline(never)]
fn verify_allocator_marker() {
    let value = unsafe {
        std::ptr::read_volatile(&raw const UNIALLOC_REDB_ACTIX_ALLOCATOR_MARKER)
    };
    assert_eq!(value, 0xa5);
}

const TABLE: TableDefinition<u64, &[u8]> = TableDefinition::new("primary");

fn database_path(work_dir: &Path) -> PathBuf {
    fs::create_dir_all(work_dir).expect("create work directory");
    work_dir.join("redb-primary.db")
}

fn new_database(work_dir: &Path) -> Database {
    let path = database_path(work_dir);
    if path.exists() {
        fs::remove_file(&path).expect("remove stale database");
    }
    Database::create(path).expect("create database")
}

fn non_durable_write(database: &Database) -> redb::WriteTransaction {
    let mut transaction = database.begin_write().expect("begin write");
    transaction
        .set_durability(Durability::None)
        .expect("set non-durable mode");
    transaction
}

fn table_len(database: &Database) -> u64 {
    let transaction = database.begin_read().expect("begin read");
    let table = transaction.open_table(TABLE).expect("open table");
    table.len().expect("read table length")
}

fn bulk_small(database: &Database) -> u64 {
    let value = [0x5au8; 128];
    let transaction = non_durable_write(database);
    {
        let mut table = transaction.open_table(TABLE).expect("open table");
        for key in 0..200_000u64 {
            table.insert(key, value.as_slice()).expect("insert value");
        }
    }
    transaction.commit().expect("commit bulk transaction");
    assert_eq!(table_len(database), 200_000);
    200_000
}

fn transaction_churn(database: &Database) -> u64 {
    let value = [0xa5u8; 64];
    for transaction_index in 0..2_000u64 {
        let transaction = non_durable_write(database);
        {
            let mut table = transaction.open_table(TABLE).expect("open table");
            for item in 0..64u64 {
                let key = transaction_index * 64 + item;
                table.insert(key, value.as_slice()).expect("insert value");
            }
        }
        transaction.commit().expect("commit churn transaction");
    }
    assert_eq!(table_len(database), 2_000 * 64);
    2_000 * 64
}

fn delete_reinsert(database: &Database) -> u64 {
    let value = [0x3cu8; 64];
    let preload = non_durable_write(database);
    {
        let mut table = preload.open_table(TABLE).expect("open table");
        for key in 0..200_000u64 {
            table.insert(key, value.as_slice()).expect("preload value");
        }
    }
    preload.commit().expect("commit preload");

    let transaction = non_durable_write(database);
    {
        let mut table = transaction.open_table(TABLE).expect("open table");
        for key in 0..100_000u64 {
            table.remove(key).expect("remove value");
        }
        for key in 200_000..300_000u64 {
            table.insert(key, value.as_slice()).expect("reinsert value");
        }
    }
    transaction.commit().expect("commit replacement");
    assert_eq!(table_len(database), 200_000);
    200_000
}

fn large_values(database: &Database) -> u64 {
    let mut value = vec![0x69u8; 512 * 1024];
    let transaction = non_durable_write(database);
    {
        let mut table = transaction.open_table(TABLE).expect("open table");
        for key in 0..64u64 {
            value[..8].copy_from_slice(&key.to_le_bytes());
            table.insert(key, value.as_slice()).expect("insert large value");
        }
    }
    transaction.commit().expect("commit large values");
    assert_eq!(table_len(database), 64);
    64
}

fn main() {
    verify_allocator_marker();
    let arguments: Vec<String> = env::args().collect();
    assert_eq!(arguments.len(), 3, "usage: runner HARNESS WORK_DIR");
    let harness = arguments[1].as_str();
    let work_dir = Path::new(&arguments[2]);
    let database = new_database(work_dir);
    let started = Instant::now();
    let operations = match harness {
        "bulk_small" => bulk_small(&database),
        "transaction_churn" => transaction_churn(&database),
        "delete_reinsert" => delete_reinsert(&database),
        "large_values" => large_values(&database),
        other => panic!("unknown harness: {other}"),
    };
    let elapsed = started.elapsed().as_secs_f64();
    drop(database);
    fs::remove_dir_all(work_dir).expect("remove work directory");
    println!(
        "UNIALLOC_REDB_ACTIX_RESULT={{\"harness\":\"{}\",\"operations\":{},\"elapsed_seconds\":{:.9},\"correctness\":true}}",
        harness, operations, elapsed
    );
}
"""


ACTIX_BENCHES = {
    "async_service_direct": (
        "actix-web",
        "service",
        "async_service_direct",
        pathlib.Path("actix-web/benches/service.rs"),
    ),
    "async_web_service_direct": (
        "actix-web",
        "service",
        "async_web_service_direct",
        pathlib.Path("actix-web/benches/service.rs"),
    ),
    "get_body_async_burst": (
        "actix-web",
        "server",
        "get_body_async_burst",
        pathlib.Path("actix-web/benches/server.rs"),
    ),
    "compression_gzip": (
        "actix-http",
        "response-body-compression",
        "compression responses/gzip",
        pathlib.Path("actix-http/benches/response-body-compression.rs"),
    ),
    "router_actix": (
        "actix-router",
        "router",
        "Compare Routers/actix",
        pathlib.Path("actix-router/benches/router.rs"),
    ),
}

ACTIX_GET_BODY_BURST_SOURCE = """\
                let burst = (0..iters).map(|_| client.send());
                let resps = join_all(burst).await;

                let elapsed = start.elapsed();

                // if there are failed requests that might be an issue
                let failed = resps.iter().filter(|r| r.is_err()).count();
                if failed > 0 {
                    eprintln!("failed {} requests (might be bench timeout)", failed);
                };
"""

ACTIX_GET_BODY_BOUNDED_SOURCE = """\
                const MAX_IN_FLIGHT_REQUESTS: u64 = 8;
                let mut remaining = iters;
                while remaining > 0 {
                    let batch_size = remaining.min(MAX_IN_FLIGHT_REQUESTS);
                    let responses =
                        join_all((0..batch_size).map(|_| client.send())).await;
                    for response in responses {
                        let mut response =
                            response.expect("get_body_async_burst request failed");
                        assert!(
                            response.status().is_success(),
                            "get_body_async_burst returned {}",
                            response.status()
                        );
                        let body = response
                            .body()
                            .await
                            .expect("get_body_async_burst body read failed");
                        assert_eq!(
                            body.as_ref(),
                            STR.as_bytes(),
                            "response body mismatch"
                        );
                    }
                    remaining -= batch_size;
                }

                let elapsed = start.elapsed();
"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    return matrix.sha256_file(path)


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def run_command(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: pathlib.Path,
    env: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    result = matrix.execute(command, cwd=cwd, env=env, timeout=timeout)
    return {
        **result,
        "stdout_text": result["stdout"].decode("utf-8", errors="replace"),
        "stderr_text": result["stderr"].decode("utf-8", errors="replace"),
    }


def git_output(checkout: pathlib.Path, *arguments: str) -> str:
    result = run_command(
        ["git", *arguments], cwd=checkout, env=os.environ.copy(), timeout=120
    )
    if result["exit_code"] != 0 or result["timed_out"]:
        raise CampaignError(result["stderr_text"])
    return result["stdout_text"].strip()


def canonical_implementation_path(path: str) -> bool:
    candidate = pathlib.PurePosixPath(path)
    if candidate in pass_closure.PASS_SOURCE_CANDIDATE_RELATIVE_PATHS:
        return True
    if not candidate.parts or candidate.parts[0] not in {"unialloc", "alloc_macros"}:
        return False
    return "target" not in candidate.parts and (
        candidate.suffix in {".rs", ".toml"} or candidate.name == "build.rs"
    )


def canonical_stream(root: pathlib.Path, paths: Sequence[str]) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    selected = sorted(path for path in paths if canonical_implementation_path(path))
    for relative in selected:
        payload = (root / relative).read_bytes()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        size_bytes += len(payload)
    return digest.hexdigest(), len(selected), size_bytes


def snapshot_stream(root: pathlib.Path, paths: Sequence[str]) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    selected = sorted(set(paths))
    for relative in selected:
        payload = (root / relative).read_bytes()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        size_bytes += len(payload)
    return digest.hexdigest(), len(selected), size_bytes


def working_tree_paths(repository: pathlib.Path) -> list[str]:
    paths = ["Cargo.toml", "Cargo.lock", "rust-toolchain"]
    paths.extend(
        str(path) for path in pass_closure.source_closure_relative_paths(repository)
    )
    for root_name in ("unialloc", "alloc_macros"):
        source_root = repository / root_name
        paths.extend(
            path.relative_to(repository).as_posix()
            for path in source_root.rglob("*")
            if path.is_file()
            and "target" not in path.relative_to(repository).parts
            and ".git" not in path.relative_to(repository).parts
        )
    selected = sorted(set(paths))
    missing = [
        relative for relative in selected if not (repository / relative).is_file()
    ]
    if missing:
        raise CampaignError(
            "working-tree snapshot is missing required files: " + ",".join(missing)
        )
    return selected


def git_tree_blob_rows(repository: pathlib.Path, revision: str) -> list[dict[str, str]]:
    pass_sources = pass_closure.git_source_closure_relative_paths(repository, revision)
    tree = run_command(
        [
            "git",
            "ls-tree",
            "-r",
            revision,
            "--",
            "unialloc",
            "alloc_macros",
            "Cargo.toml",
            "Cargo.lock",
            "rust-toolchain",
            *[str(path) for path in pass_sources],
        ],
        cwd=repository,
        env=os.environ.copy(),
        timeout=120,
    )
    if tree["exit_code"] != 0 or tree["timed_out"]:
        raise CampaignError(f"Git tree enumeration failed: {tree['stderr_text']}")
    rows: list[dict[str, str]] = []
    for line in tree["stdout_text"].splitlines():
        metadata, separator, path = line.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise CampaignError(f"malformed git ls-tree row: {line!r}")
        mode, object_type, object_id = fields
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise CampaignError(
                f"unsupported implementation Git object: {mode} {object_type} {path}"
            )
        rows.append(
            {
                "path": path,
                "git_mode": mode,
                "git_object_id": object_id,
            }
        )
    paths = {row["path"] for row in rows}
    required = {"Cargo.toml", "Cargo.lock", *[str(path) for path in pass_sources]}
    if not required.issubset(paths):
        raise CampaignError(
            "implementation revision is missing required files: "
            + ",".join(sorted(required - paths))
        )
    return rows


def git_blob(repository: pathlib.Path, revision: str, path: str) -> bytes:
    result = matrix.execute(
        ["git", "show", f"{revision}:{path}"],
        cwd=repository,
        env=os.environ.copy(),
        timeout=120,
    )
    if result["exit_code"] != 0 or result["timed_out"]:
        detail = result["stderr"].decode("utf-8", errors="replace")
        raise CampaignError(f"failed to read Git blob {revision}:{path}: {detail}")
    return result["stdout"]


def verify_implementation_snapshot(
    snapshot: ImplementationSnapshot,
) -> dict[str, Any]:
    try:
        manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CampaignError(
            f"invalid implementation snapshot manifest: {snapshot.manifest_path}"
        ) from error
    source_kind = manifest.get("source_kind", "git_revision")
    if source_kind == "git_revision":
        rows = manifest.get("git_blobs")
        if not isinstance(rows, list) or not rows:
            raise CampaignError("implementation snapshot manifest has no Git blobs")
        expected_git_rows = git_tree_blob_rows(snapshot.repository, snapshot.revision)
        expected_git_identity = {
            (row["path"], row["git_mode"], row["git_object_id"])
            for row in expected_git_rows
        }
        observed_git_identity = {
            (
                str(row.get("path")),
                str(row.get("git_mode")),
                str(row.get("git_object_id")),
            )
            for row in rows
            if isinstance(row, dict)
        }
        if observed_git_identity != expected_git_identity:
            raise CampaignError("implementation snapshot Git blob identity mismatch")
    elif source_kind == "working_tree":
        rows = manifest.get("files")
        if not isinstance(rows, list) or not rows:
            raise CampaignError("working-tree snapshot manifest has no files")
        if (
            manifest.get("campaign_classification") != "diagnostic_current_worktree"
            or manifest.get("primary_eligible") is not False
        ):
            raise CampaignError("working-tree snapshot lacks diagnostic classification")
    else:
        raise CampaignError(f"unknown implementation source kind: {source_kind!r}")
    paths: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise CampaignError("implementation snapshot has a malformed Git blob row")
        path = str(row["path"])
        source = snapshot.path / path
        if not source.is_file() or sha256_file(source) != row.get("sha256"):
            raise CampaignError(f"implementation snapshot blob mismatch: {path}")
        paths.append(path)
    digest, file_count, size_bytes = canonical_stream(snapshot.path, paths)
    observed = (digest, file_count, size_bytes)
    expected = (snapshot.sha256, snapshot.file_count, snapshot.size_bytes)
    if observed != expected:
        raise CampaignError(
            "implementation canonical stream mismatch: "
            f"observed={observed}, expected={expected}"
        )
    if (
        manifest.get("implementation_revision") != snapshot.revision
        or manifest.get("implementation_sha256") != snapshot.sha256
    ):
        raise CampaignError("implementation snapshot identity mismatch")
    if source_kind == "working_tree":
        context = snapshot_stream(snapshot.path, paths)
        expected_context = (
            manifest.get("campaign_snapshot_sha256"),
            manifest.get("campaign_snapshot_file_count"),
            manifest.get("campaign_snapshot_size_bytes"),
        )
        if context != expected_context:
            raise CampaignError(
                "working-tree snapshot context mismatch: "
                f"observed={context}, expected={expected_context}"
            )
        status = str(manifest.get("repository_status", ""))
        if manifest.get("repository_status_sha256") != sha256_bytes(status.encode()):
            raise CampaignError("working-tree repository status digest mismatch")
    return manifest


def materialize_implementation_revision(
    raw_dir: pathlib.Path,
    revision: str,
    *,
    repository: pathlib.Path = ROOT,
) -> ImplementationSnapshot:
    commit = git_output(repository, "rev-parse", f"{revision}^{{commit}}")
    if commit != revision:
        raise CampaignError(
            f"implementation revision must be an exact commit: {revision} -> {commit}"
        )
    rows = git_tree_blob_rows(repository, commit)
    staging_root = raw_dir / "frozen-implementation"
    staging = staging_root / f"snapshot-{os.getpid()}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    persisted_rows: list[dict[str, Any]] = []
    try:
        for row in rows:
            relative = row["path"]
            payload = git_blob(repository, commit, relative)
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            if row["git_mode"] == "100755":
                destination.chmod(0o755)
            persisted_rows.append(
                {
                    **row,
                    "sha256": sha256_bytes(payload),
                    "size_bytes": len(payload),
                }
            )
        digest, file_count, size_bytes = canonical_stream(
            staging, [str(row["path"]) for row in rows]
        )
        if (
            digest != PINNED_IMPLEMENTATION_SHA256
            or file_count != PINNED_IMPLEMENTATION_FILE_COUNT
            or size_bytes != PINNED_IMPLEMENTATION_SIZE_BYTES
        ):
            raise CampaignError(
                "pinned implementation canonical stream mismatch: "
                f"sha256={digest}, files={file_count}, bytes={size_bytes}"
            )
        destination = staging_root / digest
        manifest_path = destination / "snapshot.json"
        manifest = {
            "schema_version": 1,
            "implementation_revision": commit,
            "implementation_tree": git_output(
                repository, "rev-parse", f"{commit}^{{tree}}"
            ),
            "implementation_sha256": digest,
            "canonical_stream_format": "sorted-relative-path-nul-bytes-nul",
            "canonical_file_count": file_count,
            "canonical_size_bytes": size_bytes,
            "git_blobs": persisted_rows,
        }
        (staging / "snapshot.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        snapshot = ImplementationSnapshot(
            revision=commit,
            sha256=digest,
            path=destination.resolve(),
            manifest_path=manifest_path.resolve(),
            file_count=file_count,
            size_bytes=size_bytes,
            repository=repository.resolve(),
        )
        if destination.exists():
            shutil.rmtree(staging)
        else:
            staging.replace(destination)
        verify_implementation_snapshot(snapshot)
        return snapshot
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def materialize_working_tree_implementation(
    raw_dir: pathlib.Path,
    *,
    repository: pathlib.Path = ROOT,
) -> ImplementationSnapshot:
    head_before = git_output(repository, "rev-parse", "HEAD")
    status_before = git_output(repository, "status", "--short")
    paths = working_tree_paths(repository)
    source_canonical = canonical_stream(repository, paths)
    source_context = snapshot_stream(repository, paths)
    staging_root = raw_dir / "frozen-implementation"
    staging = staging_root / f"working-tree-{os.getpid()}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    persisted_rows: list[dict[str, Any]] = []
    try:
        for relative in paths:
            source = repository / relative
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            payload = destination.read_bytes()
            persisted_rows.append(
                {
                    "path": relative,
                    "sha256": sha256_bytes(payload),
                    "size_bytes": len(payload),
                }
            )
        head_after = git_output(repository, "rev-parse", "HEAD")
        status_after = git_output(repository, "status", "--short")
        if (
            head_after != head_before
            or status_after != status_before
            or canonical_stream(repository, paths) != source_canonical
            or snapshot_stream(repository, paths) != source_context
        ):
            raise CampaignError("working tree changed while it was being frozen")
        digest, file_count, size_bytes = canonical_stream(staging, paths)
        context_digest, context_count, context_size = snapshot_stream(staging, paths)
        if (digest, file_count, size_bytes) != source_canonical or (
            context_digest,
            context_count,
            context_size,
        ) != source_context:
            raise CampaignError("frozen working-tree snapshot differs from its source")
        status_digest = sha256_bytes(status_before.encode())
        destination = staging_root / (
            f"working-tree-{digest}-{context_digest[:16]}-{status_digest[:16]}"
        )
        manifest_path = destination / "snapshot.json"
        manifest = {
            "schema_version": 1,
            "source_kind": "working_tree",
            "campaign_classification": "diagnostic_current_worktree",
            "primary_eligible": False,
            "implementation_revision": head_before,
            "implementation_sha256": digest,
            "canonical_stream_format": "sorted-relative-path-nul-bytes-nul",
            "canonical_file_count": file_count,
            "canonical_size_bytes": size_bytes,
            "campaign_snapshot_sha256": context_digest,
            "campaign_snapshot_file_count": context_count,
            "campaign_snapshot_size_bytes": context_size,
            "repository_head": head_before,
            "repository_status": status_before,
            "repository_status_sha256": status_digest,
            "files": persisted_rows,
        }
        (staging / "snapshot.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        snapshot = ImplementationSnapshot(
            revision=head_before,
            sha256=digest,
            path=destination.resolve(),
            manifest_path=manifest_path.resolve(),
            file_count=file_count,
            size_bytes=size_bytes,
            repository=repository.resolve(),
        )
        if destination.exists():
            existing_manifest = destination / "snapshot.json"
            if not existing_manifest.is_file():
                raise CampaignError(
                    f"working-tree snapshot destination is incomplete: {destination}"
                )
            shutil.rmtree(staging)
        else:
            staging.replace(destination)
        verify_implementation_snapshot(snapshot)
        return snapshot
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def parse_cpu_set(raw: str) -> set[int]:
    cpus: set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise CampaignError(f"invalid CPU range: {token}")
            cpus.update(range(start, end + 1))
        else:
            cpus.add(int(token))
    if not cpus or min(cpus) < 0:
        raise CampaignError(f"invalid CPU set: {raw}")
    return cpus


def environment_record() -> dict[str, Any]:
    affinity = sorted(os.sched_getaffinity(0))
    load = os.getloadavg()
    uname = os.uname()
    return {
        "cpu_affinity": affinity,
        "cpu_affinity_text": ",".join(str(cpu) for cpu in affinity),
        "load_average_1m": load[0],
        "load_average_5m": load[1],
        "load_average_15m": load[2],
        "logical_cpu_count": os.cpu_count(),
        "kernel_release": uname.release,
        "machine": uname.machine,
        "glibc_tunables": matrix.merge_glibc_tunable(
            os.environ.get("GLIBC_TUNABLES", "")
        ),
    }


@contextlib.contextmanager
def primary_measurement_lock(target_id: str):
    MEASUREMENT_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MEASUREMENT_LOCK_PATH.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "target_id": target_id,
                    "acquired_unix_seconds": time.time(),
                    "cpu_affinity": sorted(os.sched_getaffinity(0)),
                },
                sort_keys=True,
            )
            + "\n"
        )
        handle.flush()
        try:
            yield str(MEASUREMENT_LOCK_PATH.resolve())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def materialize_source(
    spec: TargetSpec, checkout_root: pathlib.Path, raw_dir: pathlib.Path
) -> tuple[pathlib.Path, dict[str, Any]]:
    checkout = checkout_root / spec.checkout_name
    if not (checkout / ".git").is_dir():
        checkout_root.mkdir(parents=True, exist_ok=True)
        result = run_command(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                spec.repository,
                checkout,
            ],
            cwd=checkout_root,
            env=os.environ.copy(),
            timeout=900,
        )
        if result["exit_code"] != 0 or result["timed_out"]:
            raise CampaignError(f"clone failed for {spec.id}: {result['stderr_text']}")
    fetch = run_command(
        ["git", "fetch", "--depth=1", "origin", spec.source_commit],
        cwd=checkout,
        env=os.environ.copy(),
        timeout=900,
    )
    if fetch["exit_code"] != 0 or fetch["timed_out"]:
        raise CampaignError(f"fetch failed for {spec.id}: {fetch['stderr_text']}")
    checkout_result = run_command(
        ["git", "checkout", "--detach", spec.source_commit],
        cwd=checkout,
        env=os.environ.copy(),
        timeout=120,
    )
    if checkout_result["exit_code"] != 0 or checkout_result["timed_out"]:
        raise CampaignError(
            f"checkout failed for {spec.id}: {checkout_result['stderr_text']}"
        )
    head = git_output(checkout, "rev-parse", "HEAD")
    status = git_output(checkout, "status", "--short")
    if head != spec.source_commit:
        raise CampaignError(
            f"{spec.id} source mismatch: got {head}, expected {spec.source_commit}"
        )
    if status:
        raise CampaignError(f"{spec.id} checkout is dirty:\n{status}")
    record = {
        "target_id": spec.id,
        "repository": spec.repository,
        "source_ref": spec.source_ref,
        "source_commit": head,
        "tree": git_output(checkout, "rev-parse", "HEAD^{tree}"),
        "checkout": str(checkout.resolve()),
        "status": status,
        "cargo_lock_sha256": (
            sha256_file(checkout / "Cargo.lock")
            if (checkout / "Cargo.lock").is_file()
            else None
        ),
    }
    write_json(raw_dir / "sources" / spec.id / "source.json", record)
    return checkout.resolve(), record


def actix_benchmark_selection(harness_id: str) -> tuple[str, str, str]:
    try:
        package, bench, selector, _source = ACTIX_BENCHES[harness_id]
    except KeyError as error:
        raise CampaignError(f"unknown Actix Web harness: {harness_id}") from error
    return package, bench, selector


def parse_criterion_estimate_seconds(text: str) -> float:
    matches = re.findall(
        r"time:\s*\[\s*[0-9.eE+-]+\s+(ns|us|µs|ms|s)\s+"
        r"([0-9.eE+-]+)\s+(ns|us|µs|ms|s)\s+"
        r"[0-9.eE+-]+\s+(ns|us|µs|ms|s)\s*\]",
        text,
    )
    if not matches:
        raise CampaignError("Criterion emitted no complete three-estimate time record")
    _low_unit, center, center_unit, _high_unit = matches[-1]
    multipliers = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0}
    value = float(center) * multipliers[center_unit]
    if not math.isfinite(value) or value <= 0:
        raise CampaignError(f"invalid Criterion estimate: {value}")
    return value


def actix_failed_request_count(text: str) -> int:
    """Return the largest nonzero workload-failure count emitted by Actix benches."""

    counts = [
        int(value)
        for value in re.findall(
            r"\bfailed\s+([0-9]+)\s+requests?\s*\(might be bench timeout\)",
            text,
            flags=re.IGNORECASE,
        )
    ]
    return max(counts, default=0)


def parse_redb_result(text: str, harness_id: str) -> float:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith(RESULT_PREFIX):
            continue
        try:
            value = json.loads(line[len(RESULT_PREFIX) :])
        except json.JSONDecodeError as error:
            raise CampaignError("redb runner emitted malformed result JSON") from error
        if isinstance(value, dict):
            rows.append(value)
    if len(rows) != 1:
        raise CampaignError(f"redb runner emitted {len(rows)} result records")
    row = rows[0]
    if row.get("harness") != harness_id or row.get("correctness") is not True:
        raise CampaignError(f"redb runner correctness record mismatch: {row}")
    value = float(row.get("elapsed_seconds", 0.0))
    if not math.isfinite(value) or value <= 0:
        raise CampaignError(f"invalid redb elapsed time: {value}")
    return value


def allocator_marker_source() -> str:
    return (
        matrix.allocator_source("unialloc")
        + "\n#[unsafe(no_mangle)]\n"
        + "pub static UNIALLOC_REDB_ACTIX_ALLOCATOR_MARKER: u8 = 0xa5;\n"
        + "#[inline(never)]\n"
        + 'extern "C" fn unialloc_redb_actix_verify_allocator_marker() {\n'
        + "    let value = unsafe { std::ptr::read_volatile("
        + "&raw const UNIALLOC_REDB_ACTIX_ALLOCATOR_MARKER) };\n"
        + "    assert_eq!(value, 0xa5);\n"
        + "}\n"
        + "#[used]\n"
        + '#[cfg_attr(target_family = "unix", unsafe(link_section = ".init_array"))]\n'
        + 'static UNIALLOC_REDB_ACTIX_MARKER_INIT: extern "C" fn() = '
        + "unialloc_redb_actix_verify_allocator_marker;\n"
    )


def unialloc_configuration_for_primary_variant(
    variant: str,
) -> tuple[tuple[str, ...], bool]:
    if variant == "unialloc":
        return (), True
    if variant in {"typed_plain", "typeiso_perf"}:
        return ("type_isolation",), True
    raise CampaignError(f"unsupported variant: {variant}")


def cargo_unialloc_dependency(variant: str, unialloc_path: pathlib.Path) -> str | None:
    if variant == "unialloc":
        features, _default_features_enabled = (
            unialloc_configuration_for_primary_variant(variant)
        )
        return matrix.cargo_path_dependency(unialloc_path, features)
    if variant in {"typed_plain", "typeiso_perf"}:
        return None
    raise CampaignError(f"unsupported variant: {variant}")


def ensure_baseline_force_wrapper(path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if (
        not path.is_file()
        or path.read_text(encoding="utf-8") != BASELINE_FORCE_WRAPPER_SOURCE
    ):
        path.write_text(BASELINE_FORCE_WRAPPER_SOURCE, encoding="utf-8")
    path.chmod(0o755)
    return path.resolve()


def ensure_revision_mir_wrapper(
    path: pathlib.Path,
    *,
    implementation: ImplementationSnapshot,
    toolchain: str,
    timeout: int,
) -> pathlib.Path:
    verify_implementation_snapshot(implementation)
    pass_source = implementation.path / PASS_RELATIVE_PATH
    pass_provenance = pass_closure.source_closure_provenance(implementation.path)
    record_path = path.with_name(path.name + ".build.json")
    if path.is_file() and record_path.is_file():
        try:
            cached = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = {}
        if (
            cached.get("success") is True
            and cached.get("toolchain") == toolchain
            and cached.get("implementation_revision") == implementation.revision
            and cached.get("implementation_sha256") == implementation.sha256
            and cached.get("pass_source_closure_sha256")
            == pass_provenance["pass_source_closure_sha256"]
            and cached.get("wrapper_sha256") == sha256_file(path)
        ):
            return path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    env = os.environ.copy()
    env["RUSTC_BOOTSTRAP"] = "1"
    result = run_command(
        [
            "rustc",
            f"+{toolchain}",
            "--cfg",
            "unialloc_rustc_current",
            pass_source,
            "-O",
            "-o",
            temporary,
        ],
        cwd=implementation.path,
        env=env,
        timeout=timeout,
    )
    if result["exit_code"] != 0 or result["timed_out"] or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise CampaignError(
            "pinned MIR wrapper build failed:\n" + result["stderr_text"][-8000:]
        )
    temporary.replace(path)
    write_json(
        record_path,
        {
            "schema_version": 1,
            "success": True,
            "toolchain": toolchain,
            "implementation_revision": implementation.revision,
            "implementation_sha256": implementation.sha256,
            "implementation_snapshot": str(implementation.path),
            **pass_provenance,
            "wrapper_sha256": sha256_file(path),
            "command": result["command"],
        },
    )
    return path.resolve()


def transitive_typeiso_force_wrapper_source() -> str:
    source = matrix.FORCE_LOAD_WRAPPER_SOURCE
    original = """selected = crate_name(arguments) in targets
if selected:
    if not rlib.is_file() or rlib.suffix != ".rlib":
        fail(f"missing force-load rlib: {rlib}")
    if not dependency_dir.is_dir():
        fail(f"missing force-load dependency directory: {dependency_dir}")
    for spec in extern_specs(arguments):
        name = spec.split("=", 1)[0].split(":")[-1]
        if normalized(name) == "unialloc":
            fail("selected crate already has a UniAlloc --extern entry")
    arguments.extend(
        [
            "-Z",
            "unstable-options",
            "--extern",
            f"force:unialloc={rlib}",
            "-L",
            f"dependency={dependency_dir}",
        ]
    )
"""
    replacement = """selected = crate_name(arguments) in targets
if not rlib.is_file() or rlib.suffix != ".rlib":
    fail(f"missing force-load rlib: {rlib}")
if not dependency_dir.is_dir():
    fail(f"missing force-load dependency directory: {dependency_dir}")
arguments.extend(["-L", f"dependency={dependency_dir}"])
if selected:
    for spec in extern_specs(arguments):
        name = spec.split("=", 1)[0].split(":")[-1]
        if normalized(name) == "unialloc":
            fail("selected crate already has a UniAlloc --extern entry")
    arguments.extend(
        [
            "-Z",
            "unstable-options",
            "--extern",
            f"force:unialloc={rlib}",
        ]
    )
"""
    if original not in source:
        # The shared wrapper may already contain the transitive dependency fix.
        if (
            'arguments.extend(["-L", f"dependency={dependency_dir}"])' in source
            and source.index('arguments.extend(["-L"') < source.index("if selected:")
        ):
            return source
        raise CampaignError("unrecognized shared force-load wrapper contract")
    return source.replace(original, replacement)


def ensure_transitive_typeiso_force_wrapper(path: pathlib.Path) -> pathlib.Path:
    source = transitive_typeiso_force_wrapper_source()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_text(encoding="utf-8") != source:
        path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path.resolve()


def build_baseline_force_load_rlib(
    *,
    raw_dir: pathlib.Path,
    implementation: ImplementationSnapshot,
    toolchain: str,
    jobs: int,
    timeout: int,
) -> dict[str, Any]:
    verify_implementation_snapshot(implementation)
    build_root = raw_dir / "force-load" / "baseline-default"
    target_dir = build_root / "target"
    record_path = build_root / "build.json"
    implementation_sha256 = implementation.sha256
    if record_path.is_file():
        try:
            cached = json.loads(record_path.read_text(encoding="utf-8"))
            rlib = pathlib.Path(str(cached.get("rlib", "")))
            if (
                cached.get("success") is True
                and cached.get("toolchain") == toolchain
                and cached.get("implementation_revision") == implementation.revision
                and cached.get("implementation_sha256") == implementation_sha256
                and rlib.is_file()
                and cached.get("rlib_sha256") == sha256_file(rlib)
            ):
                return cached
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    shutil.rmtree(build_root, ignore_errors=True)
    build_root.mkdir(parents=True)
    env = os.environ.copy()
    env["CARGO_INCREMENTAL"] = "0"
    env["CARGO_PROFILE_RELEASE_PANIC"] = "unwind"
    env["TMPDIR"] = str((raw_dir / "tmp" / "baseline-force-load").resolve())
    pathlib.Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    command = [
        "cargo",
        f"+{toolchain}",
        "build",
        "--release",
        "--locked",
        "--manifest-path",
        implementation.path / "unialloc" / "Cargo.toml",
        "--lib",
        "--target-dir",
        target_dir,
        "--jobs",
        str(jobs),
    ]
    result = run_command(command, cwd=implementation.path, env=env, timeout=timeout)
    artifacts = command_artifacts(result, build_root, "build")
    candidates = sorted((target_dir / "release" / "deps").glob("libunialloc-*.rlib"))
    success = (
        result["exit_code"] == 0 and not result["timed_out"] and len(candidates) == 1
    )
    record: dict[str, Any] = {
        "success": success,
        "toolchain": toolchain,
        "features": [],
        "default_features_enabled": True,
        "implementation_revision": implementation.revision,
        "implementation_sha256": implementation_sha256,
        "implementation_snapshot": str(implementation.path),
        "implementation_manifest_path": str(implementation.manifest_path),
        **artifacts,
    }
    if len(candidates) == 1:
        rlib = candidates[0].resolve()
        record.update(
            {
                "rlib": str(rlib),
                "rlib_sha256": sha256_file(rlib),
                "dependency_dir": str(rlib.parent),
            }
        )
    write_json(record_path, record)
    if not success:
        raise CampaignError(
            "baseline force-load UniAlloc build failed: "
            + result["stderr_text"][-8000:]
        )
    return record


def baseline_force_environment(
    base: dict[str, str],
    *,
    raw_dir: pathlib.Path,
    implementation: ImplementationSnapshot,
    toolchain: str,
    jobs: int,
    timeout: int,
    target_crates: Sequence[str],
) -> tuple[dict[str, str], dict[str, Any]]:
    force_load = build_baseline_force_load_rlib(
        raw_dir=raw_dir,
        implementation=implementation,
        toolchain=toolchain,
        jobs=jobs,
        timeout=timeout,
    )
    wrapper = ensure_baseline_force_wrapper(
        raw_dir / "tools" / "unialloc-baseline-force-load-wrapper"
    )
    env = dict(base)
    env.update(
        {
            "RUSTC_WRAPPER": str(wrapper),
            "UNIALLOC_RUSTC_TARGET_CRATES": ",".join(target_crates),
            "UNIALLOC_FORCE_LOAD_RLIB": str(force_load["rlib"]),
            "UNIALLOC_FORCE_LOAD_DEPENDENCY_DIR": str(force_load["dependency_dir"]),
        }
    )
    return env, {
        "force_load": force_load,
        "force_load_wrapper": str(wrapper),
        "force_load_wrapper_sha256": sha256_file(wrapper),
    }


def build_snapshot_typeiso_force_load_rlib(
    variant: str,
    *,
    raw_dir: pathlib.Path,
    implementation: ImplementationSnapshot,
    toolchain: str,
    jobs: int,
    timeout: int,
) -> dict[str, Any]:
    if variant not in {"typed_plain", "typeiso_perf"}:
        raise CampaignError(f"unsupported Type Isolation force-load variant: {variant}")
    verify_implementation_snapshot(implementation)
    features, default_features_enabled = unialloc_configuration_for_primary_variant(
        variant
    )
    feature_key = "-".join(features) if features else "default"
    build_root = raw_dir / "force-load" / feature_key
    target_dir = build_root / "target"
    record_path = build_root / "build.json"
    if record_path.is_file():
        try:
            cached = json.loads(record_path.read_text(encoding="utf-8"))
            rlib = pathlib.Path(str(cached.get("rlib", "")))
            if (
                cached.get("success") is True
                and cached.get("toolchain") == toolchain
                and cached.get("features") == list(features)
                and cached.get("panic_strategy") == "unwind"
                and cached.get("implementation_revision") == implementation.revision
                and cached.get("implementation_sha256") == implementation.sha256
                and rlib.is_file()
                and cached.get("rlib_sha256") == sha256_file(rlib)
            ):
                return cached
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    shutil.rmtree(build_root, ignore_errors=True)
    build_root.mkdir(parents=True)
    temp_dir = raw_dir / "tmp" / "typeiso-force-load"
    temp_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "CARGO_INCREMENTAL": "0",
            "CARGO_PROFILE_RELEASE_PANIC": "unwind",
            "TMPDIR": str(temp_dir.resolve()),
        }
    )
    command: list[str | os.PathLike[str]] = [
        "cargo",
        f"+{toolchain}",
        "build",
        "--release",
        "--locked",
        "--manifest-path",
        implementation.path / "unialloc" / "Cargo.toml",
        "--lib",
        "--target-dir",
        target_dir,
        "--jobs",
        str(jobs),
    ]
    if features:
        command.extend(["--features", ",".join(features)])
    result = run_command(command, cwd=implementation.path, env=env, timeout=timeout)
    artifacts = command_artifacts(result, build_root, "build")
    candidates = sorted((target_dir / "release" / "deps").glob("libunialloc-*.rlib"))
    success = (
        result["exit_code"] == 0 and not result["timed_out"] and len(candidates) == 1
    )
    record: dict[str, Any] = {
        "mode": "selected-crate-rustc-force-extern",
        "success": success,
        "toolchain": toolchain,
        "features": list(features),
        "default_features_enabled": default_features_enabled,
        "default_features": ["pthread_dtor", "rseq"],
        "panic_strategy": "unwind",
        "implementation_revision": implementation.revision,
        "implementation_sha256": implementation.sha256,
        "implementation_snapshot": str(implementation.path),
        "implementation_manifest_path": str(implementation.manifest_path),
        "candidate_count": len(candidates),
        **artifacts,
    }
    if len(candidates) == 1:
        rlib = candidates[0].resolve()
        record.update(
            {
                "rlib": str(rlib),
                "rlib_sha256": sha256_file(rlib),
                "rlib_size_bytes": rlib.stat().st_size,
                "dependency_dir": str(rlib.parent),
                "force_extern": f"force:unialloc={rlib}",
            }
        )
    write_json(record_path, record)
    if not success:
        detail = (
            f"expected one libunialloc rlib, found {len(candidates)}"
            if result["exit_code"] == 0 and not result["timed_out"]
            else result["stderr_text"][-8000:]
        )
        raise CampaignError(f"pinned force-load UniAlloc rlib build failed: {detail}")
    return record


def typeiso_build_environment(
    variant: str,
    *,
    base: dict[str, str],
    raw_dir: pathlib.Path,
    implementation: ImplementationSnapshot,
    wrapper: pathlib.Path,
    sysroot: pathlib.Path,
    toolchain: str,
    jobs: int,
    timeout: int,
    target_crates: Sequence[str],
) -> tuple[dict[str, str], dict[str, Any] | None]:
    if variant == "unialloc":
        return dict(base), None
    force_load = build_snapshot_typeiso_force_load_rlib(
        variant,
        raw_dir=raw_dir,
        implementation=implementation,
        toolchain=toolchain,
        jobs=jobs,
        timeout=timeout,
    )
    force_load_wrapper = ensure_transitive_typeiso_force_wrapper(
        raw_dir / "tools" / "unialloc-force-load-wrapper"
    )
    audit_dir = raw_dir / "audits" / target_crates[0] / variant
    pass_log_dir = raw_dir / "pass-logs" / target_crates[0] / variant
    shutil.rmtree(audit_dir, ignore_errors=True)
    shutil.rmtree(pass_log_dir, ignore_errors=True)
    audit_dir.mkdir(parents=True)
    pass_log_dir.mkdir(parents=True)
    env = matrix.typeiso_environment(
        base,
        wrapper=force_load_wrapper,
        audit_dir=audit_dir,
        pass_log_dir=pass_log_dir,
        sysroot=sysroot,
        target_crates=target_crates,
        policy_flags=0 if variant == "typed_plain" else 1,
    )
    env = matrix.force_load_environment(
        env, driver_wrapper=wrapper, force_load=force_load
    )
    return env, {
        "force_load": force_load,
        "force_load_wrapper": str(force_load_wrapper),
        "audit_dir": str(audit_dir),
        "pass_log_dir": str(pass_log_dir),
    }


def command_artifacts(
    result: dict[str, Any], artifact_dir: pathlib.Path, label: str
) -> dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = artifact_dir / f"{label}.stdout"
    stderr_path = artifact_dir / f"{label}.stderr"
    stdout_path.write_bytes(result["stdout"])
    stderr_path.write_bytes(result["stderr"])
    return {
        "command": result["command"],
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "wall_seconds": result["wall_seconds"],
        "stdout_path": str(stdout_path.resolve()),
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_path": str(stderr_path.resolve()),
        "stderr_sha256": sha256_file(stderr_path),
    }


def inspect_allocator_activation(binary: pathlib.Path) -> dict[str, Any]:
    result = run_command(
        ["nm", "-C", binary], cwd=binary.parent, env=os.environ.copy(), timeout=120
    )
    text = result["stdout_text"]
    marker = "UNIALLOC_REDB_ACTIX_ALLOCATOR_MARKER" in text
    implementation = "unialloc::" in text or "__unialloc_" in text
    return {
        "passed": result["exit_code"] == 0 and marker and implementation,
        "binary": str(binary.resolve()),
        "binary_sha256": sha256_file(binary),
        "nm_exit_code": result["exit_code"],
        "marker_present": marker,
        "implementation_symbol_present": implementation,
        "nm_sha256": sha256_bytes(result["stdout"]),
    }


def redb_manifest(
    *, variant: str, checkout: pathlib.Path, unialloc_path: pathlib.Path
) -> str:
    dependencies = [f"redb = {{ path = {json.dumps(str(checkout.resolve()))} }}"]
    dependency = cargo_unialloc_dependency(variant, unialloc_path)
    if dependency:
        dependencies.append(dependency)
    patch = ""
    if dependency:
        spin = matrix.find_cached_spin()
        patch = f"\n[patch.crates-io]\nspin = {{ path = {json.dumps(str(spin))} }}\n"
    return (
        '[package]\nname = "unialloc-redb-actix-runner"\nversion = "0.1.0"\n'
        'edition = "2024"\nrust-version = "1.89"\npublish = false\n\n'
        "[dependencies]\n" + "\n".join(dependencies) + "\n\n[workspace]\n" + patch
    )


def build_redb_variant(
    spec: TargetSpec,
    variant: str,
    *,
    checkout: pathlib.Path,
    raw_dir: pathlib.Path,
    implementation: ImplementationSnapshot,
    wrapper: pathlib.Path,
    sysroot: pathlib.Path,
    toolchain: str,
    jobs: int,
    timeout: int,
) -> dict[str, Any]:
    worktree = raw_dir / "build-work" / spec.id / variant
    shutil.rmtree(worktree, ignore_errors=True)
    (worktree / "src").mkdir(parents=True)
    manifest_text = redb_manifest(
        variant=variant,
        checkout=checkout,
        unialloc_path=implementation.path / "unialloc",
    )
    (worktree / "Cargo.toml").write_text(manifest_text, encoding="utf-8")
    (worktree / "src" / "main.rs").write_text(REDB_RUNNER_SOURCE, encoding="utf-8")
    target_dir = raw_dir / "targets" / spec.id / variant
    shutil.rmtree(target_dir, ignore_errors=True)
    temp_dir = raw_dir / "tmp" / spec.id / variant
    temp_dir.mkdir(parents=True, exist_ok=True)
    base = os.environ.copy()
    base.update(
        {
            "CARGO_TARGET_DIR": str(target_dir.resolve()),
            "CARGO_INCREMENTAL": "0",
            "TMPDIR": str(temp_dir.resolve()),
        }
    )
    env, typeiso = typeiso_build_environment(
        variant,
        base=base,
        raw_dir=raw_dir,
        implementation=implementation,
        wrapper=wrapper,
        sysroot=sysroot,
        toolchain=toolchain,
        jobs=jobs,
        timeout=timeout,
        target_crates=spec.target_crates,
    )
    command = [
        "cargo",
        f"+{toolchain}",
        "build",
        "--release",
        "--jobs",
        str(jobs),
    ]
    result = run_command(command, cwd=worktree, env=env, timeout=timeout)
    artifacts = command_artifacts(
        result, raw_dir / "builds" / spec.id / variant, "build"
    )
    binary = target_dir / "release" / "unialloc-redb-actix-runner"
    if result["exit_code"] != 0 or result["timed_out"] or not binary.is_file():
        raise CampaignError(
            f"redb {variant} build failed:\n{result['stderr_text'][-8000:]}"
        )
    audit = None
    if typeiso:
        audit_dir = pathlib.Path(typeiso["audit_dir"])
        audit = matrix.summarize_audits(audit_dir)
        try:
            matrix.validate_typeiso_audits(audit, spec.target_crates)
        except matrix.MatrixError as error:
            raise CampaignError(str(error)) from error
        write_json(raw_dir / "builds" / spec.id / variant / "audit.json", audit)
    activation = inspect_allocator_activation(binary)
    write_json(raw_dir / "builds" / spec.id / variant / "activation.json", activation)
    record = {
        "target_id": spec.id,
        "variant": variant,
        "source_commit": spec.source_commit,
        "toolchain": toolchain,
        "workload_source_sha256": sha256_bytes(REDB_RUNNER_SOURCE.encode()),
        "manifest_sha256": sha256_file(worktree / "Cargo.toml"),
        "cargo_lock_sha256": sha256_file(worktree / "Cargo.lock"),
        "implementation_revision": implementation.revision,
        "implementation_sha256": implementation.sha256,
        "implementation_snapshot": str(implementation.path),
        "implementation_manifest_path": str(implementation.manifest_path),
        "unialloc_dependency_path": (
            str((implementation.path / "unialloc").resolve())
            if variant == "unialloc"
            else None
        ),
        "unialloc_implementation_sha256": implementation.sha256,
        "stats_enabled": False,
        "actual_mir_rewrite": typeiso is not None,
        "lowering_policy_flags": (
            None if variant == "unialloc" else (0 if variant == "typed_plain" else 1)
        ),
        "binary": str(binary.resolve()),
        "binary_sha256": sha256_file(binary),
        "activation": activation,
        "audit": audit,
        "typeiso": typeiso,
        **artifacts,
    }
    path = raw_dir / "builds" / spec.id / variant / "build.json"
    write_json(path, record)
    record["build_path"] = str(path.resolve())
    return record


def patch_actix_get_body_benchmark(worktree: pathlib.Path) -> dict[str, Any]:
    source = pathlib.Path("actix-web/benches/server.rs")
    path = worktree / source
    original = path.read_bytes()
    text = original.decode("utf-8")
    occurrences = text.count(ACTIX_GET_BODY_BURST_SOURCE)
    if occurrences != 1:
        raise CampaignError(
            "Actix get_body_async_burst source drift: expected one unbounded "
            f"request block, found {occurrences}"
        )
    path.write_text(
        text.replace(
            ACTIX_GET_BODY_BURST_SOURCE,
            ACTIX_GET_BODY_BOUNDED_SOURCE,
            1,
        ),
        encoding="utf-8",
    )
    return {
        "path": source.as_posix(),
        "patch": "bounded_get_body_async_burst",
        "max_in_flight_requests": 8,
        "upstream_sha256": sha256_bytes(original),
        "patched_sha256": sha256_file(path),
    }


def append_actix_instrumentation(
    worktree: pathlib.Path,
    source_patches: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    patches_by_path = {str(row["path"]): row for row in source_patches}
    rows: list[dict[str, Any]] = []
    for source in sorted({value[3] for value in ACTIX_BENCHES.values()}):
        path = worktree / source
        original = path.read_bytes()
        text = original.decode("utf-8")
        if "UNIALLOC_REDB_ACTIX_ALLOCATOR_MARKER" in text:
            raise CampaignError(f"Actix instrumentation already exists: {source}")
        patch = patches_by_path.get(source.as_posix())
        if patch and patch.get("patched_sha256") != sha256_bytes(original):
            raise CampaignError(f"Actix patch audit mismatch: {source}")
        path.write_text(
            text.rstrip() + "\n" + allocator_marker_source(), encoding="utf-8"
        )
        row = (
            dict(patch)
            if patch
            else {
                "path": source.as_posix(),
                "upstream_sha256": sha256_bytes(original),
            }
        )
        row["instrumented_sha256"] = sha256_file(path)
        rows.append(row)
    unknown_patches = set(patches_by_path).difference(row["path"] for row in rows)
    if unknown_patches:
        raise CampaignError(f"Actix patch audit has unknown sources: {unknown_patches}")
    return rows


def executables_from_cargo_json(text: str, bench_name: str) -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or value.get("reason") != "compiler-artifact":
            continue
        target = value.get("target")
        executable = value.get("executable")
        if isinstance(target, dict) and target.get("name") == bench_name and executable:
            paths.append(pathlib.Path(str(executable)).resolve())
    return list(dict.fromkeys(paths))


def build_actix_variant(
    spec: TargetSpec,
    variant: str,
    *,
    checkout: pathlib.Path,
    raw_dir: pathlib.Path,
    implementation: ImplementationSnapshot,
    wrapper: pathlib.Path,
    sysroot: pathlib.Path,
    toolchain: str,
    jobs: int,
    timeout: int,
) -> dict[str, Any]:
    worktree = raw_dir / "build-work" / spec.id / variant
    matrix.copy_checkout(checkout, worktree)
    source_patches = [patch_actix_get_body_benchmark(worktree)]
    source_audit = append_actix_instrumentation(worktree, source_patches)
    target_dir = raw_dir / "targets" / spec.id / variant
    shutil.rmtree(target_dir, ignore_errors=True)
    temp_dir = raw_dir / "tmp" / spec.id / variant
    temp_dir.mkdir(parents=True, exist_ok=True)
    base = os.environ.copy()
    base.update(
        {
            "CARGO_TARGET_DIR": str(target_dir.resolve()),
            "CARGO_INCREMENTAL": "0",
            "TMPDIR": str(temp_dir.resolve()),
        }
    )
    baseline_force = None
    if variant == "unialloc":
        env, baseline_force = baseline_force_environment(
            base,
            raw_dir=raw_dir,
            implementation=implementation,
            toolchain=toolchain,
            jobs=jobs,
            timeout=timeout,
            target_crates=spec.target_crates,
        )
        typeiso = None
    else:
        env, typeiso = typeiso_build_environment(
            variant,
            base=base,
            raw_dir=raw_dir,
            implementation=implementation,
            wrapper=wrapper,
            sysroot=sysroot,
            toolchain=toolchain,
            jobs=jobs,
            timeout=timeout,
            target_crates=spec.target_crates,
        )
    selections = sorted({value[:2] for value in ACTIX_BENCHES.values()})
    executables: dict[str, str] = {}
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
        result = run_command(command, cwd=worktree, env=env, timeout=timeout)
        label = f"{package}-{bench}"
        command_record = command_artifacts(
            result, raw_dir / "builds" / spec.id / variant, label
        )
        commands.append(command_record)
        candidates = executables_from_cargo_json(result["stdout_text"], bench)
        if result["exit_code"] != 0 or result["timed_out"] or len(candidates) != 1:
            raise CampaignError(
                f"Actix {variant} {package}/{bench} build failed or emitted "
                f"{len(candidates)} executables:\n{result['stderr_text'][-8000:]}"
            )
        executables[bench] = str(candidates[0])
    audit = None
    if typeiso:
        audit_dir = pathlib.Path(typeiso["audit_dir"])
        audit = matrix.summarize_audits(audit_dir)
        try:
            matrix.validate_typeiso_audits(audit, spec.target_crates)
        except matrix.MatrixError as error:
            raise CampaignError(str(error)) from error
        write_json(raw_dir / "builds" / spec.id / variant / "audit.json", audit)
    activation = {
        bench: inspect_allocator_activation(pathlib.Path(binary))
        for bench, binary in executables.items()
    }
    record = {
        "target_id": spec.id,
        "variant": variant,
        "source_commit": spec.source_commit,
        "toolchain": toolchain,
        "source_audit": source_audit,
        "source_audit_sha256": sha256_bytes(
            json.dumps(source_audit, sort_keys=True).encode()
        ),
        "cargo_lock_sha256": sha256_file(worktree / "Cargo.lock"),
        "implementation_revision": implementation.revision,
        "implementation_sha256": implementation.sha256,
        "implementation_snapshot": str(implementation.path),
        "implementation_manifest_path": str(implementation.manifest_path),
        "unialloc_implementation_sha256": implementation.sha256,
        "stats_enabled": False,
        "actual_mir_rewrite": typeiso is not None,
        "lowering_policy_flags": (
            None if variant == "unialloc" else (0 if variant == "typed_plain" else 1)
        ),
        "executables": executables,
        "activation": activation,
        "audit": audit,
        "typeiso": typeiso,
        "baseline_force_load": baseline_force,
        "commands": commands,
    }
    path = raw_dir / "builds" / spec.id / variant / "build.json"
    write_json(path, record)
    record["build_path"] = str(path.resolve())
    return record


def build_target_variants(
    spec: TargetSpec,
    *,
    checkout: pathlib.Path,
    raw_dir: pathlib.Path,
    implementation: ImplementationSnapshot,
    wrapper: pathlib.Path,
    sysroot: pathlib.Path,
    toolchain: str,
    jobs: int,
    timeout: int,
) -> dict[str, dict[str, Any]]:
    builder = build_redb_variant if spec.id == "redb" else build_actix_variant
    verify_implementation_snapshot(implementation)
    builds: dict[str, dict[str, Any]] = {}
    for variant in VARIANTS:
        verify_implementation_snapshot(implementation)
        builds[variant] = builder(
            spec,
            variant,
            checkout=checkout,
            raw_dir=raw_dir,
            implementation=implementation,
            wrapper=wrapper,
            sysroot=sysroot,
            toolchain=toolchain,
            jobs=jobs,
            timeout=timeout,
        )
    verify_implementation_snapshot(implementation)
    observed = {
        (
            str(build.get("implementation_revision")),
            str(build.get("implementation_sha256")),
            str(build.get("implementation_snapshot")),
        )
        for build in builds.values()
    }
    expected = {
        (
            implementation.revision,
            implementation.sha256,
            str(implementation.path),
        )
    }
    if observed != expected:
        raise CampaignError(
            f"variant implementation identities differ: {sorted(observed)}"
        )
    for build in builds.values():
        build["frozen_implementation_sha256"] = implementation.sha256
        build_path = pathlib.Path(build["build_path"])
        persisted = json.loads(build_path.read_text(encoding="utf-8"))
        persisted["frozen_implementation_sha256"] = implementation.sha256
        write_json(build_path, persisted)
    return builds


def load_exact_builds(
    spec: TargetSpec,
    *,
    raw_dir: pathlib.Path,
    implementation: ImplementationSnapshot,
) -> dict[str, dict[str, Any]]:
    """Load only byte-verified build artifacts from the pinned snapshot."""

    verify_implementation_snapshot(implementation)
    builds: dict[str, dict[str, Any]] = {}
    for variant in VARIANTS:
        build_path = raw_dir / "builds" / spec.id / variant / "build.json"
        try:
            build = json.loads(build_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CampaignError(
                f"missing reusable build record: {build_path}"
            ) from error
        identity = (
            build.get("implementation_revision"),
            build.get("implementation_sha256"),
            build.get("frozen_implementation_sha256"),
            build.get("implementation_snapshot"),
        )
        expected = (
            implementation.revision,
            implementation.sha256,
            implementation.sha256,
            str(implementation.path),
        )
        if identity != expected:
            raise CampaignError(
                f"reusable {spec.id}/{variant} implementation mismatch: {identity}"
            )
        if build.get("stats_enabled") is not False:
            raise CampaignError(f"reusable {spec.id}/{variant} enables stats")
        if spec.id == "redb":
            binary = pathlib.Path(str(build.get("binary", "")))
            if not binary.is_file() or build.get("binary_sha256") != sha256_file(
                binary
            ):
                raise CampaignError(f"reusable redb/{variant} binary mismatch")
            worktree = raw_dir / "build-work" / spec.id / variant
            if (
                build.get("workload_source_sha256")
                != sha256_file(worktree / "src" / "main.rs")
                or build.get("manifest_sha256") != sha256_file(worktree / "Cargo.toml")
                or build.get("cargo_lock_sha256")
                != sha256_file(worktree / "Cargo.lock")
            ):
                raise CampaignError(f"reusable redb/{variant} source mismatch")
        else:
            executables = build.get("executables") or {}
            activation = build.get("activation") or {}
            if set(executables) != set(activation) or not executables:
                raise CampaignError(f"reusable Actix/{variant} executable set mismatch")
            for bench, raw_binary in executables.items():
                binary = pathlib.Path(str(raw_binary))
                evidence = activation.get(bench) or {}
                if (
                    not binary.is_file()
                    or evidence.get("binary") != str(binary)
                    or evidence.get("binary_sha256") != sha256_file(binary)
                    or evidence.get("passed") is not True
                ):
                    raise CampaignError(
                        f"reusable Actix/{variant}/{bench} binary mismatch"
                    )
            worktree = raw_dir / "build-work" / spec.id / variant
            if build.get("cargo_lock_sha256") != sha256_file(worktree / "Cargo.lock"):
                raise CampaignError(f"reusable Actix/{variant} Cargo.lock mismatch")
            for source in build.get("source_audit") or []:
                source_path = worktree / str(source.get("path", ""))
                if not source_path.is_file() or source.get(
                    "instrumented_sha256"
                ) != sha256_file(source_path):
                    raise CampaignError(
                        f"reusable Actix/{variant} source instrumentation mismatch"
                    )
        if variant in {"typed_plain", "typeiso_perf"}:
            audit_path = build_path.parent / "audit.json"
            try:
                persisted_audit = json.loads(audit_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise CampaignError(
                    f"missing reusable audit record: {audit_path}"
                ) from error
            if persisted_audit != build.get("audit"):
                raise CampaignError(f"reusable {spec.id}/{variant} audit mismatch")
            force_load = (build.get("typeiso") or {}).get("force_load") or {}
        else:
            force_load = (build.get("baseline_force_load") or {}).get("force_load", {})
        if force_load:
            rlib = pathlib.Path(str(force_load.get("rlib", "")))
            if (
                force_load.get("implementation_revision") != implementation.revision
                or force_load.get("implementation_sha256") != implementation.sha256
                or not rlib.is_file()
                or force_load.get("rlib_sha256") != sha256_file(rlib)
            ):
                raise CampaignError(
                    f"reusable {spec.id}/{variant} force-load artifact mismatch"
                )
        build["build_path"] = str(build_path.resolve())
        builds[variant] = build
    gates = build_gates(spec, builds)
    failed = [
        gate
        for gate, passed in gates.items()
        if gate not in {"correctness", "compiler_route_equivalent"} and not passed
    ]
    if failed:
        raise CampaignError("reusable build gates failed: " + ",".join(failed))
    return builds


def build_gates(spec: TargetSpec, builds: dict[str, dict[str, Any]]) -> dict[str, bool]:
    activation = True
    provenance = True
    source_audit = True
    stats_disabled = True
    implementation_digests: set[str] = set()
    implementation_revisions: set[str] = set()
    implementation_snapshots: set[str] = set()
    for variant, build in builds.items():
        implementation_digests.add(str(build.get("frozen_implementation_sha256", "")))
        implementation_revisions.add(str(build.get("implementation_revision", "")))
        implementation_snapshots.add(str(build.get("implementation_snapshot", "")))
        stats_disabled &= build.get("stats_enabled") is False
        if spec.id == "redb":
            activation &= build.get("activation", {}).get("passed") is True
        else:
            activation &= all(
                row.get("passed") is True
                for row in build.get("activation", {}).values()
            )
        if variant in {"typed_plain", "typeiso_perf"}:
            audit = build.get("audit") or {}
            provenance &= (
                build.get("actual_mir_rewrite") is True
                and int(audit.get("audit_file_count", 0)) > 0
                and int(audit.get("semantic_rewrites_applied", 0)) > 0
                and set(spec.target_crates).issubset(set(audit.get("crate_names", [])))
            )
            typeiso = build.get("typeiso") or {}
            source_audit &= pathlib.Path(str(typeiso.get("audit_dir", ""))).is_dir()
            force_load = typeiso.get("force_load") or {}
            source_audit &= (
                force_load.get("implementation_revision")
                == build.get("implementation_revision")
                and force_load.get("implementation_sha256")
                == build.get("frozen_implementation_sha256")
                and force_load.get("implementation_snapshot")
                == build.get("implementation_snapshot")
            )
        elif spec.id == "actix_web":
            force_load = (build.get("baseline_force_load") or {}).get("force_load", {})
            source_audit &= (
                force_load.get("implementation_revision")
                == build.get("implementation_revision")
                and force_load.get("implementation_sha256")
                == build.get("frozen_implementation_sha256")
                and force_load.get("implementation_snapshot")
                == build.get("implementation_snapshot")
            )
        elif spec.id == "redb":
            source_audit &= build.get("unialloc_dependency_path") == str(
                pathlib.Path(str(build.get("implementation_snapshot", ""))) / "unialloc"
            )
    consistent_snapshot = (
        len(implementation_digests) == 1
        and "" not in implementation_digests
        and len(implementation_revisions) == 1
        and "" not in implementation_revisions
        and len(implementation_snapshots) == 1
        and "" not in implementation_snapshots
        and all(
            pathlib.Path(value).is_dir() for value in implementation_snapshots if value
        )
    )
    return {
        "build_success": build_records_succeeded(spec, builds),
        "correctness": False,
        "allocator_activation": activation,
        "actual_mir_provenance": provenance,
        "stats_disabled": stats_disabled,
        # Measurement equivalence is decided per harness from the configured
        # paired typed_plain/unialloc execution-cost ratios after collection.
        "compiler_route_equivalent": False,
        "source_audit_retained": source_audit and consistent_snapshot,
    }


def build_records_succeeded(
    spec: TargetSpec, builds: dict[str, dict[str, Any]]
) -> bool:
    if set(builds) != set(VARIANTS):
        return False
    for build in builds.values():
        if spec.id == "redb":
            if build.get("exit_code") != 0 or build.get("timed_out") is not False:
                return False
            if not pathlib.Path(str(build.get("binary", ""))).is_file():
                return False
        else:
            commands = build.get("commands")
            if not isinstance(commands, list) or len(commands) != len(
                {value[:2] for value in ACTIX_BENCHES.values()}
            ):
                return False
            if any(
                command.get("exit_code") != 0 or command.get("timed_out") is not False
                for command in commands
            ):
                return False
            if not all(
                pathlib.Path(str(binary)).is_file()
                for binary in (build.get("executables") or {}).values()
            ):
                return False
    return True


def compiler_route_equivalence(
    measurements: Sequence[dict[str, Any]],
    *,
    measured_rounds: int = ROUNDS,
) -> tuple[bool, float]:
    by_pair = {
        (int(row["round"]), str(row["variant"])): float(row["performance"])
        for row in measurements
    }
    ratios: list[float] = []
    for round_index in range(1, measured_rounds + 1):
        try:
            reference = by_pair[(round_index, "unialloc")]
            subject = by_pair[(round_index, "typed_plain")]
        except KeyError as error:
            raise CampaignError(
                "compiler-route gate requires "
                f"{measured_rounds} complete typed_plain/unialloc pairs"
            ) from error
        if reference <= 0 or subject <= 0:
            raise CampaignError("compiler-route gate received non-positive timing")
        ratios.append(subject / reference)
    median_ratio = statistics.median(ratios)
    return 0.85 <= median_ratio <= 1.15, median_ratio


def measured_command(
    spec: TargetSpec,
    harness: HarnessSpec,
    build: dict[str, Any],
    run_dir: pathlib.Path,
) -> tuple[list[str], pathlib.Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    if spec.id == "redb":
        return [str(build["binary"]), harness.id, str(run_dir / "database")], run_dir
    _package, bench, selector = actix_benchmark_selection(harness.id)
    binary = build["executables"][bench]
    return (
        [
            str(binary),
            "--bench",
            selector,
            "--warm-up-time",
            "0.1",
            "--measurement-time",
            "0.3",
            "--sample-size",
            "10",
            "--noplot",
        ],
        run_dir,
    )


def persist_measurement(
    result: dict[str, Any],
    *,
    path: pathlib.Path,
    spec: TargetSpec,
    harness: HarnessSpec,
    variant: str,
    round_index: int,
    build: dict[str, Any],
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = path.with_suffix(".stdout")
    stderr_path = path.with_suffix(".stderr")
    stdout_path.write_bytes(result["stdout"])
    stderr_path.write_bytes(result["stderr"])
    stdout = result["stdout"].decode("utf-8", errors="replace")
    stderr = result["stderr"].decode("utf-8", errors="replace")
    failed_requests = (
        actix_failed_request_count(stdout + "\n" + stderr)
        if spec.id == "actix_web"
        else 0
    )
    if failed_requests:
        raise HarnessCorrectnessError(
            harness.id,
            f"{spec.id}/{harness.id}/{variant} correctness failure: "
            f"failed {failed_requests} requests (might be bench timeout)",
        )
    if result["exit_code"] != 0 or result["timed_out"]:
        raise CampaignError(
            f"{spec.id}/{harness.id}/{variant} failed: {stderr[-4000:]}"
        )
    performance = (
        parse_redb_result(stdout, harness.id)
        if spec.id == "redb"
        else parse_criterion_estimate_seconds(stdout + "\n" + stderr)
    )
    peak_rss_mib = float(result["peak_rss_kib"]) / 1024.0
    if not math.isfinite(peak_rss_mib) or peak_rss_mib <= 0:
        raise CampaignError(f"invalid peak RSS: {peak_rss_mib}")
    record = {
        "target_id": spec.id,
        "harness_id": harness.id,
        "variant": variant,
        "phase": "warmup" if round_index == 0 else "measurement",
        "round": round_index,
        "performance": performance,
        "performance_unit": "seconds",
        "peak_rss_mib": peak_rss_mib,
        "command": result["command"],
        "measured_command": result["measured_command"],
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "process_wall_seconds": result["wall_seconds"],
        "gnu_time_exit_status": result["gnu_time_exit_status"],
        "stdout_path": str(stdout_path.resolve()),
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_path": str(stderr_path.resolve()),
        "stderr_sha256": sha256_file(stderr_path),
        "build_path": build["build_path"],
        "audit_path": (
            str((pathlib.Path(build["build_path"]).parent / "audit.json").resolve())
            if build.get("audit") is not None
            else build["build_path"]
        ),
        "environment": environment_record(),
    }
    write_json(path, record)
    return {
        "round": round_index,
        "variant": variant,
        "performance": performance,
        "peak_rss_mib": peak_rss_mib,
        "raw_path": str(path.resolve()),
        "build_path": record["build_path"],
        "audit_path": record["audit_path"],
    }


def execute_one(
    spec: TargetSpec,
    harness: HarnessSpec,
    variant: str,
    build: dict[str, Any],
    *,
    raw_dir: pathlib.Path,
    round_index: int,
    timeout: int,
    warmup: bool,
) -> dict[str, Any]:
    phase = "warmup" if warmup else f"round-{round_index:02d}"
    run_root = raw_dir / "runs" / spec.id / harness.id / phase / variant
    command, cwd = measured_command(spec, harness, build, run_root / "work")
    env = matrix.runtime_environment(os.environ.copy(), disable_glibc_rseq=True)
    temp_dir = raw_dir / "tmp" / "runs" / spec.id / harness.id / phase / variant
    temp_dir.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(temp_dir.resolve())
    result = matrix.run_measured(
        command,
        cwd=cwd,
        env=env,
        time_binary=pathlib.Path("/usr/bin/time"),
        rss_path=run_root / "gnu-time.txt",
        timeout=timeout,
    )
    output_path = run_root / "measurement.json"
    return persist_measurement(
        result,
        path=output_path,
        spec=spec,
        harness=harness,
        variant=variant,
        round_index=round_index,
        build=build,
    )


def load_retained_warmups(
    spec: TargetSpec, raw_dir: pathlib.Path
) -> dict[str, dict[str, list[dict[str, str]]]]:
    retained: dict[str, dict[str, list[dict[str, str]]]] = {}
    for harness in spec.harnesses:
        rows: dict[str, list[dict[str, str]]] = {}
        for variant in VARIANTS:
            path = (
                raw_dir
                / "runs"
                / spec.id
                / harness.id
                / "warmup"
                / variant
                / "measurement.json"
            )
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise CampaignError(f"missing retained warmup: {path}") from error
            if (
                record.get("target_id") != spec.id
                or record.get("harness_id") != harness.id
                or record.get("variant") != variant
                or record.get("phase") != "warmup"
                or record.get("round") != 0
                or record.get("exit_code") != 0
                or record.get("timed_out") is not False
            ):
                raise CampaignError(f"retained warmup identity mismatch: {path}")
            for artifact, digest_field in (
                ("stdout_path", "stdout_sha256"),
                ("stderr_path", "stderr_sha256"),
            ):
                artifact_path = pathlib.Path(str(record.get(artifact, "")))
                if not artifact_path.is_file() or record.get(
                    digest_field
                ) != sha256_file(artifact_path):
                    raise CampaignError(f"retained warmup artifact mismatch: {path}")
            for field in ("performance", "peak_rss_mib"):
                value = float(record.get(field, 0.0))
                if not math.isfinite(value) or value <= 0:
                    raise CampaignError(f"retained warmup has invalid {field}: {path}")
            rows[variant] = [
                {
                    "record_path": str(path.resolve()),
                    "sha256": sha256_file(path),
                }
            ]
        retained[harness.id] = rows
    return retained


def empty_target_result(spec: TargetSpec, raw_dir: pathlib.Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "target_id": spec.id,
        "source_ref": spec.source_ref,
        "source_commit": spec.source_commit,
        "status": "incomplete",
        "core_eligible": False,
        "raw_root": str(raw_dir.resolve()),
        "blockers": [],
        "harnesses": [
            {
                "id": harness.id,
                "metric_direction": harness.metric_direction,
                "gates": {gate: False for gate in REQUIRED_GATES},
                "warmup_evidence": {variant: [] for variant in VARIANTS},
                "measurements": [],
            }
            for harness in spec.harnesses
        ],
    }


def mark_target_ineligible(
    record: dict[str, Any],
    blocker: str,
    *,
    retain_measurements: bool = False,
    correctness_harness_id: str | None = None,
) -> dict[str, Any]:
    record["status"] = "ineligible"
    record["core_eligible"] = False
    record.setdefault("blockers", []).append(blocker)
    if not retain_measurements:
        for harness in record["harnesses"]:
            harness["gates"] = {gate: False for gate in REQUIRED_GATES}
            harness["warmup_evidence"] = {variant: [] for variant in VARIANTS}
            harness["measurements"] = []
    if correctness_harness_id is not None:
        for harness in record["harnesses"]:
            if harness.get("id") == correctness_harness_id:
                harness["gates"]["correctness"] = False
                break
    return record


def validate_target_result(
    record: dict[str, Any],
    *,
    allow_attribution_limits: bool = False,
    measured_rounds: int = ROUNDS,
) -> None:
    if record.get("schema_version") != 1:
        raise CampaignError("result schema version must be 1")
    expected_pairs = {
        (round_index, variant)
        for round_index in range(1, measured_rounds + 1)
        for variant in VARIANTS
    }
    for harness in record.get("harnesses", []):
        gates = harness.get("gates", {})
        required_gates = (
            CORE_REQUIRED_GATES if allow_attribution_limits else REQUIRED_GATES
        )
        missing_gates = [gate for gate in required_gates if gates.get(gate) is not True]
        if missing_gates:
            raise CampaignError(
                f"{harness.get('id')} has failing gates: {','.join(missing_gates)}"
            )
        warmup_evidence = harness.get("warmup_evidence")
        if not isinstance(warmup_evidence, dict) or set(warmup_evidence) != set(
            VARIANTS
        ):
            raise CampaignError(
                f"{harness.get('id')} warmup evidence variants do not match"
            )
        for variant in VARIANTS:
            evidence_rows = warmup_evidence[variant]
            if not isinstance(evidence_rows, list) or not evidence_rows:
                raise CampaignError(
                    f"{harness.get('id')} has no {variant} warmup evidence"
                )
            for evidence in evidence_rows:
                record_path = pathlib.Path(str(evidence.get("record_path", "")))
                if not record_path.is_file() or evidence.get("sha256") != sha256_file(
                    record_path
                ):
                    raise CampaignError(
                        f"{harness.get('id')} has invalid {variant} warmup artifact"
                    )
                try:
                    warmup = json.loads(record_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise CampaignError(
                        f"{harness.get('id')} has malformed {variant} warmup"
                    ) from error
                if (
                    warmup.get("target_id") != record.get("target_id")
                    or warmup.get("harness_id") != harness.get("id")
                    or warmup.get("variant") != variant
                    or warmup.get("phase") != "warmup"
                    or warmup.get("round") != 0
                ):
                    raise CampaignError(
                        f"{harness.get('id')} has mismatched {variant} warmup identity"
                    )
                for field in ("performance", "peak_rss_mib"):
                    value = float(warmup.get(field, 0.0))
                    if not math.isfinite(value) or value <= 0:
                        raise CampaignError(
                            f"{harness.get('id')} has invalid warmup {field}: {value}"
                        )
        rows = harness.get("measurements", [])
        observed = {(row.get("round"), row.get("variant")) for row in rows}
        if observed != expected_pairs or len(rows) != len(expected_pairs):
            raise CampaignError(
                f"{harness.get('id')} does not contain "
                f"{measured_rounds} complete paired rounds"
            )
        for row in rows:
            for field in ("performance", "peak_rss_mib"):
                value = float(row.get(field, 0.0))
                if not math.isfinite(value) or value <= 0:
                    raise CampaignError(
                        f"{harness.get('id')} has invalid {field}: {value}"
                    )


def classify_complete_result(
    record: dict[str, Any], *, measured_rounds: int = ROUNDS
) -> str:
    """Classify a core-eligible result while retaining route attribution limits."""

    validate_target_result(
        record,
        allow_attribution_limits=True,
        measured_rounds=measured_rounds,
    )
    limited_harnesses = [
        str(harness["id"])
        for harness in record["harnesses"]
        if harness["gates"].get("compiler_route_equivalent") is not True
    ]
    record["core_eligible"] = True
    record["attribution_limits"] = [
        {
            "gate": "compiler_route_equivalent",
            "harness_id": harness_id,
            "classification": "outside_predeclared_interval",
        }
        for harness_id in limited_harnesses
    ]
    if limited_harnesses:
        return "complete_with_attribution_limits"
    return "complete"


def mark_diagnostic_current_worktree(
    record: dict[str, Any],
    implementation: ImplementationSnapshot,
    *,
    required_cpus: str | None,
    measured_rounds: int,
) -> None:
    if measured_rounds != ROUNDS:
        raise CampaignError("working-tree diagnostics require three measured rounds")
    manifest = verify_implementation_snapshot(implementation)
    if manifest.get("source_kind") != "working_tree":
        raise CampaignError("diagnostic result does not use a working-tree snapshot")
    record.update(
        {
            "campaign_classification": "diagnostic_current_worktree",
            "primary_eligible": False,
            "primary_ineligibility_reason": "working_tree_implementation",
            "implementation_source_kind": "working_tree",
            "repository_head": manifest["repository_head"],
            "repository_status": manifest["repository_status"],
            "repository_status_sha256": manifest["repository_status_sha256"],
            "implementation_revision": implementation.revision,
            "implementation_sha256": implementation.sha256,
            "campaign_snapshot_sha256": manifest["campaign_snapshot_sha256"],
            "campaign_snapshot_file_count": manifest["campaign_snapshot_file_count"],
            "campaign_snapshot_size_bytes": manifest["campaign_snapshot_size_bytes"],
            "required_cpus": required_cpus,
            "measured_rounds": measured_rounds,
        }
    )
    record["core_eligible"] = False


def primary_publication_allowed(
    record: dict[str, Any], *, diagnostic_current_worktree: bool
) -> bool:
    return (
        not diagnostic_current_worktree
        and record.get("campaign_classification") != "diagnostic_current_worktree"
        and record.get("primary_eligible") is not False
        and record.get("measured_rounds") in PUBLISHABLE_ROUND_COUNTS
        and record.get("status") in {"complete", "complete_with_attribution_limits"}
    )


def run_target(
    spec: TargetSpec,
    *,
    checkout: pathlib.Path,
    source_record: dict[str, Any],
    raw_dir: pathlib.Path,
    implementation: ImplementationSnapshot,
    wrapper: pathlib.Path,
    sysroot: pathlib.Path,
    toolchain: str,
    jobs: int,
    build_timeout: int,
    run_timeout: int,
    preflight_only: bool,
    measure_only: bool,
    diagnostic_current_worktree: bool = False,
    required_cpus: str | None = None,
    measured_rounds: int = ROUNDS,
) -> dict[str, Any]:
    if diagnostic_current_worktree and measured_rounds != ROUNDS:
        raise CampaignError("working-tree diagnostics require three measured rounds")
    if not diagnostic_current_worktree and measured_rounds != ROUNDS:
        raise CampaignError("primary results require exactly three measured rounds")
    result = empty_target_result(spec, raw_dir)
    result["source"] = source_record
    result.update(
        {
            "implementation_revision": implementation.revision,
            "implementation_sha256": implementation.sha256,
            "implementation_snapshot": str(implementation.path),
            "implementation_manifest_path": str(implementation.manifest_path),
            "implementation_file_count": implementation.file_count,
            "implementation_size_bytes": implementation.size_bytes,
            "measured_rounds": measured_rounds,
        }
    )
    output_path = raw_dir / "results" / f"{spec.id}.json"
    try:
        verify_implementation_snapshot(implementation)
        if measure_only:
            builds = load_exact_builds(
                spec, raw_dir=raw_dir, implementation=implementation
            )
        else:
            builds = build_target_variants(
                spec,
                checkout=checkout,
                raw_dir=raw_dir,
                implementation=implementation,
                wrapper=wrapper,
                sysroot=sysroot,
                toolchain=toolchain,
                jobs=jobs,
                timeout=build_timeout,
            )
        result["builds"] = {
            variant: build["build_path"] for variant, build in builds.items()
        }
        gates = build_gates(spec, builds)
        failed_build_gates = [
            gate
            for gate, passed in gates.items()
            if gate not in {"correctness", "compiler_route_equivalent"} and not passed
        ]
        if failed_build_gates:
            raise CampaignError("build gates failed: " + ",".join(failed_build_gates))
        verify_implementation_snapshot(implementation)
        harness_results = {row["id"]: row for row in result["harnesses"]}
        with primary_measurement_lock(spec.id) as lock_path:
            result["measurement_lock"] = lock_path
            for harness in spec.harnesses:
                for variant in VARIANTS:
                    warmup = execute_one(
                        spec,
                        harness,
                        variant,
                        builds[variant],
                        raw_dir=raw_dir,
                        round_index=0,
                        timeout=run_timeout,
                        warmup=True,
                    )
                    raw_path = pathlib.Path(str(warmup["raw_path"]))
                    harness_results[harness.id]["warmup_evidence"][variant].append(
                        {
                            "record_path": str(raw_path.resolve()),
                            "sha256": sha256_file(raw_path),
                        }
                    )
            gates["correctness"] = True
            for harness_result in result["harnesses"]:
                harness_result["gates"] = dict(gates)
            if preflight_only:
                verify_implementation_snapshot(implementation)
                result["implementation_recomputed_sha256"] = implementation.sha256
                result["implementation_verification_passed"] = True
                result["status"] = "preflight_passed"
                if diagnostic_current_worktree:
                    mark_diagnostic_current_worktree(
                        result,
                        implementation,
                        required_cpus=required_cpus,
                        measured_rounds=measured_rounds,
                    )
                write_json(output_path, result)
                return result
            for round_index in range(1, measured_rounds + 1):
                rotation = (round_index - 1) % len(VARIANTS)
                variant_order = VARIANTS[rotation:] + VARIANTS[:rotation]
                for harness in spec.harnesses:
                    for variant in variant_order:
                        measurement = execute_one(
                            spec,
                            harness,
                            variant,
                            builds[variant],
                            raw_dir=raw_dir,
                            round_index=round_index,
                            timeout=run_timeout,
                            warmup=False,
                        )
                        harness_results[harness.id]["measurements"].append(measurement)
        for harness_result in result["harnesses"]:
            equivalent, ratio = compiler_route_equivalence(
                harness_result["measurements"], measured_rounds=measured_rounds
            )
            harness_result["gates"]["compiler_route_equivalent"] = equivalent
            harness_result["compiler_route_median_ratio"] = ratio
        verify_implementation_snapshot(implementation)
        result["implementation_recomputed_sha256"] = implementation.sha256
        result["implementation_verification_passed"] = True
        result["status"] = classify_complete_result(
            result, measured_rounds=measured_rounds
        )
    except (
        CampaignError,
        matrix.MatrixError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        retain_measurements = any(
            harness.get("measurements") for harness in result["harnesses"]
        )
        mark_target_ineligible(
            result,
            str(error),
            retain_measurements=retain_measurements,
            correctness_harness_id=(
                error.harness_id
                if isinstance(error, HarnessCorrectnessError)
                else None
            ),
        )
    if diagnostic_current_worktree:
        mark_diagnostic_current_worktree(
            result,
            implementation,
            required_cpus=required_cpus,
            measured_rounds=measured_rounds,
        )
    write_json(output_path, result)
    if primary_publication_allowed(
        result, diagnostic_current_worktree=diagnostic_current_worktree
    ):
        write_json(ASSEMBLER_TARGET_DIR / f"{spec.id}.json", result)
    return result


def parse_target_ids(raw: str) -> tuple[str, ...]:
    values = tuple(
        dict.fromkeys(value.strip() for value in raw.split(",") if value.strip())
    )
    unknown = sorted(set(values) - set(TARGETS))
    if not values:
        raise argparse.ArgumentTypeError("at least one target is required")
    if unknown:
        raise argparse.ArgumentTypeError("unknown targets: " + ",".join(unknown))
    return values


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", default="redb,actix_web")
    parser.add_argument("--raw-dir", type=pathlib.Path, default=DEFAULT_RAW_DIR)
    parser.add_argument(
        "--checkout-root", type=pathlib.Path, default=DEFAULT_CHECKOUT_ROOT
    )
    parser.add_argument("--toolchain", default="nightly-2026-06-11")
    implementation = parser.add_mutually_exclusive_group()
    implementation.add_argument("--unialloc-revision")
    implementation.add_argument(
        "--current-working-tree",
        action="store_true",
        help=(
            "freeze the current allocator/pass working tree for a diagnostic-only "
            "campaign that cannot enter primary-suite results"
        ),
    )
    parser.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--build-timeout", type=int, default=3600)
    parser.add_argument("--run-timeout", type=int, default=900)
    parser.add_argument(
        "--rounds",
        type=int,
        default=ROUNDS,
        help="one warmup, exactly three paired rounds, and median point estimates",
    )
    parser.add_argument(
        "--required-cpus",
        help="fail unless the campaign process has exactly this CPU affinity",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--materialize-only", action="store_true")
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument(
        "--measure-only",
        action="store_true",
        help="reuse byte-verified exact-revision builds and rerun measurements",
    )
    args = parser.parse_args(argv)
    args.targets = parse_target_ids(args.targets)
    if args.unialloc_revision is None and not args.current_working_tree:
        args.unialloc_revision = PINNED_IMPLEMENTATION_REVISION
    if (
        not args.current_working_tree
        and args.unialloc_revision != PINNED_IMPLEMENTATION_REVISION
    ):
        parser.error(
            "--unialloc-revision must equal the predeclared exact revision "
            + PINNED_IMPLEMENTATION_REVISION
        )
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if args.rounds != ROUNDS:
        parser.error("the campaign requires exactly three measured rounds")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    raw_dir = args.raw_dir.resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "tmp").mkdir(parents=True, exist_ok=True)
    environment = environment_record()
    if args.required_cpus:
        expected = parse_cpu_set(args.required_cpus)
        observed = set(environment["cpu_affinity"])
        if observed != expected:
            raise CampaignError(
                "campaign CPU affinity mismatch: "
                f"got {sorted(observed)}, expected {sorted(expected)}"
            )
    write_json(raw_dir / "environment.json", environment)
    implementation = (
        materialize_working_tree_implementation(raw_dir)
        if args.current_working_tree
        else materialize_implementation_revision(raw_dir, args.unialloc_revision)
    )
    source_rows: dict[str, tuple[pathlib.Path, dict[str, Any]]] = {}
    for target_id in args.targets:
        spec = TARGETS[target_id]
        source_rows[target_id] = materialize_source(
            spec, args.checkout_root.resolve(), raw_dir
        )
    if args.materialize_only:
        return 0
    wrapper = ensure_revision_mir_wrapper(
        raw_dir / "tools" / "unialloc-rustc-wrapper",
        implementation=implementation,
        toolchain=args.toolchain,
        timeout=args.build_timeout,
    )
    sysroot = matrix.rustc_sysroot(args.toolchain)
    results: list[dict[str, Any]] = []
    for target_id in args.targets:
        checkout, source_record = source_rows[target_id]
        results.append(
            run_target(
                TARGETS[target_id],
                checkout=checkout,
                source_record=source_record,
                raw_dir=raw_dir,
                implementation=implementation,
                wrapper=wrapper,
                sysroot=sysroot,
                toolchain=args.toolchain,
                jobs=args.jobs,
                build_timeout=args.build_timeout,
                run_timeout=args.run_timeout,
                preflight_only=args.preflight_only,
                measure_only=args.measure_only,
                diagnostic_current_worktree=args.current_working_tree,
                required_cpus=args.required_cpus,
                measured_rounds=args.rounds,
            )
        )
    summary = {
        "schema_version": 1,
        "created_unix_seconds": time.time(),
        "implementation_revision": implementation.revision,
        "implementation_sha256": implementation.sha256,
        "implementation_manifest_path": str(implementation.manifest_path),
        "measured_rounds": args.rounds,
        "targets": [
            {
                "target_id": result["target_id"],
                "status": result["status"],
                "result_path": str(
                    (raw_dir / "results" / f"{result['target_id']}.json").resolve()
                ),
                "blockers": result.get("blockers", []),
            }
            for result in results
        ],
    }
    if args.current_working_tree:
        manifest = verify_implementation_snapshot(implementation)
        summary.update(
            {
                "campaign_classification": "diagnostic_current_worktree",
                "primary_eligible": False,
                "core_eligible": False,
                "implementation_source_kind": "working_tree",
                "repository_head": manifest["repository_head"],
                "repository_status": manifest["repository_status"],
                "repository_status_sha256": manifest["repository_status_sha256"],
                "campaign_snapshot_sha256": manifest["campaign_snapshot_sha256"],
                "campaign_snapshot_file_count": manifest[
                    "campaign_snapshot_file_count"
                ],
                "campaign_snapshot_size_bytes": manifest[
                    "campaign_snapshot_size_bytes"
                ],
                "required_cpus": args.required_cpus,
            }
        )
    write_json(raw_dir / "summary.json", summary)
    return (
        0
        if all(
            result["status"]
            in {"complete", "complete_with_attribution_limits", "preflight_passed"}
            for result in results
        )
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
