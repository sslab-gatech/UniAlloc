#!/usr/bin/env python3
"""Audit the pinned RustSec/Rudra heap-security classification corpus.

This command validates catalog structure and, when local source checkouts are
provided, verifies every selected source byte against its pinned commit and
SHA-256.  A passing catalog audit is classification evidence only; executable
mitigation/detection claims require separate raw run artifacts.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tomllib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "evaluation" / "config" / "rustsec_heap_security_corpus.json"
HARNESS_CATALOG_RELPATH = "evaluation/config/rustsec_heap_harnesses.json"
DEFAULT_HARNESS_CATALOG = ROOT / HARNESS_CATALOG_RELPATH
RUSTSEC_SOURCE_URL = "https://github.com/RustSec/advisory-db"
RUDRA_POC_SOURCE_URL = "https://github.com/sslab-gatech/Rudra-PoC"

ADVISORY_ID_RE = re.compile(r"^RUSTSEC-\d{4}-\d{4}$")
CASE_ID_RE = re.compile(r"^RSH-\d{3}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

ALLOWED_COHORTS = {"rudra_poc", "rustsec_extended"}
ALLOWED_DIMENSIONS = {
    "temporal",
    "reclaim",
    "spatial",
    "initialization",
    "concurrency",
    "provenance",
}
ALLOWED_CONTROL_ROLES = {"positive_candidate", "negative_control"}
ALLOWED_PUBLISHED_TYPEISO_EFFECTS = {"no_direct_effect"}
ALLOWED_DERIVED_TYPEISO_EFFECTS = {
    "conditional_reuse_edge_mitigation",
    "no_direct_effect",
}
ALLOWED_BOUNDARY_EFFECTS = {"conditional_detection", "no_direct_effect"}
ALLOWED_HYPOTHESES = {
    "candidate_reuse_edge_blocked",
    "candidate_detected",
    "negative_control_expected_unsafe",
}
ALLOWED_REUSE_RELATIONS = {"none", "same", "cross", "any", "unknown"}
ALLOWED_PRIMARY_PRIMITIVES = {
    "aliasing_violation",
    "double_free",
    "invalid_free",
    "misaligned_allocation",
    "misaligned_reference",
    "out_of_bounds_read",
    "out_of_bounds_write",
    "type_confusion",
    "uninitialized_drop",
    "uninitialized_read",
    "use_after_free",
}
ALLOWED_SECONDARY_PRIMITIVES = {
    "data_race",
    "double_free",
    "invalid_free",
    "out_of_bounds_read",
    "out_of_bounds_write",
    "uninitialized_read",
    "use_after_free",
}
ALLOWED_STORAGE_DOMAINS = {
    "arena_managed",
    "foreign_heap",
    "heap",
    "mixed",
    "stack_inline",
}
ALLOWED_ALLOCATOR_PATHS = {
    "arena_internal",
    "foreign_or_custom",
    "rust_global_candidate",
    "stack_only",
}
ALLOWED_ALLOCATOR_VISIBILITY = {
    "allocator_returned_bytes",
    "arena_internal_access",
    "concurrency_only",
    "deallocation_boundary",
    "external_ffi_access",
    "external_ffi_reclaim",
    "foreign_allocator_access",
    "invalid_value_drop",
    "memory_access_only",
    "memory_access_then_deallocation",
    "reuse_dependent_access",
}
ALLOWED_LIFECYCLE_PHASES = {
    "allocation",
    "construction",
    "drop",
    "live_access",
    "post_free_access",
    "post_move_access",
    "reallocation",
    "unwind",
}
PROFILE_PRIMARY_PRIMITIVES = {
    "allocator_visible_double_free": {"double_free"},
    "concurrency_unsoundness": {"use_after_free"},
    "foreign_allocator_boundary": {
        "misaligned_allocation",
        "out_of_bounds_write",
        "use_after_free",
    },
    "heap_oob": {"out_of_bounds_read", "out_of_bounds_write"},
    "invalid_deallocation": {"invalid_free"},
    "language_invariant_violation": {
        "aliasing_violation",
        "misaligned_reference",
        "type_confusion",
        "use_after_free",
    },
    "panic_double_drop": {"double_free"},
    "reuse_dependent_uaf": {"use_after_free"},
    "same_identity_or_pre_reuse_uaf": {"use_after_free"},
    "uninitialized_exposure": {"uninitialized_drop", "uninitialized_read"},
}
ALLOWED_READINESS = {
    "poc_source_available",
    "pinned_harness_source_available",
    "fresh_poc_required",
    "advisory_reproducer_identified",
    "upstream_reproducer_identified",
    "external_reproducer_identified",
    "fresh_harness_required",
    "environment_harness_required",
}
ALLOWED_PATCHED_CONTROLS = {
    "upstream_patched_version_required",
    "hashed_local_patch_required",
}
ALLOWED_REPRODUCTION_KINDS = {
    "rudra_poc",
    "rudra_stub",
    "repository_harness",
    "rustsec_inline",
    "upstream_inline",
    "linked_repository",
    "fresh_harness_required",
    "environment_harness_required",
    "root_cause_only",
}
EXPECTED_HARNESS_COUNTS = {
    "harness_case_count": 22,
    "repository_scenario_count": 28,
    "published_or_upstream_scenario_count": 24,
    "derived_adapter_scenario_count": 4,
    "derived_reuse_scenario_count": 2,
    "pinned_rudra_source_scenario_count": 18,
    "total_source_ready_scenario_count": 46,
    "source_ready_distinct_advisory_count": 40,
}
EXPECTED_SOURCE_READY_ADVISORY_COUNT = 40
ALLOWED_HARNESS_PROVENANCE = {
    "derived_adapter",
    "linked_repository",
    "rustsec_inline",
    "upstream_attachment",
    "upstream_issue",
    "upstream_patch_regression",
    "upstream_regression_test",
}
ALLOWED_HARNESS_ADAPTER_KINDS = {"mechanical_adapter", "derived_adapter"}
ALLOWED_HARNESS_ROLES = {
    "allocator_boundary_detection_candidate",
    "derived_reuse_experiment",
    "published_negative_control",
}
ALLOWED_HARNESS_TOOLS = {
    "asan",
    "asan_c_and_rust",
    "miri",
    "native_address_trace",
    "native_alignment",
    "native_diagnostic",
    "native_value_oracle",
}
ALLOWED_CARGO_PROFILES = {"dev", "release"}
SCHEMA_MINIMUM_STRATA = {
    "total_cases": 40,
    "rudra_poc_cases": 19,
    "rustsec_extended_cases": 21,
    "temporal_cases": 6,
    "reclaim_cases": 13,
    "spatial_cases": 9,
    "initialization_cases": 5,
    "concurrency_cases": 2,
    "provenance_cases": 5,
    "negative_controls": 24,
}

SEMVER_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def strict_posix_relative_path(value: Any) -> pathlib.PurePosixPath | None:
    """Return a normalized, traversal-free POSIX relative path."""

    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        return None
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        return None
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or path.root or tuple(raw_parts) != path.parts:
        return None
    return path


def confined_snapshot_path(root: pathlib.Path, relative: Any) -> pathlib.Path:
    """Resolve a strict relative path while keeping it beneath ``root``."""

    pure = strict_posix_relative_path(relative)
    if pure is None:
        raise ValueError("path is not a strict POSIX relative path")
    try:
        resolved_root = root.resolve()
        resolved = resolved_root.joinpath(*pure.parts).resolve()
    except (OSError, RuntimeError) as error:
        raise ValueError(f"path cannot be safely resolved: {error}") from error
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("path escapes the pinned snapshot root")
    return resolved


def all_confined_snapshot_files(
    root: pathlib.Path, relative_paths: list[str]
) -> bool:
    """Return whether every selected snapshot path is confined and regular."""

    try:
        return all(
            confined_snapshot_path(root, relative).is_file()
            for relative in relative_paths
        )
    except ValueError:
        return False


def selected_files_sha256(root: pathlib.Path, relative_paths: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(relative_paths):
        path = confined_snapshot_path(root, relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def git_head(path: pathlib.Path) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def parse_rustsec_document(path: pathlib.Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"```toml\n(.*?)\n```", text, re.S)
    if match is None:
        return None
    try:
        return tomllib.loads(match.group(1))
    except tomllib.TOMLDecodeError:
        return None


def parse_rustsec_advisory(path: pathlib.Path) -> dict[str, Any] | None:
    document = parse_rustsec_document(path)
    if not document:
        return None
    advisory = document.get("advisory")
    return advisory if isinstance(advisory, dict) else None


def parse_rustsec_heading(path: pathlib.Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^# (.+)$", text, re.M)
    return match.group(1).strip() if match else None


def _source_scalar(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def parse_semver(value: str) -> tuple[int, int, int, tuple[int, tuple[tuple[int, Any], ...]]]:
    match = SEMVER_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"unsupported semantic version: {value!r}")
    prerelease = match.group(4)
    if prerelease is None:
        prerelease_key: tuple[int, tuple[tuple[int, Any], ...]] = (1, ())
    else:
        identifiers: list[tuple[int, Any]] = []
        for identifier in prerelease.split("."):
            identifiers.append(
                (0, int(identifier)) if identifier.isdigit() else (1, identifier)
            )
        prerelease_key = (0, tuple(identifiers))
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease_key


def semver_matches_requirement(version: str, requirement: str) -> bool:
    value = parse_semver(version)
    comparators = [part.strip() for part in requirement.split(",")]
    if not comparators or any(not comparator for comparator in comparators):
        raise ValueError(f"unsupported semantic-version requirement: {requirement!r}")
    for comparator in comparators:
        match = re.fullmatch(r"(\^|>=|<=|>|<|=)?\s*(v?.+)", comparator)
        if match is None:
            raise ValueError(f"unsupported semantic-version comparator: {comparator!r}")
        operator = match.group(1) or "^"
        bound_text = match.group(2)
        if re.fullmatch(r"v?\d+", bound_text):
            bound_text += ".0.0"
        elif re.fullmatch(r"v?\d+\.\d+", bound_text):
            bound_text += ".0"
        bound = parse_semver(bound_text)
        if operator == ">=" and not value >= bound:
            return False
        if operator == "<=" and not value <= bound:
            return False
        if operator == ">" and not value > bound:
            return False
        if operator == "<" and not value < bound:
            return False
        if operator == "=" and not value == bound:
            return False
        if operator == "^":
            major, minor, patch, _ = bound
            if major > 0:
                upper = parse_semver(f"{major + 1}.0.0")
            elif minor > 0:
                upper = parse_semver(f"0.{minor + 1}.0")
            else:
                upper = parse_semver(f"0.0.{patch + 1}")
            if not (value >= bound and value < upper):
                return False
    return True


def replay_pin_is_vulnerable(
    replay_pin: str,
    patched_versions: list[str],
    unaffected_versions: list[str],
) -> bool:
    parse_semver(replay_pin)
    return not any(
        semver_matches_requirement(replay_pin, requirement)
        for requirement in [*patched_versions, *unaffected_versions]
    )


def parse_rudra_metadata(path: pathlib.Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"```rudra-poc\n(.*?)\n```", text, re.S)
    if match is None:
        return None
    try:
        return tomllib.loads(match.group(1))
    except tomllib.TOMLDecodeError:
        return None


def rustsec_screen_counts(root: pathlib.Path, query: dict[str, Any]) -> dict[str, Any]:
    advisory_files = sorted((root / "crates").glob("*/RUSTSEC-*.md"))
    category_terms = set(query.get("category_any", []))
    keyword_terms = set(query.get("keyword_any", []))
    category_ids: set[str] = set()
    candidate_ids: set[str] = set()
    parsed_count = 0
    for path in advisory_files:
        advisory = parse_rustsec_advisory(path)
        if not advisory:
            continue
        parsed_count += 1
        advisory_id = str(advisory.get("id", ""))
        categories = set(advisory.get("categories", []))
        keywords = {str(value).lower() for value in advisory.get("keywords", [])}
        if categories & category_terms:
            category_ids.add(advisory_id)
        if categories & category_terms or keywords & keyword_terms:
            candidate_ids.add(advisory_id)
    return {
        "advisory_file_count": len(advisory_files),
        "parsed_advisory_count": parsed_count,
        "memory_corruption_category_count": len(category_ids),
        "candidate_screen_count": len(candidate_ids),
        "candidate_ids": sorted(candidate_ids),
    }


def _nonempty_strings(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and item.strip() for item in value
    )


def audit_harness_catalog(
    catalog_path: pathlib.Path,
    *,
    bundle: Any,
    corpus_cases: list[Any],
    profiles: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Validate the repository harness bundle and its corpus cross-references."""

    blockers: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            blockers.append(message)

    bundle_value = bundle if isinstance(bundle, dict) else {}
    require(isinstance(bundle, dict), "harness_bundle must be an object")
    require(
        bundle_value.get("catalog_path") == HARNESS_CATALOG_RELPATH,
        f"harness_bundle catalog_path must equal {HARNESS_CATALOG_RELPATH}",
    )
    require(
        bool(SHA256_RE.fullmatch(str(bundle_value.get("catalog_sha256", "")))),
        "harness_bundle catalog_sha256 must be SHA-256",
    )
    require(
        bundle_value.get("claim_grade") is False,
        "harness_bundle must remain claim_grade=false",
    )
    require(
        bundle_value.get("distinct_advisory_count")
        == EXPECTED_SOURCE_READY_ADVISORY_COUNT,
        "harness_bundle distinct_advisory_count must equal 40",
    )
    for bundle_field in (
        "harness_case_count",
        "repository_scenario_count",
        "total_source_ready_scenario_count",
    ):
        require(
            bundle_value.get(bundle_field) == EXPECTED_HARNESS_COUNTS[bundle_field],
            f"harness_bundle {bundle_field} disagrees with frozen harness counts",
        )

    try:
        catalog_bytes = catalog_path.read_bytes()
    except OSError as error:
        blockers.append(f"cannot read harness catalog: {error}")
        return {
            "catalog_path": str(catalog_path),
            "counts": {},
            "distinct_advisory_count": 0,
        }, blockers

    catalog_sha256 = hashlib.sha256(catalog_bytes).hexdigest()
    require(
        catalog_sha256 == bundle_value.get("catalog_sha256"),
        "harness catalog SHA-256 disagrees with harness_bundle catalog_sha256",
    )
    try:
        catalog = json.loads(catalog_bytes)
    except json.JSONDecodeError as error:
        blockers.append(f"cannot parse harness catalog: {error}")
        return {
            "catalog_path": str(catalog_path),
            "catalog_sha256": catalog_sha256,
            "counts": {},
            "distinct_advisory_count": 0,
        }, blockers
    if not isinstance(catalog, dict):
        blockers.append("harness catalog root must be an object")
        return {
            "catalog_path": str(catalog_path),
            "catalog_sha256": catalog_sha256,
            "counts": {},
            "distinct_advisory_count": 0,
        }, blockers

    require(catalog.get("schema_version") == 1, "harness catalog schema_version must equal 1")
    require(
        catalog.get("source") == "unialloc-rustsec-heap-harness-bundle",
        "harness catalog source is invalid",
    )
    require(catalog.get("claim_grade") is False, "harness catalog must remain claim_grade=false")
    require(bool(catalog.get("claim_boundary")), "harness catalog claim_boundary is required")

    execution_policy = catalog.get("execution_policy")
    require(isinstance(execution_policy, dict), "harness execution_policy must be an object")
    if isinstance(execution_policy, dict):
        require(bool(execution_policy.get("build")), "harness build policy is required")
        require(bool(execution_policy.get("run")), "harness run policy is required")
        require(
            execution_policy.get("unsafe_opt_in_required") is True,
            "harness execution must require explicit unsafe opt-in",
        )

    toolchain = catalog.get("toolchain")
    require(isinstance(toolchain, dict), "harness toolchain must be an object")
    if isinstance(toolchain, dict):
        image = str(toolchain.get("compile_smoke_image", ""))
        require(
            bool(re.search(r"@sha256:[0-9a-f]{64}$", image)),
            "harness compile image must be digest-pinned",
        )
        require(bool(toolchain.get("sanitizer_toolchain")), "harness sanitizer toolchain is required")
        require(bool(toolchain.get("sanitizer_rustc")), "harness sanitizer rustc is required")
        require(bool(toolchain.get("validated_target")), "harness validated target is required")

    counts_value = catalog.get("counts")
    require(isinstance(counts_value, dict), "harness counts must be an object")
    counts = counts_value if isinstance(counts_value, dict) else {}
    require(
        counts == EXPECTED_HARNESS_COUNTS,
        "harness counts must match the frozen 22/28/24/4/2/18/46/40 split",
    )

    corpus_by_id = {
        str(case.get("case_id")): case
        for case in corpus_cases
        if isinstance(case, dict)
    }
    expected_catalog_case_ids = {
        case_id
        for case_id, case in corpus_by_id.items()
        if isinstance(case.get("execution"), dict)
        and case["execution"].get("readiness")
        == "pinned_harness_source_available"
    }
    rudra_source_cases = {
        case_id: case
        for case_id, case in corpus_by_id.items()
        if isinstance(case.get("execution"), dict)
        and case["execution"].get("reproduction_kind") == "rudra_poc"
        and isinstance(case.get("poc"), dict)
        and case["poc"].get("executable_source") is True
    }

    def repo_artifact(
        case_id: str,
        relative: Any,
        expected_sha256: Any,
        label: str,
    ) -> pathlib.Path | None:
        require(isinstance(relative, str) and bool(relative), f"{label} path is required")
        require(
            bool(SHA256_RE.fullmatch(str(expected_sha256))),
            f"{label} sha256 is invalid",
        )
        if not isinstance(relative, str) or not relative:
            return None
        pure = pathlib.PurePosixPath(relative)
        expected_root = pathlib.PurePosixPath(
            f"evaluation/harnesses/rustsec_heap/{case_id}"
        )
        confined = (
            not pure.is_absolute()
            and ".." not in pure.parts
            and pure != expected_root
            and pure.is_relative_to(expected_root)
        )
        require(confined, f"{label} must stay beneath {expected_root}")
        if not confined:
            return None
        path = ROOT.joinpath(*pure.parts)
        resolved_root = ROOT.joinpath(*expected_root.parts).resolve()
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        require(resolved.is_relative_to(resolved_root), f"{label} escapes its case directory")
        is_regular = path.is_file() and not path.is_symlink()
        require(is_regular, f"{label} must reference a regular repository file: {relative}")
        if is_regular and SHA256_RE.fullmatch(str(expected_sha256)):
            require(
                file_sha256(path) == expected_sha256,
                f"{label} SHA-256 mismatch: {relative}",
            )
        return path if is_regular else None

    def archive_metadata(
        value: Any,
        *,
        label: str,
        expected_package: str,
        expected_version: str | None = None,
    ) -> dict[str, Any]:
        require(isinstance(value, dict), f"{label} must be an object")
        archive = value if isinstance(value, dict) else {}
        require(archive.get("kind") == "crates_io_archive", f"{label} kind is invalid")
        package = archive.get("package")
        version = archive.get("version")
        require(package == expected_package, f"{label} package disagrees with corpus crate")
        require(isinstance(version, str) and bool(version), f"{label} version is required")
        if isinstance(version, str) and version:
            try:
                parse_semver(version)
            except ValueError as error:
                require(False, f"{label} version is invalid: {error}")
        if expected_version is not None:
            require(version == expected_version, f"{label} version disagrees with corpus replay pin")
        expected_url = (
            f"https://static.crates.io/crates/{package}/{package}-{version}.crate"
        )
        require(archive.get("url") == expected_url, f"{label} crates.io URL is not exact")
        byte_count = archive.get("bytes")
        require(
            isinstance(byte_count, int)
            and not isinstance(byte_count, bool)
            and byte_count > 0,
            f"{label} bytes must be a positive integer",
        )
        require(
            bool(SHA256_RE.fullmatch(str(archive.get("sha256", "")))),
            f"{label} sha256 is invalid",
        )
        require(
            bool(COMMIT_RE.fullmatch(str(archive.get("upstream_commit", "")))),
            f"{label} upstream_commit must be a full 40-hex pin",
        )
        return archive

    catalog_cases_value = catalog.get("cases")
    require(isinstance(catalog_cases_value, list), "harness cases must be a list")
    catalog_cases = catalog_cases_value if isinstance(catalog_cases_value, list) else []
    catalog_case_ids: set[str] = set()
    scenario_ids: set[str] = set()
    source_paths: set[str] = set()
    repository_scenario_count = 0
    adapter_counts: collections.Counter[str] = collections.Counter()
    role_counts: collections.Counter[str] = collections.Counter()
    catalog_advisory_ids: set[str] = set()

    for index, harness_case in enumerate(catalog_cases):
        prefix = f"harness case[{index}]"
        require(isinstance(harness_case, dict), f"{prefix} must be an object")
        if not isinstance(harness_case, dict):
            continue
        case_id = str(harness_case.get("case_id", ""))
        require(bool(CASE_ID_RE.fullmatch(case_id)), f"{prefix} has invalid case_id")
        require(case_id not in catalog_case_ids, f"duplicate harness case_id: {case_id}")
        catalog_case_ids.add(case_id)
        corpus_case_value = corpus_by_id.get(case_id)
        require(corpus_case_value is not None, f"{prefix} has no corpus case cross-reference")
        corpus_case = corpus_case_value if isinstance(corpus_case_value, dict) else {}
        corpus_execution_value = corpus_case.get("execution")
        corpus_execution = (
            corpus_execution_value if isinstance(corpus_execution_value, dict) else {}
        )
        require(
            corpus_execution.get("readiness") == "pinned_harness_source_available",
            f"{prefix} corpus readiness must be pinned_harness_source_available",
        )
        require(
            corpus_execution.get("reproduction_kind") == "repository_harness",
            f"{prefix} corpus reproduction_kind must be repository_harness",
        )
        require(
            corpus_execution.get("harness_case_id") == case_id,
            f"{prefix} corpus harness_case_id must bind to case_id",
        )
        advisory_id = str(corpus_case.get("advisory_id", ""))
        require(
            bool(ADVISORY_ID_RE.fullmatch(advisory_id)),
            f"{prefix} corpus advisory cross-reference is invalid",
        )
        if ADVISORY_ID_RE.fullmatch(advisory_id):
            catalog_advisory_ids.add(advisory_id)

        crate_name = str(harness_case.get("crate", ""))
        require(crate_name == corpus_case.get("crate"), f"{prefix} crate disagrees with corpus")
        replay_pin = corpus_execution.get("replay_pin")
        vulnerable_dependency = archive_metadata(
            harness_case.get("vulnerable_dependency"),
            label=f"{prefix} vulnerable dependency",
            expected_package=crate_name,
            expected_version=replay_pin if isinstance(replay_pin, str) else None,
        )

        cargo = harness_case.get("cargo")
        require(isinstance(cargo, dict), f"{prefix} cargo metadata must be an object")
        if isinstance(cargo, dict):
            require(
                isinstance(cargo.get("default_features"), bool),
                f"{prefix} cargo.default_features must be boolean",
            )
            features = cargo.get("features")
            require(
                isinstance(features, list)
                and all(isinstance(item, str) and item for item in features),
                f"{prefix} cargo.features must be a string list",
            )
            for extra_field in (
                "vulnerable_extra_dependencies",
                "patched_extra_dependencies",
            ):
                extras = cargo.get(extra_field)
                require(isinstance(extras, dict), f"{prefix} cargo.{extra_field} must be an object")
                if isinstance(extras, dict):
                    require(
                        all(
                            isinstance(name, str)
                            and bool(name)
                            and isinstance(requirement, str)
                            and bool(requirement)
                            for name, requirement in extras.items()
                        ),
                        f"{prefix} cargo.{extra_field} entries must be nonempty strings",
                    )

        scenarios_value = harness_case.get("scenarios")
        require(
            isinstance(scenarios_value, list) and bool(scenarios_value),
            f"{prefix} scenarios must be a nonempty list",
        )
        scenarios = scenarios_value if isinstance(scenarios_value, list) else []
        case_has_local_patch = False
        for scenario_index, scenario in enumerate(scenarios):
            scenario_prefix = f"{prefix} scenario[{scenario_index}]"
            require(isinstance(scenario, dict), f"{scenario_prefix} must be an object")
            if not isinstance(scenario, dict):
                continue
            repository_scenario_count += 1
            scenario_id = str(scenario.get("scenario_id", ""))
            require(
                scenario_id.startswith(f"{case_id}-")
                and bool(re.fullmatch(r"RSH-\d{3}-[a-z0-9]+(?:-[a-z0-9]+)*", scenario_id)),
                f"{scenario_prefix} has invalid scenario_id",
            )
            require(scenario_id not in scenario_ids, f"duplicate scenario_id: {scenario_id}")
            scenario_ids.add(scenario_id)

            adapter_kind = scenario.get("adapter_kind")
            provenance = scenario.get("provenance")
            role = scenario.get("classification_role")
            require(
                adapter_kind in ALLOWED_HARNESS_ADAPTER_KINDS,
                f"{scenario_prefix} has unknown adapter_kind",
            )
            require(
                provenance in ALLOWED_HARNESS_PROVENANCE,
                f"{scenario_prefix} has unknown provenance",
            )
            require(role in ALLOWED_HARNESS_ROLES, f"{scenario_prefix} has unknown role")
            adapter_counts[str(adapter_kind)] += 1
            role_counts[str(role)] += 1
            is_derived = adapter_kind == "derived_adapter"
            require(
                (provenance == "derived_adapter") is is_derived,
                f"{scenario_prefix} provenance disagrees with adapter_kind",
            )
            require(
                role != "derived_reuse_experiment" or is_derived,
                f"{scenario_prefix} derived reuse role requires a derived adapter",
            )

            profile_value = profiles.get(corpus_case.get("assessment_profile"), {})
            profile = profile_value if isinstance(profile_value, dict) else {}
            if role == "derived_reuse_experiment":
                require(
                    profile.get("derived_type_isolation_experiment_effect")
                    == "conditional_reuse_edge_mitigation",
                    f"{scenario_prefix} derived role disagrees with corpus profile",
                )
            if role == "allocator_boundary_detection_candidate":
                require(
                    profile.get("unialloc_boundary_effect")
                    == "conditional_detection",
                    f"{scenario_prefix} boundary-detection role requires a conditional UniAlloc boundary effect",
                )
            if role == "published_negative_control":
                require(
                    profile.get("published_witness_type_isolation_effect")
                    == "no_direct_effect",
                    f"{scenario_prefix} published role disagrees with corpus profile",
                )

            source_path = scenario.get("source_path")
            require(source_path not in source_paths, f"duplicate harness source_path: {source_path}")
            if isinstance(source_path, str):
                source_paths.add(source_path)
            repo_artifact(
                case_id,
                source_path,
                scenario.get("source_sha256"),
                f"{scenario_prefix} source",
            )
            origin_url = scenario.get("origin_url")
            require(
                isinstance(origin_url, str) and origin_url.startswith("https://"),
                f"{scenario_prefix} origin_url must use HTTPS",
            )
            if "origin_content_sha256" in scenario:
                require(
                    bool(SHA256_RE.fullmatch(str(scenario.get("origin_content_sha256", "")))),
                    f"{scenario_prefix} origin_content_sha256 is invalid",
                )
            require(
                scenario.get("cargo_profile") in ALLOWED_CARGO_PROFILES,
                f"{scenario_prefix} has unknown cargo_profile",
            )
            rustflags = scenario.get("rustflags")
            require(
                isinstance(rustflags, list)
                and all(isinstance(flag, str) and bool(flag) for flag in rustflags),
                f"{scenario_prefix} rustflags must be a string list",
            )
            environment = scenario.get("required_environment")
            require(
                isinstance(environment, dict)
                and all(
                    isinstance(name, str)
                    and bool(name)
                    and isinstance(value, str)
                    for name, value in environment.items()
                ),
                f"{scenario_prefix} required_environment must map strings to strings",
            )
            oracle = scenario.get("oracle")
            require(isinstance(oracle, dict), f"{scenario_prefix} oracle must be an object")
            if isinstance(oracle, dict):
                require(
                    oracle.get("tool") in ALLOWED_HARNESS_TOOLS,
                    f"{scenario_prefix} has unknown oracle tool",
                )
                require(
                    isinstance(oracle.get("vulnerable"), str)
                    and bool(oracle.get("vulnerable")),
                    f"{scenario_prefix} vulnerable oracle is required",
                )
                require(
                    isinstance(oracle.get("patched_control"), str)
                    and bool(oracle.get("patched_control")),
                    f"{scenario_prefix} patched-control oracle is required",
                )

            patched_source = scenario.get("patched_source")
            if patched_source is not None:
                require(
                    isinstance(patched_source, dict),
                    f"{scenario_prefix} patched_source must be an object",
                )
                if isinstance(patched_source, dict):
                    repo_artifact(
                        case_id,
                        patched_source.get("source_path"),
                        patched_source.get("source_sha256"),
                        f"{scenario_prefix} patched source",
                    )

            local_patch = scenario.get("local_patched_control")
            if local_patch is not None:
                case_has_local_patch = True
                require(
                    isinstance(local_patch, dict),
                    f"{scenario_prefix} local_patched_control must be an object",
                )
                if isinstance(local_patch, dict):
                    require(
                        local_patch.get("kind") == "hashed_local_patch",
                        f"{scenario_prefix} local patch kind is invalid",
                    )
                    require(bool(local_patch.get("label")), f"{scenario_prefix} local patch label is required")
                    repo_artifact(
                        case_id,
                        local_patch.get("path"),
                        local_patch.get("sha256"),
                        f"{scenario_prefix} local patch",
                    )
                    resulting_value = local_patch.get("resulting_source_files")
                    require(
                        isinstance(resulting_value, dict) and bool(resulting_value),
                        f"{scenario_prefix} resulting_source_files must be a nonempty object",
                    )
                    if isinstance(resulting_value, dict):
                        for relative, result_sha256 in resulting_value.items():
                            pure = pathlib.PurePosixPath(str(relative))
                            require(
                                isinstance(relative, str)
                                and bool(relative)
                                and not pure.is_absolute()
                                and ".." not in pure.parts
                                and pure.name not in {"", "."},
                                f"{scenario_prefix} resulting source path must be crate-relative",
                            )
                            require(
                                bool(SHA256_RE.fullmatch(str(result_sha256))),
                                f"{scenario_prefix} resulting source sha256 is invalid",
                            )

            subject_patches_value = scenario.get("subject_patches", [])
            require(
                isinstance(subject_patches_value, list),
                f"{scenario_prefix} subject_patches must be a list",
            )
            subject_patches = (
                subject_patches_value
                if isinstance(subject_patches_value, list)
                else []
            )
            for patch_index, subject_patch in enumerate(subject_patches):
                patch_prefix = f"{scenario_prefix} subject patch[{patch_index}]"
                require(
                    isinstance(subject_patch, dict),
                    f"{patch_prefix} must be an object",
                )
                if not isinstance(subject_patch, dict):
                    continue
                require(
                    subject_patch.get("kind") == "hashed_subject_patch",
                    f"{patch_prefix} kind is invalid",
                )
                require(bool(subject_patch.get("label")), f"{patch_prefix} label is required")
                repo_artifact(
                    case_id,
                    subject_patch.get("path"),
                    subject_patch.get("sha256"),
                    patch_prefix,
                )
                apply_variants = subject_patch.get("apply_variants")
                valid_variants = (
                    isinstance(apply_variants, list)
                    and len(apply_variants) == 2
                    and all(isinstance(variant, str) for variant in apply_variants)
                    and set(apply_variants) == {"vulnerable", "patched"}
                )
                require(
                    valid_variants,
                    f"{patch_prefix} apply_variants must equal vulnerable and patched",
                )
                results_by_variant = subject_patch.get(
                    "resulting_source_files_by_variant"
                )
                require(
                    isinstance(results_by_variant, dict),
                    f"{patch_prefix} resulting_source_files_by_variant must be an object",
                )
                if not valid_variants or not isinstance(results_by_variant, dict):
                    continue
                require(
                    set(results_by_variant) == set(apply_variants),
                    f"{patch_prefix} result variants must equal apply_variants",
                )
                for selected_variant in apply_variants:
                    selected_results = results_by_variant.get(selected_variant)
                    require(
                        isinstance(selected_results, dict) and bool(selected_results),
                        f"{patch_prefix} {selected_variant} results must be nonempty",
                    )
                    if not isinstance(selected_results, dict):
                        continue
                    for relative, result_sha256 in selected_results.items():
                        pure = pathlib.PurePosixPath(str(relative))
                        require(
                            isinstance(relative, str)
                            and bool(relative)
                            and not pure.is_absolute()
                            and ".." not in pure.parts
                            and pure.name not in {"", "."},
                            f"{patch_prefix} result path must be crate-relative",
                        )
                        require(
                            bool(SHA256_RE.fullmatch(str(result_sha256))),
                            f"{patch_prefix} result sha256 is invalid",
                        )

        patched_dependency_value = harness_case.get("patched_dependency")
        require(
            isinstance(patched_dependency_value, dict),
            f"{prefix} patched dependency must be an object",
        )
        patched_dependency = (
            patched_dependency_value if isinstance(patched_dependency_value, dict) else {}
        )
        patched_kind = patched_dependency.get("kind")
        corpus_advisory_value = corpus_case.get("advisory")
        corpus_advisory = (
            corpus_advisory_value if isinstance(corpus_advisory_value, dict) else {}
        )
        patched_versions = corpus_advisory.get("patched_versions", [])
        if patched_kind == "crates_io_archive":
            require(
                not case_has_local_patch,
                f"{prefix} upstream patched archive cannot be mixed with "
                "scenario-local patches",
            )
            patched_dependency = archive_metadata(
                patched_dependency,
                label=f"{prefix} patched dependency",
                expected_package=crate_name,
            )
            patched_version = patched_dependency.get("version")
            if (
                isinstance(patched_version, str)
                and isinstance(patched_versions, list)
                and all(isinstance(item, str) for item in patched_versions)
            ):
                try:
                    require(
                        any(
                            semver_matches_requirement(patched_version, requirement)
                            for requirement in patched_versions
                        ),
                        f"{prefix} patched dependency version is outside RustSec patched ranges",
                    )
                except ValueError as error:
                    require(False, f"{prefix} patched dependency version metadata is invalid: {error}")
            require(
                corpus_execution.get("patched_control")
                == "upstream_patched_version_required",
                f"{prefix} patched archive disagrees with corpus patched-control policy",
            )
        elif patched_kind == "scenario_local_patches":
            require(
                patched_dependency.get("base_version") == vulnerable_dependency.get("version"),
                f"{prefix} local-patch base version must equal the vulnerable version",
            )
            require(
                corpus_execution.get("patched_control") == "hashed_local_patch_required",
                f"{prefix} local-patch dependency disagrees with corpus patched-control policy",
            )
            require(
                not patched_versions,
                f"{prefix} local-patch policy requires no RustSec patched version",
            )
            require(
                bool(scenarios) and all(
                    isinstance(scenario, dict)
                    and (
                        isinstance(scenario.get("local_patched_control"), dict)
                        or (
                            scenario.get("adapter_kind") == "derived_adapter"
                            and isinstance(scenario.get("patched_source"), dict)
                        )
                    )
                    for scenario in scenarios
                ),
                f"{prefix} scenario-local patch policy requires every scenario "
                "to provide a patch or an explicit derived patched source",
            )
        else:
            require(False, f"{prefix} patched dependency kind is invalid")

        lockfiles = harness_case.get("lockfiles")
        require(isinstance(lockfiles, dict), f"{prefix} lockfiles must be an object")
        lockfiles = lockfiles if isinstance(lockfiles, dict) else {}
        require(set(lockfiles) == {"vulnerable", "patched"}, f"{prefix} lockfile arms are invalid")
        for arm in ("vulnerable", "patched"):
            lock_meta_value = lockfiles.get(arm)
            require(isinstance(lock_meta_value, dict), f"{prefix} {arm} lockfile must be an object")
            lock_meta = lock_meta_value if isinstance(lock_meta_value, dict) else {}
            lock_path = repo_artifact(
                case_id,
                lock_meta.get("path"),
                lock_meta.get("sha256"),
                f"{prefix} {arm} lockfile",
            )
            if lock_path is None:
                continue
            try:
                lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as error:
                require(False, f"{prefix} {arm} lockfile cannot be parsed: {error}")
                continue
            packages = lock.get("package") if isinstance(lock, dict) else None
            require(isinstance(packages, list), f"{prefix} {arm} lockfile has no package list")
            package_versions = {
                (package.get("name"), package.get("version"))
                for package in packages or []
                if isinstance(package, dict)
            }
            if arm == "vulnerable" or patched_kind == "scenario_local_patches":
                expected_lock_dependency = (
                    vulnerable_dependency.get("package"),
                    vulnerable_dependency.get("version"),
                )
            else:
                expected_lock_dependency = (
                    patched_dependency.get("package"),
                    patched_dependency.get("version"),
                )
            require(
                expected_lock_dependency in package_versions,
                f"{prefix} {arm} lockfile does not pin its selected dependency",
            )

    require(
        catalog_case_ids == expected_catalog_case_ids,
        "harness catalog case IDs disagree with pinned corpus readiness rows",
    )
    require(
        len(catalog_cases) == EXPECTED_HARNESS_COUNTS["harness_case_count"],
        "observed harness case count must equal 22",
    )
    require(
        repository_scenario_count
        == EXPECTED_HARNESS_COUNTS["repository_scenario_count"],
        "observed repository scenario count must equal 28",
    )
    require(
        adapter_counts["mechanical_adapter"]
        == EXPECTED_HARNESS_COUNTS["published_or_upstream_scenario_count"],
        "observed mechanical-adapter scenario count must equal 24",
    )
    require(
        adapter_counts["derived_adapter"]
        == EXPECTED_HARNESS_COUNTS["derived_adapter_scenario_count"],
        "observed derived-adapter scenario count must equal 4",
    )
    derived_reuse_scenario_count = role_counts["derived_reuse_experiment"]
    require(
        derived_reuse_scenario_count
        == EXPECTED_HARNESS_COUNTS["derived_reuse_scenario_count"],
        "observed derived-reuse scenario count must equal 2",
    )
    require(
        len(rudra_source_cases)
        == EXPECTED_HARNESS_COUNTS["pinned_rudra_source_scenario_count"],
        "observed pinned Rudra source scenario count must equal 18",
    )
    total_source_ready = repository_scenario_count + len(rudra_source_cases)
    require(
        total_source_ready
        == EXPECTED_HARNESS_COUNTS["total_source_ready_scenario_count"],
        "observed total source-ready scenario count must equal 46",
    )
    rudra_advisory_ids = {
        str(case.get("advisory_id")) for case in rudra_source_cases.values()
    }
    source_ready_advisory_ids = catalog_advisory_ids | rudra_advisory_ids
    require(
        len(source_ready_advisory_ids) == EXPECTED_SOURCE_READY_ADVISORY_COUNT,
        "repository harnesses plus Rudra sources must cover 40 distinct advisories",
    )
    require(
        not (catalog_advisory_ids & rudra_advisory_ids),
        "repository harness and Rudra source advisory sets must be disjoint",
    )

    observed_counts = {
        "harness_case_count": len(catalog_cases),
        "repository_scenario_count": repository_scenario_count,
        "published_or_upstream_scenario_count": adapter_counts["mechanical_adapter"],
        "derived_adapter_scenario_count": adapter_counts["derived_adapter"],
        "derived_reuse_scenario_count": derived_reuse_scenario_count,
        "pinned_rudra_source_scenario_count": len(rudra_source_cases),
        "total_source_ready_scenario_count": total_source_ready,
        "source_ready_distinct_advisory_count": len(source_ready_advisory_ids),
    }
    for field, observed in observed_counts.items():
        require(
            counts.get(field) == observed,
            f"harness declared {field} disagrees with observed metadata",
        )

    return {
        "catalog_path": str(catalog_path),
        "catalog_sha256": catalog_sha256,
        "counts": observed_counts,
        "distinct_advisory_count": len(source_ready_advisory_ids),
        "case_ids": sorted(catalog_case_ids),
        "scenario_ids": sorted(scenario_ids),
    }, blockers


