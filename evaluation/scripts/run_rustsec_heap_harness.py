#!/usr/bin/env python3
"""Materialize and contain pinned RustSec heap-safety harnesses.

The default action only lists the catalog.  Source archives are downloaded
only with ``--allow-download``.  Vulnerable programs execute only with both
``--action run-native`` and ``--execute-unsafe``; build and run occur in
separate containers, and the run container has no network or writable root.
ASan and Miri scenarios are materialized by this command and retain their
catalog-pinned tool/oracle metadata for the dedicated sanitizer environment.
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
import tarfile
import tempfile
import urllib.request
import uuid
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = ROOT / "evaluation" / "config" / "rustsec_heap_harnesses.json"
DEFAULT_IMAGE = (
    "rust:1.85-slim@sha256:"
    "9f841bbe9e7d8e37ceb96ed907265a3a0df7f44e3737d0b100e7907a679acb36"
)
MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
COMPATIBILITY_RUSTFLAGS = [
    "-Ainvalid_reference_casting",
    "-Adangerous_implicit_autorefs",
]
SCENARIO_RE = re.compile(r"^RSH-\d{3}-[a-z0-9-]+$")
CASE_RE = re.compile(r"^RSH-\d{3}$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BUILD_ENVIRONMENT_ALLOWLIST = {"CC", "CFLAGS", "RUSTC_BOOTSTRAP"}
RUN_ENVIRONMENT_ALLOWLIST = {"ASAN_OPTIONS", "RUST_BACKTRACE"}
DEFAULT_BUILD_TIMEOUT = 900
CARGO_HOME_OVERLAY_SCRIPT = """\
set -eu
for component in registry git; do
    if [ -e "/cargo-cache-ro/$component" ]; then
        ln -s "/cargo-cache-ro/$component" "/cargo-home/$component"
    fi
