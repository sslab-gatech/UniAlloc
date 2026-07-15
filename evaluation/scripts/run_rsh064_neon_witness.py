#!/usr/bin/env python3
"""Run the pinned RSH-064 Neon witness through a real Node/V8 context.

The generic RustSec runner targets standalone binaries. RSH-064 requires a
Node-loaded N-API addon, so this small runner keeps that host transition
explicit. ``list`` and ``preflight`` are non-executing actions. ``run`` builds
and executes memory-unsafe vulnerable code only after ``--execute-unsafe``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import realworld_type_isolation_matrix as realworld  # noqa: E402
import run_rustsec_heap_harness as harness  # noqa: E402
import run_rustsec_heap_experiment as rustsec_experiment  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = (
    ROOT / "evaluation" / "config" / "rustsec_heap_neon_node_harnesses.json"
)
VARIANTS = (
    "system",
    "reclaim_plain",
    "reclaim_checks",
    "typed_plain",
    "typeiso",
)
RECLAIM_SIGNAL_RE = re.compile(
    r"pointer already released|pointer belongs to evicted released history|"
    r"type-cache pointer already retained or released",
    re.IGNORECASE,
)
REUSE_DENIAL_RE = re.compile(
    r"UNIALLOC_SECURITY_REUSE_DENIAL|typed_cache_wrong_identity_denials",
    re.IGNORECASE,
)
NODE_OUTPUT_RE = re.compile(
    r"^byteLength=(?P<length>\d+) bytes=(?P<before>[0-9,]*)$.*^"
    r"afterChurn=(?P<after>[0-9,]*)$",
    re.MULTILINE | re.DOTALL,
)
PATCHED_ERROR_CODES = frozenset(("E0505", "E0597"))


class NeonHarnessError(RuntimeError):
    """Raised when a pinned input or experiment contract is violated."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_sha256(value: object) -> str:
    return sha256_bytes(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    )


