#!/usr/bin/env python3
"""Reconstruct source-audit evidence from immutable Type Isolation build records.

This compatibility adapter reads no measurement record. It validates the pinned
source record and every exact variant build plus the artifacts named by those
builds, publishes one content-addressed audit manifest, and adds that manifest
to the runner's mutable target-result view. Frozen build, measurement, suite,
protocol, and published target-result records remain untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from evaluation.scripts import immutable_evidence  # noqa: E402
from evaluation.scripts import type_isolation_suite_contract as suite_contract  # noqa: E402
import realworld_type_isolation_matrix as matrix  # noqa: E402


VARIANTS = ("unialloc", "typed_plain", "typeiso_perf")
POLICY_FLAGS = {"unialloc": None, "typed_plain": 0, "typeiso_perf": 1}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PSR_TARGETS = {"polars", "swc", "rustpython"}


class MaterializationError(RuntimeError):
    """Source or build evidence cannot support a source-audit manifest."""


def sha256_file(path: Path) -> str:
    return immutable_evidence.sha256_file(path)


def load_object(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MaterializationError(f"{context} is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise MaterializationError(f"{context} must be a JSON object: {path}")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise MaterializationError(message)


def artifact_ref(path: Path, context: str, expected_sha256: Any = None) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    require(resolved.is_file(), f"{context} is missing: {resolved}")
    reference = immutable_evidence.artifact_ref(resolved)
    if expected_sha256 is not None:
        require(
            isinstance(expected_sha256, str)
            and SHA256_RE.fullmatch(expected_sha256) is not None
            and reference["sha256"] == expected_sha256,
            f"{context} digest mismatch",
        )
    return reference


def exact_child(path: Path, parent: Path, context: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(parent.expanduser().resolve())
    except ValueError as error:
        raise MaterializationError(f"{context} escapes its evidence root") from error
    return resolved


def run_git(checkout: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise MaterializationError(
            f"source checkout Git probe failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def suite_target(contract: suite_contract.SuiteContract, target_id: str) -> Mapping[str, Any]:
    matches = [
        row
        for row in contract.manifest.get("targets", [])
        if isinstance(row, dict) and row.get("id") == target_id
    ]
    require(len(matches) == 1, f"suite does not contain exactly one {target_id} target")
    return matches[0]


def validate_source(
    contract: suite_contract.SuiteContract,
    target_spec: Mapping[str, Any],
    target_view: Mapping[str, Any],
    source_path: Path,
    target_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = load_object(source_path, f"{target_id} source record")
    expected = target_spec.get("source")
    require(isinstance(expected, dict), f"{target_id} suite source is invalid")
    require(source.get("target_id") == target_id, f"{target_id} source target mismatch")
    require(
        source.get("source_commit") == expected.get("commit"),
        f"{target_id} source commit mismatch",
    )
    require(
        source.get("source_ref") == expected.get("ref"),
        f"{target_id} source ref mismatch",
    )
    require(target_view.get("source") == source, f"{target_id} target-view source mismatch")
    require(source.get("status") == "", f"{target_id} source checkout was recorded dirty")
    checkout = Path(str(source.get("checkout", ""))).expanduser().resolve()
    require((checkout / ".git").is_dir(), f"{target_id} source checkout is missing")
    require(
        run_git(checkout, "rev-parse", "HEAD") == source.get("source_commit"),
        f"{target_id} live source commit differs from source.json",
    )
    require(
        run_git(checkout, "rev-parse", "HEAD^{tree}") == source.get("tree"),
        f"{target_id} live source tree differs from source.json",
    )
    require(
        run_git(checkout, "status", "--short") == source.get("status"),
        f"{target_id} live source status differs from source.json",
    )
    lock_digest = source.get("cargo_lock_sha256")
    lock_path = checkout / "Cargo.lock"
    if lock_digest is None:
        require(not lock_path.exists(), f"{target_id} unrecorded source Cargo.lock exists")
        lock_ref = None
    else:
        lock_ref = artifact_ref(lock_path, f"{target_id} source Cargo.lock", lock_digest)
    return source, {
        "record": artifact_ref(source_path, f"{target_id} source record"),
        "repository": source.get("repository"),
        "source_ref": source.get("source_ref"),
        "source_commit": source.get("source_commit"),
        "tree": source.get("tree"),
        "status": source.get("status"),
        "cargo_lock": lock_ref,
    }


def validate_embedded_source(
    target_spec: Mapping[str, Any],
    target_view: Mapping[str, Any],
    target_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = target_view.get("source")
    expected = target_spec.get("source")
    require(isinstance(source, dict), f"{target_id} embedded source record is missing")
    require(isinstance(expected, dict), f"{target_id} suite source is invalid")
    require(source.get("head") == expected.get("commit"), f"{target_id} source commit mismatch")
    require(source.get("source_ref") == expected.get("ref"), f"{target_id} source ref mismatch")
    require(target_view.get("source_commit") == source.get("head"), f"{target_id} flattened source commit mismatch")
    require(target_view.get("source_ref") == source.get("source_ref"), f"{target_id} flattened source ref mismatch")
    checkout = Path(str(source.get("checkout", ""))).expanduser().resolve()
    require((checkout / ".git").exists(), f"{target_id} source checkout is missing")
    require(run_git(checkout, "rev-parse", "HEAD") == source.get("head"), f"{target_id} live source commit differs from target view")
    require(run_git(checkout, "status", "--short") == "", f"{target_id} live source checkout is dirty")
    lock = artifact_ref(
        checkout / "Cargo.lock",
        f"{target_id} source Cargo.lock",
        source.get("cargo_lock_sha256"),
    )
    return dict(source), {
        "record_kind": "runner_target_view_embedded_source",
        "checkout": str(checkout),
        "source_ref": source.get("source_ref"),
        "source_commit": source.get("head"),
        "tree": run_git(checkout, "rev-parse", "HEAD^{tree}"),
        "status": "",
        "cargo_lock": lock,
        "campaign_toolchain": source.get("campaign_toolchain"),
        "upstream_toolchain": source.get("upstream_toolchain"),
    }


def validate_implementation(
    contract: suite_contract.SuiteContract, target_view: Mapping[str, Any]
) -> dict[str, Any]:
    require(
        target_view.get("implementation_revision") == contract.implementation_revision,
        "target-view implementation revision mismatch",
    )
    require(
        target_view.get("implementation_sha256") == contract.implementation_sha256,
        "target-view implementation digest mismatch",
    )
    frozen_record_value = target_view.get("frozen_snapshot_record")
    if isinstance(frozen_record_value, str) and frozen_record_value:
        manifest_path = Path(frozen_record_value).expanduser().resolve()
        manifest = load_object(manifest_path, "implementation snapshot manifest")
        snapshot_path = Path(str(manifest.get("path", ""))).expanduser().resolve()
        require(
            manifest.get("allocator_revision") == contract.implementation_revision
            and manifest.get("source_head") == contract.implementation_revision
            and manifest.get("unialloc_implementation_sha256")
            == contract.implementation_sha256
            and manifest.get("implementation_file_count")
            == contract.implementation_file_count
            and manifest.get("implementation_total_bytes")
            == contract.implementation_size_bytes,
            "implementation snapshot identity mismatch",
        )
        files = manifest.get("implementation_files")
        require(isinstance(files, list) and len(files) == contract.implementation_file_count, "implementation snapshot file list mismatch")
        stream = hashlib.sha256()
        total_bytes = 0
        for relative_value in sorted(files):
            require(isinstance(relative_value, str), "implementation snapshot path is invalid")
            relative = PurePosixPath(relative_value)
            require(bool(relative.parts) and ".." not in relative.parts and not relative.is_absolute(), "implementation snapshot path is unsafe")
            payload = (snapshot_path / relative).read_bytes()
            stream.update(relative.as_posix().encode())
            stream.update(b"\0")
            stream.update(payload)
            stream.update(b"\0")
            total_bytes += len(payload)
        require(stream.hexdigest() == contract.implementation_sha256, "implementation snapshot content digest mismatch")
        require(total_bytes == contract.implementation_size_bytes, "implementation snapshot byte count mismatch")
        return {
            "revision": contract.implementation_revision,
            "sha256": contract.implementation_sha256,
            "snapshot": str(snapshot_path),
            "manifest": artifact_ref(manifest_path, "implementation snapshot manifest"),
            "campaign_snapshot_sha256": manifest.get("campaign_snapshot_sha256"),
            "file_count": contract.implementation_file_count,
            "size_bytes": contract.implementation_size_bytes,
        }
    manifest_path = Path(str(target_view.get("implementation_manifest_path", "")))
    snapshot_path = Path(str(target_view.get("implementation_snapshot", ""))).resolve()
    require(manifest_path.resolve() == snapshot_path / "snapshot.json", "implementation manifest path mismatch")
    manifest = load_object(manifest_path, "implementation snapshot manifest")
    require(
        manifest.get("implementation_revision") == contract.implementation_revision
        and manifest.get("implementation_sha256") == contract.implementation_sha256
        and manifest.get("canonical_file_count") == contract.implementation_file_count
        and manifest.get("canonical_size_bytes") == contract.implementation_size_bytes,
        "implementation snapshot identity mismatch",
    )
    return {
        "revision": contract.implementation_revision,
        "sha256": contract.implementation_sha256,
        "snapshot": str(snapshot_path),
        "manifest": artifact_ref(manifest_path, "implementation snapshot manifest"),
        "file_count": contract.implementation_file_count,
        "size_bytes": contract.implementation_size_bytes,
    }


def validate_log_artifacts(record: Mapping[str, Any], context: str) -> list[dict[str, Any]]:
    rows = record.get("commands")
    commands = rows if isinstance(rows, list) else [record]
    require(bool(commands), f"{context} has no build command records")
    artifacts: list[dict[str, Any]] = []
    for index, command in enumerate(commands):
        require(isinstance(command, dict), f"{context} command {index} is invalid")
        require(
            command.get("exit_code") == 0 and command.get("timed_out") is False,
            f"{context} build command failed",
        )
        artifacts.append(
            {
                "stdout": artifact_ref(
                    Path(str(command.get("stdout_path", ""))),
                    f"{context} command {index} stdout",
                    command.get("stdout_sha256"),
                ),
                "stderr": artifact_ref(
                    Path(str(command.get("stderr_path", ""))),
                    f"{context} command {index} stderr",
                    command.get("stderr_sha256"),
                ),
            }
        )
    return artifacts


def validate_force_load(
    force_load: Any,
    *,
    contract: suite_contract.SuiteContract,
    context: str,
) -> dict[str, Any]:
    require(isinstance(force_load, dict), f"{context} force-load record is missing")
    require(
        force_load.get("success") is True
        and force_load.get("exit_code") == 0
        and force_load.get("timed_out") is False,
        f"{context} force-load build failed",
    )
    require(
        force_load.get("implementation_revision") == contract.implementation_revision
        and force_load.get("implementation_sha256") == contract.implementation_sha256,
        f"{context} force-load implementation mismatch",
    )
    return {
        "rlib": artifact_ref(
            Path(str(force_load.get("rlib", ""))),
            f"{context} force-load rlib",
            force_load.get("rlib_sha256"),
        ),
        "stdout": artifact_ref(
            Path(str(force_load.get("stdout_path", ""))),
            f"{context} force-load stdout",
            force_load.get("stdout_sha256"),
        ),
        "stderr": artifact_ref(
            Path(str(force_load.get("stderr_path", ""))),
            f"{context} force-load stderr",
            force_load.get("stderr_sha256"),
        ),
    }


def validate_audit_dir(
    record: Mapping[str, Any], build_path: Path, raw_dir: Path, context: str
) -> dict[str, Any]:
    audit = record.get("audit")
    typeiso = record.get("typeiso")
    require(isinstance(audit, dict) and isinstance(typeiso, dict), f"{context} compiler audit is missing")
    audit_dir = exact_child(Path(str(typeiso.get("audit_dir", ""))), raw_dir / "audits", f"{context} audit directory")
    require(audit_dir.is_dir(), f"{context} audit directory is missing")
    recomputed = matrix.summarize_audits(audit_dir)
    require(recomputed == audit, f"{context} compiler audit summary mismatch")
    require(
        int(audit.get("audit_file_count", 0)) > 0
        and int(audit.get("semantic_rewrites_applied", 0)) > 0,
        f"{context} compiler audit has no semantic rewrites",
    )
    persisted = load_object(build_path.parent / "audit.json", f"{context} persisted audit")
    require(persisted == audit, f"{context} persisted audit differs from build record")
    return {
        "summary": artifact_ref(build_path.parent / "audit.json", f"{context} audit summary"),
        "files": [artifact_ref(path, f"{context} audit file") for path in sorted(audit_dir.glob("*.json"))],
        "audit_sha256": audit.get("audit_sha256"),
        "audit_file_count": audit.get("audit_file_count"),
        "semantic_rewrites_applied": audit.get("semantic_rewrites_applied"),
    }


def validate_activation(record: Mapping[str, Any], target_id: str, context: str) -> dict[str, Any]:
    activation = record.get("activation")
    require(isinstance(activation, dict) and activation, f"{context} activation evidence is missing")
    rows = {"primary": activation} if target_id == "redb" else activation
    result: dict[str, Any] = {}
    executables = record.get("executables") if target_id == "actix_web" else None
    if target_id == "actix_web":
        require(isinstance(executables, dict) and set(executables) == set(rows), f"{context} executable set mismatch")
    for name, row in rows.items():
        require(isinstance(row, dict) and row.get("passed") is True, f"{context}/{name} activation failed")
        binary = artifact_ref(Path(str(row.get("binary", ""))), f"{context}/{name} binary", row.get("binary_sha256"))
        if target_id == "actix_web":
            require(str(Path(str(executables[name])).resolve()) == binary["path"], f"{context}/{name} executable path mismatch")
        else:
            require(record.get("binary_sha256") == binary["sha256"], f"{context} binary digest mismatch")
        result[name] = {
            "binary": binary,
            "marker_present": row.get("marker_present"),
            "implementation_symbol_present": row.get("implementation_symbol_present"),
            "passed": True,
        }
    return result


def validate_variant(
    contract: suite_contract.SuiteContract,
    raw_dir: Path,
    target_id: str,
    source: Mapping[str, Any],
    variant: str,
    build_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    expected_path = (raw_dir / "builds" / target_id / variant / "build.json").resolve()
    require(build_path.resolve() == expected_path, f"{target_id}/{variant} build path is not exact")
    immutable_evidence.validate_committed_file(expected_path)
    record = load_object(expected_path, f"{target_id}/{variant} build record")
    context = f"{target_id}/{variant}"
    require(record.get("target_id") == target_id and record.get("variant") == variant, f"{context} build identity mismatch")
    require(record.get("source_commit") == source.get("source_commit"), f"{context} source commit mismatch")
    require(
        record.get("implementation_revision") == contract.implementation_revision
        and record.get("implementation_sha256") == contract.implementation_sha256
        and record.get("frozen_implementation_sha256") == contract.implementation_sha256
        and record.get("unialloc_implementation_sha256") == contract.implementation_sha256,
        f"{context} implementation identity mismatch",
    )
    require(record.get("stats_enabled") is False, f"{context} enables statistics")
    require(record.get("lowering_policy_flags") == POLICY_FLAGS[variant], f"{context} policy flag mismatch")
    require(record.get("actual_mir_rewrite") is (variant != "unialloc"), f"{context} MIR route mismatch")
    result: dict[str, Any] = {
        "build_record": artifact_ref(expected_path, f"{context} build record"),
        "policy_flags": POLICY_FLAGS[variant],
        "build_logs": validate_log_artifacts(record, context),
        "activation": validate_activation(record, target_id, context),
        "source_artifacts": {},
    }
    worktree = raw_dir / "build-work" / target_id / variant
    if target_id == "redb":
        result["source_artifacts"] = {
            "manifest": artifact_ref(worktree / "Cargo.toml", f"{context} manifest", record.get("manifest_sha256")),
            "cargo_lock": artifact_ref(worktree / "Cargo.lock", f"{context} Cargo.lock", record.get("cargo_lock_sha256")),
            "workload": artifact_ref(worktree / "src" / "main.rs", f"{context} workload", record.get("workload_source_sha256")),
        }
        if variant == "unialloc":
            require(
                Path(str(record.get("unialloc_dependency_path", ""))).resolve()
                == Path(str(record.get("implementation_snapshot", ""))).resolve() / "unialloc",
                f"{context} direct dependency path mismatch",
            )
    else:
        result["source_artifacts"]["cargo_lock"] = artifact_ref(
            worktree / "Cargo.lock", f"{context} Cargo.lock", record.get("cargo_lock_sha256")
        )
        require(record.get("cargo_lock_sha256") == source.get("cargo_lock_sha256"), f"{context} Cargo.lock differs from source pin")
        rows = record.get("source_audit")
        require(isinstance(rows, list) and rows, f"{context} source instrumentation audit is missing")
        expected_digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        require(record.get("source_audit_sha256") == expected_digest, f"{context} source-audit digest mismatch")
        source_files: list[dict[str, Any]] = []
        checkout = Path(str(source.get("checkout", "")))
        for row in rows:
            require(isinstance(row, dict), f"{context} source-audit row is invalid")
            relative = PurePosixPath(str(row.get("path", "")))
            require(bool(relative.parts) and ".." not in relative.parts and not relative.is_absolute(), f"{context} source-audit path is unsafe")
            source_files.append(
                {
                    "path": relative.as_posix(),
                    "upstream": artifact_ref(checkout / relative, f"{context} upstream source", row.get("upstream_sha256")),
                    "instrumented": artifact_ref(worktree / relative, f"{context} instrumented source", row.get("instrumented_sha256")),
                    **({"patch": row["patch"], "patched_sha256": row["patched_sha256"]} if "patch" in row else {}),
                }
            )
        result["source_artifacts"]["instrumentation"] = source_files
        if variant == "unialloc":
            result["force_load"] = validate_force_load(
                (record.get("baseline_force_load") or {}).get("force_load"),
                contract=contract,
                context=context,
            )
    if variant != "unialloc":
        result["compiler_audit"] = validate_audit_dir(record, expected_path, raw_dir, context)
        result["force_load"] = validate_force_load(
            (record.get("typeiso") or {}).get("force_load"),
            contract=contract,
            context=context,
        )
    return result, record.get("source_audit") if target_id == "actix_web" else None


def psr_origin_root(record: Mapping[str, Any], context: str) -> Path:
    binary = Path(str(record.get("binary", ""))).expanduser().resolve()
    for parent in binary.parents:
        if parent.name == "polars-swc-rustpython" and parent.parent.name == "campaigns":
            return exact_child(parent, ROOT / "evaluation" / "raw", f"{context} origin")
    raise MaterializationError(f"{context} binary does not identify a retained campaign root")


def validate_psr_audit_dir(
    record: Mapping[str, Any], context: str, *, require_rewrites: bool
) -> dict[str, Any]:
    audit_dir = exact_child(
        Path(str(record.get("audit_dir", ""))),
        ROOT / "evaluation" / "raw",
        f"{context} audit directory",
    )
    require(audit_dir.is_dir(), f"{context} audit directory is missing")
    files = sorted(audit_dir.glob("*.json"))
    audit = record.get("audit")
    if require_rewrites:
        require(isinstance(audit, dict), f"{context} compiler audit is missing")
        require(matrix.summarize_audits(audit_dir) == audit, f"{context} compiler audit summary mismatch")
        require(
            int(audit.get("audit_file_count", 0)) > 0
            and int(audit.get("semantic_rewrites_applied", 0)) > 0,
            f"{context} compiler audit has no semantic rewrites",
        )
    else:
        require(audit is None and not files, f"{context} baseline audit directory is not empty")
    return {
        "path": str(audit_dir),
        "files": [artifact_ref(path, f"{context} audit file") for path in files],
        "audit_sha256": audit.get("audit_sha256") if isinstance(audit, dict) else None,
        "audit_file_count": len(files),
        "semantic_rewrites_applied": (
            audit.get("semantic_rewrites_applied") if isinstance(audit, dict) else 0
        ),
    }


def validate_psr_variant(
    contract: suite_contract.SuiteContract,
    raw_dir: Path,
    target_id: str,
    source: Mapping[str, Any],
    implementation: Mapping[str, Any],
    variant: str,
    build_path: Path,
) -> dict[str, Any]:
    expected_path = (raw_dir / "builds" / target_id / variant / "build.json").resolve()
    require(build_path.resolve() == expected_path, f"{target_id}/{variant} build path is not exact")
    immutable_evidence.validate_committed_file(expected_path)
    record = load_object(expected_path, f"{target_id}/{variant} build record")
    context = f"{target_id}/{variant}"
    require(record.get("target_id") == target_id and record.get("variant") == variant, f"{context} build identity mismatch")
    require(
        record.get("source_commit") == source.get("head")
        and record.get("source_ref") == source.get("source_ref"),
        f"{context} source identity mismatch",
    )
    require(
        record.get("implementation_revision") == contract.implementation_revision
        and record.get("allocator_revision") == contract.implementation_revision
        and record.get("implementation_sha256") == contract.implementation_sha256
        and record.get("unialloc_implementation_sha256")
        == contract.implementation_sha256
        and record.get("campaign_snapshot_sha256")
        == implementation.get("campaign_snapshot_sha256"),
        f"{context} implementation identity mismatch",
    )
    require(
        record.get("success") is True
        and record.get("exit_code") == 0
        and record.get("timed_out") is False,
        f"{context} build failed",
    )
    require(record.get("stats_enabled") is False, f"{context} enables statistics")
    require(record.get("policy_flags") == POLICY_FLAGS[variant], f"{context} policy flag mismatch")
    require(record.get("actual_mir_provenance") is (variant != "unialloc"), f"{context} MIR route mismatch")

    origin_root = psr_origin_root(record, context)
    origin_build = origin_root / "builds" / target_id / variant / "build.json"
    require(origin_build.is_file(), f"{context} origin build record is missing")
    require(origin_build.read_bytes() == expected_path.read_bytes(), f"{context} copied build record differs from its origin")
    logs = {
        name: artifact_ref(origin_build.parent / f"build.{name}", f"{context} build {name}")
        for name in ("stdout", "stderr")
    }
    binary = artifact_ref(
        Path(str(record.get("binary", ""))),
        f"{context} binary",
        record.get("binary_sha256"),
    )
    activation = record.get("activation")
    require(
        isinstance(activation, dict)
        and activation.get("success") is True
        and activation.get("exit_code") == 0
        and int(activation.get("unialloc_symbol_count", 0)) > 0,
        f"{context} allocator activation failed",
    )
    patch = artifact_ref(
        Path(str(record.get("allocator_patch", ""))),
        f"{context} allocator patch",
        record.get("allocator_patch_sha256"),
    )
    wrapper = artifact_ref(
        Path(str(record.get("build_wrapper", ""))),
        f"{context} build wrapper",
        record.get("build_wrapper_sha256"),
    )
    if variant == "unialloc":
        route_artifact = artifact_ref(
            Path(str(record.get("direct_load_rlib", ""))),
            f"{context} direct-load rlib",
            record.get("direct_load_rlib_sha256"),
        )
    else:
        force_wrapper = Path(str(record.get("typeiso_force_load_wrapper", ""))).resolve()
        require(force_wrapper == Path(wrapper["path"]), f"{context} force-load wrapper mismatch")
        route_artifact = wrapper
    audit = validate_psr_audit_dir(
        record, context, require_rewrites=variant != "unialloc"
    )
    return {
        "build_record": artifact_ref(expected_path, f"{context} build record"),
        "origin_build_record": artifact_ref(origin_build, f"{context} origin build record"),
        "policy_flags": POLICY_FLAGS[variant],
        "build_logs": logs,
        "binary": binary,
        "activation": {
            "success": True,
            "unialloc_symbol_count": activation.get("unialloc_symbol_count"),
            "nm_stdout_sha256": activation.get("nm_stdout_sha256"),
        },
        "source_artifacts": {
            "allocator_patch": patch,
            "recorded_build_cargo_lock_sha256": record.get("cargo_lock_sha256"),
        },
        "compiler_audit": audit,
        "allocator_route_artifact": route_artifact,
        "build_wrapper": wrapper,
    }


def materialize_target(
    contract: suite_contract.SuiteContract,
    raw_dir: Path,
    view_path: Path,
    target_id: str,
    *,
    audit_root: Path | None = None,
) -> tuple[Path, str]:
    raw_dir = raw_dir.expanduser().resolve()
    view_path = view_path.expanduser().resolve()
    require(view_path.is_file(), f"mutable target view is missing: {view_path}")
    mode = stat.S_IMODE(view_path.stat().st_mode)
    require(mode & 0o200 != 0, f"refusing to replace read-only target record: {view_path}")
    target_spec = suite_target(contract, target_id)
    view = load_object(view_path, f"{target_id} mutable target view")
    require(view.get("target_id") == target_id, f"{target_id} target-view identity mismatch")
    require(view.get("suite_id") == contract.suite_id, f"{target_id} suite id mismatch")
    require(view.get("suite_manifest_sha256") == contract.manifest_sha256, f"{target_id} suite digest mismatch")
    suite_ref = artifact_ref(
        Path(str(view.get("suite_manifest_path", ""))),
        f"{target_id} bound suite manifest",
        contract.manifest_sha256,
    )
    source_path = raw_dir / "sources" / target_id / "source.json"
    if source_path.is_file():
        source, source_evidence = validate_source(
            contract, target_spec, view, source_path, target_id
        )
        runner_shape = "redb_actix"
    else:
        require(
            target_id in PSR_TARGETS,
            f"{target_id} runner has no supported source record",
        )
        source, source_evidence = validate_embedded_source(
            target_spec, view, target_id
        )
        runner_shape = "polars_swc_rustpython"
    implementation = validate_implementation(contract, view)
    references = view.get("builds") or view.get("build_records")
    require(isinstance(references, dict) and set(references) == set(VARIANTS), f"{target_id} build references do not exactly cover variants")
    variants: dict[str, Any] = {}
    instrumentation: list[dict[str, Any]] | None = None
    for variant in VARIANTS:
        if runner_shape == "redb_actix":
            row, variant_instrumentation = validate_variant(
                contract,
                raw_dir,
                target_id,
                source,
                variant,
                Path(str(references[variant])),
            )
        else:
            row = validate_psr_variant(
                contract,
                raw_dir,
                target_id,
                source,
                implementation,
                variant,
                Path(str(references[variant])),
            )
            variant_instrumentation = None
        variants[variant] = row
        if variant_instrumentation is not None:
            if instrumentation is None:
                instrumentation = variant_instrumentation
            else:
                require(instrumentation == variant_instrumentation, f"{target_id} variants used different source instrumentation")
    for harness in view.get("harnesses", []):
        require(
            isinstance(harness, dict)
            and isinstance(harness.get("gates"), dict)
            and harness["gates"].get("source_audit_retained") is True,
            f"{target_id} target view has a failed source-audit gate",
        )
    manifest = {
        "schema_version": 1,
        "evidence_kind": "type_isolation_source_audit_compatibility_materialization",
        "runner_shape": runner_shape,
        "suite": {"suite_id": contract.suite_id, "manifest": suite_ref},
        "target_id": target_id,
        "source": source_evidence,
        "implementation": implementation,
        "variants": variants,
        "gates": {
            "exact_variant_set": True,
            "suite_binding": True,
            "target_identity": True,
            "source_identity": True,
            "implementation_identity": True,
            "build_success": True,
            "allocator_activation": True,
            "actual_mir_provenance": True,
            "stats_disabled": True,
            "source_audit_retained": True,
        },
        "derivation": {
            "measurement_records_read": 0,
            "measurement_records_modified": 0,
            "inputs": ["source.json", "unialloc/build.json", "typed_plain/build.json", "typeiso_perf/build.json", "build-record artifacts"],
        },
    }
    payload = immutable_evidence.canonical_json_bytes(manifest)
    digest = hashlib.sha256(payload).hexdigest()
    destination = (audit_root or raw_dir / "source-audits") / target_id / f"{digest}.json"
    committed = immutable_evidence.persist_immutable_json(destination, manifest)
    require(committed.record_sha256 == digest, f"{target_id} content address mismatch")
    existing = view.get("evidence")
    evidence = dict(existing) if isinstance(existing, dict) else {}
    prior_path = evidence.get("source_audit")
    prior_digest = evidence.get("source_audit_sha256")
    if prior_path is not None:
        if prior_path != str(committed.path):
            validate_compatible_predecessor(prior_path, prior_digest, target_id)
    if prior_digest is not None:
        if prior_path == str(committed.path):
            require(prior_digest == digest, f"{target_id} target view names a different source-audit digest")
    evidence.update(
        {
            "source_audit": str(committed.path),
            "source_audit_sha256": digest,
            "source_audit_bytes": committed.path.stat().st_size,
        }
    )
    view["evidence"] = evidence
    immutable_evidence.atomic_write_bytes(view_path, immutable_evidence.canonical_json_bytes(view))
    view_path.chmod(mode)
    return committed.path, digest


def parse_targets(raw: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(value.strip() for value in raw.split(",") if value.strip()))
    if not values:
        raise argparse.ArgumentTypeError("at least one target is required")
    return values


def runner_namespace(target_id: str) -> str:
    if target_id in {"redb", "actix_web"}:
        return "redb-actix"
    if target_id in PSR_TARGETS:
        return "polars-swc-rustpython"
    raise MaterializationError(f"unsupported compatibility target: {target_id}")


def validate_compatible_predecessor(
    path_value: Any, digest_value: Any, target_id: str
) -> None:
    require(isinstance(path_value, str), f"{target_id} prior source-audit path is invalid")
    reference = artifact_ref(
        Path(path_value), f"{target_id} prior source-audit evidence", digest_value
    )
    prior = load_object(Path(reference["path"]), f"{target_id} prior source-audit evidence")
    derivation = prior.get("derivation")
    gates = prior.get("gates")
    require(
        prior.get("evidence_kind")
        == "type_isolation_source_audit_compatibility_materialization"
        and prior.get("target_id") == target_id
        and isinstance(derivation, dict)
        and derivation.get("measurement_records_read") == 0
        and derivation.get("measurement_records_modified") == 0
        and isinstance(gates, dict)
        and gates.get("source_audit_retained") is True,
        f"{target_id} prior source-audit evidence is not a compatible predecessor",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=suite_contract.CURRENT_SUITE_PATH)
    parser.add_argument(
        "--targets",
        type=parse_targets,
        default=("redb", "actix_web", "polars", "swc", "rustpython"),
    )
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--views-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        args.contract = suite_contract.load_suite_contract(args.suite)
        suite_contract.verify_protocol_files(args.contract)
        suite_contract.verify_suite_manifest_binding(args.contract)
    except suite_contract.SuiteContractError as error:
        parser.error(str(error))
    args.raw_dir = args.raw_dir.resolve() if args.raw_dir is not None else None
    args.views_dir = args.views_dir.resolve() if args.views_dir is not None else None
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        for target_id in args.targets:
            raw_dir = args.raw_dir or args.contract.runner_raw_dir(
                runner_namespace(target_id)
            ).resolve()
            views_dir = args.views_dir or raw_dir / "results"
            path, digest = materialize_target(
                args.contract,
                raw_dir,
                views_dir / f"{target_id}.json",
                target_id,
            )
            print(f"{target_id}\t{digest}\t{path}")
    except (MaterializationError, immutable_evidence.ImmutableEvidenceError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
