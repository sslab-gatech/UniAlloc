#!/usr/bin/env python3
"""Run reproducible RustSec allocator experiments from the pinned harness catalog.

The default action is read-only ``list``.  ``preflight`` validates pinned
materializations and experiment topology without compiling or executing the
witness.  ``run`` executes crate build scripts and memory-unsafe witnesses on
the host only after the explicit ``--execute-unsafe`` opt-in.

Every result remains non-claim-grade.  Tool findings, clean executions, and
patched-control observations are recorded independently; a clean exit from a
Type Isolation arm is never classified as mitigation evidence by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from typing import Any, Iterable, Sequence


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import realworld_type_isolation_matrix as realworld  # noqa: E402
import run_rustsec_heap_harness as harness  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = ROOT / "evaluation" / "config" / "rustsec_heap_harnesses.json"
TOOLCHAIN = "nightly-2026-06-11"
VALIDATED_TARGET = "x86_64-unknown-linux-gnu"
VARIANTS = (
    "system",
    "unialloc",
    "reclaim_plain",
    "reclaim_checks",
    "typed_plain",
    "typeiso",
)
ARCHIVE_VARIANTS = ("vulnerable", "patched")
TYPEISO_VARIANTS = frozenset(("typed_plain", "typeiso"))
DIRECT_UNIALLOC_VARIANTS = frozenset(
    ("unialloc", "reclaim_plain", "reclaim_checks")
)
RECLAIM_ATTRIBUTION_VARIANTS = frozenset(("reclaim_plain", "reclaim_checks"))
DIRECT_UNIALLOC_RESOLVED_FEATURES = {
    "unialloc": ("default", "pthread_dtor", "rseq", "stats"),
    "reclaim_plain": ("stats",),
    "reclaim_checks": ("reclaim_checks", "stats"),
}
GROUND_TRUTH_TOOLS = frozenset(("asan", "asan_c_and_rust", "miri"))
ASAN_RUSTFLAGS = ("-Zsanitizer=address", "-Cforce-frame-pointers=yes")
STATS_PREFIX = "UNIALLOC_SECURITY_STATS="
REUSE_DENIAL_PREFIX = "UNIALLOC_SECURITY_REUSE_DENIAL="
VULNERABILITY_EDGE_IDENTITY_HOOK = "crate::with_vulnerability_edge_identity"
VULNERABILITY_EDGE_REPORT_HOOK = (
    "crate::report_vulnerability_edge_reuse_denial"
)
FINGERPRINT_SCHEMA = 1
RESULT_SCHEMA = 1

BASE_ENV_ALLOWLIST = (
    "CARGO_HOME",
    "HOME",
    "LOGNAME",
    "PATH",
    "RUSTUP_HOME",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
)
CATALOG_ENV_ALLOWLIST = frozenset(
    ("ASAN_OPTIONS", "CC", "CFLAGS", "MIRIFLAGS", "RUSTC_BOOTSTRAP")
)
CATALOG_CONSTRAINT_KEYS = frozenset(("platform",))
MAIN_PATTERN = re.compile(r"(?m)^(?P<indent>[ \t]*)fn[ \t]+main[ \t]*\([ \t]*\)[ \t]*\{")
ANY_MAIN_PATTERN = re.compile(
    r"(?m)^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?"
    r"(?:unsafe[ \t]+)?fn[ \t]+main\b"
)
GLOBAL_ALLOCATOR_TOKEN_RE = re.compile(r"\bglobal_allocator\b")
DIRECT_GLOBAL_ALLOCATOR_RE = re.compile(
    r"#\s*\[\s*global_allocator\s*\]", re.MULTILINE
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CARGO_FEATURE_RE = re.compile(r"^[A-Za-z0-9_+./?:-]+$")
ERROR_CODE_RE = re.compile(r"\bE\d{4}\b")
ASAN_FINDING_RE = re.compile(
    r"(?:ERROR|SUMMARY): AddressSanitizer|AddressSanitizer:\s*DEADLYSIGNAL",
    re.IGNORECASE,
)
ASAN_FINDING_PATTERNS = (
    (
        "asan_double_free",
        re.compile(
            r"AddressSanitizer:\s*(?:attempting\s+)?double-free\b",
            re.IGNORECASE,
        ),
    ),
    (
        "asan_heap_use_after_free",
        re.compile(r"AddressSanitizer:\s*heap-use-after-free\b", re.IGNORECASE),
    ),
    (
        "asan_heap_buffer_overflow",
        re.compile(r"AddressSanitizer:\s*heap-buffer-overflow\b", re.IGNORECASE),
    ),
    (
        "asan_bad_free",
        re.compile(
            r"AddressSanitizer:\s*(?:bad-free\b|attempting\s+free\s+on\s+"
            r"address\s+which\s+was\s+not\s+malloc\(\)-ed)",
            re.IGNORECASE,
        ),
    ),
    (
        "asan_deadly_fault",
        re.compile(
            r"AddressSanitizer:\s*(?:DEADLYSIGNAL\b|(?:SEGV|BUS|ILL|FPE)\b)",
            re.IGNORECASE,
        ),
    ),
)
MIRI_FINDING_RE = re.compile(
    r"Undefined Behavior|Data race detected|error:.*undefined behavior",
    re.IGNORECASE,
)
RUST_ASSERTION_RE = re.compile(
    r"\bassertion(?:\s+[`'][^`']+[`'])?\s+failed\b",
    re.IGNORECASE,
)
RUST_PANIC_OR_ASSERTION_RE = re.compile(
    rf"\bpanicked at\b|{RUST_ASSERTION_RE.pattern}", re.IGNORECASE
)
ADDRESS_RE = re.compile(
    r"(?:original(?:_address)?|stale_value)=(0x[0-9a-fA-F]+)"
    r".*replacement(?:_address)?=(0x[0-9a-fA-F]+)",
    re.DOTALL,
)
UNIALLOC_RELEASE_PANIC_RE = re.compile(
    r"(?m)^thread '[^'\r\n]+'(?: \(\d+\))? panicked at "
    r"(?:[^\r\n]*/)?unialloc/src/alloc_api/type_isolation\.rs:"
    r"[1-9][0-9]*:[1-9][0-9]*:\r?\n"
    r"pointer already released\r?$"
)
RUST_PANIC_RE = re.compile(r"\bpanicked at\b", re.IGNORECASE)
ALLOCATOR_STATS_FIELDS = (
    "total_allocations",
    "typed_allocations",
    "fallback_allocations",
    "typed_deallocations",
    "fallback_deallocations",
    "cache_hits",
    "cache_inserts",
    "cache_bypasses",
    "typed_cache_wrong_identity_denials",
    "last_wrong_identity_requested_type_id",
    "last_wrong_identity_retained_type_id",
    "last_wrong_identity_requested_module_id",
    "last_wrong_identity_retained_module_id",
    "last_wrong_identity_requested_callsite",
    "last_wrong_identity_size",
    "last_wrong_identity_align",
    "side_cache_entries",
    "side_cache_corrupt_slots",
)
REUSE_DENIAL_FIELDS = (
    "typed_cache_wrong_identity_denials",
    "last_wrong_identity_requested_type_id",
    "last_wrong_identity_retained_type_id",
    "last_wrong_identity_requested_module_id",
    "last_wrong_identity_retained_module_id",
    "last_wrong_identity_requested_callsite",
    "last_wrong_identity_size",
    "last_wrong_identity_align",
)
FORCE_BUILD_HOST_FLAGS = ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS")
FORCE_BUILD_ENVIRONMENT = {
    "RUSTFLAGS": None,
    "CARGO_ENCODED_RUSTFLAGS": None,
    "CARGO_INCREMENTAL": "0",
    "CARGO_PROFILE_RELEASE_PANIC": "unwind",
}


class ExperimentError(RuntimeError):
    """A fail-closed experiment setup or provenance error."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    return sha256_bytes(path.read_bytes())