def write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def file_record(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        raise NeonHarnessError(f"missing evidence file: {path}")
    try:
        display = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        display = str(path.resolve())
    return {
        "path": display,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def parse_csv(raw: str, allowed: Iterable[str], label: str) -> tuple[str, ...]:
    allowed_values = tuple(allowed)
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not values:
        raise NeonHarnessError(f"{label} must contain at least one value")
    unknown = sorted(set(values) - set(allowed_values))
    if unknown:
        raise NeonHarnessError(f"unknown {label}: {', '.join(unknown)}")
    if len(values) != len(set(values)):
        raise NeonHarnessError(f"duplicate {label} values")
    return values


def cache_root(raw: pathlib.Path | None) -> pathlib.Path:
    if raw is not None:
        return raw.expanduser().resolve()
    base = pathlib.Path(os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache"))
    return (base / "unialloc" / "rustsec-heap").resolve()


def load_catalog(path: pathlib.Path) -> dict[str, Any]:
    value = harness.load_catalog(path)
    cases, scenarios = harness.index_catalog(value)
    expected_scenarios = {"RSH-064-node-addon", "RSH-064-derived-reuse"}
    if set(cases) != {"RSH-064"} or set(scenarios) != expected_scenarios:
        raise NeonHarnessError(
            "RSH-064 catalog must contain the pinned Node witness and derived edge"
        )
    case = cases["RSH-064"]
    scenario = scenarios["RSH-064-node-addon"][1]
    if case.get("advisory_id") != "RUSTSEC-2022-0028" or case.get("crate") != "neon":
        raise NeonHarnessError("unexpected RSH-064 advisory identity")
    if scenario.get("host_kind") != "node_napi_addon":
        raise NeonHarnessError("RSH-064 scenario must declare the Node N-API host")
    if scenario.get("expected_payload") != [0, 1, 2, 3]:
        raise NeonHarnessError("RSH-064 expected payload must be [0, 1, 2, 3]")
    for key in ("source", "node_driver", "published_witness"):
        record = scenario.get(key)
        if not isinstance(record, dict):
            raise NeonHarnessError(f"missing pinned scenario {key}")
        harness.validate_repo_file(record["path"], record["sha256"])
    derived = scenarios["RSH-064-derived-reuse"][1]
    if (
        derived.get("adapter_kind") != "derived_adapter"
        or derived.get("classification_role") != "derived_reuse_experiment"
        or derived.get("oracle", {}).get("tool") != "native_address_trace"
    ):
        raise NeonHarnessError("invalid RSH-064 derived reuse scenario")
    harness.validate_repo_file(
        derived["source_path"], derived["source_sha256"]
    )
    patched_source = derived.get("patched_source")
    if not isinstance(patched_source, dict):
        raise NeonHarnessError("missing RSH-064 derived patched source")
    harness.validate_repo_file(
        patched_source["source_path"], patched_source["source_sha256"]
    )
    annotation = derived.get("type_isolation_edge_annotation")
    if (
        not isinstance(annotation, dict)
        or annotation.get("kind")
        != "manual_exact_vulnerability_edge_identity"
        or annotation.get("expected_layout") != {"size": 4, "align": 1}
    ):
        raise NeonHarnessError("invalid RSH-064 derived Type Isolation annotation")
    lockfiles = case.get("lockfiles")
    if not isinstance(lockfiles, dict):
        raise NeonHarnessError("RSH-064 lockfiles must be an object")
    for name in (
        "vulnerable",
        "patched",
        "reclaim",
        "legacy_vulnerable",
        "legacy_patched",
    ):
        record = lockfiles.get(name)
        if not isinstance(record, dict):
            raise NeonHarnessError(f"missing pinned RSH-064 {name} lock")
        harness.validate_repo_file(record["path"], record["sha256"])
    for key, version in (("vulnerable_dependency", "0.10.0"), ("patched_dependency", "0.10.1")):
        dependency = case.get(key)
        if not isinstance(dependency, dict) or dependency.get("kind") != "crates_io_archive":
            raise NeonHarnessError(f"invalid RSH-064 {key}")
        if dependency.get("package") != "neon" or dependency.get("version") != version:
            raise NeonHarnessError(f"unexpected RSH-064 {key} identity")
        harness.validate_sha256(dependency.get("sha256"), label=key)
    spin = case.get("reclaim_spin_dependency")
    if (
        not isinstance(spin, dict)
        or spin.get("package") != "spin"
        or spin.get("version") != "0.9.0"
    ):
        raise NeonHarnessError("missing pinned reclaim-arm spin 0.9.0 archive")
    harness.validate_sha256(spin.get("sha256"), label="reclaim_spin_dependency")
    return value


def selected_case(catalog: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    return harness.select_scenario(catalog, "RSH-064-node-addon")


def list_payload(catalog: dict[str, Any], catalog_path: pathlib.Path) -> dict[str, Any]:
    case, scenario = selected_case(catalog)
    return {
        "schema_version": 1,
        "source": "unialloc-rsh064-neon-node-runner",
        "claim_grade": False,
        "catalog": file_record(catalog_path),
        "case_id": case["case_id"],
        "advisory_id": case["advisory_id"],
        "crate": case["crate"],
        "scenario_id": scenario["scenario_id"],
        "host_kind": scenario["host_kind"],
        "allocator_variants": list(VARIANTS),
        "archive_variants": ["vulnerable", "patched"],
        "patched_control": "expected_compile_rejection",
        "actions": ["list", "preflight", "run"],
    }


def clean_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        env.pop(key, None)
    env.update(
        {
            "CARGO_INCREMENTAL": "0",
            "CARGO_TERM_COLOR": "never",
            "LC_ALL": "C",
        }
    )
    return env


def command_record(
    command: list[str],
    *,
    cwd: pathlib.Path,
    output_prefix: pathlib.Path,
    timeout: int,
    env: dict[str, str],
) -> dict[str, Any]:
    started = time.time()
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        returncode: int | None = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as error:
        timed_out = True
        returncode = None
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
    stdout_path = output_prefix.with_suffix(".stdout")
    stderr_path = output_prefix.with_suffix(".stderr")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return {
        "command": command,
        "cwd": str(cwd),
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": time.time() - started,
        "stdout": file_record(stdout_path),
        "stderr": file_record(stderr_path),
    }


def node_version(node: str, timeout: int) -> dict[str, Any]:
    executable = shutil.which(node)
    if executable is None:
        raise NeonHarnessError(f"Node executable not found: {node}")
    completed = subprocess.run(
        [executable, "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip().startswith("v"):
        raise NeonHarnessError(f"Node version probe failed: {completed.stderr.strip()}")
    return {"executable": executable, "version": completed.stdout.strip()}


def cargo_version(toolchain: str, timeout: int) -> dict[str, Any]:
    completed = subprocess.run(
        ["cargo", f"+{toolchain}", "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise NeonHarnessError(
            f"Cargo toolchain probe failed for {toolchain}: {completed.stderr.strip()}"
        )
    return {"toolchain": toolchain, "version": completed.stdout.strip()}


def lock_path(case: dict[str, Any], archive_variant: str, allocator_variant: str) -> pathlib.Path:
    key = (
        "reclaim"
        if allocator_variant in ("reclaim_plain", "reclaim_checks")
        else archive_variant
    )
    record = case["lockfiles"][key]
    return harness.validate_repo_file(record["path"], record["sha256"])


def dependency_for(case: dict[str, Any], archive_variant: str) -> dict[str, Any]:
    return case[f"{archive_variant}_dependency"]


def manifest_text(
    *,
    dependency: dict[str, Any],
    subject: pathlib.Path,
    allocator_variant: str,
    spin_subject: pathlib.Path | None = None,
) -> str:
    relative_subject = pathlib.Path("..") / "subject" / subject.name
    lines = [
        "[package]",
        'name = "rsh-064-harness"',
        'version = "0.0.0"',
        'edition = "2021"',
        "publish = false",
        "",
        "[lib]",
        'crate-type = ["cdylib"]',
        "",
        "[dependencies]",
        (
            "neon = { path = "
            f"{json.dumps(relative_subject.as_posix())}, "
            f"version = {json.dumps('=' + dependency['version'])}, "
            'default-features = false, features = ["napi-6"] }'
        ),
    ]
    if allocator_variant in ("reclaim_plain", "reclaim_checks"):
        if spin_subject is None:
            raise NeonHarnessError("reclaim allocator manifest requires pinned spin source")
        features = ["stats"]
        if allocator_variant == "reclaim_checks":
            features.append("reclaim_checks")
        lines.append(
            "unialloc = { path = "
            f"{json.dumps(str((ROOT / 'unialloc').resolve()))}, "
            f"default-features = false, features = {json.dumps(features)} }}"
        )
        relative_spin = pathlib.Path("..") / "spin" / spin_subject.name
        lines.extend(
            (
                "",
                "[patch.crates-io]",
                f"spin = {{ path = {json.dumps(relative_spin.as_posix())} }}",
            )
        )
    lines.extend(("", "[workspace]", ""))
    return "\n".join(lines)


def allocator_source(base: str, allocator_variant: str) -> str:
    if allocator_variant in ("system", "typed_plain", "typeiso"):
        return base
    return (
        base.rstrip()
        + "\n\nuse unialloc::UniAlloc;\n\n"
        + "#[global_allocator]\nstatic ALLOCATOR: UniAlloc = UniAlloc;\n"
    )


def materialize_arm(
    catalog: dict[str, Any],
    *,
    archive_variant: str,
    allocator_variant: str,
    root: pathlib.Path,
    archive_cache: pathlib.Path,
    allow_download: bool,
) -> dict[str, Any]:
    case, scenario = selected_case(catalog)
    dependency = dependency_for(case, archive_variant)
    archive = harness.stage_archive(dependency, archive_cache, allow_download)
    arm_root = root / f"{archive_variant}-{allocator_variant}"
    subject = harness.safe_extract_crate(archive, arm_root / "subject")
    spin_subject: pathlib.Path | None = None
    spin_archive_record: dict[str, Any] | None = None
    if allocator_variant in ("reclaim_plain", "reclaim_checks"):
        spin_archive = harness.stage_archive(
            case["reclaim_spin_dependency"], archive_cache, allow_download
        )
        spin_subject = harness.safe_extract_crate(spin_archive, arm_root / "spin")
        spin_archive_record = file_record(spin_archive)
    project = arm_root / "project"
    (project / "src").mkdir(parents=True)
    source_record = scenario["source"]
    source_path = harness.validate_repo_file(source_record["path"], source_record["sha256"])
    base_source = source_path.read_text(encoding="utf-8")
    transformed = allocator_source(base_source, allocator_variant)
    materialized_source = project / "src" / "lib.rs"
    materialized_source.write_text(transformed, encoding="utf-8")
    manifest = project / "Cargo.toml"
    manifest.write_text(
        manifest_text(
            dependency=dependency,
            subject=subject,
            allocator_variant=allocator_variant,
            spin_subject=spin_subject,
        ),
        encoding="utf-8",
    )
    selected_lock = lock_path(case, archive_variant, allocator_variant)
    shutil.copy2(selected_lock, project / "Cargo.lock")
    driver_record = scenario["node_driver"]
    driver = harness.validate_repo_file(driver_record["path"], driver_record["sha256"])
    return {
        "root": arm_root,
        "project": project,
        "manifest": manifest,
        "driver": driver,
        "dependency": dependency,
        "archive": file_record(archive),
        "reclaim_spin_archive": spin_archive_record,
        "subject": str(subject),
        "source": file_record(source_path),
        "materialized_source": file_record(materialized_source),
        "source_transformation": (
            "append_selected_global_allocator_only"
            if allocator_variant in ("reclaim_plain", "reclaim_checks")
            else "none"
        ),
        "manifest_record": file_record(manifest),
        "lockfile": file_record(project / "Cargo.lock"),
    }


def planned_arms(variants: tuple[str, ...]) -> list[dict[str, str]]:
    return [
        {
            "archive_variant": "patched",
            "allocator_variant": "system",
            "oracle": "expected_compile_rejection",
        },
        *[
            {
                "archive_variant": "vulnerable",
                "allocator_variant": variant,
                "oracle": "corrupt_external_array_buffer_bytes",
            }
            for variant in variants
        ],
    ]


def parse_node_output(stdout: str, expected_payload: list[int]) -> dict[str, Any]:
    match = NODE_OUTPUT_RE.search(stdout)
    if match is None:
        return {
            "status": "malformed_node_output",
            "observed": False,
            "before": None,
            "after_churn": None,
        }

    def parse_bytes(raw: str) -> list[int]:
        if raw == "":
            return []
        return [int(value) for value in raw.split(",")]

    before = parse_bytes(match.group("before"))
    after = parse_bytes(match.group("after"))
    length = int(match.group("length"))
    valid_shape = length == len(expected_payload) == len(before) == len(after)
    corrupted = valid_shape and (before != expected_payload or after != expected_payload)
    return {
        "status": (
            "corrupt_bytes_observed"
            if corrupted
            else "expected_payload_preserved"
            if valid_shape
            else "malformed_node_output"
        ),
        "observed": corrupted,
        "byte_length": length,
        "expected": expected_payload,
        "before": before,
        "after_churn": after,
    }


def classify_compile_rejection(returncode: int | None, stderr: str) -> dict[str, Any]:
    codes = sorted(set(re.findall(r"error\[(E\d{4})\]", stderr)))
    expected = (
        returncode not in (None, 0)
        and PATCHED_ERROR_CODES <= set(codes)
        and "'static" in stderr
    )
    return {
        "status": (
            "patched_control_compile_rejection_observed"
            if expected
            else "expected_compile_rejection_not_observed"
        ),
        "observed": expected,
        "error_codes": codes,
        "static_bound_observed": "'static" in stderr,
    }


def locate_addon(target_dir: pathlib.Path) -> pathlib.Path:
    candidates = sorted((target_dir / "debug").glob("librsh_064_harness.*"))
    libraries = [
        path
        for path in candidates
        if path.suffix in (".so", ".dylib", ".dll") and path.is_file()
    ]
    if len(libraries) != 1:
        raise NeonHarnessError(
            f"expected exactly one RSH-064 cdylib, observed {len(libraries)}"
        )
    return libraries[0]


def input_fingerprint(catalog_path: pathlib.Path) -> dict[str, str]:
    return {
        "catalog_sha256": sha256_file(catalog_path),
        "runner_sha256": sha256_file(pathlib.Path(__file__).resolve()),
        "unialloc_implementation_sha256": realworld.implementation_digest(),
    }


def preflight_payload(
    catalog: dict[str, Any],
    *,
    catalog_path: pathlib.Path,
    variants: tuple[str, ...],
    archive_cache: pathlib.Path,
    allow_download: bool,
    node: str,
    toolchain: str,
    timeout: int,
) -> dict[str, Any]:
    host = {
        "node": node_version(node, timeout),
        "cargo": cargo_version(toolchain, timeout),
        "platform": sys.platform,
    }
    if sys.platform != "linux":
        raise NeonHarnessError("RSH-064 Node addon evidence is validated on Linux")
    materialized: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="rsh064-preflight-") as temporary:
        temporary_root = pathlib.Path(temporary)
        for arm in planned_arms(variants):
            record = materialize_arm(
                catalog,
                archive_variant=arm["archive_variant"],
                allocator_variant=arm["allocator_variant"],
                root=temporary_root,
                archive_cache=archive_cache,
                allow_download=allow_download,
            )
            materialized.append(
                {
                    **arm,
                    "archive": record["archive"],
                    "reclaim_spin_archive": record["reclaim_spin_archive"],
                    "source": record["source"],
                    "materialized_source_sha256": record["materialized_source"]["sha256"],
                    "source_transformation": record["source_transformation"],
                    "manifest_sha256": record["manifest_record"]["sha256"],
                    "lockfile": record["lockfile"],
                }
            )
    return {
        "schema_version": 1,
        "source": "unialloc-rsh064-neon-node-preflight",
        "claim_grade": False,
        "scenario_id": "RSH-064-node-addon",
        "catalog": file_record(catalog_path),
        "input_fingerprint": input_fingerprint(catalog_path),
        "host": host,
        "unsafe_execution_requested": False,
        "variants": list(variants),
        "planned_arms": materialized,
        "valid": True,
    }


def run_arm(
    catalog: dict[str, Any],
    *,
    arm: dict[str, str],
    work_root: pathlib.Path,
    evidence_root: pathlib.Path,
    archive_cache: pathlib.Path,
    allow_download: bool,
    node: str,
    toolchain: str,
    repetitions: int,
    build_timeout: int,
    run_timeout: int,
    offline: bool,
    typeiso_tools: dict[str, Any] | None,
) -> dict[str, Any]:
    archive_variant = arm["archive_variant"]
    allocator_variant = arm["allocator_variant"]
    materialized = materialize_arm(
        catalog,
        archive_variant=archive_variant,
        allocator_variant=allocator_variant,
        root=work_root,
        archive_cache=archive_cache,
        allow_download=allow_download,
    )
    arm_id = f"{archive_variant}-{allocator_variant}"
    arm_evidence = evidence_root / "arms" / arm_id
    target_dir = materialized["root"] / "target"
    build_command = [
        "cargo",
        f"+{toolchain}",
        "build",
        "--locked",
        "--manifest-path",
        str(materialized["manifest"]),
        "--target-dir",
        str(target_dir),
    ]
    if offline:
        build_command.append("--offline")
    build_environment = clean_environment()
    audit_dir: pathlib.Path | None = None
    pass_log_dir: pathlib.Path | None = None
    if allocator_variant in ("typed_plain", "typeiso"):
        if typeiso_tools is None:
            raise NeonHarnessError("missing Type Isolation compiler tools")
        audit_dir = materialized["root"] / "audits"
        pass_log_dir = materialized["root"] / "pass-logs"
        audit_dir.mkdir()
        pass_log_dir.mkdir()
        build_environment = rustsec_experiment.typeiso_environment(
            build_environment,
            tools=typeiso_tools,
            audit_dir=audit_dir,
            pass_log_dir=pass_log_dir,
            target_crates=("neon", "rsh_064_harness"),
            policy_flags=0 if allocator_variant == "typed_plain" else 1,
        )
    build = command_record(
        build_command,
        cwd=materialized["project"],
        output_prefix=arm_evidence / "build",
        timeout=build_timeout,
        env=build_environment,
    )
    result: dict[str, Any] = {
        "archive_variant": archive_variant,
        "allocator_variant": allocator_variant,
        "oracle": arm["oracle"],
        "archive": materialized["archive"],
        "reclaim_spin_archive": materialized["reclaim_spin_archive"],
        "source": materialized["source"],
        "materialized_source": materialized["materialized_source"],
        "source_transformation": materialized["source_transformation"],
        "manifest_sha256": materialized["manifest_record"]["sha256"],
        "lockfile": materialized["lockfile"],
        "build": build,
        "executions": [],
        "claim_grade": False,
    }
    if audit_dir is not None and pass_log_dir is not None:
        copied_audits: list[dict[str, Any]] = []
        copied_logs: list[dict[str, Any]] = []
        audit_evidence = arm_evidence / "audits"
        pass_log_evidence = arm_evidence / "pass-logs"
        audit_evidence.mkdir(parents=True, exist_ok=True)
        pass_log_evidence.mkdir(parents=True, exist_ok=True)
        harness_audit: dict[str, Any] | None = None
        for source in sorted(audit_dir.glob("*.json")):
            destination = audit_evidence / source.name
            shutil.copy2(source, destination)
            copied_audits.append(file_record(destination))
            if source.name.startswith("rsh_064_harness-"):
                value = json.loads(source.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    harness_audit = value
        for source in sorted(pass_log_dir.glob("*.log")):
            destination = pass_log_evidence / source.name
            shutil.copy2(source, destination)
            copied_logs.append(file_record(destination))
        result["compiler_evidence"] = {
            "policy_flags": 0 if allocator_variant == "typed_plain" else 1,
            "audit_files": copied_audits,
            "pass_log_files": copied_logs,
            "harness_summary": (
                harness_audit.get("summary", {})
                if harness_audit is not None
                else None
            ),
            "harness_compiler_pass": (
                harness_audit.get("compiler_pass", {})
                if harness_audit is not None
                else None
            ),
        }
    build_stderr = pathlib.Path(build["stderr"]["path"])
    if not build_stderr.is_absolute():
        build_stderr = ROOT / build_stderr
    stderr = build_stderr.read_text(encoding="utf-8")
    if archive_variant == "patched":
        result["oracle_validation"] = classify_compile_rejection(
            build["returncode"], stderr
        )
        result["execution_status"] = "completed"
        return result
    if build["returncode"] != 0 or build["timed_out"]:
        result["execution_status"] = "build_failed"
        result["oracle_validation"] = {
            "status": "vulnerable_addon_build_failed",
            "observed": False,
        }
        return result
    addon_library = locate_addon(target_dir)
    addon = materialized["root"] / "rsh064.node"
    shutil.copy2(addon_library, addon)
    result["addon"] = file_record(addon)
    case, scenario = selected_case(catalog)
    _ = case
    expected_payload = scenario["expected_payload"]
    observations: list[dict[str, Any]] = []
    signal_count = 0
    for repetition in range(1, repetitions + 1):
        execution = command_record(
            [node, str(materialized["driver"]), str(addon)],
            cwd=materialized["root"],
            output_prefix=arm_evidence / f"run-{repetition:03d}",
            timeout=run_timeout,
            env=clean_environment(),
        )
        stdout_path = pathlib.Path(execution["stdout"]["path"])
        stderr_path = pathlib.Path(execution["stderr"]["path"])
        if not stdout_path.is_absolute():
            stdout_path = ROOT / stdout_path
        if not stderr_path.is_absolute():
            stderr_path = ROOT / stderr_path
        stdout = stdout_path.read_text(encoding="utf-8")
        run_stderr = stderr_path.read_text(encoding="utf-8")
        oracle = parse_node_output(stdout, expected_payload)
        combined_output = stdout + "\n" + run_stderr
        diagnostic = bool(RECLAIM_SIGNAL_RE.search(combined_output))
        reuse_denial = bool(REUSE_DENIAL_RE.search(combined_output))
        signal_count += diagnostic
        observations.append(
            {
                "repetition": repetition,
                "execution": execution,
                "oracle": oracle,
                "exact_reclaim_signal_observed": diagnostic,
                "reuse_denial_signal_observed": reuse_denial,
            }
        )
    result["executions"] = observations
    completed = sum(
        observation["execution"]["returncode"] == 0
        and not observation["execution"]["timed_out"]
        for observation in observations
    )
    corrupt = sum(observation["oracle"]["observed"] for observation in observations)
    result["repetition_summary"] = {
        "requested": repetitions,
        "completed": completed,
        "corrupt_bytes_observed": corrupt,
        "exact_reclaim_signal_observed": signal_count,
        "reuse_denial_signal_observed": sum(
            observation["reuse_denial_signal_observed"]
            for observation in observations
        ),
    }
    result["oracle_validation"] = {
        "status": (
            "corrupt_bytes_observed_in_all_repetitions"
            if corrupt == repetitions and completed == repetitions
            else "corrupt_bytes_or_execution_incomplete"
        ),
        "observed": corrupt == repetitions and completed == repetitions,
        "expected_payload": expected_payload,
    }
    result["execution_status"] = "completed"
    return result


def run_experiment(
    catalog: dict[str, Any],
    *,
    catalog_path: pathlib.Path,
    variants: tuple[str, ...],
    archive_cache: pathlib.Path,
    allow_download: bool,
    output_dir: pathlib.Path,
    node: str,
    toolchain: str,
    repetitions: int,
    build_timeout: int,
    run_timeout: int,
    offline: bool,
) -> dict[str, Any]:
    before = input_fingerprint(catalog_path)
    arms: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="rsh064-run-", dir=output_dir) as temporary:
        work_root = pathlib.Path(temporary)
        typeiso_tools = (
            rustsec_experiment.prepare_typeiso_tools(
                archive_cache / "rsh064-typeiso-tools",
                jobs=4,
                timeout=build_timeout,
            )
            if any(variant in ("typed_plain", "typeiso") for variant in variants)
            else None
        )
        for arm in planned_arms(variants):
            arms.append(
                run_arm(
                    catalog,
                    arm=arm,
                    work_root=work_root,
                    evidence_root=output_dir,
                    archive_cache=archive_cache,
                    allow_download=allow_download,
                    node=node,
                    toolchain=toolchain,
                    repetitions=repetitions,
                    build_timeout=build_timeout,
                    run_timeout=run_timeout,
                    offline=offline,
                    typeiso_tools=typeiso_tools,
                )
            )
    after = input_fingerprint(catalog_path)
    drift = {
        key: {"before": value, "after": after.get(key)}
        for key, value in before.items()
        if after.get(key) != value
    }
    patched = next(
        arm
        for arm in arms
        if arm["archive_variant"] == "patched"
        and arm["allocator_variant"] == "system"
    )
    vulnerable_by_allocator = {
        arm["allocator_variant"]: arm
        for arm in arms
        if arm["archive_variant"] == "vulnerable"
    }
    baseline = vulnerable_by_allocator.get("system")
    baseline_reproduced = bool(
        baseline and baseline["oracle_validation"].get("observed") is True
    )
    patched_rejected = patched["oracle_validation"].get("observed") is True
    reclaim_plain = vulnerable_by_allocator.get("reclaim_plain")
    reclaim_checks = vulnerable_by_allocator.get("reclaim_checks")
    typed_plain = vulnerable_by_allocator.get("typed_plain")
    typeiso = vulnerable_by_allocator.get("typeiso")
    reclaim_complete = bool(
        reclaim_plain
        and reclaim_checks
        and reclaim_plain["oracle_validation"].get("observed") is True
        and reclaim_checks["oracle_validation"].get("observed") is True
    )
    exact_signal_count = sum(
        arm.get("repetition_summary", {}).get(
            "exact_reclaim_signal_observed", 0
        )
        for arm in (reclaim_plain, reclaim_checks)
        if arm is not None
    )
    typed_pair_complete = bool(
        typed_plain
        and typeiso
        and typed_plain["oracle_validation"].get("observed") is True
        and typeiso["oracle_validation"].get("observed") is True
    )
    typeiso_summary = (
        typeiso.get("compiler_evidence", {}).get("harness_summary")
        if typeiso is not None
        else None
    )
    critical_site_coverage = bool(
        isinstance(typeiso_summary, dict)
        and typeiso_summary.get("complete_for_claim") is True
        and int(typeiso_summary.get("rewrite_applied_count", 0) or 0) > 0
    )
    reuse_denial_count = int(
        typeiso.get("repetition_summary", {}).get(
            "reuse_denial_signal_observed", 0
        )
        if typeiso is not None
        else 0
    )
    unexpected = int(not patched_rejected) + int("system" in variants and not baseline_reproduced)
    unexpected += int(
        reclaim_plain is not None
        and reclaim_plain["oracle_validation"].get("observed") is not True
    )
    unexpected += int(
        reclaim_checks is not None
        and reclaim_checks["oracle_validation"].get("observed") is not True
    )
    unexpected += len(drift)
    return {
        "schema_version": 1,
        "source": "unialloc-rsh064-neon-node-experiment",
        "claim_grade": False,
        "mitigation_inferred": False,
        "case_id": "RSH-064",
        "advisory_id": "RUSTSEC-2022-0028",
        "scenario_id": "RSH-064-node-addon",
        "catalog": file_record(catalog_path),
        "input_fingerprint_before": before,
        "input_fingerprint_after": after,
        "input_drift": drift,
        "unsafe_execution_requested": True,
        "variants": list(variants),
        "repetitions_requested": repetitions,
        "arms": arms,
        "checks": {
            "vulnerable_system_corrupt_bytes_reproduced": baseline_reproduced,
            "patched_static_bound_compile_rejection_observed": patched_rejected,
            "feature_matched_reclaim_arms_reproduced_uaf": reclaim_complete,
            "exact_reclaim_signal_count": exact_signal_count,
        },
        "mechanism_evaluation": {
            "mechanism": "reclaim_checks",
            "outcome": (
                "no_signal"
                if reclaim_complete and exact_signal_count == 0
                else "detected"
                if reclaim_complete and exact_signal_count > 0
                else "not_evaluated"
            ),
            "reason": (
                "single_valid_free_followed_by_stale_v8_read_without_duplicate_reclaim"
                if reclaim_complete and exact_signal_count == 0
                else "exact_reclaim_diagnostic_observed"
                if reclaim_complete
                else "feature_matched_reclaim_arms_not_complete"
            ),
        },
        "type_isolation_evaluation": {
            "outcome": (
                "inconclusive"
                if typed_pair_complete and not critical_site_coverage
                else "detected"
                if typed_pair_complete and critical_site_coverage and reuse_denial_count > 0
                else "no_signal"
                if typed_pair_complete and critical_site_coverage
                else "not_evaluated"
            ),
            "reason": (
                "compiler_critical_site_coverage_not_validated"
                if typed_pair_complete and not critical_site_coverage
                else "exact_cross_identity_reuse_denial_observed"
                if typed_pair_complete and reuse_denial_count > 0
                else "complete_compiler_coverage_without_reuse_denial"
                if typed_pair_complete and critical_site_coverage
                else "feature_matched_type_isolation_arms_not_complete"
            ),
            "typed_plain_and_typeiso_uaf_reproduced": typed_pair_complete,
            "compiler_critical_site_coverage_validated": critical_site_coverage,
            "reuse_denial_signal_count": reuse_denial_count,
            "harness_compiler_summary": typeiso_summary,
        },
        "unexpected_arm_count": unexpected,
        "orchestration_success": unexpected == 0,
        "claim_boundary": (
            "The system arm reproduces the source UAF symptom and the patched "
            "archive rejects the same source. Direct UniAlloc reclaim arms test "
            "duplicate-reclaim diagnostics. The typed compiler pair remains "
            "inconclusive unless the critical Vec/external allocation path is rewritten."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=pathlib.Path, default=DEFAULT_CATALOG)
    parser.add_argument("--action", choices=("list", "preflight", "run"), default="list")
    parser.add_argument("--variants", default=",".join(VARIANTS))
    parser.add_argument("--output-dir", type=pathlib.Path)
    parser.add_argument("--cache", type=pathlib.Path)
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--execute-unsafe", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--node", default="node")
    parser.add_argument("--toolchain")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--build-timeout", type=int, default=900)
    parser.add_argument("--run-timeout", type=int, default=60)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        catalog_path = args.catalog.expanduser().resolve()
        catalog = load_catalog(catalog_path)
        variants = parse_csv(args.variants, VARIANTS, "allocator variants")
        if args.repetitions <= 0:
            raise NeonHarnessError("repetitions must be positive")
        if args.build_timeout <= 0 or args.run_timeout <= 0:
            raise NeonHarnessError("timeouts must be positive")
        toolchain = args.toolchain or str(catalog["toolchain"])
        archive_cache = cache_root(args.cache)
        if args.action == "list":
            print(json.dumps(list_payload(catalog, catalog_path), indent=2, sort_keys=True))
            return 0
        if args.output_dir is None:
            raise NeonHarnessError("--output-dir is required for preflight and run")
        if args.action == "run" and not args.execute_unsafe:
            raise NeonHarnessError("run requires explicit --execute-unsafe opt-in")
        output_dir = args.output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        preflight = preflight_payload(
            catalog,
            catalog_path=catalog_path,
            variants=variants,
            archive_cache=archive_cache,
            allow_download=args.allow_download,
            node=args.node,
            toolchain=toolchain,
            timeout=min(args.build_timeout, 60),
        )
        write_json(output_dir / "preflight.json", preflight)
        if args.action == "preflight":
            print(json.dumps(preflight, indent=2, sort_keys=True))
            return 0
        arms_dir = output_dir / "arms"
        if arms_dir.exists():
            shutil.rmtree(arms_dir)
        experiment = run_experiment(
            catalog,
            catalog_path=catalog_path,
            variants=variants,
            archive_cache=archive_cache,
            allow_download=args.allow_download,
            output_dir=output_dir,
            node=args.node,
            toolchain=toolchain,
            repetitions=args.repetitions,
            build_timeout=args.build_timeout,
            run_timeout=args.run_timeout,
            offline=args.offline,
        )
        write_json(output_dir / "experiment.json", experiment)
        print(json.dumps(experiment, indent=2, sort_keys=True))
        return 0 if experiment["orchestration_success"] else 1
    except (
        NeonHarnessError,
        harness.HarnessError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