def audit_manifest(
    manifest_path: pathlib.Path,
    *,
    rustsec_db: pathlib.Path | None = None,
    rudra_poc: pathlib.Path | None = None,
    harness_catalog: pathlib.Path | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {
            "schema_version": 1,
            "source": "rustsec-heap-security-corpus-audit",
            "passed": False,
            "catalog_ready": False,
            "claim_grade": False,
            "manifest": str(manifest_path),
            "blockers": [f"cannot read manifest: {error}"],
            "warnings": [],
            "summary": {},
        }
    if not isinstance(data, dict):
        return {
            "schema_version": 1,
            "source": "rustsec-heap-security-corpus-audit",
            "passed": False,
            "catalog_ready": False,
            "claim_grade": False,
            "manifest": str(manifest_path),
            "blockers": ["manifest root must be an object"],
            "warnings": [],
            "summary": {},
        }

    def require(condition: bool, message: str) -> None:
        if not condition:
            blockers.append(message)

    require(data.get("schema_version") == 1, "schema_version must equal 1")
    require(
        data.get("source") == "unialloc-rustsec-heap-security-corpus",
        "source must identify the UniAlloc RustSec heap corpus",
    )
    require(data.get("claim_grade") is False, "classification corpus must remain claim_grade=false")
    require(bool(data.get("claim_boundary")), "claim_boundary is required")

    snapshots = data.get("source_snapshots")
    require(isinstance(snapshots, dict), "source_snapshots must be an object")
    snapshots = snapshots if isinstance(snapshots, dict) else {}
    rustsec_snapshot = snapshots.get("rustsec_advisory_db", {})
    rudra_snapshot = snapshots.get("rudra_poc", {})
    for name, snapshot in (("rustsec_advisory_db", rustsec_snapshot), ("rudra_poc", rudra_snapshot)):
        require(isinstance(snapshot, dict), f"source snapshot {name} must be an object")
        if not isinstance(snapshot, dict):
            continue
        require(bool(COMMIT_RE.fullmatch(str(snapshot.get("commit", "")))), f"{name} commit must be a full 40-hex pin")
        require(str(snapshot.get("url", "")).startswith("https://"), f"{name} URL must use HTTPS")
        require(bool(snapshot.get("license")), f"{name} license/reference boundary is required")
        require(bool(SHA256_RE.fullmatch(str(snapshot.get("selected_files_sha256", "")))), f"{name} selected-files digest must be SHA-256")
    rustsec_source = rustsec_snapshot if isinstance(rustsec_snapshot, dict) else {}
    rudra_source = rudra_snapshot if isinstance(rudra_snapshot, dict) else {}
    require(
        rustsec_source.get("url") == RUSTSEC_SOURCE_URL,
        "rustsec_advisory_db URL must identify the pinned RustSec advisory-db",
    )
    require(
        rudra_source.get("url") == RUDRA_POC_SOURCE_URL,
        "rudra_poc URL must identify the pinned Rudra-PoC repository",
    )
    for field in (
        "advisory_file_count",
        "parsed_advisory_count",
        "memory_corruption_category_count",
        "candidate_screen_count",
        "selected_case_candidate_screen_count",
        "selected_case_outside_candidate_screen_count",
        "selected_file_count",
    ):
        require(
            isinstance(rustsec_source.get(field), int)
            and rustsec_source.get(field, -1) >= 0,
            f"rustsec_advisory_db {field} must be a nonnegative integer",
        )
    require(
        rustsec_source.get("parsed_advisory_count")
        == rustsec_source.get("advisory_file_count"),
        "RustSec snapshot must record a fully parsed advisory frame",
    )
    screened_selected = rustsec_source.get("selected_case_candidate_screen_count")
    outside_screen = rustsec_source.get("selected_case_outside_candidate_screen_count")
    selected_count = rustsec_source.get("selected_file_count")
    require(
        all(isinstance(value, int) for value in (screened_selected, outside_screen, selected_count))
        and screened_selected + outside_screen == selected_count,
        "RustSec selected-case screen partitions must sum to selected_file_count",
    )

    protocol = data.get("selection_protocol")
    require(isinstance(protocol, dict), "selection_protocol must be an object")
    protocol = protocol if isinstance(protocol, dict) else {}
    require(protocol.get("design") == "purposive_stratified_subset", "selection design must remain explicit and non-prevalence")
    require(_nonempty_strings(protocol.get("inclusion")), "selection inclusion criteria are required")
    require(_nonempty_strings(protocol.get("exclusion")), "selection exclusion criteria are required")
    require(bool(protocol.get("prevalence_use")), "prevalence-use boundary is required")
    quota_value = protocol.get("minimum_strata", {})
    require(isinstance(quota_value, dict), "minimum_strata must be an object")
    quotas = quota_value if isinstance(quota_value, dict) else {}
    require(
        quotas == SCHEMA_MINIMUM_STRATA,
        "minimum_strata must match the schema-version-1 frozen floors",
    )

    profiles = data.get("mechanism_profiles")
    require(isinstance(profiles, dict) and bool(profiles), "mechanism_profiles must be a nonempty object")
    profiles = profiles if isinstance(profiles, dict) else {}
    for profile_id, profile in profiles.items():
        prefix = f"mechanism profile {profile_id}"
        require(isinstance(profile, dict), f"{prefix} must be an object")
        if not isinstance(profile, dict):
            continue
        require(profile.get("control_role") in ALLOWED_CONTROL_ROLES, f"{prefix} has unknown control_role")
        require(
            profile.get("published_witness_type_isolation_effect")
            in ALLOWED_PUBLISHED_TYPEISO_EFFECTS,
            f"{prefix} has unknown published_witness_type_isolation_effect",
        )
        require(
            profile.get("derived_type_isolation_experiment_effect")
            in ALLOWED_DERIVED_TYPEISO_EFFECTS,
            f"{prefix} has unknown derived_type_isolation_experiment_effect",
        )
        require(
            profile.get("unialloc_boundary_effect") in ALLOWED_BOUNDARY_EFFECTS,
            f"{prefix} has unknown unialloc_boundary_effect",
        )
        require(profile.get("preregistered_hypothesis") in ALLOWED_HYPOTHESES, f"{prefix} has unknown preregistered_hypothesis")
        require(_nonempty_strings(profile.get("allocator_event_sequence")), f"{prefix} must declare allocator_event_sequence")
        require(bool(profile.get("claim")), f"{prefix} must declare a bounded claim")
        require(_nonempty_strings(profile.get("required_conditions")), f"{prefix} must declare required_conditions")
        require(_nonempty_strings(profile.get("boundaries")), f"{prefix} must declare boundaries")
        if (
            profile.get("published_witness_type_isolation_effect") == "no_direct_effect"
            and profile.get("derived_type_isolation_experiment_effect")
            == "no_direct_effect"
            and profile.get("unialloc_boundary_effect") == "no_direct_effect"
        ):
            require(
                profile.get("control_role") == "negative_control",
                f"{prefix} all-primary-axis no-effect profile must be a negative control",
            )
        if profile.get("preregistered_hypothesis") == "candidate_reuse_edge_blocked":
            require(
                profile.get("published_witness_type_isolation_effect")
                == "no_direct_effect",
                f"{prefix} published advisory witness must remain a no-direct-effect control",
            )
            require(
                profile.get("derived_type_isolation_experiment_effect")
                == "conditional_reuse_edge_mitigation",
                f"{prefix} derived experiment hypothesis must be reuse-conditional",
            )
            require(
                profile.get("unialloc_boundary_effect") == "no_direct_effect",
                f"{prefix} reuse-edge hypothesis cannot rely on a boundary detector",
            )
        if profile.get("preregistered_hypothesis") == "candidate_detected":
            require(
                profile.get("published_witness_type_isolation_effect")
                == "no_direct_effect",
                f"{prefix} detection hypothesis cannot be attributed to Type Isolation",
            )
            require(
                profile.get("derived_type_isolation_experiment_effect")
                == "no_direct_effect",
                f"{prefix} detection hypothesis cannot be attributed to a derived Type Isolation experiment",
            )
            require(
                profile.get("unialloc_boundary_effect") == "conditional_detection",
                f"{prefix} detection hypothesis must name a conditional UniAlloc boundary effect",
            )
        if profile.get("preregistered_hypothesis") == "negative_control_expected_unsafe":
            require(
                profile.get("published_witness_type_isolation_effect")
                == "no_direct_effect"
                and profile.get("derived_type_isolation_experiment_effect")
                == "no_direct_effect"
                and profile.get("unialloc_boundary_effect") == "no_direct_effect",
                f"{prefix} negative control must preregister no direct effect for all primary axes",
            )

    cases = data.get("cases")
    require(isinstance(cases, list), "cases must be a list")
    cases = cases if isinstance(cases, list) else []
    case_ids: set[str] = set()
    advisory_ids: set[str] = set()
    poc_ids: set[str] = set()
    dimensions: collections.Counter[str] = collections.Counter()
    cohorts: collections.Counter[str] = collections.Counter()
    primitives: collections.Counter[str] = collections.Counter()
    profile_counts: collections.Counter[str] = collections.Counter()
    published_effect_counts: collections.Counter[str] = collections.Counter()
    derived_effect_counts: collections.Counter[str] = collections.Counter()
    boundary_effect_counts: collections.Counter[str] = collections.Counter()
    readiness_counts: collections.Counter[str] = collections.Counter()
    control_counts: collections.Counter[str] = collections.Counter()
    advisory_paths: list[str] = []
    rudra_paths: list[str] = []
    safe_advisory_paths: list[str] = []
    safe_rudra_paths: list[str] = []

    for index, case in enumerate(cases):
        prefix = f"case[{index}]"
        require(isinstance(case, dict), f"{prefix} must be an object")
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("case_id", ""))
        advisory_id = str(case.get("advisory_id", ""))
        require(bool(CASE_ID_RE.fullmatch(case_id)), f"{prefix} has invalid case_id {case_id!r}")
        require(case_id not in case_ids, f"duplicate case_id: {case_id}")
        case_ids.add(case_id)
        require(bool(ADVISORY_ID_RE.fullmatch(advisory_id)), f"{prefix} has invalid advisory_id {advisory_id!r}")
        require(advisory_id not in advisory_ids, f"duplicate advisory_id: {advisory_id}")
        advisory_ids.add(advisory_id)
        require(bool(case.get("crate")), f"{prefix} crate is required")
        require(bool(case.get("title")), f"{prefix} title is required")
        cohort = case.get("cohort")
        require(cohort in ALLOWED_COHORTS, f"{prefix} has unknown cohort")
        cohorts[str(cohort)] += 1

        advisory = case.get("advisory")
        require(isinstance(advisory, dict), f"{prefix} advisory must be an object")
        advisory = advisory if isinstance(advisory, dict) else {}
        expected_url = f"https://rustsec.org/advisories/{advisory_id}.html"
        require(advisory.get("rustsec_url") == expected_url, f"{prefix} RustSec URL must bind to its advisory ID")
        advisory_path = str(advisory.get("snapshot_path", ""))
        advisory_path_is_safe = strict_posix_relative_path(
            advisory.get("snapshot_path")
        ) is not None
        require(
            advisory_path_is_safe,
            f"{prefix} advisory snapshot_path must be a strict POSIX relative path without root, empty, dot, or traversal segments",
        )
        expected_advisory_path = f"crates/{case.get('crate')}/{advisory_id}.md"
        require(
            advisory_path == expected_advisory_path,
            f"{prefix} advisory snapshot path must equal {expected_advisory_path}",
        )
        expected_snapshot_url = (
            f"{RUSTSEC_SOURCE_URL}/blob/{rustsec_source.get('commit')}/"
            f"{expected_advisory_path}"
        )
        require(
            advisory.get("snapshot_url") == expected_snapshot_url,
            f"{prefix} advisory snapshot URL must bind to its source commit, crate, and advisory ID",
        )
        require(bool(SHA256_RE.fullmatch(str(advisory.get("sha256", "")))), f"{prefix} advisory sha256 is invalid")
        patched_value = advisory.get("patched_versions")
        unaffected_value = advisory.get("unaffected_versions")
        require(isinstance(patched_value, list), f"{prefix} patched_versions must be a list")
        require(isinstance(unaffected_value, list), f"{prefix} unaffected_versions must be a list")
        patched_versions = patched_value if isinstance(patched_value, list) else []
        unaffected_versions = unaffected_value if isinstance(unaffected_value, list) else []
        patched_versions_valid = all(
            isinstance(item, str) and item.strip() for item in patched_versions
        )
        unaffected_versions_valid = all(
            isinstance(item, str) and item.strip() for item in unaffected_versions
        )
        require(
            patched_versions_valid,
            f"{prefix} patched_versions entries must be nonempty strings",
        )
        require(
            unaffected_versions_valid,
            f"{prefix} unaffected_versions entries must be nonempty strings",
        )
        advisory_paths.append(advisory_path)
        if advisory_path_is_safe:
            safe_advisory_paths.append(advisory_path)

        taxonomy = case.get("taxonomy")
        require(isinstance(taxonomy, dict), f"{prefix} taxonomy must be an object")
        taxonomy = taxonomy if isinstance(taxonomy, dict) else {}
        dimension = taxonomy.get("dimension")
        primitive = taxonomy.get("primary_primitive")
        require(dimension in ALLOWED_DIMENSIONS, f"{prefix} has unknown dimension")
        require(primitive in ALLOWED_PRIMARY_PRIMITIVES, f"{prefix} has unknown primary_primitive")
        secondary = taxonomy.get("secondary_primitives")
        require(isinstance(secondary, list), f"{prefix} secondary_primitives must be a list")
        require(
            isinstance(secondary, list)
            and all(item in ALLOWED_SECONDARY_PRIMITIVES for item in secondary),
            f"{prefix} has unknown secondary_primitive",
        )
        require(taxonomy.get("storage_domain") in ALLOWED_STORAGE_DOMAINS, f"{prefix} has unknown storage_domain")
        require(taxonomy.get("allocator_path") in ALLOWED_ALLOCATOR_PATHS, f"{prefix} has unknown allocator_path")
        require(taxonomy.get("allocator_visibility") in ALLOWED_ALLOCATOR_VISIBILITY, f"{prefix} has unknown allocator_visibility")
        require(taxonomy.get("lifecycle_phase") in ALLOWED_LIFECYCLE_PHASES, f"{prefix} has unknown lifecycle_phase")
        require(taxonomy.get("reuse_relation") in ALLOWED_REUSE_RELATIONS, f"{prefix} has unknown reuse_relation")
        require(taxonomy.get("identity_relation") in {"must_measure", "not_required"}, f"{prefix} has unknown identity_relation")
        require(taxonomy.get("size_bytes") == "must_measure", f"{prefix} size_bytes must remain an execution-time measurement")
        require(taxonomy.get("align_bytes") == "must_measure", f"{prefix} align_bytes must remain an execution-time measurement")
        require(taxonomy.get("semantic_cache_eligibility") == "must_measure", f"{prefix} semantic_cache_eligibility must remain an execution-time measurement")
        for required in (
            "storage_domain",
            "allocator_path",
            "root_cause",
            "root_cause_cluster",
            "allocator_visibility",
            "lifecycle_phase",
            "identity_relation",
            "size_bytes",
            "align_bytes",
            "semantic_cache_eligibility",
        ):
            require(bool(taxonomy.get(required)), f"{prefix} taxonomy.{required} is required")
        dimensions[str(dimension)] += 1
        primitives[str(primitive)] += 1

        profile_id = case.get("assessment_profile")
        require(profile_id in profiles, f"{prefix} references unknown assessment_profile")
        allowed_profile_primitives = PROFILE_PRIMARY_PRIMITIVES.get(str(profile_id), set())
        require(
            primitive in allowed_profile_primitives,
            f"{prefix} primary_primitive is incompatible with assessment_profile",
        )
        profile_counts[str(profile_id)] += 1
        profile_value = profiles.get(profile_id, {})
        profile = profile_value if isinstance(profile_value, dict) else {}
        published_effect_counts[
            str(profile.get("published_witness_type_isolation_effect"))
        ] += 1
        derived_effect_counts[
            str(profile.get("derived_type_isolation_experiment_effect"))
        ] += 1
        boundary_effect_counts[str(profile.get("unialloc_boundary_effect"))] += 1
        control_counts[str(profile.get("control_role"))] += 1
        if (
            profile.get("derived_type_isolation_experiment_effect")
            == "conditional_reuse_edge_mitigation"
        ):
            require(dimension == "temporal", f"{prefix} reuse-edge experiment must be temporal")
            require(primitive == "use_after_free", f"{prefix} reuse-edge experiment must be a UAF")
            require(taxonomy.get("identity_relation") == "must_measure", f"{prefix} reuse-edge experiment must measure identity relation")
            require(taxonomy.get("reuse_relation") in {"cross", "unknown"}, f"{prefix} reuse-edge experiment cannot preregister same/no reuse")

        execution = case.get("execution")
        require(isinstance(execution, dict), f"{prefix} execution must be an object")
        execution = execution if isinstance(execution, dict) else {}
        require(execution.get("claim_grade") is False, f"{prefix} classification row must remain claim_grade=false")
        replay_pin = execution.get("replay_pin")
        require(isinstance(replay_pin, str) and bool(replay_pin), f"{prefix} vulnerable replay pin is required")
        if (
            isinstance(replay_pin, str)
            and replay_pin
            and patched_versions_valid
            and unaffected_versions_valid
        ):
            try:
                require(
                    replay_pin_is_vulnerable(
                        replay_pin, patched_versions, unaffected_versions
                    ),
                    f"{prefix} replay pin is patched or explicitly unaffected",
                )
            except ValueError as error:
                require(False, f"{prefix} has unsupported version metadata: {error}")
        require(execution.get("readiness") in ALLOWED_READINESS, f"{prefix} has unknown readiness")
        require(execution.get("reproduction_kind") in ALLOWED_REPRODUCTION_KINDS, f"{prefix} has unknown reproduction_kind")
        repository_readiness = (
            execution.get("readiness") == "pinned_harness_source_available"
        )
        repository_reproduction = execution.get("reproduction_kind") == "repository_harness"
        require(
            repository_readiness is repository_reproduction,
            f"{prefix} repository harness readiness and reproduction kind must agree",
        )
        if repository_readiness:
            require(
                execution.get("harness_case_id") == case_id,
                f"{prefix} harness_case_id must bind to its case_id",
            )
        else:
            require(
                "harness_case_id" not in execution,
                f"{prefix} non-repository row must not name a harness_case_id",
            )
        require(execution.get("patched_control") in ALLOWED_PATCHED_CONTROLS, f"{prefix} must require a supported patched control")
        expected_patched_control = (
            "upstream_patched_version_required"
            if patched_versions
            else "hashed_local_patch_required"
        )
        require(
            execution.get("patched_control") == expected_patched_control,
            f"{prefix} patched-control strategy disagrees with RustSec versions",
        )
        require(execution.get("baseline_oracle") == "must_be_defined_before_execution", f"{prefix} baseline oracle must remain preregistered-before-run")
        require(execution.get("treatment_oracle") == "must_be_defined_before_execution", f"{prefix} treatment oracle must remain preregistered-before-run")
        readiness_counts[str(execution.get("readiness"))] += 1

        poc = case.get("poc")
        if cohort == "rudra_poc":
            require(isinstance(poc, dict), f"{prefix} Rudra cohort requires poc metadata")
            if isinstance(poc, dict):
                poc_id = str(poc.get("id", ""))
                require(bool(re.fullmatch(r"\d{4}", poc_id)), f"{prefix} has invalid Rudra PoC ID")
                require(poc_id not in poc_ids, f"duplicate Rudra PoC ID: {poc_id}")
                poc_ids.add(poc_id)
                poc_path = str(poc.get("snapshot_path", ""))
                poc_path_is_safe = strict_posix_relative_path(
                    poc.get("snapshot_path")
                ) is not None
                require(
                    poc_path_is_safe,
                    f"{prefix} Rudra snapshot_path must be a strict POSIX relative path without root, empty, dot, or traversal segments",
                )
                expected_poc_path = f"poc/{poc_id}-{poc.get('target_crate')}.rs"
                require(
                    poc_path == expected_poc_path,
                    f"{prefix} Rudra path must equal {expected_poc_path}",
                )
                expected_poc_url = (
                    f"{RUDRA_POC_SOURCE_URL}/blob/{rudra_source.get('commit')}/"
                    f"{expected_poc_path}"
                )
                require(
                    poc.get("snapshot_url") == expected_poc_url,
                    f"{prefix} Rudra snapshot URL must bind to its source commit, PoC ID, and target crate",
                )
                require(bool(SHA256_RE.fullmatch(str(poc.get("sha256", "")))), f"{prefix} Rudra PoC sha256 is invalid")
                require(bool(poc.get("target_crate")), f"{prefix} Rudra target crate is required")
                require(bool(poc.get("vulnerable_version")), f"{prefix} Rudra vulnerable version is required")
                require("indexed_version" in poc, f"{prefix} Rudra indexed version field is required")
                require(bool(str(poc.get("issue_url", "")).strip()), f"{prefix} Rudra issue reference is required")
                require(_nonempty_strings(poc.get("rudra_analyzers")), f"{prefix} Rudra analyzer classification is required")
                require(_nonempty_strings(poc.get("rudra_bug_classes")), f"{prefix} Rudra bug-class classification is required")
                require(
                    execution.get("replay_pin") == poc.get("vulnerable_version"),
                    f"{prefix} replay pin must equal the pinned Rudra target version",
                )
                expected_executable = execution.get("reproduction_kind") == "rudra_poc"
                require(poc.get("executable_source") is expected_executable, f"{prefix} Rudra executable-source marker disagrees with reproduction kind")
                rudra_paths.append(poc_path)
                if poc_path_is_safe:
                    safe_rudra_paths.append(poc_path)
        else:
            require(poc is None, f"{prefix} non-Rudra cohort must not claim a Rudra PoC")

    require(
        len(cases) >= SCHEMA_MINIMUM_STRATA["total_cases"],
        "case count is below the schema-version-1 minimum",
    )
    quota_observed = {
        "total_cases": len(cases),
        "rudra_poc_cases": cohorts["rudra_poc"],
        "rustsec_extended_cases": cohorts["rustsec_extended"],
        "temporal_cases": dimensions["temporal"],
        "reclaim_cases": dimensions["reclaim"],
        "spatial_cases": dimensions["spatial"],
        "initialization_cases": dimensions["initialization"],
        "concurrency_cases": dimensions["concurrency"],
        "provenance_cases": dimensions["provenance"],
        "negative_controls": control_counts["negative_control"],
    }
    for quota_name, minimum in SCHEMA_MINIMUM_STRATA.items():
        observed = quota_observed.get(quota_name)
        if observed is not None:
            require(observed >= minimum, f"{quota_name}={observed} is below minimum {minimum}")
    require(
        set(profile_counts) == set(profiles),
        "every mechanism profile must be referenced by at least one case",
    )
    require(control_counts["negative_control"] * 3 >= len(cases), "at least one third of the corpus must be no-effect negative controls")
    require(len(advisory_paths) == len(set(advisory_paths)), "advisory snapshot paths must be unique")
    require(len(rudra_paths) == len(set(rudra_paths)), "Rudra snapshot paths must be unique")
    require(rustsec_source.get("selected_file_count") == len(advisory_paths), "RustSec selected_file_count disagrees with cases")
    require(rudra_source.get("selected_file_count") == len(rudra_paths), "Rudra selected_file_count disagrees with cases")

    selected_harness_catalog = (
        harness_catalog.resolve() if harness_catalog is not None else DEFAULT_HARNESS_CATALOG
    )
    harness_summary, harness_blockers = audit_harness_catalog(
        selected_harness_catalog,
        bundle=data.get("harness_bundle"),
        corpus_cases=cases,
        profiles=profiles,
    )
    blockers.extend(harness_blockers)

    snapshot_requested = rustsec_db is not None or rudra_poc is not None
    snapshot_verified = rustsec_db is not None and rudra_poc is not None
    if rustsec_db is not None:
        head = git_head(rustsec_db)
        require(head == rustsec_source.get("commit"), "RustSec checkout HEAD differs from pinned commit")
        for case in cases:
            if not isinstance(case, dict):
                continue
            advisory_value = case.get("advisory", {})
            if not isinstance(advisory_value, dict):
                continue
            advisory = advisory_value
            relative = advisory.get("snapshot_path")
            try:
                path = confined_snapshot_path(rustsec_db, relative)
            except ValueError as error:
                require(False, f"RustSec snapshot path is unsafe: {relative}: {error}")
                continue
            require(path.is_file(), f"missing RustSec snapshot file: {relative}")
            if path.is_file():
                require(file_sha256(path) == advisory.get("sha256"), f"RustSec snapshot SHA-256 mismatch: {relative}")
                document = parse_rustsec_document(path)
                require(bool(document), f"cannot parse RustSec advisory: {relative}")
                if document:
                    parsed = document.get("advisory", {})
                    versions = document.get("versions", {})
                    require(isinstance(parsed, dict), f"RustSec advisory table is invalid: {relative}")
                    require(isinstance(versions, dict), f"RustSec versions table is invalid: {relative}")
                    if not isinstance(parsed, dict):
                        parsed = {}
                    if not isinstance(versions, dict):
                        versions = {}
                    require(parsed.get("id") == case.get("advisory_id"), f"RustSec advisory ID drift: {relative}")
                    require(parsed.get("package") == case.get("crate"), f"RustSec advisory package drift: {relative}")
                    require(_source_scalar(parsed.get("date")) == case.get("disclosure_date"), f"RustSec advisory date drift: {relative}")
                    require(parsed.get("url") == advisory.get("upstream_url"), f"RustSec upstream URL drift: {relative}")
                    require(parsed.get("categories", []) == advisory.get("categories"), f"RustSec categories drift: {relative}")
                    require(parsed.get("keywords", []) == advisory.get("keywords"), f"RustSec keywords drift: {relative}")
                    require(parsed.get("references", []) == advisory.get("references"), f"RustSec references drift: {relative}")
                    require(parsed.get("informational") == advisory.get("informational"), f"RustSec informational status drift: {relative}")
                    require(_source_scalar(parsed.get("withdrawn")) == advisory.get("withdrawn"), f"RustSec withdrawn status drift: {relative}")
                    require(versions.get("patched", []) == advisory.get("patched_versions"), f"RustSec patched versions drift: {relative}")
                    require(versions.get("unaffected", []) == advisory.get("unaffected_versions"), f"RustSec unaffected versions drift: {relative}")
                    require(parse_rustsec_heading(path) == case.get("title"), f"RustSec advisory title drift: {relative}")
        if (
            len(safe_advisory_paths) == len(advisory_paths)
            and all_confined_snapshot_files(rustsec_db, safe_advisory_paths)
        ):
            require(
                selected_files_sha256(rustsec_db, safe_advisory_paths) == rustsec_source.get("selected_files_sha256"),
                "RustSec selected-files aggregate digest mismatch",
            )
        query = rustsec_source.get("candidate_screen_query", {})
        observed_screen = rustsec_screen_counts(rustsec_db, query)
        for field in (
            "advisory_file_count",
            "parsed_advisory_count",
            "memory_corruption_category_count",
            "candidate_screen_count",
        ):
            require(observed_screen[field] == rustsec_source.get(field), f"RustSec {field} drift: expected {rustsec_source.get(field)}, observed {observed_screen[field]}")
        candidate_ids = set(observed_screen["candidate_ids"])
        selected_screen_count = len(advisory_ids & candidate_ids)
        require(
            selected_screen_count == rustsec_source.get("selected_case_candidate_screen_count"),
            "RustSec selected-case candidate-screen overlap drift: "
            f"expected {rustsec_source.get('selected_case_candidate_screen_count')}, "
            f"observed {selected_screen_count}",
        )
        require(
            len(advisory_ids - candidate_ids)
            == rustsec_source.get("selected_case_outside_candidate_screen_count"),
            "RustSec selected-case outside-screen count drift: "
            f"expected {rustsec_source.get('selected_case_outside_candidate_screen_count')}, "
            f"observed {len(advisory_ids - candidate_ids)}",
        )
    else:
        warnings.append("RustSec checkout was not supplied; source-byte and census verification were skipped")

    if rudra_poc is not None:
        head = git_head(rudra_poc)
        require(head == rudra_source.get("commit"), "Rudra-PoC checkout HEAD differs from pinned commit")
        for case in cases:
            if not isinstance(case, dict):
                continue
            poc = case.get("poc")
            if not isinstance(poc, dict):
                continue
            relative = poc.get("snapshot_path")
            try:
                path = confined_snapshot_path(rudra_poc, relative)
            except ValueError as error:
                require(False, f"Rudra PoC snapshot path is unsafe: {relative}: {error}")
                continue
            require(path.is_file(), f"missing Rudra PoC snapshot file: {relative}")
            if path.is_file():
                require(file_sha256(path) == poc.get("sha256"), f"Rudra PoC SHA-256 mismatch: {relative}")
                parsed = parse_rudra_metadata(path)
                require(bool(parsed), f"cannot parse Rudra PoC metadata: {relative}")
                if parsed:
                    target = parsed.get("target", {})
                    report = parsed.get("report", {})
                    bugs = parsed.get("bugs", [])
                    require(report.get("rustsec_id") == case.get("advisory_id"), f"Rudra/RustSec ID drift: {relative}")
                    require(report.get("issue_url") == poc.get("issue_url"), f"Rudra issue URL drift: {relative}")
                    require(target.get("crate") == poc.get("target_crate"), f"Rudra target crate drift: {relative}")
                    require(target.get("version") == poc.get("vulnerable_version"), f"Rudra vulnerable version drift: {relative}")
                    require(target.get("indexed_version") == poc.get("indexed_version"), f"Rudra indexed version drift: {relative}")
                    analyzers = sorted({str(bug.get("analyzer")) for bug in bugs if bug.get("analyzer")})
                    bug_classes = sorted({str(bug.get("bug_class")) for bug in bugs if bug.get("bug_class")})
                    require(analyzers == sorted(poc.get("rudra_analyzers", [])), f"Rudra analyzer classification drift: {relative}")
                    require(bug_classes == sorted(poc.get("rudra_bug_classes", [])), f"Rudra bug-class classification drift: {relative}")
        if (
            len(safe_rudra_paths) == len(rudra_paths)
            and all_confined_snapshot_files(rudra_poc, safe_rudra_paths)
        ):
            require(
                selected_files_sha256(rudra_poc, safe_rudra_paths) == rudra_source.get("selected_files_sha256"),
                "Rudra selected-files aggregate digest mismatch",
            )
        observed_rust_pocs = len(list((rudra_poc / "poc").glob("*.rs")))
        require(observed_rust_pocs == rudra_source.get("rust_poc_file_count"), f"Rudra PoC file-count drift: expected {rudra_source.get('rust_poc_file_count')}, observed {observed_rust_pocs}")
    else:
        warnings.append("Rudra-PoC checkout was not supplied; source-byte verification was skipped")

    if snapshot_requested and blockers:
        snapshot_verified = False
    warnings.append("catalog readiness is classification evidence only; no mitigation or detection result is claim-grade without raw matched-arm execution")
    passed = not blockers
    return {
        "schema_version": 1,
        "source": "rustsec-heap-security-corpus-audit",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "manifest": str(manifest_path),
        "manifest_sha256": file_sha256(manifest_path),
        "passed": passed,
        "catalog_ready": passed,
        "claim_grade": False,
        "snapshot_verification_requested": snapshot_requested,
        "snapshot_verified": snapshot_verified if snapshot_requested else None,
        "summary": {
            "case_count": len(cases),
            "cohorts": dict(sorted(cohorts.items())),
            "dimensions": dict(sorted(dimensions.items())),
            "primary_primitives": dict(sorted(primitives.items())),
            "assessment_profiles": dict(sorted(profile_counts.items())),
            "published_witness_type_isolation_effects": dict(
                sorted(published_effect_counts.items())
            ),
            "derived_type_isolation_experiment_effects": dict(
                sorted(derived_effect_counts.items())
            ),
            "unialloc_boundary_effects": dict(sorted(boundary_effect_counts.items())),
            "control_roles": dict(sorted(control_counts.items())),
            "execution_readiness": dict(sorted(readiness_counts.items())),
            "harness_bundle": harness_summary,
            "quota_observed": quota_observed,
        },
        "blockers": blockers,
        "warnings": warnings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--rustsec-db", type=pathlib.Path)
    parser.add_argument("--rudra-poc", type=pathlib.Path)
    parser.add_argument("--harness-catalog", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = audit_manifest(
        args.manifest.resolve(),
        rustsec_db=args.rustsec_db.resolve() if args.rustsec_db else None,
        rudra_poc=args.rudra_poc.resolve() if args.rudra_poc else None,
        harness_catalog=(
            args.harness_catalog.resolve() if args.harness_catalog else None
        ),
    )
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    if not args.quiet:
        sys.stdout.write(encoded)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