def file_artifact_record(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        raise ExperimentError(f"missing artifact: {path}")
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256_bytes(encoded)


def write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def normalize_crate_name(value: str) -> str:
    normalized = value.strip().replace("-", "_")
    if not normalized or not re.fullmatch(r"[A-Za-z0-9_]+", normalized):
        raise ExperimentError(f"invalid rustc target crate: {value!r}")
    return normalized


def normalize_target_crates(*values: str) -> tuple[str, ...]:
    """Normalize Cargo/rustc crate names and preserve first-seen order."""

    result: list[str] = []
    observed: set[str] = set()
    for value in values:
        normalized = normalize_crate_name(value)
        if normalized not in observed:
            observed.add(normalized)
            result.append(normalized)
    return tuple(result)


def compiler_target_crates(
    case: dict[str, Any], scenario: dict[str, Any]
) -> tuple[str, ...]:
    """Resolve the rustc-pass target set, always retaining the harness crate."""

    harness_crate = normalize_crate_name(
        str(case["case_id"]).lower() + "-harness"
    )
    override = scenario.get("compiler_target_crates")
    if override is None:
        return normalize_target_crates(str(case["crate"]), harness_crate)
    if not isinstance(override, list) or not override:
        raise ExperimentError(
            "scenario compiler_target_crates must be a nonempty list"
        )
    if any(not isinstance(value, str) for value in override):
        raise ExperimentError(
            "scenario compiler_target_crates entries must be strings"
        )
    result = normalize_target_crates(*override)
    if harness_crate not in result:
        raise ExperimentError(
            "scenario compiler_target_crates must include harness crate "
            f"{harness_crate}"
        )
    subject_crate = normalize_crate_name(str(case["crate"]))
    exclusion = scenario.get("compiler_target_exclusion")
    if subject_crate in result:
        if exclusion is not None:
            raise ExperimentError(
                "compiler_target_exclusion is only valid when the subject crate "
                "is excluded"
            )
        return result
    annotation = scenario.get("type_isolation_edge_annotation")
    if (
        scenario.get("classification_role") != "derived_reuse_experiment"
        or not isinstance(annotation, dict)
        or annotation.get("kind") != "manual_exact_vulnerability_edge_identity"
    ):
        raise ExperimentError(
            "excluding the subject crate requires a manually attributed "
            "derived reuse experiment"
        )
    if not isinstance(exclusion, dict):
        raise ExperimentError(
            "excluding the subject crate requires compiler_target_exclusion"
        )
    if normalize_crate_name(str(exclusion.get("subject_crate", ""))) != subject_crate:
        raise ExperimentError(
            "compiler_target_exclusion.subject_crate must match the catalog subject"
        )
    for field in ("reason", "claim_boundary"):
        value = exclusion.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ExperimentError(
                f"compiler_target_exclusion.{field} must be a nonempty string"
            )
    if exclusion.get("compiler_automatic_victim_coverage") is not False:
        raise ExperimentError(
            "compiler_target_exclusion must record "
            "compiler_automatic_victim_coverage=false"
        )
    return result


def parse_csv(value: str, allowed: Iterable[str], label: str) -> tuple[str, ...]:
    allowed_values = tuple(allowed)
    selected = tuple(part.strip() for part in value.split(",") if part.strip())
    if not selected:
        raise ExperimentError(f"{label} must contain at least one value")
    unknown = sorted(set(selected) - set(allowed_values))
    if unknown:
        raise ExperimentError(f"unknown {label}: {', '.join(unknown)}")
    if len(set(selected)) != len(selected):
        raise ExperimentError(f"duplicate {label} values")
    return selected


def clean_base_environment() -> dict[str, str]:
    env = {
        name: os.environ[name]
        for name in BASE_ENV_ALLOWLIST
        if name in os.environ and "\0" not in os.environ[name]
    }
    env.setdefault(
        "PATH",
        "/usr/local/cargo/bin:/usr/local/bin:/usr/bin:/bin",
    )
    env.setdefault("HOME", str(pathlib.Path.home()))
    env["LANG"] = "C"
    env["LC_ALL"] = "C"
    return env


def validate_required_environment(
    required: object,
) -> tuple[dict[str, str], dict[str, str]]:
    if not isinstance(required, dict):
        raise ExperimentError("scenario required_environment must be an object")
    environment: dict[str, str] = {}
    constraints: dict[str, str] = {}
    for key, value in sorted(required.items()):
        if not isinstance(key, str) or not isinstance(value, str) or "\0" in value:
            raise ExperimentError("scenario environment keys and values must be strings")
        if key in CATALOG_ENV_ALLOWLIST:
            environment[key] = value
        elif key in CATALOG_CONSTRAINT_KEYS:
            constraints[key] = value
        else:
            raise ExperimentError(f"unapproved scenario environment key: {key}")
    return environment, constraints




def catalog_allocator_exclusion(scenario: dict[str, Any], allocator_variant: str) -> str | None:
    exclusions = scenario.get("unsupported_allocator_variants", {})
    if not isinstance(exclusions, dict):
        raise ExperimentError("scenario unsupported_allocator_variants must be an object")
    reason = exclusions.get(allocator_variant)
    if reason is None and allocator_variant in RECLAIM_ATTRIBUTION_VARIANTS:
        reason = exclusions.get("unialloc")
    if reason is None:
        return None
    if not isinstance(reason, str) or not reason.strip():
        raise ExperimentError("unsupported allocator exclusion reason must be a string")
    return reason


def allocator_cargo_feature_overrides(
    scenario: dict[str, Any],
) -> dict[str, list[str]]:
    """Validate allocator-specific subject dependency feature overrides.

    Overrides are scenario-local because sanitizer ground-truth arms can require
    instrumentation-only dependency features that native allocator diagnostics
    must omit.  Every configured entry is validated even when the current run
    selects a different allocator variant.
    """

    raw = scenario.get("allocator_cargo_feature_overrides")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ExperimentError(
            "allocator_cargo_feature_overrides must be an object"
        )
    result: dict[str, list[str]] = {}
    for variant, features in raw.items():
        if not isinstance(variant, str) or variant not in VARIANTS:
            raise ExperimentError(
                "allocator_cargo_feature_overrides contains unknown allocator "
                f"variant: {variant!r}"
            )
        if not isinstance(features, list):
            raise ExperimentError(
                "allocator_cargo_feature_overrides values must be feature lists"
            )
        normalized: list[str] = []
        for feature in features:
            if (
                not isinstance(feature, str)
                or not feature
                or not CARGO_FEATURE_RE.fullmatch(feature)
            ):
                raise ExperimentError(
                    "allocator_cargo_feature_overrides contains an invalid Cargo "
                    f"feature for {variant}: {feature!r}"
                )
            normalized.append(feature)
        if len(set(normalized)) != len(normalized):
            raise ExperimentError(
                "allocator_cargo_feature_overrides contains duplicate Cargo "
                f"features for {variant}"
            )
        result[variant] = normalized
    if "reclaim_plain" in result:
        if "reclaim_checks" not in result:
            raise ExperimentError(
                "reclaim_plain Cargo feature override requires the matched "
                "reclaim_checks override"
            )
        if result["reclaim_plain"] != result["reclaim_checks"]:
            raise ExperimentError(
                "reclaim_plain and reclaim_checks Cargo feature overrides must match"
            )
    return result


def validate_catalog_allocator_cargo_feature_overrides(
    catalog: dict[str, Any],
) -> None:
    """Reject malformed overrides before any catalog action proceeds."""

    _, scenarios = harness.index_catalog(catalog)
    for _, scenario in scenarios.values():
        allocator_cargo_feature_overrides(scenario)


def validate_catalog_compiler_target_crates(catalog: dict[str, Any]) -> None:
    """Reject malformed compiler targeting overrides before catalog actions."""

    _, scenarios = harness.index_catalog(catalog)
    for case, scenario in scenarios.values():
        compiler_target_crates(case, scenario)


def subject_dependency_configuration(
    manifest: pathlib.Path, crate_name: str
) -> dict[str, Any]:
    value = tomllib.loads(manifest.read_text(encoding="utf-8"))
    dependencies = value.get("dependencies")
    if not isinstance(dependencies, dict):
        raise ExperimentError("Cargo.toml dependencies must be a table")
    dependency = dependencies.get(crate_name)
    if not isinstance(dependency, dict):
        raise ExperimentError(
            f"Cargo.toml subject dependency must be an inline table: {crate_name}"
        )
    features = dependency.get("features", [])
    if not isinstance(features, list) or any(
        not isinstance(feature, str) for feature in features
    ):
        raise ExperimentError(
            f"Cargo.toml subject dependency has invalid features: {crate_name}"
        )
    default_features = dependency.get("default-features", True)
    if not isinstance(default_features, bool):
        raise ExperimentError(
            f"Cargo.toml subject dependency has invalid default-features: {crate_name}"
        )
    return {
        "features": list(features),
        "default_features_enabled": default_features,
    }


def replace_subject_dependency_features(
    manifest: pathlib.Path, crate_name: str, features: Sequence[str]
) -> None:
    """Replace the generated harness dependency's feature list exactly once."""

    text = manifest.read_text(encoding="utf-8")
    dependency_pattern = re.compile(
        rf"(?m)^(?P<prefix>{re.escape(json.dumps(crate_name))}\s*=\s*\{{)"
        rf"(?P<body>[^\n]*)(?P<suffix>\}}\s*)$"
    )
    matches = list(dependency_pattern.finditer(text))
    if len(matches) != 1:
        raise ExperimentError(
            "expected exactly one generated subject dependency entry for "
            f"{crate_name}"
        )
    match = matches[0]
    body = match.group("body")
    feature_pattern = re.compile(
        r"(?<![-A-Za-z0-9_])features\s*=\s*\[[^\]\n]*\]"
    )
    rewritten_body, count = feature_pattern.subn(
        "features = " + json.dumps(list(features)), body, count=1
    )
    if count != 1:
        raise ExperimentError(
            f"generated subject dependency lacks a feature list: {crate_name}"
        )
    rewritten = (
        text[: match.start()]
        + match.group("prefix")
        + rewritten_body
        + match.group("suffix")
        + text[match.end() :]
    )
    manifest.write_text(rewritten, encoding="utf-8")


def configure_subject_dependency_features(
    project: pathlib.Path,
    *,
    crate_name: str,
    scenario: dict[str, Any],
    allocator_variant: str,
) -> dict[str, Any]:
    """Apply and record the effective subject dependency feature selection."""

    overrides = allocator_cargo_feature_overrides(scenario)
    manifest = project / "Cargo.toml"
    catalog_configuration = subject_dependency_configuration(manifest, crate_name)
    override_variant = allocator_variant
    if (
        allocator_variant == "reclaim_plain"
        and allocator_variant not in overrides
        and "reclaim_checks" in overrides
    ):
        # A reclaim ablation must keep subject-crate Cargo features identical to
        # its treatment.  This also lets existing reviewed catalogs such as
        # RSH-075 acquire the matched arm without duplicating configuration.
        override_variant = "reclaim_checks"
    override_applied = override_variant in overrides
    effective_features = (
        overrides[override_variant]
        if override_applied
        else catalog_configuration["features"]
    )
    if override_applied:
        replace_subject_dependency_features(
            manifest, crate_name, effective_features
        )
    observed = subject_dependency_configuration(manifest, crate_name)
    if observed["features"] != effective_features:
        raise ExperimentError(
            "effective subject dependency features differ from the selected "
            f"configuration for {allocator_variant}"
        )
    if (
        observed["default_features_enabled"]
        != catalog_configuration["default_features_enabled"]
    ):
        raise ExperimentError(
            "allocator Cargo feature override changed default-features"
        )
    return {
        "allocator_variant": allocator_variant,
        "crate": crate_name,
        "catalog_features": catalog_configuration["features"],
        "effective_features": observed["features"],
        "default_features_enabled": observed["default_features_enabled"],
        "override_applied": override_applied,
        "override_allocator_variant": override_variant if override_applied else None,
        "override_source": (
            "scenario.allocator_cargo_feature_overrides"
            if override_applied
            else None
        ),
    }


def oracle_toolchain(scenario: dict[str, Any], mode: str) -> str:
    override = scenario.get("oracle_toolchain")
    if override is None or mode != "miri":
        return TOOLCHAIN
    if not isinstance(override, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", override):
        raise ExperimentError("scenario oracle_toolchain must be a toolchain name")
    return override

def execution_mode(tool: str, allocator_variant: str) -> str:
    """Keep sanitizer/interpreter ground truth separate from allocator pilots."""

    if allocator_variant != "system" and tool in GROUND_TRUTH_TOOLS:
        return "native_diagnostic"
    return tool


def effective_required_environment(
    required: dict[str, str], mode: str
) -> dict[str, str]:
    """Remove sanitizer-only environment from native allocator diagnostics."""

    result = dict(required)
    if mode != "native_diagnostic":
        return result
    result.pop("ASAN_OPTIONS", None)
    cflags = result.get("CFLAGS")
    if cflags is not None:
        retained = [
            value
            for value in shlex.split(cflags)
            if value not in ("-fsanitize=address", "-fno-omit-frame-pointer")
        ]
        if retained:
            result["CFLAGS"] = shlex.join(retained)
        else:
            result.pop("CFLAGS", None)
    return result


def transform_witness_source(source: str) -> str:
    """Convert the catalog-pinned standalone main into a private module entry."""

    candidates = list(ANY_MAIN_PATTERN.finditer(source))
    exact = list(MAIN_PATTERN.finditer(source))
    if len(candidates) != 1 or len(exact) != 1:
        raise ExperimentError(
            "witness must contain exactly one supported `fn main() {` definition"
        )
    match = exact[0]
    replacement = f"{match.group('indent')}pub(crate) fn run() {{"
    return source[: match.start()] + replacement + source[match.end() :]


def subject_allocator_topology(source: str, variant: str) -> dict[str, Any]:
    """Classify subject-owned global allocator syntax conservatively.

    Direct ``#[global_allocator]`` attributes are recognized precisely.  Any
    other occurrence of the reserved attribute name is treated as allocator
    ownership too: silently adding a second allocator is a worse failure mode
    than conservatively declining an allocator-substitution arm.
    """

    token_count = len(GLOBAL_ALLOCATOR_TOKEN_RE.findall(source))
    direct_count = len(DIRECT_GLOBAL_ALLOCATOR_RE.findall(source))
    detected = token_count > 0
    detection = (
        "direct_global_allocator_attribute"
        if direct_count > 0
        else "fail_closed_global_allocator_token"
        if detected
        else "absent"
    )
    substitution_supported = not detected or variant == "system"
    unsupported_reason = None
    if not substitution_supported:
        unsupported_reason = (
            "subject source defines or references #[global_allocator]; replacing "
            f"it with the {variant} allocator would create an ambiguous or duplicate "
            "global-allocator topology"
        )
    return {
        "subject_global_allocator_detected": detected,
        "detection": detection,
        "global_allocator_token_count": token_count,
        "direct_global_allocator_attribute_count": direct_count,
        "fail_closed": detected and direct_count == 0,
        "allocator_variant": variant,
        "substitution_supported": substitution_supported,
        "unsupported_reason": unsupported_reason,
        "wrapper_defines_global_allocator": not detected,
        "effective_allocator_owner": (
            "subject" if detected and variant == "system" else "experiment_wrapper"
        ),
        "subject_allocator_preserved": detected and variant == "system",
        "claim_grade": False,
    }


def effective_allocator_topology(
    source: str, variant: str, mode: str
) -> dict[str, Any]:
    """Record the allocator wrapper policy used by an experiment arm."""

    topology = subject_allocator_topology(source, variant)
    preserve_default = bool(
        variant == "system"
        and mode == "miri"
        and not topology["subject_global_allocator_detected"]
    )
    topology["default_system_allocator_preserved"] = preserve_default
    if preserve_default:
        topology["wrapper_defines_global_allocator"] = False
        topology["effective_allocator_owner"] = "rust_default_allocator"
        topology["allocator_wrapper_policy"] = "preserve_rust_default_for_miri"
    elif topology["subject_allocator_preserved"]:
        topology["allocator_wrapper_policy"] = "preserve_subject_allocator"
    else:
        topology["allocator_wrapper_policy"] = "explicit_experiment_wrapper"
    return topology


def stats_wrapper(
    allocator: str,
    *,
    vulnerability_edge_hooks: bool = False,
    legacy_allocator_abi_bridge: bool = False,
) -> str:
    stat_fields = (
        "total_allocations",
        "typed_allocations",
        "fallback_allocations",
        "typed_deallocations",
        "fallback_deallocations",
        "cache_hits",
        "cache_inserts",
        "cache_bypasses",
        "typed_cache_wrong_identity_denials",
        "last_wrong_identity_requested_type_id",
        "last_wrong_identity_retained_type_id",
        "last_wrong_identity_requested_module_id",
        "last_wrong_identity_retained_module_id",
        "last_wrong_identity_requested_callsite",
        "last_wrong_identity_size",
        "last_wrong_identity_align",
        "side_cache_entries",
        "side_cache_corrupt_slots",
    )
    format_template = STATS_PREFIX + '{{"allocator":' + json.dumps(allocator)
    format_template += "".join(f',"{field}":{{}}' for field in stat_fields)
    format_template += "}}"
    rust_format_literal = json.dumps(format_template)
    if vulnerability_edge_hooks and allocator in TYPEISO_VARIANTS:
        policy_flags = 0 if allocator == "typed_plain" else 1
        identity_helper = f'''pub(crate) fn with_vulnerability_edge_identity<R>(
    type_id: u64,
    module_id: u64,
    callsite: u64,
    f: impl FnOnce() -> R,
) -> R {{
    let metadata = AllocationMetadata::for_type(type_id)
        .with_module(module_id)
        .with_flags({policy_flags})
        .with_callsite(callsite);
    with_semantic_metadata(metadata, f)
}}
'''
        identity_imports = "with_semantic_metadata, AllocationMetadata, "
    elif vulnerability_edge_hooks:
        identity_helper = '''pub(crate) fn with_vulnerability_edge_identity<R>(
    _type_id: u64,
    _module_id: u64,
    _callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    f()
}
'''
        identity_imports = ""
    else:
        identity_helper = ""
        identity_imports = ""
    if vulnerability_edge_hooks:
        denial_format_template = (
            REUSE_DENIAL_PREFIX + '{{"allocator":' + json.dumps(allocator)
        )
        denial_format_template += "".join(
            f',"{field}":{{}}' for field in REUSE_DENIAL_FIELDS
        )
        denial_format_template += "}}"
        denial_rust_format_literal = json.dumps(denial_format_template)
        denial_reporter = f'''pub(crate) fn report_vulnerability_edge_reuse_denial() {{
    let stats = semantic_stats_snapshot();
    eprintln!(
        {denial_rust_format_literal},
        stats.typed_cache_wrong_identity_denials,
        stats.last_wrong_identity_requested_type_id,
        stats.last_wrong_identity_retained_type_id,
        stats.last_wrong_identity_requested_module_id,
        stats.last_wrong_identity_retained_module_id,
        stats.last_wrong_identity_requested_callsite,
        stats.last_wrong_identity_size,
        stats.last_wrong_identity_align,
    );
}}
'''
    else:
        denial_reporter = ""
    legacy_bridge = (
        legacy_rust_allocator_abi_bridge_source()
        if legacy_allocator_abi_bridge
        else ""
    )
    return f'''use unialloc::{{
    semantic_stats_recording_enable, semantic_stats_reset, semantic_stats_snapshot,
    type_isolation_side_cache_snapshot, {identity_imports}UniAlloc,
}};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

{legacy_bridge}
{identity_helper}
{denial_reporter}
mod witness;

fn main() {{
    semantic_stats_reset();
    semantic_stats_recording_enable();
    witness::run();
    let stats = semantic_stats_snapshot();
    let cache = type_isolation_side_cache_snapshot();
    eprintln!(
        {rust_format_literal},
        stats.total_allocations,
        stats.typed_allocations,
        stats.fallback_allocations,
        stats.typed_deallocations,
        stats.fallback_deallocations,
        stats.typed_cache_hits,
        stats.typed_cache_inserts,
        stats.typed_cache_bypasses,
        stats.typed_cache_wrong_identity_denials,
        stats.last_wrong_identity_requested_type_id,
        stats.last_wrong_identity_retained_type_id,
        stats.last_wrong_identity_requested_module_id,
        stats.last_wrong_identity_retained_module_id,
        stats.last_wrong_identity_requested_callsite,
        stats.last_wrong_identity_size,
        stats.last_wrong_identity_align,
        cache.occupied_entries,
        cache.corrupt_slots,
    );
}}
'''


def legacy_rust_allocator_abi_bridge_source() -> str:
    """Bridge the allocator ABI used by pre-1.32 ``default_allocator`` crates.

    Current Rust no longer exports the six unmangled ``__rust_*`` entrypoints.
    A catalog-pinned legacy dependency can request this mechanical wrapper so
    its allocator calls still flow through the allocator selected for the arm.
    Returning zero from the two in-place operations preserves the legacy ABI's
    documented "cannot resize in place" result.
    """

    return r'''fn legacy_allocator_layout(
    size: usize,
    align: usize,
) -> Option<std::alloc::Layout> {
    std::alloc::Layout::from_size_align(size, align).ok()
}

#[unsafe(no_mangle)]
unsafe extern "Rust" fn __rust_alloc(size: usize, align: usize) -> *mut u8 {
    let Some(layout) = legacy_allocator_layout(size, align) else {
        return std::ptr::null_mut();
    };
    unsafe { std::alloc::GlobalAlloc::alloc(&ALLOCATOR, layout) }
}

#[unsafe(no_mangle)]
unsafe extern "Rust" fn __rust_dealloc(ptr: *mut u8, size: usize, align: usize) {
    if let Some(layout) = legacy_allocator_layout(size, align) {
        unsafe { std::alloc::GlobalAlloc::dealloc(&ALLOCATOR, ptr, layout) }
    }
}

#[unsafe(no_mangle)]
unsafe extern "Rust" fn __rust_realloc(
    ptr: *mut u8,
    old_size: usize,
    align: usize,
    new_size: usize,
) -> *mut u8 {
    let Some(layout) = legacy_allocator_layout(old_size, align) else {
        return std::ptr::null_mut();
    };
    unsafe { std::alloc::GlobalAlloc::realloc(&ALLOCATOR, ptr, layout, new_size) }
}

#[unsafe(no_mangle)]
unsafe extern "Rust" fn __rust_alloc_zeroed(size: usize, align: usize) -> *mut u8 {
    let Some(layout) = legacy_allocator_layout(size, align) else {
        return std::ptr::null_mut();
    };
    unsafe { std::alloc::GlobalAlloc::alloc_zeroed(&ALLOCATOR, layout) }
}

#[unsafe(no_mangle)]
unsafe extern "Rust" fn __rust_grow_in_place(
    _ptr: *mut u8,
    _old_size: usize,
    _old_align: usize,
    _new_size: usize,
) -> u8 {
    0
}

#[unsafe(no_mangle)]
unsafe extern "Rust" fn __rust_shrink_in_place(
    _ptr: *mut u8,
    _old_size: usize,
    _old_align: usize,
    _new_size: usize,
) -> u8 {
    0
}
'''


def legacy_allocator_abi_bridge_requested(scenario: dict[str, Any]) -> bool:
    value = scenario.get("legacy_allocator_abi_bridge", False)
    if not isinstance(value, bool):
        raise ExperimentError("legacy_allocator_abi_bridge must be a boolean")
    return value


def system_vulnerability_edge_helpers() -> str:
    return '''pub(crate) fn with_vulnerability_edge_identity<R>(
    _type_id: u64,
    _module_id: u64,
    _callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    f()
}

pub(crate) fn report_vulnerability_edge_reuse_denial() {}

'''


def wrapper_source(
    variant: str,
    *,
    preserve_subject_allocator: bool = False,
    preserve_default_system_allocator: bool = False,
    vulnerability_edge_hooks: bool = False,
    legacy_allocator_abi_bridge: bool = False,
) -> str:
    if legacy_allocator_abi_bridge and (
        preserve_subject_allocator or preserve_default_system_allocator
    ):
        raise ExperimentError(
            "legacy_allocator_abi_bridge requires an explicit experiment allocator"
        )
    if variant == "system":
        helpers = (
            system_vulnerability_edge_helpers()
            if vulnerability_edge_hooks
            else ""
        )
        if preserve_subject_allocator or preserve_default_system_allocator:
            return helpers + """mod witness;

fn main() {
    witness::run();
}
"""
        legacy_bridge = (
            legacy_rust_allocator_abi_bridge_source()
            if legacy_allocator_abi_bridge
            else ""
        )
        return """use std::alloc::System;

#[global_allocator]
static ALLOCATOR: System = System;

""" + legacy_bridge + helpers + """mod witness;

fn main() {
    witness::run();
}
"""
    if variant in (*DIRECT_UNIALLOC_VARIANTS, *TYPEISO_VARIANTS):
        return stats_wrapper(
            variant,
            vulnerability_edge_hooks=vulnerability_edge_hooks,
            legacy_allocator_abi_bridge=legacy_allocator_abi_bridge,
        )
    raise ExperimentError(f"unsupported allocator variant: {variant}")


def transform_witness(
    project: pathlib.Path,
    variant: str,
    *,
    allocator_topology: dict[str, Any] | None = None,
    legacy_allocator_abi_bridge: bool = False,
) -> dict[str, str]:
    main = project / "src" / "main.rs"
    if not main.is_file():
        raise ExperimentError(f"missing materialized witness: {main}")
    original = main.read_text(encoding="utf-8")
    topology = allocator_topology or subject_allocator_topology(original, variant)
    if not topology.get("substitution_supported"):
        raise ExperimentError(str(topology.get("unsupported_reason")))
    witness = transform_witness_source(original)
    witness_path = project / "src" / "witness.rs"
    witness_path.write_text(witness, encoding="utf-8")
    generated = wrapper_source(
        variant,
        preserve_subject_allocator=bool(topology["subject_allocator_preserved"]),
        preserve_default_system_allocator=bool(
            topology.get("default_system_allocator_preserved", False)
        ),
        vulnerability_edge_hooks=(
            VULNERABILITY_EDGE_IDENTITY_HOOK in original
            and VULNERABILITY_EDGE_REPORT_HOOK in original
        ),
        legacy_allocator_abi_bridge=legacy_allocator_abi_bridge,
    )
    main.write_text(generated, encoding="utf-8")
    return {
        "original_source_sha256": sha256_bytes(original.encode("utf-8")),
        "witness_module_sha256": sha256_file(witness_path),
        "wrapper_source_sha256": sha256_file(main),
    }


def direct_dependencies(manifest: pathlib.Path) -> set[str]:
    value = tomllib.loads(manifest.read_text(encoding="utf-8"))
    dependencies = value.get("dependencies", {})
    if not isinstance(dependencies, dict):
        raise ExperimentError("Cargo.toml dependencies must be a table")
    return {str(name).replace("-", "_") for name in dependencies}


def lock_packages(lock: pathlib.Path) -> set[str]:
    value = tomllib.loads(lock.read_text(encoding="utf-8"))
    packages = value.get("package", [])
    if not isinstance(packages, list):
        raise ExperimentError("Cargo.lock package entries must be a list")
    result: set[str] = set()
    for package in packages:
        if isinstance(package, dict) and isinstance(package.get("name"), str):
            result.add(package["name"].replace("-", "_"))
    return result


def validate_allocator_identity(
    variant: str,
    manifest: pathlib.Path,
    lock: pathlib.Path,
    *,
    force_rlib: pathlib.Path | None,
    require_direct_lock: bool = True,
) -> None:
    direct = "unialloc" in direct_dependencies(manifest)
    locked = "unialloc" in lock_packages(lock)
    if variant in TYPEISO_VARIANTS:
        if direct or locked:
            raise ExperimentError(
                "mixed UniAlloc identity: force-loaded Type Isolation arm contains "
                "a Cargo UniAlloc dependency"
            )
        if force_rlib is not None and not force_rlib.is_file():
            raise ExperimentError(f"missing force-loaded UniAlloc rlib: {force_rlib}")
        return
    if force_rlib is not None:
        raise ExperimentError(
            f"mixed UniAlloc identity: {variant} arm received a force-loaded rlib"
        )
    if variant in DIRECT_UNIALLOC_VARIANTS and not direct:
        raise ExperimentError(f"{variant} arm requires a direct Cargo dependency")
    if variant in DIRECT_UNIALLOC_VARIANTS and require_direct_lock and not locked:
        raise ExperimentError(f"{variant} arm lock does not contain UniAlloc")


def configure_manifest(project: pathlib.Path, variant: str) -> None:
    manifest = project / "Cargo.toml"
    if variant == "unialloc":
        dependency = realworld.cargo_path_dependency(ROOT / "unialloc", ("stats",))
        realworld.add_dependency(manifest, dependency)
        realworld.add_spin_patch(manifest, realworld.find_cached_spin())
    elif variant in RECLAIM_ATTRIBUTION_VARIANTS:
        features = (
            ("stats", "reclaim_checks")
            if variant == "reclaim_checks"
            else ("stats",)
        )
        dependency = realworld.cargo_path_dependency(
            ROOT / "unialloc",
            features,
            default_features_enabled=False,
        )
        realworld.add_dependency(manifest, dependency)
        realworld.add_spin_patch(manifest, realworld.find_cached_spin())


def direct_unialloc_feature_evidence(variant: str) -> dict[str, Any] | None:
    if variant == "unialloc":
        return {"features": ["stats"], "default_features_enabled": True}
    if variant == "reclaim_plain":
        return {"features": ["stats"], "default_features_enabled": False}
    if variant == "reclaim_checks":
        return {
            "features": ["stats", "reclaim_checks"],
            "default_features_enabled": False,
        }
    return None


def ensure_within(path: pathlib.Path, root: pathlib.Path, *, label: str) -> pathlib.Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise ExperimentError(f"{label} escapes output directory: {path}") from error
    if resolved == resolved_root:
        raise ExperimentError(f"refusing to use output root as {label}")
    return resolved


def safe_remove_tree(path: pathlib.Path, root: pathlib.Path, *, label: str) -> None:
    ensure_within(path, root, label=label)
    if path.is_symlink():
        raise ExperimentError(f"refusing to remove symlinked {label}: {path}")
    if path.exists():
        if not path.is_dir():
            raise ExperimentError(f"{label} must be a directory: {path}")
        shutil.rmtree(path)


def reset_target_for_fingerprint(
    target: pathlib.Path,
    record_path: pathlib.Path,
    fingerprint: str,
    *,
    output_root: pathlib.Path,
    force_reset_reason: str | None = None,
) -> bool:
    if not SHA256_RE.fullmatch(fingerprint):
        raise ExperimentError("invalid arm fingerprint")
    previous: str | None = None
    if record_path.is_file():
        try:
            value = json.loads(record_path.read_text(encoding="utf-8"))
            candidate = value.get("fingerprint") if isinstance(value, dict) else None
            if isinstance(candidate, str):
                previous = candidate
        except (OSError, json.JSONDecodeError):
            previous = None
    changed = previous != fingerprint
    reset_reasons: list[str] = []
    if changed:
        reset_reasons.append("fingerprint_changed")
    if force_reset_reason is not None:
        if not force_reset_reason.strip():
            raise ExperimentError("forced target reset reason must be non-empty")
        reset_reasons.append(force_reset_reason)
    target_existed = target.exists()
    reset = bool(reset_reasons) and target_existed
    if reset:
        safe_remove_tree(target, output_root, label="arm target")
    target.mkdir(parents=True, exist_ok=True)
    write_json(
        record_path,
        {
            "schema_version": FINGERPRINT_SCHEMA,
            "fingerprint": fingerprint,
            "previous_fingerprint": previous,
            "target_reset": reset,
            "target_existed_before_reset": target_existed,
            "target_reset_reasons": reset_reasons,
            "full_rebuild_required": bool(reset_reasons) or not target_existed,
        },
    )
    return reset


def reset_arm_artifact_directory(
    artifact_dir: pathlib.Path, *, output_root: pathlib.Path
) -> None:
    """Create a fresh per-arm transcript directory for this execution."""

    safe_remove_tree(artifact_dir, output_root, label="arm artifact directory")
    artifact_dir.mkdir(parents=True)


def serialize_execution(
    result: dict[str, Any],
    *,
    cwd: pathlib.Path,
    environment: dict[str, str],
    artifact_dir: pathlib.Path,
    label: str,
) -> dict[str, Any]:
    stdout = bytes(result["stdout"])
    stderr = bytes(result["stderr"])
    stdout_path = artifact_dir / f"{label}.stdout"
    stderr_path = artifact_dir / f"{label}.stderr"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    return {
        "command": list(result["command"]),
        "cwd": str(cwd.resolve()),
        "environment_allowlist": sorted(environment),
        "environment": {key: environment[key] for key in sorted(environment)},
        "exit_code": result["exit_code"],
        "timed_out": bool(result["timed_out"]),
        "wall_seconds": result["wall_seconds"],
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "stdout_path": str(stdout_path.resolve()),
        "stderr_path": str(stderr_path.resolve()),
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
    }


def execute_recorded(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: pathlib.Path,
    environment: dict[str, str],
    timeout: int,
    artifact_dir: pathlib.Path,
    label: str,
) -> dict[str, Any]:
    result = realworld.execute(command, cwd=cwd, env=environment, timeout=timeout)
    return serialize_execution(
        result,
        cwd=cwd,
        environment=environment,
        artifact_dir=artifact_dir,
        label=label,
    )


def execution_text(record: dict[str, Any] | None) -> str:
    if not isinstance(record, dict):
        return ""
    return str(record.get("stdout", "")) + "\n" + str(record.get("stderr", ""))


def asan_finding_classes(text: str) -> tuple[str, ...]:
    """Return the concrete ASan classes present in a tool transcript."""

    return tuple(
        signature
        for signature, pattern in ASAN_FINDING_PATTERNS
        if pattern.search(text)
    )


def has_sanitizer_finding(text: str) -> bool:
    """Recognize sanitizer evidence without treating a tool name as a finding."""

    return bool(
        asan_finding_classes(text)
        or ASAN_FINDING_RE.search(text)
        or MIRI_FINDING_RE.search(text)
    )


def finding_signature(tool: str, text: str) -> str | None:
    if tool in ("asan", "asan_c_and_rust"):
        classes = asan_finding_classes(text)
        if len(classes) == 1:
            return classes[0]
        if len(classes) > 1:
            return "asan_ambiguous_finding"
        if ASAN_FINDING_RE.search(text):
            return "asan_unclassified_finding"
    if tool == "miri" and MIRI_FINDING_RE.search(text):
        return "miri_undefined_behavior"
    if tool == "native_address_trace" and address_reuse_observed(text):
        return "address_reuse_observed"
    if tool == "native_alignment" and RUST_ASSERTION_RE.search(text):
        return "native_alignment_assertion_finding"
    if tool == "native_value_oracle" and RUST_ASSERTION_RE.search(text):
        return "native_assertion_finding"
    return None


def address_reuse_observed(text: str) -> bool:
    match = ADDRESS_RE.search(text)
    return bool(match and int(match.group(1), 16) == int(match.group(2), 16))


def native_diagnostic_signature(record: dict[str, Any]) -> str | None:
    """Bind allocator diagnostics to a nonzero Rust panic on stderr.

    The exact reclaim signal requires the real UniAlloc source location and the
    adjacent canonical panic message.  Witness output that merely mentions the
    message, including stdout text and successful executions, cannot satisfy
    this parser.
    """

    exit_code = record.get("exit_code")
    if (
        isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or exit_code == 0
        or bool(record.get("timed_out"))
    ):
        return None
    stderr = record.get("stderr", "")
    if not isinstance(stderr, str):
        return None
    if UNIALLOC_RELEASE_PANIC_RE.search(stderr):
        return "unialloc_pointer_already_released_check"
    if RUST_PANIC_RE.search(stderr):
        return "rust_panic_observed"
    return None


def parse_expected_oracle_contract(
    *, tool: str, archive_variant: str, expected: str
) -> dict[str, Any]:
    """Parse catalog prose into a small, fail-closed evidence contract.

    The parser deliberately recognizes only the vocabulary used by the pinned
    catalog.  Unknown or multiply classified descriptions remain observable in
    result artifacts and can never satisfy an oracle.
    """

    lowered = expected.casefold()
    error_codes = sorted(set(ERROR_CODE_RE.findall(expected)))
    base: dict[str, Any] = {
        "parse_status": "parsed",
        "kind": None,
        "expected_finding_signature": None,
        "required_evidence": [],
        "compile_error_codes": error_codes,
    }

    if error_codes:
        base["kind"] = "compile_rejection"
        base["required_evidence"] = ["build_nonzero", "compile_error_code"]
        return base

    if archive_variant == "patched":
        candidates: list[str] = []
        safe_panic_markers = (
            "intentional panic",
            "with an assertion",
            "safe rust panic",
            "fails safely without asan finding",
        )
        clean_exit_markers = ("exit 0", "exits 0", "passes all")
        if any(marker in lowered for marker in safe_panic_markers):
            candidates.append("safe_rust_panic")
        if any(marker in lowered for marker in clean_exit_markers):
            candidates.append("clean_exit")
        if len(candidates) != 1:
            base["parse_status"] = "ambiguous" if candidates else "unknown"
            base["kind"] = "unknown"
            return base
        base["kind"] = candidates[0]
        if candidates[0] == "safe_rust_panic":
            base["required_evidence"] = [
                "run_nonzero",
                "rust_panic_or_assertion",
                "no_sanitizer_finding",
            ]
        else:
            base["required_evidence"] = ["clean_exit", "no_sanitizer_finding"]
            if "same address" in lowered and "grooming" in lowered:
                base["required_evidence"].append("address_reuse")
        return base

    if archive_variant != "vulnerable":
        base["parse_status"] = "unknown"
        base["kind"] = "unknown"
        return base

    if tool in ("asan", "asan_c_and_rust"):
        finding_candidates: list[str] = []
        expected_patterns = (
            ("asan_double_free", re.compile(r"\bdouble[- ]free\b")),
            (
                "asan_heap_use_after_free",
                re.compile(r"\bheap[- ]use[- ]after[- ]free\b"),
            ),
            (
                "asan_heap_buffer_overflow",
                re.compile(r"\bheap[- ]buffer[- ]overflow\b"),
            ),
            ("asan_bad_free", re.compile(r"\b(?:bad|invalid)[- ]free\b")),
            (
                "asan_deadly_fault",
                re.compile(
                    r"\bsanitizer fault\b|\binvalid write at forged address\b|"
                    r"\bwild (?:high-address )?out-of-bounds (?:write|read|access)\b"
                ),
            ),
        )
        for signature, pattern in expected_patterns:
            if pattern.search(lowered):
                finding_candidates.append(signature)
        if len(finding_candidates) != 1:
            base["parse_status"] = (
                "ambiguous" if finding_candidates else "unknown"
            )
            base["kind"] = "unknown"
            return base
        base["kind"] = "asan_finding"
        base["expected_finding_signature"] = finding_candidates[0]
        base["required_evidence"] = [
            finding_candidates[0],
            "run_nonzero",
        ]
        if (
            "address equals" in lowered
            or "addresses are equal" in lowered
            or "same address" in lowered
        ):
            base["required_evidence"].append("address_reuse_same_run")
        return base

    if tool == "miri" and "miri reports" in lowered:
        base["kind"] = "miri_undefined_behavior"
        base["expected_finding_signature"] = "miri_undefined_behavior"
        base["required_evidence"] = ["miri_undefined_behavior", "run_nonzero"]
        return base

    if tool == "native_address_trace" and (
        "address equals" in lowered
        or "addresses are equal" in lowered
        or "same address" in lowered
    ):
        base["kind"] = "native_address_reuse"
        base["expected_finding_signature"] = "address_reuse_observed"
        base["required_evidence"] = ["address_reuse"]
        return base

    if tool == "native_value_oracle" and "assertion fails" in lowered:
        base["kind"] = "native_assertion"
        base["expected_finding_signature"] = "native_assertion_finding"
        base["required_evidence"] = ["native_assertion_finding", "run_nonzero"]
        return base

    if tool == "native_alignment" and "assertion fails" in lowered:
        base["kind"] = "native_assertion"
        base["expected_finding_signature"] = (
            "native_alignment_assertion_finding"
        )
        base["required_evidence"] = [
            "native_alignment_assertion_finding",
            "run_nonzero",
        ]
        return base

    base["parse_status"] = "unknown"
    base["kind"] = "unknown"
    return base


def repetition_observations(
    *, tool: str, mode: str, runs: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for index, record in enumerate(runs, start=1):
        text = execution_text(record)
        tool_signature = (
            finding_signature(tool, text) if mode != "native_diagnostic" else None
        )
        diagnostic_signature = (
            native_diagnostic_signature(record)
            if mode == "native_diagnostic"
            else None
        )
        observations.append(
            {
                "repetition": index,
                "exit_code": record.get("exit_code"),
                "timed_out": bool(record.get("timed_out")),
                "clean_exit": record.get("exit_code") == 0
                and not record.get("timed_out"),
                "tool_finding_signature": tool_signature,
                "native_diagnostic_signature": diagnostic_signature,
                "address_reuse_observed": address_reuse_observed(text),
                "rust_panic_or_assertion_observed": bool(
                    RUST_PANIC_OR_ASSERTION_RE.search(text)
                ),
                "sanitizer_finding_observed": has_sanitizer_finding(text),
                "claim_grade": False,
                "mitigation_inferred": False,
            }
        )
    return observations


def validate_expected_oracle(
    *,
    tool: str,
    mode: str,
    archive_variant: str,
    expected: str,
    build: dict[str, Any] | None,
    runs: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    contract = parse_expected_oracle_contract(
        tool=tool,
        archive_variant=archive_variant,
        expected=expected,
    )
    observations = repetition_observations(tool=tool, mode=mode, runs=runs)
    build_text = execution_text(build)
    expected_codes = list(contract["compile_error_codes"])
    build_exit = build.get("exit_code") if isinstance(build, dict) else None
    run_exits = [run.get("exit_code") for run in runs]
    timed_out = bool(
        (isinstance(build, dict) and build.get("timed_out"))
        or any(run.get("timed_out") for run in runs)
    )
    tool_signatures = [
        str(value)
        for value in (
            observation["tool_finding_signature"] for observation in observations
        )
        if value is not None
    ]
    diagnostic_signatures = [
        str(value)
        for value in (
            observation["native_diagnostic_signature"]
            for observation in observations
        )
        if value is not None
    ]
    matched_codes = [code for code in expected_codes if code in build_text]
    diagnostic_only = mode == "native_diagnostic"

    if timed_out:
        status = "timed_out"
        observed = False
    elif contract["kind"] == "compile_rejection":
        compile_rejection_observed = (
            build_exit not in (None, 0)
            and len(matched_codes) == len(expected_codes)
        )
        if diagnostic_only and compile_rejection_observed:
            status = "native_diagnostic_compile_rejection_observed"
            observed = False
        elif compile_rejection_observed:
            status = "patched_control_compile_rejection_observed"
            observed = True
        elif build_exit not in (None, 0):
            status = "unexpected_build_failure"
            observed = False
        else:
            status = "expected_compile_rejection_not_observed"
            observed = False
    elif build_exit not in (None, 0):
        status = "unexpected_build_failure"
        observed = False
    elif diagnostic_only and diagnostic_signatures:
        status = "native_diagnostic_signal_observed"
        observed = False
    elif diagnostic_only and not runs:
        status = "native_diagnostic_run_not_performed"
        observed = False
    elif diagnostic_only and all(value == 0 for value in run_exits):
        status = "native_clean_runs_inconclusive"
        observed = False
    elif diagnostic_only:
        status = "native_abnormal_exit_inconclusive"
        observed = False
    elif not runs:
        status = "run_not_performed"
        observed = False
    elif contract["parse_status"] != "parsed":
        status = f'oracle_contract_{contract["parse_status"]}'
        observed = False
    elif contract["kind"] == "safe_rust_panic":
        sanitizer_observed = any(
            observation["sanitizer_finding_observed"]
            for observation in observations
        )
        safe_panics = all(
            observation["exit_code"] not in (None, 0)
            and observation["rust_panic_or_assertion_observed"]
            and not observation["sanitizer_finding_observed"]
            for observation in observations
        )
        if sanitizer_observed:
            status = "patched_control_sanitizer_finding_observed"
            observed = False
        elif safe_panics:
            status = "patched_safe_panic_control_observed"
            observed = True
        else:
            status = "patched_safe_panic_contract_mismatch"
            observed = False
    elif contract["kind"] == "clean_exit":
        sanitizer_observed = any(
            observation["sanitizer_finding_observed"]
            for observation in observations
        )
        address_required = "address_reuse" in contract["required_evidence"]
        clean_controls = all(
            observation["clean_exit"]
            and not observation["sanitizer_finding_observed"]
            and (
                not address_required or observation["address_reuse_observed"]
            )
            for observation in observations
        )
        if sanitizer_observed:
            status = "patched_control_sanitizer_finding_observed"
            observed = False
        elif clean_controls:
            status = "clean_tool_runs_observed"
            observed = True
        elif address_required and all(
            observation["clean_exit"] for observation in observations
        ):
            status = "patched_address_reuse_contract_mismatch"
            observed = False
        else:
            status = "patched_clean_exit_contract_mismatch"
            observed = False
    elif contract["kind"] in (
        "asan_finding",
        "miri_undefined_behavior",
        "native_assertion",
    ):
        expected_signature = contract["expected_finding_signature"]
        address_required = (
            "address_reuse_same_run" in contract["required_evidence"]
        )
        exact_signature_observations = [
            observation
            for observation in observations
            if observation["tool_finding_signature"] == expected_signature
        ]
        conflicting_signatures = [
            observation["tool_finding_signature"]
            for observation in observations
            if observation["tool_finding_signature"] is not None
            and observation["tool_finding_signature"] != expected_signature
        ]
        exact_matches = [
            observation
            for observation in exact_signature_observations
            if observation["exit_code"] not in (None, 0)
            and (
                not address_required or observation["address_reuse_observed"]
            )
        ]
        if conflicting_signatures:
            status = "tool_finding_class_mismatch"
            observed = False
        elif exact_matches:
            status = "tool_finding_observed"
            observed = True
        elif exact_signature_observations and address_required:
            status = "tool_finding_conjunction_mismatch"
            observed = False
        elif exact_signature_observations:
            status = "tool_finding_exit_contract_mismatch"
            observed = False
        elif any(
            observation["sanitizer_finding_observed"]
            for observation in observations
        ):
            status = "tool_finding_class_mismatch"
            observed = False
        elif all(value == 0 for value in run_exits):
            status = "clean_tool_runs_observed"
            observed = False
        else:
            status = "abnormal_exit_without_validated_tool_finding"
            observed = False
    elif contract["kind"] == "native_address_reuse":
        if any(
            observation["address_reuse_observed"]
            for observation in observations
        ):
            status = "tool_finding_observed"
            observed = True
        elif all(value == 0 for value in run_exits):
            status = "clean_tool_runs_observed"
            observed = False
        else:
            status = "native_address_reuse_not_observed"
            observed = False
    else:
        status = "oracle_contract_unknown"
        observed = False

    return {
        "expected_description": expected,
        "expected_contract": contract,
        "expected_oracle_observed": observed,
        "oracle_role": (
            "allocator_native_diagnostic" if diagnostic_only else "catalog_oracle"
        ),
        "diagnostic_only": diagnostic_only,
        "tool_finding_signatures": tool_signatures,
        "native_diagnostic_signatures": diagnostic_signatures,
        "matched_compile_error_codes": matched_codes,
        "repetition_observations": observations,
        "status": status,
        "mitigation_inferred": False,
        "claim_grade": False,
        "boundary": (
            "A clean exit or absent tool finding does not establish mitigation; "
            "matched repeated baseline/treatment evidence is required."
        ),
    }


def parse_runtime_stats(*records: dict[str, Any] | None) -> dict[str, Any] | None:
    parsed: dict[str, Any] | None = None
    for record in records:
        for line in execution_text(record).splitlines():
            if not line.startswith(STATS_PREFIX):
                continue
            try:
                candidate = json.loads(line[len(STATS_PREFIX) :])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
    return parsed


def prefixed_json_objects(
    record: dict[str, Any] | None, prefix: str
) -> tuple[list[dict[str, Any]], int, int]:
    """Decode object-valued JSON lines carrying an exact experiment prefix."""

    lines = [
        line[len(prefix) :]
        for line in execution_text(record).splitlines()
        if line.startswith(prefix)
    ]
    decoded: list[dict[str, Any]] = []
    malformed = 0
    for payload in lines:
        try:
            candidate = json.loads(payload)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(candidate, dict):
            decoded.append(candidate)
    return decoded, len(lines), malformed


def load_rewrite_candidates(audit_dir: pathlib.Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if not audit_dir.is_dir():
        return candidates
    for path in sorted(audit_dir.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = value.get("rewrite_candidates", [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                candidates.append({**row, "audit_path": str(path.resolve())})
    return candidates


def bind_reuse_denial_report_to_audits(
    report: dict[str, Any],
    *,
    audit_dir: pathlib.Path,
    annotation: dict[str, Any],
) -> dict[str, Any]:
    """Bind allocator metadata to the compiler-audited replacement site."""

    requested_type_id = report["last_wrong_identity_requested_type_id"]

    def type_identity_matches(candidate: dict[str, Any]) -> bool:
        if candidate.get("type_id") == requested_type_id:
            return True
        return (
            candidate.get("type_id") == 0
            and candidate.get("type_id_basis")
            == "monomorphized_compiler_type_id_runtime"
            and candidate.get("rewrite_status")
            == "actual_semantic_scope_generic_type_rewrite_applied"
            and "push_for_rust_type"
            in str(candidate.get("replacement_symbol", ""))
        )

    exact = [
        candidate
        for candidate in load_rewrite_candidates(audit_dir)
        if type_identity_matches(candidate)
        and candidate.get("module_id")
        == report["last_wrong_identity_requested_module_id"]
        and candidate.get("callsite")
        == report["last_wrong_identity_requested_callsite"]
    ]
    source_fragment = str(annotation["replacement_source_file_fragment"])
    type_fragment = str(annotation["replacement_semantic_type_fragment"])
    matching = [
        candidate
        for candidate in exact
        if source_fragment in str(candidate.get("source_span", ""))
        and type_fragment in str(candidate.get("semantic_object_type", ""))
        and candidate.get("rewrite_status")
        in {
            "actual_semantic_scope_enter_exit_rewrite_applied",
            "actual_semantic_scope_generic_type_rewrite_applied",
        }
    ]
    if len(matching) != 1:
        return {
            "valid": False,
            "status": "replacement_site_binding_not_unique",
            "exact_metadata_candidate_count": len(exact),
            "matching_replacement_candidate_count": len(matching),
            "claim_grade": False,
        }
    candidate = matching[0]
    return {
        "valid": True,
        "status": "allocator_event_bound_to_compiler_replacement_site",
        "allocation_site_id": candidate.get("allocation_site_id"),
        "source_span": candidate.get("source_span"),
        "semantic_object_type": candidate.get("semantic_object_type"),
        "rewrite_status": candidate.get("rewrite_status"),
        "audit_path": candidate.get("audit_path"),
        "type_id": candidate.get("type_id"),
        "requested_runtime_type_id": requested_type_id,
        "type_id_binding": (
            "exact_numeric_type_id"
            if candidate.get("type_id") == requested_type_id
            else "monomorphized_compiler_type_id_runtime"
        ),
        "module_id": candidate.get("module_id"),
        "callsite": candidate.get("callsite"),
        "claim_grade": False,
    }


def validate_reuse_denial_record(
    allocator_variant: str,
    record: dict[str, Any],
    *,
    repetition: int,
    annotation: dict[str, Any],
    audit_dir: pathlib.Path,
) -> dict[str, Any]:
    decoded, line_count, malformed = prefixed_json_objects(
        record, REUSE_DENIAL_PREFIX
    )
    matching = [
        candidate
        for candidate in decoded
        if candidate.get("allocator") == allocator_variant
    ]
    errors: list[str] = []
    if len(matching) != 1:
        errors.append(
            "expected exactly one reuse-denial report with allocator="
            f"{allocator_variant!r}; observed {len(matching)}"
        )
    report = matching[0] if len(matching) == 1 else None
    if report is not None:
        missing = [field for field in REUSE_DENIAL_FIELDS if field not in report]
        if missing:
            errors.append("missing reuse-denial fields: " + ", ".join(missing))
        for field in REUSE_DENIAL_FIELDS:
            value = report.get(field)
            if field in report and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                errors.append(f"{field} must be a non-negative integer")

    schema_valid = not errors
    event_observed = bool(
        schema_valid and report["typed_cache_wrong_identity_denials"] > 0
    )
    expected_layout = annotation["expected_layout"]
    match_checks = (
        schema_valid
        and report is not None
        and report["last_wrong_identity_retained_type_id"]
        == annotation["victim_type_id"]
        and report["last_wrong_identity_retained_module_id"]
        == annotation["victim_module_id"]
        and report["last_wrong_identity_requested_type_id"] != 0
        and report["last_wrong_identity_requested_type_id"]
        != report["last_wrong_identity_retained_type_id"]
        and report["last_wrong_identity_requested_module_id"] != 0
        and report["last_wrong_identity_requested_callsite"] != 0
        and report["last_wrong_identity_size"] == expected_layout["size"]
        and report["last_wrong_identity_align"] == expected_layout["align"]
    )
    event_matches_annotation = bool(event_observed and match_checks)
    site_binding = (
        bind_reuse_denial_report_to_audits(
            report, audit_dir=audit_dir, annotation=annotation
        )
        if event_matches_annotation and report is not None
        else {
            "valid": None,
            "status": "no_matching_allocator_event_to_bind",
            "claim_grade": False,
        }
    )
    direct_edge_coverage = bool(
        event_matches_annotation and site_binding.get("valid") is True
    )
    if not schema_valid:
        status = "invalid_reuse_denial_report"
    elif direct_edge_coverage:
        status = "cross_identity_reuse_denial_bound_to_replacement_site"
    elif event_observed:
        status = "reuse_denial_event_did_not_match_annotated_edge"
    else:
        status = "no_reuse_denial_observed"
    return {
        "repetition": repetition,
        "status": status,
        "valid": schema_valid,
        "report": report,
        "prefixed_line_count": line_count,
        "malformed_json_count": malformed,
        "errors": errors,
        "event_observed": event_observed,
        "event_matches_annotation": event_matches_annotation,
        "requested_site_binding": site_binding,
        "direct_reuse_edge_coverage_observed": direct_edge_coverage,
        "address_reuse_observed": address_reuse_observed(execution_text(record)),
        "exit_code": record.get("exit_code"),
        "timed_out": bool(record.get("timed_out")),
        "claim_grade": False,
    }


def validate_reuse_denial_evidence(
    allocator_variant: str,
    runs: Sequence[dict[str, Any]],
    *,
    annotation: dict[str, Any] | None,
    audit_dir: pathlib.Path,
) -> dict[str, Any]:
    if annotation is None:
        return {
            "status": "not_applicable",
            "valid": None,
            "repetitions": [],
            "claim_grade": False,
        }
    if allocator_variant == "system":
        return {
            "status": "system_baseline_uses_address_and_sanitizer_oracles",
            "valid": None,
            "repetitions": [],
            "annotation": annotation,
            "claim_grade": False,
        }
    repetitions = [
        validate_reuse_denial_record(
            allocator_variant,
            run,
            repetition=index,
            annotation=annotation,
            audit_dir=audit_dir,
        )
        for index, run in enumerate(runs, start=1)
    ]
    valid = bool(repetitions) and all(row["valid"] for row in repetitions)
    direct_count = sum(
        1
        for row in repetitions
        if row["direct_reuse_edge_coverage_observed"]
    )
    return {
        "status": "validated" if valid else "invalid_reuse_denial_reports",
        "valid": valid,
        "annotation": annotation,
        "manual_victim_identity_annotation": True,
        "compiler_automatic_victim_coverage": False,
        "repetitions": repetitions,
        "reuse_denial_event_count": sum(
            1 for row in repetitions if row["event_observed"]
        ),
        "matching_reuse_denial_event_count": sum(
            1 for row in repetitions if row["event_matches_annotation"]
        ),
        "bound_replacement_site_count": direct_count,
        "direct_reuse_edge_coverage_observed": bool(repetitions)
        and direct_count == len(repetitions),
        "source_vulnerability_detection_claimed": False,
        "signal_semantics": "allocator cross-identity reuse denial",
        "claim_grade": False,
        "boundary": (
            "The signal identifies a denied A-to-B reuse edge. It does not identify "
            "the source-level stale-pointer bug and can also occur in safe programs."
        ),
    }


def validate_runtime_stats_record(
    allocator_variant: str,
    record: dict[str, Any],
    *,
    repetition: int,
) -> dict[str, Any]:
    """Validate the experiment-wrapper stats line for one clean run."""

    if record.get("timed_out") or record.get("exit_code") != 0:
        return {
            "repetition": repetition,
            "status": "stats_unavailable_due_to_early_termination",
            "valid": None,
            "stats": None,
            "exit_code": record.get("exit_code"),
            "timed_out": bool(record.get("timed_out")),
            "errors": [],
            "claim_grade": False,
        }

    prefixed_lines = [
        line[len(STATS_PREFIX) :]
        for line in execution_text(record).splitlines()
        if line.startswith(STATS_PREFIX)
    ]
    decoded: list[dict[str, Any]] = []
    malformed_json_count = 0
    for payload in prefixed_lines:
        try:
            candidate = json.loads(payload)
        except json.JSONDecodeError:
            malformed_json_count += 1
            continue
        if isinstance(candidate, dict):
            decoded.append(candidate)

    matching = [
        candidate
        for candidate in decoded
        if candidate.get("allocator") == allocator_variant
    ]
    errors: list[str] = []
    if len(matching) != 1:
        errors.append(
            "expected exactly one schema candidate with allocator="
            f"{allocator_variant!r}; observed {len(matching)}"
        )
    stats = matching[0] if len(matching) == 1 else None
    if stats is not None:
        missing = [field for field in ALLOCATOR_STATS_FIELDS if field not in stats]
        if missing:
            errors.append("missing counter fields: " + ", ".join(missing))
        for field in ALLOCATOR_STATS_FIELDS:
            value = stats.get(field)
            if field in stats and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                errors.append(f"{field} must be a non-negative integer")
        if not missing and not any("must be" in error for error in errors):
            if (
                stats["typed_allocations"] + stats["fallback_allocations"]
                != stats["total_allocations"]
            ):
                errors.append(
                    "typed_allocations + fallback_allocations must equal "
                    "total_allocations"
                )

    observed_allocators = sorted(
        {
            str(candidate["allocator"])
            for candidate in decoded
            if "allocator" in candidate
        }
    )
    return {
        "repetition": repetition,
        "status": "validated" if not errors else "invalid_clean_run_stats",
        "valid": not errors,
        "stats": stats,
        "exit_code": record.get("exit_code"),
        "timed_out": False,
        "prefixed_line_count": len(prefixed_lines),
        "malformed_json_count": malformed_json_count,
        "observed_allocator_names": observed_allocators,
        "errors": errors,
        "claim_grade": False,
    }


def validate_runtime_stats(
    allocator_variant: str, runs: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    boundary = {
        "efficacy_eligible": False,
        "efficacy_blockers": ["critical_site_coverage_not_validated"],
        "claim_grade": False,
    }
    if allocator_variant == "system":
        return {
            "status": "not_applicable",
            "valid": None,
            "repetitions": [],
            **boundary,
        }
    repetitions = [
        validate_runtime_stats_record(
            allocator_variant, run, repetition=index
        )
        for index, run in enumerate(runs, start=1)
    ]
    clean = [record for record in repetitions if record["valid"] is not None]
    if any(record["valid"] is False for record in clean):
        status = "invalid_clean_run_stats"
        valid: bool | None = False
    elif clean:
        status = "validated"
        valid = True
    elif repetitions:
        status = "stats_unavailable_due_to_early_termination"
        valid = None
    else:
        status = "no_runtime_runs"
        valid = None
    return {
        "status": status,
        "valid": valid,
        "clean_run_count": len(clean),
        "early_termination_count": sum(
            1
            for record in repetitions
            if record["status"] == "stats_unavailable_due_to_early_termination"
        ),
        "repetitions": repetitions,
        **boundary,
    }


def summarize_repetitions(
    requested: int, observations: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "requested": requested,
        "executed": len(observations),
        "clean_exit_count": sum(
            1 for observation in observations if observation["clean_exit"]
        ),
        "timed_out_count": sum(
            1 for observation in observations if observation["timed_out"]
        ),
        "tool_finding_count": sum(
            1
            for observation in observations
            if observation["tool_finding_signature"] is not None
        ),
        "native_diagnostic_signal_count": sum(
            1
            for observation in observations
            if observation["native_diagnostic_signature"] is not None
        ),
        "address_reuse_observation_count": sum(
            1
            for observation in observations
            if observation["address_reuse_observed"]
        ),
        "exit_codes": [observation["exit_code"] for observation in observations],
        "claim_grade": False,
        "mitigation_inferred": False,
    }


def evaluate_annotated_reuse_edge_matrix(
    scenario: dict[str, Any],
    arms: Sequence[dict[str, Any]],
    *,
    repetitions: int,
) -> dict[str, Any]:
    """Evaluate the bounded A -> free -> B reuse-edge experiment."""

    annotation = scenario.get("type_isolation_edge_annotation")
    if not isinstance(annotation, dict):
        return {
            "status": "not_applicable",
            "validated": None,
            "claim_grade": False,
        }

    claim_scope = annotation.get("claim_scope")
    if not isinstance(claim_scope, str) or not claim_scope.strip():
        claim_scope = "The manually attributed derived A-to-B replacement edge only."

    by_key = {
        (arm.get("archive_variant"), arm.get("allocator_variant")): arm
        for arm in arms
    }

    def arm(archive_variant: str, allocator_variant: str) -> dict[str, Any]:
        value = by_key.get((archive_variant, allocator_variant))
        return value if isinstance(value, dict) else {}

    system_vulnerable = arm("vulnerable", "system")
    typed_plain_vulnerable = arm("vulnerable", "typed_plain")
    typeiso_vulnerable = arm("vulnerable", "typeiso")
    system_patched = arm("patched", "system")
    typed_plain_patched = arm("patched", "typed_plain")
    typeiso_patched = arm("patched", "typeiso")

    baseline_reproduced = bool(
        system_vulnerable.get("oracle_validation", {}).get(
            "expected_oracle_observed"
        )
        is True
        and system_vulnerable.get("repetition_summary", {}).get(
            "address_reuse_observation_count"
        )
        == repetitions
    )
    patched_control_reproduced = bool(
        system_patched.get("oracle_validation", {}).get(
            "expected_oracle_observed"
        )
        is True
        and system_patched.get("repetition_summary", {}).get(
            "address_reuse_observation_count"
        )
        == repetitions
        and typed_plain_patched.get("repetition_summary", {}).get(
            "address_reuse_observation_count"
        )
        == repetitions
        and typed_plain_patched.get("reuse_denial_evidence", {}).get(
            "matching_reuse_denial_event_count"
        )
        == 0
        and typeiso_patched.get("repetition_summary", {}).get(
            "address_reuse_observation_count"
        )
        == repetitions
        and typeiso_patched.get("reuse_denial_evidence", {}).get(
            "matching_reuse_denial_event_count"
        )
        == 0
        and typeiso_patched.get("reuse_denial_evidence", {}).get(
            "bound_replacement_site_count"
        )
        == 0
    )
    typed_plain_ablation_reproduced = bool(
        typed_plain_vulnerable.get("repetition_summary", {}).get(
            "address_reuse_observation_count"
        )
        == repetitions
        and typed_plain_vulnerable.get("reuse_denial_evidence", {}).get(
            "reuse_denial_event_count"
        )
        == 0
    )
    typeiso_signal = typeiso_vulnerable.get("reuse_denial_evidence", {})
    typeiso_edge_blocked_and_reported = bool(
        typeiso_signal.get("direct_reuse_edge_coverage_observed") is True
        and typeiso_signal.get("bound_replacement_site_count") == repetitions
        and typeiso_vulnerable.get("repetition_summary", {}).get(
            "address_reuse_observation_count"
        )
        == 0
    )
    validated = all(
        (
            baseline_reproduced,
            patched_control_reproduced,
            typed_plain_ablation_reproduced,
            typeiso_edge_blocked_and_reported,
        )
    )
    patched_typeiso_signal = typeiso_patched.get("reuse_denial_evidence", {})
    patched_typeiso_denial_count = int(
        patched_typeiso_signal.get("matching_reuse_denial_event_count", 0) or 0
    )
    vulnerability_specific_signal = bool(
        typeiso_edge_blocked_and_reported and patched_typeiso_denial_count == 0
    )
    missing_required_arms = [
        f"{archive}/{allocator}"
        for archive, allocator in (
            ("vulnerable", "system"),
            ("vulnerable", "typed_plain"),
            ("vulnerable", "typeiso"),
            ("patched", "system"),
            ("patched", "typed_plain"),
            ("patched", "typeiso"),
        )
        if (archive, allocator) not in by_key
    ]
    if missing_required_arms:
        status = "incomplete_required_arm_matrix"
    elif validated:
        status = "cross_identity_reuse_edge_blocked_and_reported"
    else:
        status = "reuse_edge_criteria_not_satisfied"
    return {
        "status": status,
        "validated": validated and not missing_required_arms,
        "annotation": annotation,
        "manual_victim_identity_annotation": True,
        "compiler_automatic_victim_coverage": False,
        "baseline_vulnerability_and_address_reuse_reproduced": baseline_reproduced,
        "patched_system_control_reproduced": patched_control_reproduced,
        "typed_plain_address_reuse_ablation_reproduced": (
            typed_plain_ablation_reproduced
        ),
        "typeiso_reuse_edge_blocked_and_reported": (
            typeiso_edge_blocked_and_reported
        ),
        "patched_typeiso_matching_denial_count": patched_typeiso_denial_count,
        "vulnerability_specific_detection_signal": vulnerability_specific_signal,
        "source_vulnerability_detection_validated": False,
        "missing_required_arms": missing_required_arms,
        "claim_scope": claim_scope,
        "claim_grade": False,
        "boundary": (
            "A validated result shows that exact-identity cache routing denied and "
            "reported the exploit-enabling cross-type reuse edge. The source-level "
            "stale pointer remains, same-identity reuse remains allowed, and the "
            "manual victim annotation is outside automatic compiler coverage."
        ),
    }


def validate_audit_coverage(
    *,
    allocator_variant: str,
    summary: dict[str, Any],
    target_crates: Sequence[str],
    build: dict[str, Any] | None,
) -> dict[str, Any]:
    semantic = int(summary.get("semantic_rewrites_applied", 0) or 0)
    direct = int(summary.get("direct_allocator_rewrites_applied", 0) or 0)
    direct_layout = int(summary.get("direct_layout_allocator_rewrites_applied", 0) or 0)
    total = int(
        summary.get(
            "total_compiler_rewrites_applied",
            semantic + direct + direct_layout,
        )
        or 0
    )
    if semantic > 0:
        rewrite_route = "semantic"
    elif direct + direct_layout > 0:
        rewrite_route = "direct_only"
    else:
        rewrite_route = "none"
    blockers = ["critical_site_coverage_not_validated"]
    if rewrite_route == "none":
        blockers.append("no_compiler_rewrite_observed")
    boundary = {
        "validation_scope": "target_crate_presence_and_force_load_topology_only",
        "critical_site_coverage_validated": False,
        "efficacy_eligible": False,
        "efficacy_blockers": blockers,
        "rewrite_route": rewrite_route,
        "compiler_rewrite_counts": {
            "direct_allocator_rewrites_applied": direct,
            "direct_layout_allocator_rewrites_applied": direct_layout,
            "semantic_rewrites_applied": semantic,
            "semantic_ownership_transfer_rewrites_applied": int(summary.get("semantic_ownership_transfer_rewrites_applied", 0) or 0),
            "total_compiler_rewrites_applied": total,
        },
        "claim_grade": False,
    }
    if allocator_variant not in TYPEISO_VARIANTS:
        return {"status": "not_applicable", "valid": None, **boundary}
    if build is None or build.get("exit_code") != 0 or build.get("timed_out"):
        return {
            "status": "not_evaluated_after_unsuccessful_build",
            "valid": None,
            "expected_target_crates": list(target_crates),
            **boundary,
        }
    try:
        realworld.validate_typeiso_audit_presence(summary, target_crates)
    except realworld.MatrixError as error:
        return {
            "status": "invalid",
            "valid": False,
            "expected_target_crates": list(target_crates),
            "error": str(error),
            **boundary,
        }
    return {
        "status": "target_crate_presence_validated",
        "valid": True,
        "expected_target_crates": list(target_crates),
        "observed_target_crates": summary.get("crate_names", []),
        **boundary,
    }


def collect_file_records(directory: pathlib.Path, suffix: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not directory.exists():
        return records
    for path in sorted(directory.glob(f"*{suffix}")):
        if not path.is_file():
            continue
        record: dict[str, Any] = {
            "path": str(path.resolve()),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        if suffix == ".json":
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                value = None
            if isinstance(value, dict):
                summary = value.get("summary")
                compiler = value.get("compiler_pass")
                record["summary"] = summary if isinstance(summary, dict) else {}
                record["compiler_pass"] = (
                    compiler if isinstance(compiler, dict) else {}
                )
        records.append(record)
    return records


def tool_support(tool: str, variant: str) -> tuple[bool, str | None]:
    known = {
        "asan",
        "asan_c_and_rust",
        "miri",
        "native_address_trace",
        "native_alignment",
        "native_value_oracle",
    }
    if tool not in known:
        return False, f"unsupported catalog oracle tool: {tool}"
    _ = variant
    return True, None


def profile_args(profile: str) -> tuple[list[str], str]:
    if profile == "release":
        return ["--release"], "release"
    if profile == "dev":
        return [], "debug"
    raise ExperimentError(f"unsupported Cargo profile: {profile}")


def arm_rustflags(report: dict[str, Any], mode: str) -> list[str]:
    values = report.get("rustflags")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ExperimentError("materialization rustflags must be a string list")
    result = [value for value in dict.fromkeys(values) if value not in ASAN_RUSTFLAGS]
    if mode in ("asan", "asan_c_and_rust"):
        for value in ASAN_RUSTFLAGS:
            if value not in result:
                result.append(value)
    return result


def effective_rustflag_policy(
    report: dict[str, Any], scenario: dict[str, Any], mode: str
) -> dict[str, Any]:
    """Remove current-only compatibility lints from historical Miri runs.

    Materialization adds compatibility lint allowances for the current compiler.
    Historical Miri toolchains can predate those lint names and reject them with
    E0602 before evaluating the witness.  Keep the filtering narrowly scoped to
    a catalog-pinned historical Miri oracle and record the exact change.
    """

    requested = arm_rustflags(report, mode)
    effective = list(requested)
    removed: list[str] = []
    effective_toolchain = oracle_toolchain(scenario, mode)
    reason: str | None = None
    if mode == "miri" and effective_toolchain != TOOLCHAIN:
        compatibility_flags = frozenset(harness.COMPATIBILITY_RUSTFLAGS)
        removed = [flag for flag in effective if flag in compatibility_flags]
        effective = [
            flag for flag in effective if flag not in compatibility_flags
        ]
        if removed:
            reason = "historical_miri_toolchain_predates_current_compatibility_lints"
    return {
        "requested": requested,
        "effective": effective,
        "removed": removed,
        "reason": reason,
        "effective_oracle_toolchain": effective_toolchain,
    }


@contextmanager
def sanitized_force_build_host_environment() -> Iterable[dict[str, str | None]]:
    """Temporarily remove ambient rustflags consumed by the realworld helper."""

    saved = {name: os.environ.get(name) for name in FORCE_BUILD_HOST_FLAGS}
    try:
        for name in FORCE_BUILD_HOST_FLAGS:
            os.environ.pop(name, None)
        yield dict(FORCE_BUILD_ENVIRONMENT)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def build_force_load_rlib_sanitized(
    root: pathlib.Path, *, jobs: int, timeout: int
) -> tuple[dict[str, Any], dict[str, str | None]]:
    """Build/reuse the force rlib only under the recorded clean flag topology."""

    feature_key = "stats-type_isolation"
    record_path = root / "force-load" / feature_key / "build.json"
    expected_environment = dict(FORCE_BUILD_ENVIRONMENT)
    if record_path.is_file():
        try:
            cached = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if not isinstance(cached, dict) or cached.get(
            "experiment_tool_build_environment"
        ) != expected_environment:
            # Dropping the record makes the upstream helper recreate the whole
            # target, so a previously host-flag-contaminated rlib is not reused.
            record_path.unlink(missing_ok=True)

    with sanitized_force_build_host_environment() as effective_environment:
        force = realworld.build_force_load_rlib(
            "typeiso_coverage",
            raw_dir=root,
            toolchain=TOOLCHAIN,
            jobs=jobs,
            timeout=timeout,
        )
    force = dict(force)
    force["experiment_tool_build_environment"] = effective_environment
    record_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(record_path, force)
    return force, effective_environment


def prepare_typeiso_tools(
    output_dir: pathlib.Path, *, jobs: int, timeout: int
) -> dict[str, Any]:
    root = output_dir / "typeiso-toolchain"
    driver = realworld.ensure_wrapper(root / "tools" / "unialloc-rustc-wrapper", TOOLCHAIN, timeout)
    force_wrapper = realworld.ensure_force_load_wrapper(
        root / "tools" / "unialloc-force-load-wrapper"
    )
    force, tool_build_environment = build_force_load_rlib_sanitized(
        root, jobs=jobs, timeout=timeout
    )
    rlib = pathlib.Path(str(force["rlib"])).resolve()
    dependency_dir = pathlib.Path(str(force["dependency_dir"])).resolve()
    sysroot = realworld.rustc_sysroot(TOOLCHAIN)
    driver_record = driver.with_name(driver.name + ".build.json")
    return {
        "driver": driver,
        "driver_sha256": sha256_file(driver),
        "driver_build_record": (
            json.loads(driver_record.read_text(encoding="utf-8"))
            if driver_record.is_file()
            else None
        ),
        "force_wrapper": force_wrapper,
        "force_wrapper_sha256": sha256_file(force_wrapper),
        "force": force,
        "tool_build_environment": tool_build_environment,
        "rlib": rlib,
        "rlib_sha256": sha256_file(rlib),
        "dependency_dir": dependency_dir,
        "sysroot": sysroot,
    }


def toolchain_identity() -> dict[str, Any]:
    env = clean_base_environment()
    values: dict[str, Any] = {"name": TOOLCHAIN, "target": VALIDATED_TARGET}
    for key, command in (
        ("rustc", ["rustc", f"+{TOOLCHAIN}", "-vV"]),
        ("cargo", ["cargo", f"+{TOOLCHAIN}", "-V"]),
    ):
        result = realworld.execute(command, cwd=ROOT, env=env, timeout=60)
        text = result["stdout"].decode("utf-8", errors="replace").strip()
        values[key] = {
            "command": result["command"],
            "exit_code": result["exit_code"],
            "stdout": text,
            "stdout_sha256": sha256_bytes(result["stdout"]),
        }
        if result["exit_code"] != 0 or result["timed_out"]:
            raise ExperimentError(f"missing pinned toolchain command: {' '.join(command)}")
    return values


def typeiso_environment(
    base: dict[str, str],
    *,
    tools: dict[str, Any],
    audit_dir: pathlib.Path,
    pass_log_dir: pathlib.Path,
    target_crates: Sequence[str],
    policy_flags: int,
) -> dict[str, str]:
    env = realworld.typeiso_environment(
        base,
        wrapper=tools["force_wrapper"],
        audit_dir=audit_dir,
        pass_log_dir=pass_log_dir,
        sysroot=tools["sysroot"],
        target_crates=target_crates,
        policy_flags=policy_flags,
    )
    env = realworld.force_load_environment(
        env,
        driver_wrapper=tools["driver"],
        force_load=tools["force"],
    )
    env["RUSTC_BOOTSTRAP"] = "1"
    return env


def materialize_arm(
    *,
    catalog: dict[str, Any],
    scenario_id: str,
    archive_variant: str,
    allocator_variant: str,
    arm_dir: pathlib.Path,
    output_root: pathlib.Path,
    cache_root: pathlib.Path,
    allow_download: bool,
    mode: str,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    case, scenario = harness.select_scenario(catalog, scenario_id)
    legacy_bridge = legacy_allocator_abi_bridge_requested(scenario)
    work = arm_dir / "work"
    safe_remove_tree(work, output_root, label="arm work")
    report = harness.materialize(
        catalog,
        scenario_id,
        archive_variant,
        work,
        cache_root,
        allow_download,
    )
    project = work / "project"
    subject_cargo_features = configure_subject_dependency_features(
        project,
        crate_name=case["crate"],
        scenario=scenario,
        allocator_variant=allocator_variant,
    )
    report["subject_cargo_features"] = subject_cargo_features
    write_json(work / "materialization.json", report)
    original = (project / "src" / "main.rs").read_text(encoding="utf-8")
    topology = effective_allocator_topology(original, allocator_variant, mode)
    if not topology["substitution_supported"]:
        return (
            report,
            {"original_source_sha256": sha256_bytes(original.encode("utf-8"))},
            topology,
        )
    transformed = transform_witness(
        project,
        allocator_variant,
        allocator_topology=topology,
        legacy_allocator_abi_bridge=legacy_bridge,
    )
    configure_manifest(project, allocator_variant)
    return report, transformed, topology


def resolve_direct_lock(
    project: pathlib.Path,
    *,
    environment: dict[str, str],
    artifact_dir: pathlib.Path,
    timeout: int,
) -> dict[str, Any]:
    return execute_recorded(
        [
            "cargo",
            f"+{TOOLCHAIN}",
            "metadata",
            "--offline",
            "--format-version=1",
            "--manifest-path",
            project / "Cargo.toml",
        ],
        cwd=project,
        environment=environment,
        timeout=timeout,
        artifact_dir=artifact_dir,
        label="lock-resolution",
    )


def attest_direct_unialloc_metadata(
    allocator_variant: str,
    lock_resolution: dict[str, Any],
) -> dict[str, Any]:
    """Validate and compactly attest the resolved direct UniAlloc identity.

    The generated dependency declaration is intent. Cargo metadata is the
    authoritative observation of the package path and feature unification that
    the build will use, so direct arms fail before compilation when it differs
    from the reviewed variant contract.
    """

    expected_features_value = DIRECT_UNIALLOC_RESOLVED_FEATURES.get(
        allocator_variant
    )
    configuration = direct_unialloc_feature_evidence(allocator_variant)
    if expected_features_value is None or configuration is None:
        raise ExperimentError(
            f"{allocator_variant} has no direct UniAlloc metadata contract"
        )

    stdout = lock_resolution.get("stdout")
    stdout_sha256 = lock_resolution.get("stdout_sha256")
    if not isinstance(stdout, str):
        raise ExperimentError("Cargo metadata result is missing UTF-8 stdout")
    if (
        not isinstance(stdout_sha256, str)
        or not SHA256_RE.fullmatch(stdout_sha256)
        or stdout_sha256 != sha256_bytes(stdout.encode("utf-8"))
    ):
        raise ExperimentError("Cargo metadata stdout hash does not match its payload")
    try:
        metadata = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ExperimentError("Cargo metadata stdout is not valid JSON") from error
    if not isinstance(metadata, dict):
        raise ExperimentError("Cargo metadata root must be an object")

    packages = metadata.get("packages")
    if not isinstance(packages, list) or not all(
        isinstance(package, dict) for package in packages
    ):
        raise ExperimentError("Cargo metadata packages must be a list of objects")
    unialloc_packages = [
        package for package in packages if package.get("name") == "unialloc"
    ]
    if len(unialloc_packages) != 1:
        raise ExperimentError(
            "Cargo metadata must resolve exactly one UniAlloc package; "
            f"observed {len(unialloc_packages)}"
        )
    package = unialloc_packages[0]
    package_id = package.get("id")
    if not isinstance(package_id, str) or not package_id:
        raise ExperimentError("Cargo metadata UniAlloc package id is invalid")
    if "source" not in package or package.get("source") is not None:
        raise ExperimentError("Cargo metadata UniAlloc package must be a path dependency")
    manifest_path_value = package.get("manifest_path")
    if not isinstance(manifest_path_value, str) or not manifest_path_value:
        raise ExperimentError("Cargo metadata UniAlloc manifest path is invalid")
    manifest_path_input = pathlib.Path(manifest_path_value)
    if not manifest_path_input.is_absolute():
        raise ExperimentError("Cargo metadata UniAlloc manifest path must be absolute")
    manifest_path = manifest_path_input.resolve()
    expected_manifest_path = (ROOT / "unialloc" / "Cargo.toml").resolve()
    if manifest_path != expected_manifest_path:
        raise ExperimentError(
            "Cargo metadata UniAlloc manifest path does not match the repository: "
            f"{manifest_path}"
        )
    expected_package_id_prefix = f"path+{expected_manifest_path.parent.as_uri()}#"
    if not package_id.startswith(expected_package_id_prefix) or len(package_id) == len(
        expected_package_id_prefix
    ):
        raise ExperimentError(
            "Cargo metadata UniAlloc package id does not identify the repository path"
        )

    resolve = metadata.get("resolve")
    if not isinstance(resolve, dict):
        raise ExperimentError("Cargo metadata resolve graph must be an object")
    nodes = resolve.get("nodes")
    if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
        raise ExperimentError("Cargo metadata resolve nodes must be a list of objects")
    unialloc_nodes = [node for node in nodes if node.get("id") == package_id]
    if len(unialloc_nodes) != 1:
        raise ExperimentError(
            "Cargo metadata must contain exactly one UniAlloc resolve node; "
            f"observed {len(unialloc_nodes)}"
        )
    raw_features = unialloc_nodes[0].get("features")
    if not isinstance(raw_features, list) or any(
        not isinstance(feature, str) or not feature for feature in raw_features
    ):
        raise ExperimentError("Cargo metadata UniAlloc features must be strings")
    if len(set(raw_features)) != len(raw_features):
        raise ExperimentError("Cargo metadata UniAlloc features contain duplicates")
    resolved_features = sorted(raw_features)
    expected_features = sorted(expected_features_value)
    default_features_expected = bool(configuration["default_features_enabled"])
    default_feature_resolved = "default" in resolved_features
    if default_feature_resolved != default_features_expected:
        raise ExperimentError(
            "Cargo metadata UniAlloc default-feature resolution does not match "
            f"the variant contract for {allocator_variant}"
        )
    if resolved_features != expected_features:
        raise ExperimentError(
            "Cargo metadata UniAlloc features do not match the variant contract: "
            f"expected {expected_features}, observed {resolved_features}"
        )

    contract: dict[str, Any] = {
        "schema_version": 1,
        "package_count": 1,
        "resolve_node_count": 1,
        "package_id": package_id,
        "manifest_path": str(manifest_path),
        "package_source": None,
        "expected_features": expected_features,
        "resolved_features": resolved_features,
        "default_features_expected": default_features_expected,
        "default_feature_resolved": default_feature_resolved,
    }
    return {
        **contract,
        "cargo_metadata_stdout_sha256": stdout_sha256,
        "feature_contract_sha256": canonical_sha256(contract),
    }


def arm_fingerprint_payload(
    *,
    catalog_sha256: str,
    report: dict[str, Any],
    archive_variant: str,
    allocator_variant: str,
    project: pathlib.Path,
    transformed: dict[str, str],
    toolchain: dict[str, Any],
    tools: dict[str, Any] | None,
    target_crates: Sequence[str],
    rustflags: Sequence[str],
    subject_cargo_features: dict[str, Any],
    cargo_metadata_attestation: dict[str, Any] | None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": FINGERPRINT_SCHEMA,
        "catalog_sha256": catalog_sha256,
        "scenario_id": report["scenario_id"],
        "archive_variant": archive_variant,
        "allocator_variant": allocator_variant,
        "source_sha256": report["source_sha256"],
        "archive_sha256": report["archive_sha256"],
        "base_lock_sha256": report["cargo_lock_sha256"],
        "effective_lock_sha256": sha256_file(project / "Cargo.lock"),
        "effective_manifest_sha256": sha256_file(project / "Cargo.toml"),
        "cargo_config_sha256": sha256_file(project / ".cargo" / "config.toml"),
        "transformed_sources": transformed,
        "toolchain": toolchain,
        "target_crates": list(target_crates),
        "rustflags": list(rustflags),
        "subject_cargo_features": subject_cargo_features,
        "orchestrator_sha256": sha256_file(pathlib.Path(__file__).resolve()),
        "materializer_sha256": sha256_file(pathlib.Path(harness.__file__).resolve()),
        "realworld_driver_sha256": sha256_file(pathlib.Path(realworld.__file__).resolve()),
        "unialloc_implementation_sha256": realworld.implementation_digest(),
    }
    if allocator_variant in DIRECT_UNIALLOC_VARIANTS:
        if cargo_metadata_attestation is None:
            raise ExperimentError(
                f"{allocator_variant} fingerprint lacks Cargo metadata attestation"
            )
        value["cargo_metadata_attestation"] = cargo_metadata_attestation
    elif cargo_metadata_attestation is not None:
        raise ExperimentError(
            f"{allocator_variant} received an unexpected Cargo metadata attestation"
        )
    if allocator_variant in TYPEISO_VARIANTS:
        assert tools is not None
        value["force_load"] = {
            "driver_sha256": tools["driver_sha256"],
            "force_wrapper_sha256": tools["force_wrapper_sha256"],
            "rlib_sha256": tools["rlib_sha256"],
            "rlib_features": tools["force"].get("features"),
        }
    return value


def run_arm(
    *,
    catalog: dict[str, Any],
    catalog_path: pathlib.Path,
    scenario_id: str,
    archive_variant: str,
    allocator_variant: str,
    output_dir: pathlib.Path,
    cache_root: pathlib.Path,
    allow_download: bool,
    jobs: int,
    repetitions: int,
    build_timeout: int,
    run_timeout: int,
    toolchain: dict[str, Any],
    tools: dict[str, Any] | None,
) -> dict[str, Any]:
    case, scenario = harness.select_scenario(catalog, scenario_id)
    legacy_bridge = legacy_allocator_abi_bridge_requested(scenario)
    edge_annotation = scenario.get("type_isolation_edge_annotation")
    if edge_annotation is not None and not isinstance(edge_annotation, dict):
        raise ExperimentError("type_isolation_edge_annotation must be an object")
    tool = str(scenario["oracle"]["tool"])
    mode = execution_mode(tool, allocator_variant)
    supported, unsupported_reason = tool_support(tool, allocator_variant)
    catalog_exclusion_reason = catalog_allocator_exclusion(scenario, allocator_variant)
    if catalog_exclusion_reason is not None:
        supported = False
        unsupported_reason = catalog_exclusion_reason
    arm_dir = output_dir / "arms" / archive_variant / allocator_variant
    ensure_within(arm_dir, output_dir, label="arm directory")
    arm_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = arm_dir / "artifacts"
    reset_arm_artifact_directory(artifact_dir, output_root=output_dir)
    result_path = arm_dir / "result.json"
    expected = str(
        scenario["oracle"][
            "vulnerable" if archive_variant == "vulnerable" else "patched_control"
        ]
    )
    base_result: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "source": "unialloc-rustsec-heap-experiment",
        "claim_grade": False,
        "scenario_id": scenario_id,
        "case_id": case["case_id"],
        "crate": case["crate"],
        "archive_variant": archive_variant,
        "allocator_variant": allocator_variant,
        "tool": tool,
        "execution_mode": mode,
        "ground_truth_arm": mode in GROUND_TRUTH_TOOLS,
        "repetitions_requested": repetitions,
        "binary_artifact": None,
        "toolchain": toolchain,
        "effective_oracle_toolchain": oracle_toolchain(scenario, mode),
        "efficacy_eligible": False,
        "efficacy_blockers": ["critical_site_coverage_not_validated"],
        "legacy_allocator_abi_bridge": legacy_bridge,
    }
    if not supported:
        status = (
            "unsupported_allocator_topology"
            if catalog_exclusion_reason is not None
            else "unsupported_tool_allocator_pair"
        )
        base_result.update(
            {
                "execution_status": status,
                "unsupported_reason": unsupported_reason,
                "catalog_allocator_exclusion": catalog_exclusion_reason,
                "oracle_validation": {
                    "expected_description": expected,
                    "expected_oracle_observed": False,
                    "oracle_role": "unsupported",
                    "diagnostic_only": False,
                    "tool_finding_signatures": [],
                    "native_diagnostic_signatures": [],
                    "repetition_observations": [],
                    "status": "unsupported",
                    "mitigation_inferred": False,
                    "claim_grade": False,
                },
            }
        )
        write_json(result_path, base_result)
        return base_result

    report, transformed, allocator_topology = materialize_arm(
        catalog=catalog,
        scenario_id=scenario_id,
        archive_variant=archive_variant,
        allocator_variant=allocator_variant,
        arm_dir=arm_dir,
        output_root=output_dir,
        cache_root=cache_root,
        allow_download=allow_download,
        mode=mode,
    )
    if not allocator_topology["substitution_supported"]:
        base_result.update(
            {
                "execution_status": "unsupported_allocator_topology",
                "unsupported_reason": allocator_topology["unsupported_reason"],
                "materialization": report,
                "allocator_topology": allocator_topology,
                "source_provenance": transformed,
                "oracle_validation": {
                    "expected_description": expected,
                    "expected_oracle_observed": False,
                    "oracle_role": "unsupported",
                    "diagnostic_only": False,
                    "tool_finding_signatures": [],
                    "native_diagnostic_signatures": [],
                    "repetition_observations": [],
                    "status": "unsupported",
                    "mitigation_inferred": False,
                    "claim_grade": False,
                },
            }
        )
        write_json(result_path, base_result)
        return base_result
    project = arm_dir / "work" / "project"
    manifest = project / "Cargo.toml"
    lock = project / "Cargo.lock"
    base_env = clean_base_environment()
    required_env, platform_constraints = validate_required_environment(
        report["required_environment"]
    )
    effective_env = effective_required_environment(required_env, mode)
    base_env.update(effective_env)
    base_env["CARGO_INCREMENTAL"] = "0"
    target = arm_dir / "target"
    base_env["CARGO_TARGET_DIR"] = str(target.resolve())

    lock_resolution: dict[str, Any] | None = None
    cargo_metadata_attestation: dict[str, Any] | None = None
    if allocator_variant in DIRECT_UNIALLOC_VARIANTS:
        validate_allocator_identity(
            allocator_variant,
            manifest,
            lock,
            force_rlib=None,
            require_direct_lock=False,
        )
        lock_resolution = resolve_direct_lock(
            project,
            environment=base_env,
            artifact_dir=artifact_dir,
            timeout=build_timeout,
        )
        if lock_resolution["exit_code"] != 0 or lock_resolution["timed_out"]:
            base_result.update(
                {
                    "execution_status": "lock_resolution_failed",
                    "materialization": report,
                    "lock_resolution": lock_resolution,
                    "oracle_validation": validate_expected_oracle(
                        tool=tool,
                        mode=mode,
                        archive_variant=archive_variant,
                        expected=expected,
                        build=lock_resolution,
                        runs=(),
                    ),
                }
            )
            write_json(result_path, base_result)
            return base_result
        cargo_metadata_attestation = attest_direct_unialloc_metadata(
            allocator_variant, lock_resolution
        )

    force_rlib = tools["rlib"] if allocator_variant in TYPEISO_VARIANTS else None
    validate_allocator_identity(
        allocator_variant,
        manifest,
        lock,
        force_rlib=force_rlib,
    )
    harness_crate = case["case_id"].lower() + "-harness"
    target_crates = compiler_target_crates(case, scenario)
    rustflag_policy = effective_rustflag_policy(report, scenario, mode)
    rustflags = rustflag_policy["effective"]
    audit_dir = arm_dir / "audits"
    pass_log_dir = arm_dir / "pass-logs"
    safe_remove_tree(audit_dir, output_dir, label="audit directory")
    safe_remove_tree(pass_log_dir, output_dir, label="pass-log directory")
    audit_dir.mkdir(parents=True)
    pass_log_dir.mkdir(parents=True)
    if allocator_variant in TYPEISO_VARIANTS:
        assert tools is not None
        policy_flags = 0 if allocator_variant == "typed_plain" else 1
        build_env = typeiso_environment(
            base_env,
            tools=tools,
            audit_dir=audit_dir,
            pass_log_dir=pass_log_dir,
            target_crates=target_crates,
            policy_flags=policy_flags,
        )
    else:
        build_env = dict(base_env)
    build_env["RUSTFLAGS"] = " ".join(rustflags)

    fingerprint_payload = arm_fingerprint_payload(
        catalog_sha256=sha256_file(catalog_path),
        report=report,
        archive_variant=archive_variant,
        allocator_variant=allocator_variant,
        project=project,
        transformed=transformed,
        toolchain=toolchain,
        tools=tools,
        target_crates=target_crates,
        rustflags=rustflags,
        subject_cargo_features=report["subject_cargo_features"],
        cargo_metadata_attestation=cargo_metadata_attestation,
    )
    fingerprint = canonical_sha256(fingerprint_payload)
    target_reset = reset_target_for_fingerprint(
        target,
        arm_dir / "fingerprint.json",
        fingerprint,
        output_root=output_dir,
        force_reset_reason=(
            "typeiso_complete_pass_audit_regeneration"
            if allocator_variant in TYPEISO_VARIANTS
            else None
        ),
    )
    fingerprint_record = json.loads(
        (arm_dir / "fingerprint.json").read_text(encoding="utf-8")
    )

    profile_options, target_profile = profile_args(str(scenario["cargo_profile"]))
    build: dict[str, Any] | None = None
    binary_artifact: dict[str, Any] | None = None
    runs: list[dict[str, Any]] = []
    if mode == "miri":
        command: list[str | os.PathLike[str]] = [
            "cargo",
            f"+{oracle_toolchain(scenario, mode)}",
            "miri",
            "run",
            "--offline",
            "--locked",
            "--manifest-path",
            manifest,
        ]
        for repetition in range(1, repetitions + 1):
            runs.append(
                execute_recorded(
                    command,
                    cwd=project,
                    environment=build_env,
                    timeout=run_timeout,
                    artifact_dir=artifact_dir,
                    label=f"run-{repetition:03d}",
                )
            )
    else:
        command = [
            "cargo",
            f"+{TOOLCHAIN}",
            "build",
            "--offline",
            "--locked",
            "--manifest-path",
            manifest,
            "--jobs",
            str(jobs),
            *profile_options,
        ]
        uses_target = mode in ("asan", "asan_c_and_rust")
        if uses_target:
            command.extend(["--target", VALIDATED_TARGET])
        build = execute_recorded(
            command,
            cwd=project,
            environment=build_env,
            timeout=build_timeout,
            artifact_dir=artifact_dir,
            label="build",
        )
        if build["exit_code"] == 0 and not build["timed_out"]:
            binary_dir = target / (VALIDATED_TARGET if uses_target else "") / target_profile
            binary = binary_dir / harness_crate
            if not binary.is_file():
                raise ExperimentError(f"missing built experiment binary: {binary}")
            binary_artifact = file_artifact_record(binary)
            run_env = dict(build_env)
            run_env.pop("RUSTC_WRAPPER", None)
            for repetition in range(1, repetitions + 1):
                runs.append(
                    execute_recorded(
                        [binary],
                        cwd=binary.parent,
                        environment=run_env,
                        timeout=run_timeout,
                        artifact_dir=artifact_dir,
                        label=f"run-{repetition:03d}",
                    )
                )

    audit_summary = realworld.summarize_audits(audit_dir)
    audit_validation = validate_audit_coverage(
        allocator_variant=allocator_variant,
        summary=audit_summary,
        target_crates=target_crates,
        build=build,
    )
    audits = collect_file_records(audit_dir, ".json")
    pass_logs = collect_file_records(pass_log_dir, ".log")
    oracle_validation = validate_expected_oracle(
        tool=tool,
        mode=mode,
        archive_variant=archive_variant,
        expected=expected,
        build=build,
        runs=runs,
    )
    observations = oracle_validation["repetition_observations"]
    runtime_stats_validation = validate_runtime_stats(allocator_variant, runs)
    reuse_denial_evidence = validate_reuse_denial_evidence(
        allocator_variant,
        runs,
        annotation=edge_annotation,
        audit_dir=audit_dir,
    )
    stats_by_repetition = [
        {
            "repetition": record["repetition"],
            "status": record["status"],
            "stats": record["stats"],
        }
        for record in runtime_stats_validation["repetitions"]
    ]
    result = {
        **base_result,
        "execution_status": "completed",
        "materialization": report,
        "source_provenance": {
            "source_sha256": report["source_sha256"],
            "archive_sha256": report["archive_sha256"],
            "base_lock_sha256": report["cargo_lock_sha256"],
            "effective_lock_sha256": sha256_file(lock),
            "subject_cargo_features": report["subject_cargo_features"],
            **transformed,
        },
        "allocator_provenance": {
            "single_unialloc_identity_enforced": True,
            "direct_cargo_unialloc": allocator_variant in DIRECT_UNIALLOC_VARIANTS,
            "force_loaded_unialloc": allocator_variant in TYPEISO_VARIANTS,
            "unialloc_implementation_sha256": realworld.implementation_digest(),
            "direct_cargo_configuration": direct_unialloc_feature_evidence(
                allocator_variant
            ),
            "cargo_metadata_attestation": cargo_metadata_attestation,
            "subject_cargo_features": report["subject_cargo_features"],
            "force_rlib_sha256": (
                tools["rlib_sha256"] if allocator_variant in TYPEISO_VARIANTS else None
            ),
            "compiler_wrapper_sha256": (
                tools["driver_sha256"] if allocator_variant in TYPEISO_VARIANTS else None
            ),
            "force_wrapper_sha256": (
                tools["force_wrapper_sha256"]
                if allocator_variant in TYPEISO_VARIANTS
                else None
            ),
        },
        "allocator_topology": allocator_topology,
        "target_crates": list(target_crates),
        "required_environment": required_env,
        "effective_required_environment": effective_env,
        "rustflag_policy": rustflag_policy,
        "platform_constraints": platform_constraints,
        "fingerprint": fingerprint,
        "fingerprint_payload": fingerprint_payload,
        "target_reset": target_reset,
        "target_reset_reasons": fingerprint_record["target_reset_reasons"],
        "full_rebuild_required": fingerprint_record["full_rebuild_required"],
        "lock_resolution": lock_resolution,
        "build": build,
        "binary_artifact": binary_artifact,
        "run": runs[0] if runs else None,
        "runs": runs,
        "repetition_summary": summarize_repetitions(repetitions, observations),
        "runtime_stats": (
            stats_by_repetition[0]["stats"] if stats_by_repetition else None
        ),
        "runtime_stats_by_repetition": stats_by_repetition,
        "runtime_stats_validation": runtime_stats_validation,
        "reuse_denial_evidence": reuse_denial_evidence,
        "pass_audit_summary": audit_summary,
        "pass_audit_validation": audit_validation,
        "pass_audits": audits,
        "pass_logs": pass_logs,
        "oracle_validation": oracle_validation,
        "claim_boundary": (
            "This arm records reproduction and detector observations only. "
            "It does not establish mitigation, detection coverage, or exploit prevention."
        ),
    }
    write_json(result_path, result)
    return result


def preflight(
    *,
    catalog: dict[str, Any],
    catalog_path: pathlib.Path,
    scenario_id: str,
    variants: Sequence[str],
    archive_variants: Sequence[str],
    repetitions: int,
    output_dir: pathlib.Path,
    cache_root: pathlib.Path,
    allow_download: bool,
) -> dict[str, Any]:
    case, scenario = harness.select_scenario(catalog, scenario_id)
    cargo_feature_overrides = allocator_cargo_feature_overrides(scenario)
    legacy_bridge = legacy_allocator_abi_bridge_requested(scenario)
    required_env, constraints = validate_required_environment(
        scenario["required_environment"]
    )
    planned: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="unialloc-rustsec-preflight-") as temporary:
        temp_root = pathlib.Path(temporary)
        for archive_variant in archive_variants:
            base = temp_root / archive_variant / "base"
            report = harness.materialize(
                catalog,
                scenario_id,
                archive_variant,
                base,
                cache_root,
                allow_download,
            )
            for allocator_variant in variants:
                work = temp_root / archive_variant / allocator_variant
                shutil.copytree(base, work)
                project = work / "project"
                subject_cargo_features = configure_subject_dependency_features(
                    project,
                    crate_name=case["crate"],
                    scenario=scenario,
                    allocator_variant=allocator_variant,
                )
                original = (project / "src" / "main.rs").read_text(
                    encoding="utf-8"
                )
                mode = execution_mode(
                    str(scenario["oracle"]["tool"]), allocator_variant
                )
                allocator_topology = effective_allocator_topology(
                    original, allocator_variant, mode
                )
                tool_supported, tool_reason = tool_support(
                    str(scenario["oracle"]["tool"]), allocator_variant
                )
                catalog_exclusion_reason = catalog_allocator_exclusion(
                    scenario, allocator_variant
                )
                supported = bool(
                    tool_supported
                    and catalog_exclusion_reason is None
                    and allocator_topology["substitution_supported"]
                )
                reason = (
                    tool_reason
                    or catalog_exclusion_reason
                    or allocator_topology["unsupported_reason"]
                )
                if allocator_topology["substitution_supported"]:
                    transformed = transform_witness(
                        project,
                        allocator_variant,
                        allocator_topology=allocator_topology,
                        legacy_allocator_abi_bridge=legacy_bridge,
                    )
                    configure_manifest(project, allocator_variant)
                    validate_allocator_identity(
                        allocator_variant,
                        project / "Cargo.toml",
                        project / "Cargo.lock",
                        force_rlib=None,
                        require_direct_lock=False,
                    )
                else:
                    transformed = {
                        "original_source_sha256": sha256_bytes(
                            original.encode("utf-8")
                        )
                    }
                rustflag_policy = effective_rustflag_policy(
                    report, scenario, mode
                )
                planned.append(
                    {
                        "archive_variant": archive_variant,
                        "allocator_variant": allocator_variant,
                        "supported": supported,
                        "unsupported_reason": reason,
                        "tool_supported": tool_supported,
                        "allocator_substitution_supported": allocator_topology[
                            "substitution_supported"
                        ],
                        "catalog_allocator_exclusion": catalog_exclusion_reason,
                        "allocator_topology": allocator_topology,
                        "execution_mode": mode,
                        "ground_truth_arm": mode in GROUND_TRUTH_TOOLS,
                        "effective_oracle_toolchain": oracle_toolchain(scenario, mode),
                        "rustflag_policy": rustflag_policy,
                        "effective_rustflags": rustflag_policy["effective"],
                        "effective_required_environment": (
                            effective_required_environment(required_env, mode)
                        ),
                        "legacy_allocator_abi_bridge": legacy_bridge,
                        "source_sha256": report["source_sha256"],
                        "archive_sha256": report["archive_sha256"],
                        "lock_sha256": report["cargo_lock_sha256"],
                        "subject_cargo_features": subject_cargo_features,
                        "transformed_sources": transformed,
                    }
                )
    payload = {
        "schema_version": RESULT_SCHEMA,
        "source": "unialloc-rustsec-heap-experiment-preflight",
        "claim_grade": False,
        "catalog_path": str(catalog_path.resolve()),
        "catalog_sha256": sha256_file(catalog_path),
        "scenario_id": scenario_id,
        "case_id": case["case_id"],
        "crate": case["crate"],
        "harness_target_crates": list(compiler_target_crates(case, scenario)),
        "tool": scenario["oracle"]["tool"],
        "toolchain": toolchain_identity(),
        "required_environment": required_env,
        "platform_constraints": constraints,
        "type_isolation_edge_annotation": scenario.get(
            "type_isolation_edge_annotation"
        ),
        "legacy_allocator_abi_bridge": legacy_bridge,
        "allocator_cargo_feature_overrides": cargo_feature_overrides,
        "planned_arms": planned,
        "repetitions_requested": repetitions,
        "unsafe_execution_requested": False,
    }
    write_json(output_dir / "preflight.json", payload)
    return payload


def execute_experiment(
    *,
    catalog: dict[str, Any],
    catalog_path: pathlib.Path,
    scenario_id: str,
    variants: Sequence[str],
    archive_variants: Sequence[str],
    repetitions: int,
    output_dir: pathlib.Path,
    cache_root: pathlib.Path,
    allow_download: bool,
    jobs: int,
    build_timeout: int,
    run_timeout: int,
) -> dict[str, Any]:
    preflight_record = preflight(
        catalog=catalog,
        catalog_path=catalog_path,
        scenario_id=scenario_id,
        variants=variants,
        archive_variants=archive_variants,
        repetitions=repetitions,
        output_dir=output_dir,
        cache_root=cache_root,
        allow_download=allow_download,
    )
    scenario = {
        "type_isolation_edge_annotation": preflight_record.get(
            "type_isolation_edge_annotation"
        )
    }
    toolchain = preflight_record["toolchain"]
    supported_typeiso_arm_planned = any(
        arm.get("supported") and arm.get("allocator_variant") in TYPEISO_VARIANTS
        for arm in preflight_record["planned_arms"]
    )
    tools = (
        prepare_typeiso_tools(output_dir, jobs=jobs, timeout=build_timeout)
        if supported_typeiso_arm_planned
        else None
    )
    arms: list[dict[str, Any]] = []
    for archive_variant in archive_variants:
        for allocator_variant in variants:
            arms.append(
                run_arm(
                    catalog=catalog,
                    catalog_path=catalog_path,
                    scenario_id=scenario_id,
                    archive_variant=archive_variant,
                    allocator_variant=allocator_variant,
                    output_dir=output_dir,
                    cache_root=cache_root,
                    allow_download=allow_download,
                    jobs=jobs,
                    repetitions=repetitions,
                    build_timeout=build_timeout,
                    run_timeout=run_timeout,
                    toolchain=toolchain,
                    tools=tools,
                )
            )
    unexpected: list[dict[str, Any]] = []
    for arm in arms:
        if arm.get("execution_status") in {
            "unsupported_tool_allocator_pair",
            "unsupported_allocator_topology",
        }:
            continue
        if arm.get("execution_status") != "completed":
            unexpected.append(arm)
            continue
        oracle = arm.get("oracle_validation", {})
        if oracle.get("status") in {
            "timed_out",
            "unexpected_build_failure",
            "run_not_performed",
            "native_diagnostic_run_not_performed",
        }:
            unexpected.append(arm)
            continue
        if (
            arm.get("allocator_variant") == "system"
            and oracle.get("expected_oracle_observed") is not True
        ):
            unexpected.append(arm)
            continue
        if arm.get("pass_audit_validation", {}).get("valid") is False:
            unexpected.append(arm)
            continue
        if arm.get("runtime_stats_validation", {}).get("valid") is False:
            unexpected.append(arm)
            continue
        if arm.get("reuse_denial_evidence", {}).get("valid") is False:
            unexpected.append(arm)
            continue
        build = arm.get("build")
        if (
            isinstance(build, dict)
            and build.get("exit_code") == 0
            and arm.get("repetition_summary", {}).get("executed") != repetitions
        ):
            unexpected.append(arm)
    reuse_edge_evaluation = evaluate_annotated_reuse_edge_matrix(
        scenario, arms, repetitions=repetitions
    )
    record = {
        "schema_version": RESULT_SCHEMA,
        "source": "unialloc-rustsec-heap-experiment-matrix",
        "claim_grade": False,
        "unsafe_execution_requested": True,
        "created_at_unix": int(time.time()),
        "scenario_id": scenario_id,
        "variants": list(variants),
        "archive_variants": list(archive_variants),
        "repetitions_requested": repetitions,
        "toolchain": toolchain,
        "preflight_path": str((output_dir / "preflight.json").resolve()),
        "typeiso_toolchain": (
            {
                "driver_sha256": tools["driver_sha256"],
                "force_wrapper_sha256": tools["force_wrapper_sha256"],
                "rlib_sha256": tools["rlib_sha256"],
                "force_build": tools["force"],
                "effective_tool_build_environment": tools[
                    "tool_build_environment"
                ],
                "driver_build": tools["driver_build_record"],
            }
            if tools is not None
            else None
        ),
        "arms": arms,
        "type_isolation_reuse_edge_evaluation": reuse_edge_evaluation,
        "orchestration_success": not unexpected,
        "unexpected_arm_count": len(unexpected),
        "claim_boundary": (
            "Per-arm exit status is never a mitigation classification. Claim-grade "
            "evaluation requires preregistered repetitions, matched controls, and "
            "coverage validation."
        ),
    }
    write_json(output_dir / "experiment.json", record)
    return record


def list_payload(catalog: dict[str, Any]) -> dict[str, Any]:
    payload = harness.list_payload(catalog)
    return {
        **payload,
        "actions": ["list", "preflight", "run"],
        "allocator_variants": list(VARIANTS),
        "archive_variants": list(ARCHIVE_VARIANTS),
        "toolchain": TOOLCHAIN,
        "claim_grade": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=pathlib.Path, default=DEFAULT_CATALOG)
    parser.add_argument(
        "--action", choices=("list", "preflight", "run"), default="list"
    )
    parser.add_argument("--scenario")
    parser.add_argument("--variants", default=",".join(VARIANTS))
    parser.add_argument("--archive-variants", default=",".join(ARCHIVE_VARIANTS))
    parser.add_argument("--output-dir", type=pathlib.Path)
    parser.add_argument("--cache", type=pathlib.Path)
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--execute-unsafe", action="store_true")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--build-timeout", type=int, default=900)
    parser.add_argument("--run-timeout", type=int, default=60)
    return parser


def cache_root(args: argparse.Namespace) -> pathlib.Path:
    if args.cache is not None:
        return args.cache.expanduser().resolve()
    base = pathlib.Path(os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache"))
    return (base / "unialloc" / "rustsec-heap").resolve()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "run" and not args.execute_unsafe:
            raise ExperimentError(
                "run requires the explicit --execute-unsafe opt-in before any build "
                "script or witness can execute"
            )
        catalog_path = args.catalog.expanduser().resolve()
        catalog = harness.load_catalog(catalog_path)
        validate_catalog_allocator_cargo_feature_overrides(catalog)
        validate_catalog_compiler_target_crates(catalog)
        if args.action == "list":
            print(json.dumps(list_payload(catalog), indent=2, sort_keys=True))
            return 0
        if not args.scenario:
            raise ExperimentError("--scenario is required for preflight and run")
        if args.output_dir is None:
            raise ExperimentError("--output-dir is required for preflight and run")
        if (
            args.jobs <= 0
            or args.repetitions <= 0
            or args.build_timeout <= 0
            or args.run_timeout <= 0
        ):
            raise ExperimentError("jobs, repetitions, and timeouts must be positive")
        variants = parse_csv(args.variants, VARIANTS, "allocator variants")
        archive_variants = parse_csv(
            args.archive_variants, ARCHIVE_VARIANTS, "archive variants"
        )
        output_dir = args.output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        if output_dir == ROOT or output_dir == pathlib.Path.home().resolve():
            raise ExperimentError("--output-dir must be a dedicated experiment directory")
        if args.action == "preflight":
            record = preflight(
                catalog=catalog,
                catalog_path=catalog_path,
                scenario_id=args.scenario,
                variants=variants,
                archive_variants=archive_variants,
                repetitions=args.repetitions,
                output_dir=output_dir,
                cache_root=cache_root(args),
                allow_download=args.allow_download,
            )
            print(json.dumps(record, indent=2, sort_keys=True))
            return 0
        record = execute_experiment(
            catalog=catalog,
            catalog_path=catalog_path,
            scenario_id=args.scenario,
            variants=variants,
            archive_variants=archive_variants,
            repetitions=args.repetitions,
            output_dir=output_dir,
            cache_root=cache_root(args),
            allow_download=args.allow_download,
            jobs=args.jobs,
            build_timeout=args.build_timeout,
            run_timeout=args.run_timeout,
        )
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0 if record["orchestration_success"] else 1
    except (
        ExperimentError,
        harness.HarnessError,
        realworld.MatrixError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