done
exec "$@"
"""


class HarnessError(RuntimeError):
    """A deterministic catalog, source, or containment error."""


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise HarnessError(f"invalid SHA-256 for {label}")
    return value


def load_catalog(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise HarnessError("unsupported harness catalog")
    return value


def index_catalog(
    catalog: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[dict[str, Any], dict[str, Any]]]]:
    cases: dict[str, dict[str, Any]] = {}
    scenarios: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    raw_cases = catalog.get("cases", [])
    if not isinstance(raw_cases, list):
        raise HarnessError("catalog cases must be a list")
    for case in raw_cases:
        if not isinstance(case, dict):
            raise HarnessError("catalog case must be an object")
        case_id = str(case.get("case_id", ""))
        if not CASE_RE.fullmatch(case_id):
            raise HarnessError(f"invalid harness case id: {case_id!r}")
        if case_id in cases:
            raise HarnessError(f"duplicate harness case: {case_id}")
        cases[case_id] = case
        raw_scenarios = case.get("scenarios", [])
        if not isinstance(raw_scenarios, list):
            raise HarnessError(f"scenarios must be a list for {case_id}")
        for scenario in raw_scenarios:
            if not isinstance(scenario, dict):
                raise HarnessError(f"scenario must be an object for {case_id}")
            scenario_id = str(scenario.get("scenario_id", ""))
            if not SCENARIO_RE.fullmatch(scenario_id):
                raise HarnessError(f"invalid scenario id: {scenario_id!r}")
            if scenario_id in scenarios:
                raise HarnessError(f"duplicate scenario: {scenario_id}")
            scenarios[scenario_id] = (case, scenario)
    return cases, scenarios


def select_scenario(
    catalog: dict[str, Any], scenario_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    _, scenarios = index_catalog(catalog)
    try:
        return scenarios[scenario_id]
    except KeyError as error:
        raise HarnessError(f"unknown scenario: {scenario_id}") from error


def validate_repo_file(path_text: str, expected_sha256: str) -> pathlib.Path:
    expected_sha256 = validate_sha256(expected_sha256, label=path_text)
    relative = pathlib.PurePosixPath(path_text)
    if (
        not path_text
        or relative.is_absolute()
        or ".." in relative.parts
        or "\\" in path_text
    ):
        raise HarnessError(f"invalid repository-relative path: {path_text!r}")
    path = (ROOT / pathlib.Path(*relative.parts)).resolve()
    try:
        path.relative_to(ROOT)
    except ValueError as error:
        raise HarnessError(f"repository path escapes root: {path_text}") from error
    if not path.is_file():
        raise HarnessError(f"missing repository file: {path_text}")
    observed = sha256_file(path)
    if observed != expected_sha256:
        raise HarnessError(
            f"repository file hash mismatch for {path_text}: "
            f"expected {expected_sha256}, observed {observed}"
        )
    return path


def cache_root_from_args(args: argparse.Namespace) -> pathlib.Path:
    if args.cache:
        return args.cache.expanduser().resolve()
    base = pathlib.Path(os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache"))
    return (base / "unialloc" / "rustsec-heap").resolve()


def stage_archive(
    dependency: dict[str, Any], cache_root: pathlib.Path, allow_download: bool
) -> pathlib.Path:
    package = dependency["package"]
    version = dependency["version"]
    if not isinstance(package, str) or not PACKAGE_RE.fullmatch(package):
        raise HarnessError(f"invalid archive package name: {package!r}")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise HarnessError(f"invalid archive version: {version!r}")
    expected_bytes = dependency["bytes"]
    if (
        not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes <= 0
    ):
        raise HarnessError(f"invalid archive byte count for {package} {version}")
    expected_sha256 = validate_sha256(
        dependency["sha256"], label=f"{package} {version} archive"
    )
    archive_dir = cache_root / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{package}-{version}.crate"
    if not path.is_file():
        if not allow_download:
            raise HarnessError(
                f"missing {path}; rerun with --allow-download to fetch the pinned archive"
            )
        partial = path.with_suffix(".crate.partial")
        try:
            with urllib.request.urlopen(dependency["url"], timeout=60) as response:
                payload = response.read(expected_bytes + 1)
            partial.write_bytes(payload)
            if len(payload) != expected_bytes:
                raise HarnessError(
                    f"downloaded archive byte-count mismatch for {package} {version}"
                )
            observed = sha256_file(partial)
            if observed != expected_sha256:
                raise HarnessError(
                    f"downloaded archive SHA-256 mismatch for {package} {version}: "
                    f"expected {expected_sha256}, observed {observed}"
                )
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    if path.stat().st_size != expected_bytes:
        raise HarnessError(f"archive byte-count mismatch: {path}")
    observed = sha256_file(path)
    if observed != expected_sha256:
        raise HarnessError(
            f"archive SHA-256 mismatch for {path}: expected "
            f"{expected_sha256}, observed {observed}"
        )
    return path


def safe_extract_crate(archive: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(archive, "r:gz") as crate:
            members = crate.getmembers()
            if not members or len(members) > MAX_ARCHIVE_MEMBERS:
                raise HarnessError(f"invalid archive member count: {archive}")
            unpacked_bytes = 0
            top_levels: set[str] = set()
            for member in members:
                relative = pathlib.PurePosixPath(member.name)
                if (
                    not member.name
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or not relative.parts
                    or relative.parts[0] in ("", ".")
                ):
                    raise HarnessError(f"unsafe archive member: {member.name}")
                if not (member.isdir() or member.isfile()):
                    raise HarnessError(
                        f"unsupported archive member type: {member.name}"
                    )
                unpacked_bytes += member.size
                if unpacked_bytes > MAX_ARCHIVE_UNPACKED_BYTES:
                    raise HarnessError(f"archive expands beyond safety limit: {archive}")
                top_levels.add(relative.parts[0])
            if len(top_levels) != 1:
                raise HarnessError(
                    f"crate archive must contain one root directory: {archive}"
                )
            crate.extractall(destination, members=members, filter="data")
        root_name = next(iter(top_levels))
        root = destination / root_name
        top_level_items = list(destination.iterdir())
        if top_level_items != [root] or not root.is_dir():
            raise HarnessError(
                f"crate archive must contain one root directory: {archive}"
            )
        return root
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def validate_subject_file(
    subject: pathlib.Path, path_text: str, expected_sha256: str
) -> pathlib.Path:
    expected_sha256 = validate_sha256(expected_sha256, label=path_text)
    relative = pathlib.PurePosixPath(path_text)
    if (
        not path_text
        or relative.is_absolute()
        or ".." in relative.parts
        or "\\" in path_text
    ):
        raise HarnessError(f"invalid patched subject path: {path_text!r}")
    subject_root = subject.resolve()
    path = (subject_root / pathlib.Path(*relative.parts)).resolve()
    try:
        path.relative_to(subject_root)
    except ValueError as error:
        raise HarnessError(f"patched subject path escapes root: {path_text}") from error
    if not path.is_file():
        raise HarnessError(f"missing patched subject file: {path_text}")
    observed = sha256_file(path)
    if observed != expected_sha256:
        raise HarnessError(
            f"patched subject hash mismatch for {path_text}: "
            f"expected {expected_sha256}, observed {observed}"
        )
    return path


def apply_local_patch(subject: pathlib.Path, patch_meta: dict[str, Any]) -> None:
    if patch_meta.get("kind") != "hashed_local_patch":
        raise HarnessError("local patch must use hashed_local_patch metadata")
    patch_path = validate_repo_file(patch_meta["path"], patch_meta["sha256"])
    completed = subprocess.run(
        ["patch", "--batch", "--forward", "-p1", "-i", str(patch_path)],
        cwd=subject,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if completed.returncode != 0:
        raise HarnessError(
            f"local patch failed for {subject}:\n{completed.stdout.rstrip()}"
        )
    resulting_files = patch_meta.get("resulting_source_files")
    if not isinstance(resulting_files, dict) or not resulting_files:
        raise HarnessError("local patch must pin resulting_source_files")
    for path_text, expected_sha256 in sorted(resulting_files.items()):
        if not isinstance(path_text, str):
            raise HarnessError("patched subject paths must be strings")
        validate_subject_file(subject, path_text, expected_sha256)


def apply_subject_patches(
    subject: pathlib.Path,
    scenario: dict[str, Any],
    variant: str,
) -> list[dict[str, Any]]:
    """Apply hashed adapter patches shared by selected archive variants."""

    patches = scenario.get("subject_patches", [])
    if not isinstance(patches, list):
        raise HarnessError("subject_patches must be a list")
    applied: list[dict[str, Any]] = []
    for index, patch_meta in enumerate(patches):
        if not isinstance(patch_meta, dict):
            raise HarnessError(f"subject patch {index} must be an object")
        if patch_meta.get("kind") != "hashed_subject_patch":
            raise HarnessError(f"subject patch {index} has an invalid kind")
        variants = patch_meta.get("apply_variants")
        if (
            not isinstance(variants, list)
            or not variants
            or any(
                not isinstance(item, str)
                or item not in ("vulnerable", "patched")
                for item in variants
            )
            or len(set(variants)) != len(variants)
        ):
            raise HarnessError(f"subject patch {index} has invalid apply_variants")
        results_by_variant = patch_meta.get("resulting_source_files_by_variant")
        if not isinstance(results_by_variant, dict):
            raise HarnessError(
                f"subject patch {index} must pin results for every selected variant"
            )
        for selected_variant in variants:
            results = results_by_variant.get(selected_variant)
            if not isinstance(results, dict) or not results:
                raise HarnessError(
                    f"subject patch {index} is missing pinned {selected_variant} results"
                )
        if variant not in variants:
            continue
        selected_patch = {
            "kind": "hashed_local_patch",
            "label": patch_meta.get("label"),
            "path": patch_meta.get("path"),
            "sha256": patch_meta.get("sha256"),
            "resulting_source_files": results_by_variant[variant],
        }
        apply_local_patch(subject, selected_patch)
        applied.append(
            {
                "kind": patch_meta["kind"],
                "label": patch_meta.get("label"),
                "path": patch_meta.get("path"),
                "sha256": patch_meta.get("sha256"),
                "variant": variant,
                "resulting_source_files": results_by_variant[variant],
            }
        )
    return applied


def cargo_toml(case: dict[str, Any], dependency: dict[str, Any], subject: pathlib.Path) -> str:
    cargo = case["cargo"]
    variant = dependency["_variant"]
    dependency_path = pathlib.Path("..") / "subject" / subject.name
    crate_name = case["crate"]
    if not isinstance(crate_name, str) or not PACKAGE_RE.fullmatch(crate_name):
        raise HarnessError(f"invalid crate name: {crate_name!r}")
    features_value = cargo["features"]
    if not isinstance(features_value, list) or not all(
        isinstance(feature, str) for feature in features_value
    ):
        raise HarnessError(f"invalid Cargo features for {case['case_id']}")
    default_features = cargo["default_features"]
    if not isinstance(default_features, bool):
        raise HarnessError(f"invalid default_features for {case['case_id']}")
    extra_dependencies = cargo.get(f"{variant}_extra_dependencies")
    if not isinstance(extra_dependencies, dict):
        raise HarnessError(
            f"missing {variant} extra dependencies for {case['case_id']}"
        )
    features = json.dumps(features_value)
    lines = [
        "[package]",
        f"name = {json.dumps(case['case_id'].lower() + '-harness')}",
        'version = "0.0.0"',
        'edition = "2021"',
        "publish = false",
        "",
        "[dependencies]",
        (
            f"{json.dumps(crate_name)} = {{ path = "
            f"{json.dumps(dependency_path.as_posix())}, "
            f'default-features = {str(default_features).lower()}, '
            f'features = {features} }}'
        ),
    ]
    for name, requirement in sorted(extra_dependencies.items()):
        if not isinstance(name, str) or not PACKAGE_RE.fullmatch(name):
            raise HarnessError(f"invalid extra dependency name: {name!r}")
        if not isinstance(requirement, str):
            raise HarnessError(f"invalid dependency requirement for {name}")
        lines.append(f"{json.dumps(name)} = {json.dumps(requirement)}")
    lines.extend(
        ["", "[profile.release]", "overflow-checks = false", "", "[workspace]", ""]
    )
    return "\n".join(lines)


def materialize(
    catalog: dict[str, Any],
    scenario_id: str,
    variant: str,
    work: pathlib.Path,
    cache_root: pathlib.Path,
    allow_download: bool,
) -> dict[str, Any]:
    case, scenario = select_scenario(catalog, scenario_id)
    if variant not in ("vulnerable", "patched"):
        raise HarnessError(f"invalid materialization variant: {variant}")
    if work.exists():
        if not work.is_dir():
            raise HarnessError(f"work path must be a directory: {work}")
        if any(work.iterdir()):
            raise HarnessError(f"work directory must be empty: {work}")
    work.mkdir(parents=True, exist_ok=True)

    local_patch = scenario.get("local_patched_control") if variant == "patched" else None
    if variant == "vulnerable" or local_patch is not None:
        dependency = dict(case["vulnerable_dependency"])
    else:
        patched = case["patched_dependency"]
        if patched.get("kind") != "crates_io_archive":
            raise HarnessError(f"scenario has no executable patched control: {scenario_id}")
        dependency = dict(patched)
    dependency["_variant"] = variant

    archive = stage_archive(dependency, cache_root, allow_download)
    subject_parent = work / "subject"
    subject = safe_extract_crate(archive, subject_parent)
    subject_patches = apply_subject_patches(subject, scenario, variant)
    if local_patch is not None:
        apply_local_patch(subject, local_patch)

    source_meta = scenario.get("patched_source") if variant == "patched" else None
    source_meta = source_meta or {
        "source_path": scenario["source_path"],
        "source_sha256": scenario["source_sha256"],
    }
    if not isinstance(source_meta, dict):
        raise HarnessError(f"invalid source metadata for {scenario_id}")
    source = validate_repo_file(source_meta["source_path"], source_meta["source_sha256"])
    project = work / "project"
    (project / "src").mkdir(parents=True)
    shutil.copy2(source, project / "src" / "main.rs")
    (project / "Cargo.toml").write_text(
        cargo_toml(case, dependency, subject), encoding="utf-8"
    )

    lock_meta = case["lockfiles"][variant]
    lock = validate_repo_file(lock_meta["path"], lock_meta["sha256"])
    shutil.copy2(lock, project / "Cargo.lock")
    scenario_rustflags = scenario["rustflags"]
    if not isinstance(scenario_rustflags, list) or not all(
        isinstance(flag, str) for flag in scenario_rustflags
    ):
        raise HarnessError(f"invalid rustflags for {scenario_id}")
    rustflags = list(dict.fromkeys(COMPATIBILITY_RUSTFLAGS + scenario_rustflags))
    cargo_config = project / ".cargo" / "config.toml"
    cargo_config.parent.mkdir()
    cargo_config.write_text(
        "[build]\nrustflags = " + json.dumps(rustflags) + "\n", encoding="utf-8"
    )

    report = {
        "schema_version": 1,
        "source": "unialloc-rustsec-heap-materialization",
        "scenario_id": scenario_id,
        "case_id": case["case_id"],
        "variant": variant,
        "claim_grade": False,
        "work_directory": str(work),
        "source_path": source_meta["source_path"],
        "source_sha256": source_meta["source_sha256"],
        "archive_path": str(archive),
        "archive_bytes": dependency["bytes"],
        "archive_sha256": dependency["sha256"],
        "dependency_package": dependency["package"],
        "dependency_version": dependency["version"],
        "subject_directory": str(subject),
        "cargo_lock_path": lock_meta["path"],
        "cargo_lock_sha256": lock_meta["sha256"],
        "oracle": scenario["oracle"],
        "required_environment": scenario["required_environment"],
        "rustflags": rustflags,
    }
    if local_patch is not None:
        report["local_patched_control"] = local_patch
    if subject_patches:
        report["subject_patches"] = subject_patches
    (work / "materialization.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def docker_mount(source: pathlib.Path, destination: str, *, readonly: bool = False) -> str:
    source = source.resolve()
    if "," in str(source):
        raise HarnessError(f"Docker mount source contains an unsupported comma: {source}")
    value = f"type=bind,src={source},dst={destination}"
    if readonly:
        value += ",readonly"
    return value


def container_environment_args(
    required_environment: dict[str, Any] | None,
    *,
    phase: str,
) -> list[str]:
    """Translate cataloged process variables through a strict phase allowlist.

    Lower-case entries such as ``platform`` describe environment requirements;
    they are retained in materialization metadata and never injected into a
    container process.
    """

    if required_environment is None:
        return []
    if not isinstance(required_environment, dict):
        raise HarnessError("required_environment must be an object")
    if phase == "build":
        allowlist = BUILD_ENVIRONMENT_ALLOWLIST
    elif phase == "run":
        allowlist = RUN_ENVIRONMENT_ALLOWLIST
    else:
        raise HarnessError(f"invalid container environment phase: {phase}")
    arguments: list[str] = []
    for name, value in sorted(required_environment.items()):
        if name not in allowlist:
            continue
        if not isinstance(value, str) or "\0" in value or len(value) > 4096:
            raise HarnessError(f"invalid environment value for {name}")
        arguments.extend(["-e", f"{name}={value}"])
    return arguments


def docker_base(
    image: str,
    cache_root: pathlib.Path,
    work: pathlib.Path,
    *,
    container_name: str,
    cache_access: str,
    network_disabled: bool,
    required_environment: dict[str, Any] | None = None,
) -> list[str]:
    cargo_cache = cache_root / "cargo"
    cargo_cache.mkdir(parents=True, exist_ok=True)
    command = [
        "docker",
        "run",
        "--name",
        container_name,
        "--rm",
        "--init",
        "--cpus=2",
        "--memory=4g",
        "--pids-limit=256",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=512m",
        "--mount",
        docker_mount(work, "/work"),
        "--mount",
        docker_mount(work / "subject", "/work/subject", readonly=True),
        "--workdir",
        "/work/project",
    ]
    if network_disabled:
        command.extend(["--network", "none"])
    if cache_access == "fetch-rw":
        command.extend(
            [
                "-e",
                "CARGO_HOME=/cargo-cache",
                "-e",
                "HOME=/cargo-cache",
                "--mount",
                docker_mount(cargo_cache, "/cargo-cache"),
            ]
        )
    elif cache_access == "offline-ro":
        command.extend(
            [
                "-e",
                "CARGO_HOME=/cargo-home",
                "-e",
                "HOME=/cargo-home",
                "--tmpfs",
                "/cargo-home:rw,nosuid,nodev,size=128m,mode=1777",
                "--mount",
                docker_mount(cargo_cache, "/cargo-cache-ro", readonly=True),
            ]
        )
    else:
        raise HarnessError(f"invalid Cargo cache access mode: {cache_access}")
    command.extend(
        [
            "-e",
            "PATH=/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        ]
    )
    command.extend(
        container_environment_args(required_environment, phase="build")
    )
    command.append(image)
    return command


def run_checked(
    command: list[str],
    *,
    capture: bool = False,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        timeout=timeout_seconds,
    )


def new_container_name(phase: str) -> str:
    if not re.fullmatch(r"[a-z0-9-]+", phase):
        raise HarnessError(f"invalid container phase: {phase}")
    return f"unialloc-rustsec-{phase}-{uuid.uuid4().hex}"


def force_remove_container(container_name: str) -> None:
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_named_container(
    command: list[str],
    *,
    container_name: str,
    timeout_seconds: int,
    phase: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return run_checked(command, timeout_seconds=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        force_remove_container(container_name)
        raise HarnessError(
            f"{phase} exceeded the hard timeout on the host; forced container cleanup: "
            f"{container_name}"
        ) from error


def offline_cargo_command(base: list[str], cargo_args: list[str]) -> list[str]:
    return base + [
        "sh",
        "-ceu",
        CARGO_HOME_OVERLAY_SCRIPT,
        "cargo-overlay",
        *cargo_args,
    ]


def container_check(
    image: str,
    cache_root: pathlib.Path,
    work: pathlib.Path,
    profile: str,
    required_environment: dict[str, Any] | None = None,
    timeout: int = DEFAULT_BUILD_TIMEOUT,
) -> int:
    fetch_name = new_container_name("fetch")
    fetch_base = docker_base(
        image,
        cache_root,
        work,
        container_name=fetch_name,
        cache_access="fetch-rw",
        network_disabled=False,
        required_environment=required_environment,
    )
    fetch = run_named_container(
        fetch_base
        + [
            "cargo",
            "fetch",
            "--locked",
            "--manifest-path",
            "/work/project/Cargo.toml",
        ],
        container_name=fetch_name,
        timeout_seconds=timeout,
        phase="cargo fetch",
    )
    if fetch.returncode != 0:
        return fetch.returncode
    check_name = new_container_name("check")
    check_base = docker_base(
        image,
        cache_root,
        work,
        container_name=check_name,
        cache_access="offline-ro",
        network_disabled=True,
        required_environment=required_environment,
    )
    cargo_args = [
        "cargo",
        "check",
        "--offline",
        "--locked",
        "--manifest-path",
        "/work/project/Cargo.toml",
    ]
    if profile == "release":
        cargo_args.append("--release")
    command = offline_cargo_command(check_base, cargo_args)
    return run_named_container(
        command,
        container_name=check_name,
        timeout_seconds=timeout,
        phase="cargo check",
    ).returncode


def container_build(
    image: str,
    cache_root: pathlib.Path,
    work: pathlib.Path,
    profile: str,
    required_environment: dict[str, Any] | None = None,
    timeout: int = DEFAULT_BUILD_TIMEOUT,
) -> int:
    fetch_name = new_container_name("fetch")
    fetch_base = docker_base(
        image,
        cache_root,
        work,
        container_name=fetch_name,
        cache_access="fetch-rw",
        network_disabled=False,
        required_environment=required_environment,
    )
    fetch = run_named_container(
        fetch_base
        + [
            "cargo",
            "fetch",
            "--locked",
            "--manifest-path",
            "/work/project/Cargo.toml",
        ],
        container_name=fetch_name,
        timeout_seconds=timeout,
        phase="cargo fetch",
    )
    if fetch.returncode != 0:
        return fetch.returncode
    build_name = new_container_name("build")
    build_base = docker_base(
        image,
        cache_root,
        work,
        container_name=build_name,
        cache_access="offline-ro",
        network_disabled=True,
        required_environment=required_environment,
    )
    cargo_args = [
        "cargo",
        "build",
        "--offline",
        "--locked",
        "--manifest-path",
        "/work/project/Cargo.toml",
    ]
    if profile == "release":
        cargo_args.append("--release")
    command = offline_cargo_command(build_base, cargo_args)
    return run_named_container(
        command,
        container_name=build_name,
        timeout_seconds=timeout,
        phase="cargo build",
    ).returncode


def container_run_native(
    image: str,
    work: pathlib.Path,
    case_id: str,
    profile: str,
    timeout: int,
    required_environment: dict[str, Any] | None = None,
) -> int:
    target_profile = "release" if profile == "release" else "debug"
    binary = work / "project" / "target" / target_profile / f"{case_id.lower()}-harness"
    if not binary.is_file():
        raise HarnessError(f"missing built harness binary: {binary}")
    container_name = new_container_name("run")
    command = [
        "docker",
        "run",
        "--name",
        container_name,
        "--rm",
        "--init",
        "--network",
        "none",
        "--read-only",
        "--user",
        "65534:65534",
        "--cap-drop",
        "ALL",
        "--security-opt=no-new-privileges",
        "--cpus=1",
        "--memory=1g",
        "--pids-limit=64",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--mount",
        docker_mount(binary.parent, "/opt/harness", readonly=True),
        "--workdir",
        "/opt/harness",
    ]
    command.extend(container_environment_args(required_environment, phase="run"))
    command.extend([
        image,
        "timeout",
        "--signal=TERM",
        "--kill-after=2s",
        str(timeout),
        f"./{binary.name}",
    ])
    return run_named_container(
        command,
        container_name=container_name,
        timeout_seconds=timeout + 15,
        phase="native harness",
    ).returncode


def list_payload(catalog: dict[str, Any]) -> dict[str, Any]:
    _, scenarios = index_catalog(catalog)
    rows = []
    for scenario_id, (case, scenario) in sorted(scenarios.items()):
        rows.append(
            {
                "scenario_id": scenario_id,
                "case_id": case["case_id"],
                "crate": case["crate"],
                "tool": scenario["oracle"]["tool"],
                "classification_role": scenario["classification_role"],
            }
        )
    return {"counts": catalog["counts"], "scenarios": rows}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=pathlib.Path, default=DEFAULT_CATALOG)
    parser.add_argument(
        "--action",
        choices=("list", "materialize", "check", "run-native"),
        default="list",
    )
    parser.add_argument("--scenario")
    parser.add_argument("--variant", choices=("vulnerable", "patched"), default="vulnerable")
    parser.add_argument("--cache", type=pathlib.Path)
    parser.add_argument("--work-dir", type=pathlib.Path)
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--execute-unsafe", action="store_true")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--build-timeout", type=int, default=DEFAULT_BUILD_TIMEOUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        catalog = load_catalog(args.catalog.resolve())
        if args.action == "list":
            print(json.dumps(list_payload(catalog), indent=2, sort_keys=True))
            return 0
        if not args.scenario:
            raise HarnessError("--scenario is required for this action")
        if args.action == "materialize" and args.work_dir is None:
            raise HarnessError("--work-dir is required to retain a materialization")
        if args.timeout <= 0 or args.build_timeout <= 0:
            raise HarnessError("--timeout and --build-timeout must be positive")
        case, scenario = select_scenario(catalog, args.scenario)
        cache_root = cache_root_from_args(args)
        temporary: tempfile.TemporaryDirectory[str] | None = None
        if args.work_dir:
            work = args.work_dir.resolve()
        else:
            temporary = tempfile.TemporaryDirectory(prefix="unialloc-rustsec-")
            work = pathlib.Path(temporary.name) / "work"
        report = materialize(
            catalog,
            args.scenario,
            args.variant,
            work,
            cache_root,
            args.allow_download,
        )
        if args.action == "materialize":
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        if shutil.which("docker") is None:
            raise HarnessError("docker is required for contained build/run actions")
        profile = scenario["cargo_profile"]
        required_environment = scenario["required_environment"]
        if args.action == "check":
            return container_check(
                args.image,
                cache_root,
                work,
                profile,
                required_environment,
                timeout=args.build_timeout,
            )
        if not args.execute_unsafe:
            raise HarnessError("run-native requires the explicit --execute-unsafe opt-in")
        build_status = container_build(
            args.image,
            cache_root,
            work,
            profile,
            required_environment,
            timeout=args.build_timeout,
        )
        if build_status != 0:
            return build_status
        return container_run_native(
            args.image,
            work,
            case["case_id"],
            profile,
            args.timeout,
            required_environment,
        )
    except (
        HarnessError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        tarfile.TarError,
        json.JSONDecodeError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
