#!/usr/bin/env python3
"""Assemble complete target-local measurements into the primary-suite result."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUITE = ROOT / "evaluation/config/type_isolation_primary_suite.json"
DEFAULT_TARGETS_DIR = ROOT / "evaluation/raw/type-isolation-primary-v1/targets"
DEFAULT_OUTPUT = ROOT / "benchmark-results/type-isolation-primary-v1.json"
VARIANTS = ("unialloc", "typed_plain", "typeiso_perf")
STORED_DATA_ELIGIBILITY_GATES = (
    "correctness",
    "build_success",
    "allocator_activation",
    "actual_mir_provenance",
    "stats_disabled",
    "source_audit_retained",
)
DATA_ELIGIBILITY_GATES = (
    "correctness",
    "build_success",
    "allocator_activation",
    "actual_mir_provenance",
    "stats_disabled",
    "source_audit_retained",
)
ATTRIBUTION_GATE = "compiler_route_equivalent"
FIXED_WORK = "fixed_work"
ADAPTIVE_ITERATIONS = "workload_native_adaptive_iterations"
RSS_WORK_MODELS = {FIXED_WORK, ADAPTIVE_ITERATIONS}
RSS_CORE = "core"
RSS_DIAGNOSTIC = "diagnostic_only"


class AssemblyError(RuntimeError):
    """Raised when a target-local result cannot enter the final dataset."""


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AssemblyError(f"required target result is missing: {path}") from error
    except json.JSONDecodeError as error:
        raise AssemblyError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise AssemblyError(f"JSON root must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def object_index(
    rows: Any, key: str, expected: list[str], context: str
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(rows, list):
        raise AssemblyError(f"{context} must be a list")
    indexed: dict[str, Mapping[str, Any]] = {}
    for position, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise AssemblyError(f"{context}[{position}] must be an object")
        identifier = raw.get(key)
        if not isinstance(identifier, str) or not identifier:
            raise AssemblyError(f"{context}[{position}].{key} must be a string")
        if identifier in indexed:
            raise AssemblyError(f"duplicate {context} entry: {identifier}")
        indexed[identifier] = raw
    missing = [identifier for identifier in expected if identifier not in indexed]
    extra = [identifier for identifier in indexed if identifier not in expected]
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise AssemblyError(f"{context} mismatch: {'; '.join(detail)}")
    return indexed


def validate_measurements(rows: Any, context: str) -> None:
    if not isinstance(rows, list):
        raise AssemblyError(f"{context}.measurements must be a list")
    seen: set[tuple[int, str]] = set()
    rounds: set[int] = set()
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise AssemblyError(f"{context}.measurements[{position}] must be an object")
        round_number = row.get("round")
        variant = row.get("variant")
        if isinstance(round_number, bool) or not isinstance(round_number, int):
            raise AssemblyError(f"{context} measurement round must be an integer")
        if round_number <= 0 or variant not in VARIANTS:
            raise AssemblyError(f"{context} has an invalid round or variant")
        identity = (round_number, str(variant))
        if identity in seen:
            raise AssemblyError(
                f"{context} duplicates round {round_number} variant {variant}"
            )
        seen.add(identity)
        rounds.add(round_number)
        for field in ("performance", "peak_rss_mib"):
            value = row.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0.0
            ):
                raise AssemblyError(f"{context} {field} must be finite and positive")
    expected_rounds = set(range(1, 6))
    if rounds != expected_rounds or len(seen) != len(expected_rounds) * len(VARIANTS):
        raise AssemblyError(f"{context} must contain exact measured rounds 1 through 5")
    for round_number in sorted(expected_rounds):
        missing = [
            variant for variant in VARIANTS if (round_number, variant) not in seen
        ]
        if missing:
            raise AssemblyError(
                f"{context} round {round_number} is missing {', '.join(missing)}"
            )


def validate_warmup_evidence(
    raw_evidence: Any,
    target_id: str,
    harness_id: str,
) -> dict[str, Any]:
    context = f"{target_id}.{harness_id}.warmup_evidence"
    if not isinstance(raw_evidence, dict) or set(raw_evidence) != set(VARIANTS):
        raise AssemblyError(f"{context} must cover every variant")
    seen_paths: set[Path] = set()
    attestations: dict[str, Any] = {}
    for variant in VARIANTS:
        entries = raw_evidence[variant]
        if not isinstance(entries, list) or len(entries) != 1:
            raise AssemblyError(
                f"{context}.{variant} must retain exactly one warmup record"
            )
        for position, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise AssemblyError(
                    f"{context}.{variant}[{position}] must be an object"
                )
            path_value = entry.get("record_path")
            expected_digest = entry.get("sha256")
            if not isinstance(path_value, str) or not path_value:
                raise AssemblyError(f"{context}.{variant} record path is invalid")
            path = Path(path_value)
            if path in seen_paths or not path.is_file():
                raise AssemblyError(
                    f"{context}.{variant} record is missing or repeated"
                )
            seen_paths.add(path)
            if (
                not isinstance(expected_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
                or sha256_file(path) != expected_digest
            ):
                raise AssemblyError(f"{context}.{variant} warmup digest mismatch")
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise AssemblyError(f"{context}.{variant} record is invalid") from error
            if (
                not isinstance(record, dict)
                or record.get("target_id") != target_id
                or record.get("harness_id") != harness_id
                or record.get("variant") != variant
                or record.get("phase") != "warmup"
                or record.get("round") != 0
            ):
                raise AssemblyError(f"{context}.{variant} record identity mismatch")
            for field in ("performance", "peak_rss_mib"):
                value = record.get(field)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) <= 0.0
                ):
                    raise AssemblyError(f"{context}.{variant} {field} is invalid")
            attestation = {
                "target_id": target_id,
                "harness_id": harness_id,
                "variant": variant,
                "phase": "warmup",
                "round": 0,
                "performance": float(record["performance"]),
                "peak_rss_mib": float(record["peak_rss_mib"]),
                "record_sha256": str(expected_digest),
            }
            try:
                attestation["raw_record_path"] = (
                    path.resolve().relative_to(ROOT.resolve()).as_posix()
                )
            except ValueError:
                pass
            attestations[variant] = attestation
    return attestations


def repository_relative_path(path_value: Any) -> str | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    try:
        return Path(path_value).resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return None


def compact_build_record(path_value: Any) -> dict[str, Any] | None:
    if not isinstance(path_value, str):
        return None
    path = Path(path_value)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    compact: dict[str, Any] = {"record_sha256": sha256_file(path)}
    relative = repository_relative_path(path_value)
    if relative is not None:
        compact["raw_record_path"] = relative
    success: bool | None = record.get("success")
    if not isinstance(success, bool):
        commands = record.get("commands")
        if isinstance(commands, list):
            success = bool(commands) and all(
                isinstance(command, dict)
                and command.get("exit_code") == 0
                and command.get("timed_out") is False
                for command in commands
            )
        elif (
            isinstance(record.get("exit_code"), int)
            and not isinstance(record.get("exit_code"), bool)
            and isinstance(record.get("timed_out"), bool)
        ):
            success = record.get("exit_code") == 0 and record.get("timed_out") is False
        else:
            success = None
    if success is not None:
        compact["success"] = success
    for field in ("source_commit", "implementation_revision"):
        value = record.get(field)
        if isinstance(value, str) and value:
            compact[field] = value
    implementation_sha256 = (
        record.get("unialloc_implementation_sha256")
        or record.get("implementation_sha256")
        or record.get("frozen_implementation_sha256")
    )
    if isinstance(implementation_sha256, str):
        compact["implementation_sha256"] = implementation_sha256

    binary_digests: dict[str, str] = {}
    activation = record.get("allocator_activation")
    activation_result: bool | None = None
    if isinstance(activation, dict) and isinstance(activation.get("success"), bool):
        activation_result = activation["success"]
    elif isinstance(activation, bool):
        activation_result = activation
    legacy_activation = record.get("activation")
    if activation_result is None and isinstance(legacy_activation, dict):
        for field in ("success", "passed"):
            if isinstance(legacy_activation.get(field), bool):
                activation_result = legacy_activation[field]
                break
        if activation_result is None:
            activation_children = list(legacy_activation.values())
            activation_result = bool(activation_children) and all(
                isinstance(child, dict)
                and (child.get("passed") is True or child.get("success") is True)
                for child in activation_children
            )
        for executable, child in legacy_activation.items():
            if not isinstance(child, dict):
                continue
            digest = child.get("binary_sha256")
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                binary_digests[str(executable)] = digest
    if activation_result is not None:
        compact["allocator_activation"] = activation_result

    actual_mir = record.get("actual_mir_provenance")
    if not isinstance(actual_mir, bool):
        actual_mir = record.get("actual_mir_rewrite")
    if isinstance(actual_mir, bool):
        compact["actual_mir_provenance"] = actual_mir
    stats_enabled = record.get("stats_feature_enabled")
    if not isinstance(stats_enabled, bool):
        stats_enabled = record.get("stats_enabled")
    if isinstance(stats_enabled, bool):
        compact["stats_disabled"] = stats_enabled is False

    top_binary_digest = record.get("binary_sha256")
    if isinstance(top_binary_digest, str) and re.fullmatch(
        r"[0-9a-f]{64}", top_binary_digest
    ):
        binary_digests["primary"] = top_binary_digest
    binaries = record.get("binaries")
    if isinstance(binaries, dict):
        for name, raw_binary in binaries.items():
            if not isinstance(raw_binary, dict):
                continue
            digest = raw_binary.get("sha256") or raw_binary.get("binary_sha256")
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                binary_digests[str(name)] = digest
    if binary_digests:
        compact["binary_sha256"] = binary_digests

    raw_audit = record.get("audit")
    if isinstance(raw_audit, dict):
        audit = {
            str(field): value
            for field, value in raw_audit.items()
            if (
                field == "audit_sha256"
                and isinstance(value, str)
                and re.fullmatch(r"[0-9a-f]{64}", value)
            )
            or (
                (str(field).endswith("_count") or str(field).endswith("_applied"))
                and isinstance(value, int)
                and not isinstance(value, bool)
            )
        }
        if audit:
            compact["audit"] = audit
    return compact


def compact_rustpython_dynamic_runtime(
    target_result: Mapping[str, Any],
    builds: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    context = "rustpython.inputs.rustpython_dynamic_runtime"
    inputs = target_result.get("inputs")
    runtime = (
        inputs.get("rustpython_dynamic_runtime") if isinstance(inputs, dict) else None
    )
    if not isinstance(runtime, dict):
        raise AssemblyError(f"{context} must be an object")

    soname = runtime.get("library_soname")
    if not isinstance(soname, str) or not soname or "/" in soname or "\\" in soname:
        raise AssemblyError(f"{context}.library_soname is invalid")
    byte_count = runtime.get("library_bytes")
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count <= 0
    ):
        raise AssemblyError(f"{context}.library_bytes must be a positive integer")
    library_sha256 = runtime.get("library_sha256")
    if (
        not isinstance(library_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", library_sha256) is None
    ):
        raise AssemblyError(f"{context}.library_sha256 must be SHA-256")

    raw_proofs = runtime.get("variants")
    if not isinstance(raw_proofs, dict) or set(raw_proofs) != set(VARIANTS):
        raise AssemblyError(f"{context}.variants must exactly cover every variant")
    proofs: dict[str, dict[str, Any]] = {}
    for variant in VARIANTS:
        proof = raw_proofs[variant]
        proof_context = f"{context}.variants.{variant}"
        if not isinstance(proof, dict):
            raise AssemblyError(f"{proof_context} must be an object")
        if proof.get("success") is not True:
            raise AssemblyError(f"{proof_context}.success must be true")
        if proof.get("resolved_library_soname") != soname:
            raise AssemblyError(
                f"{proof_context}.resolved_library_soname does not match"
            )
        if proof.get("resolved_library_sha256") != library_sha256:
            raise AssemblyError(
                f"{proof_context}.resolved_library_sha256 does not match"
            )

        binary_digests = builds[variant].get("binary_sha256")
        compact_binary_sha256 = (
            binary_digests.get("primary") if isinstance(binary_digests, dict) else None
        )
        proof_binary_sha256 = proof.get("binary_sha256")
        if (
            not isinstance(proof_binary_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", proof_binary_sha256) is None
            or proof_binary_sha256 != compact_binary_sha256
        ):
            raise AssemblyError(f"{proof_context}.binary_sha256 does not match build")

        ldd_path_value = proof.get("ldd_record")
        ldd_sha256 = proof.get("ldd_record_sha256")
        if (
            not isinstance(ldd_path_value, str)
            or not ldd_path_value
            or not isinstance(ldd_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", ldd_sha256) is None
        ):
            raise AssemblyError(f"{proof_context}.ldd_record evidence is invalid")
        ldd_path = Path(ldd_path_value)
        if not ldd_path.is_file() or sha256_file(ldd_path) != ldd_sha256:
            raise AssemblyError(f"{proof_context}.ldd_record digest mismatch")

        proofs[variant] = {
            "success": True,
            "binary_sha256": proof_binary_sha256,
            "resolved_library_soname": soname,
            "resolved_library_sha256": library_sha256,
            "ldd_record_sha256": ldd_sha256,
        }
    return {
        "library_soname": soname,
        "library_bytes": byte_count,
        "library_sha256": library_sha256,
        "variants": proofs,
    }


def compact_target_provenance(
    target_result: Mapping[str, Any],
    target_id: str,
) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    raw_source = target_result.get("source")
    if isinstance(raw_source, dict):
        source = {
            field: raw_source[field]
            for field in (
                "repository",
                "source_commit",
                "source_ref",
                "tree",
                "cargo_lock_sha256",
            )
            if isinstance(raw_source.get(field), str) and raw_source[field]
        }
        if source:
            compact["source"] = source

    references: Mapping[str, Any] | None = None
    for candidate in (
        target_result.get("build_records"),
        target_result.get("builds"),
        (
            target_result.get("evidence", {}).get("build_records")
            if isinstance(target_result.get("evidence"), dict)
            else None
        ),
    ):
        if isinstance(candidate, dict):
            references = candidate
            break
    if references is None:
        raise AssemblyError(
            f"{target_id} build provenance references must cover every variant"
        )
    missing = [variant for variant in VARIANTS if variant not in references]
    extra = [variant for variant in references if variant not in VARIANTS]
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise AssemblyError(
            f"{target_id} build provenance mismatch: {'; '.join(detail)}"
        )

    builds: dict[str, dict[str, Any]] = {}
    for variant in VARIANTS:
        compact_record = compact_build_record(references[variant])
        if compact_record is None:
            raise AssemblyError(
                f"{target_id}.{variant} build provenance record is missing or invalid"
            )
        state_fields = (
            "success",
            "allocator_activation",
            "actual_mir_provenance",
            "stats_disabled",
        )
        missing_state = [
            field
            for field in state_fields
            if type(compact_record.get(field)) is not bool
        ]
        if missing_state:
            raise AssemblyError(
                f"{target_id}.{variant} build provenance is missing truthful "
                f"state: {', '.join(missing_state)}"
            )
        required_true = ["success", "allocator_activation", "stats_disabled"]
        if variant != "unialloc":
            required_true.append("actual_mir_provenance")
        failed_state = [field for field in required_true if not compact_record[field]]
        if failed_state:
            raise AssemblyError(
                f"{target_id}.{variant} build provenance failed: "
                f"{', '.join(failed_state)}"
            )
        builds[variant] = compact_record
    compact["builds"] = builds
    if target_id == "rustpython":
        compact["rustpython_dynamic_runtime"] = compact_rustpython_dynamic_runtime(
            target_result, builds
        )

    evidence = target_result.get("evidence")
    source_audit_value = (
        evidence.get("source_audit") if isinstance(evidence, dict) else None
    )
    if isinstance(source_audit_value, str) and Path(source_audit_value).is_file():
        source_audit = {"sha256": sha256_file(Path(source_audit_value))}
        relative = repository_relative_path(source_audit_value)
        if relative is not None:
            source_audit["raw_record_path"] = relative
        compact["source_audit"] = source_audit
    return compact


def normalized_measurements(result_harness: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = result_harness["measurements"]
    normalized: list[dict[str, Any]] = []
    for row in rows:
        clean = {
            "round": int(row["round"]),
            "variant": str(row["variant"]),
            "performance": float(row["performance"]),
            "peak_rss_mib": float(row["peak_rss_mib"]),
        }
        raw_path = row.get("raw_path") or row.get("record_path")
        relative = repository_relative_path(raw_path)
        if relative is not None:
            clean["raw_record_path"] = relative
        normalized.append(clean)
    return normalized


def comparison_contracts(suite: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    raw_families = suite.get("comparison_families")
    if not isinstance(raw_families, list):
        raise AssemblyError("suite.comparison_families must be a list")
    families: list[dict[str, str]] = []
    seen: set[str] = set()
    for position, raw_family in enumerate(raw_families):
        if not isinstance(raw_family, dict):
            raise AssemblyError(f"comparison family {position} must be an object")
        family_id = raw_family.get("id")
        subject = raw_family.get("subject")
        reference = raw_family.get("reference")
        if (
            not isinstance(family_id, str)
            or not family_id
            or family_id in seen
            or subject not in VARIANTS
            or reference not in VARIANTS
            or subject == reference
        ):
            raise AssemblyError(f"comparison family {position} is invalid")
        seen.add(family_id)
        families.append(
            {"id": family_id, "subject": str(subject), "reference": str(reference)}
        )
    if seen != {"compiler_route", "policy_increment", "end_to_end"}:
        raise AssemblyError("suite must define all three comparison families")
    return tuple(families)


def comparison_medians(
    measurement_rows: list[Mapping[str, Any]],
    direction: str,
    family: Mapping[str, str],
    context: str,
) -> dict[str, Any]:
    by_round_variant = {
        (int(row["round"]), str(row["variant"])): row for row in measurement_rows
    }
    rounds = sorted({int(row["round"]) for row in measurement_rows})
    subject = family["subject"]
    reference = family["reference"]
    if direction == "lower_is_better":
        execution_ratios = [
            float(by_round_variant[(round_number, subject)]["performance"])
            / float(by_round_variant[(round_number, reference)]["performance"])
            for round_number in rounds
        ]
    elif direction == "higher_is_better":
        execution_ratios = [
            float(by_round_variant[(round_number, reference)]["performance"])
            / float(by_round_variant[(round_number, subject)]["performance"])
            for round_number in rounds
        ]
    else:
        raise AssemblyError(f"{context} has invalid direction")
    rss_ratios = [
        float(by_round_variant[(round_number, subject)]["peak_rss_mib"])
        / float(by_round_variant[(round_number, reference)]["peak_rss_mib"])
        for round_number in rounds
    ]
    return {
        "reference": reference,
        "subject": subject,
        "execution_cost_ratio_median": float(statistics.median(execution_ratios)),
        "peak_rss_ratio_median": float(statistics.median(rss_ratios)),
    }


def geometric_mean(values: list[float], context: str) -> float:
    if not values or any(not math.isfinite(value) or value <= 0.0 for value in values):
        raise AssemblyError(f"{context} needs finite positive ratios")
    return math.exp(math.fsum(math.log(value) for value in values) / len(values))


def geometric_mean_or_none(values: list[float], context: str) -> float | None:
    return geometric_mean(values, context) if values else None


def rss_comparison_eligibility(rss_work_model: str) -> str:
    return RSS_CORE if rss_work_model == FIXED_WORK else RSS_DIAGNOSTIC


def aggregate_comparison_families(
    rows: list[Mapping[str, Any]],
    families: tuple[dict[str, str], ...],
    *,
    source_level: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for family in families:
        family_id = family["id"]
        values = [row["comparison_families"][family_id] for row in rows]
        if source_level == "harness":
            execution_field = "execution_cost_ratio_median"
            rss_field = "peak_rss_ratio_median"
            route_rows = [row for row in rows if row["gates"][ATTRIBUTION_GATE] is True]
            route_values = [row["comparison_families"][family_id] for row in route_rows]
            fixed_rows = [row for row in rows if row["rss_work_model"] == FIXED_WORK]
            fixed_values = [row["comparison_families"][family_id] for row in fixed_rows]
            counts = {
                "harness_count": len(values),
                "peak_rss_process_observed_harness_count": len(values),
                "route_equivalent_harness_count": len(route_values),
                "fixed_work_harness_count": len(fixed_values),
            }
        elif source_level == "target":
            execution_field = "execution_cost_ratio_geometric_mean"
            rss_field = "peak_rss_process_observed_ratio_geometric_mean"
            route_values = [
                row["comparison_families"][family_id]
                for row in rows
                if row["comparison_families"][family_id][
                    "route_equivalent_execution_cost_ratio_geometric_mean"
                ]
                is not None
            ]
            fixed_values = [
                row["comparison_families"][family_id]
                for row in rows
                if row["comparison_families"][family_id][
                    "fixed_work_peak_rss_ratio_geometric_mean"
                ]
                is not None
            ]
            counts = {
                "target_count": len(values),
                "peak_rss_process_observed_target_count": len(values),
                "peak_rss_process_observed_harness_count": sum(
                    int(value["peak_rss_process_observed_harness_count"])
                    for value in values
                ),
                "route_equivalent_target_count": len(route_values),
                "route_equivalent_harness_count": sum(
                    int(value["route_equivalent_harness_count"]) for value in values
                ),
                "fixed_work_target_count": len(fixed_values),
                "fixed_work_harness_count": sum(
                    int(value["fixed_work_harness_count"]) for value in values
                ),
            }
        else:
            raise AssertionError(source_level)
        result[family_id] = {
            "reference": family["reference"],
            "subject": family["subject"],
            **counts,
            "execution_cost_ratio_geometric_mean": geometric_mean(
                [float(value[execution_field]) for value in values],
                f"{family_id} execution aggregate",
            ),
            "route_equivalent_execution_cost_ratio_geometric_mean": (
                geometric_mean_or_none(
                    [
                        float(
                            value[
                                "execution_cost_ratio_median"
                                if source_level == "harness"
                                else "route_equivalent_execution_cost_ratio_geometric_mean"
                            ]
                        )
                        for value in route_values
                    ],
                    f"{family_id} route-equivalent execution aggregate",
                )
            ),
            "peak_rss_process_observed_ratio_geometric_mean": geometric_mean(
                [float(value[rss_field]) for value in values],
                f"{family_id} process-observed RSS aggregate",
            ),
            "fixed_work_peak_rss_ratio_geometric_mean": geometric_mean_or_none(
                [
                    float(
                        value[
                            "peak_rss_ratio_median"
                            if source_level == "harness"
                            else "fixed_work_peak_rss_ratio_geometric_mean"
                        ]
                    )
                    for value in fixed_values
                ],
                f"{family_id} fixed-work RSS aggregate",
            ),
        }
    return result


def attribution_counts(
    pass_count: int,
    fail_count: int,
    minimum_ratio: float,
    maximum_ratio: float,
) -> dict[str, Any]:
    return {
        "accepted_interval": [minimum_ratio, maximum_ratio],
        "classification": (
            "all_routes_equivalent" if fail_count == 0 else "attribution_limits_present"
        ),
        "fail_count": fail_count,
        "metric": "paired_execution_cost_ratio",
        "pass_count": pass_count,
        "total_count": pass_count + fail_count,
    }


def normalized_target(
    suite_target: Mapping[str, Any],
    target_result: Mapping[str, Any],
    route_gate: Mapping[str, Any],
    implementation_revision: str,
    implementation_sha256: str,
    families: tuple[dict[str, str], ...],
) -> dict[str, Any]:
    target_id = str(suite_target["id"])
    rss_work_model = suite_target.get("rss_work_model")
    if rss_work_model not in RSS_WORK_MODELS:
        raise AssemblyError(
            f"{target_id}.rss_work_model must be fixed_work or "
            "workload_native_adaptive_iterations"
        )
    rss_eligibility = rss_comparison_eligibility(str(rss_work_model))
    if target_result.get("target_id") != target_id:
        raise AssemblyError(
            f"target id mismatch for {target_id}: {target_result.get('target_id')!r}"
        )
    expected_commit = suite_target["source"]["commit"]
    if target_result.get("source_commit") != expected_commit:
        raise AssemblyError(
            f"{target_id} source commit mismatch: expected {expected_commit}, "
            f"got {target_result.get('source_commit')}"
        )
    if target_result.get("implementation_revision") != implementation_revision:
        raise AssemblyError(
            f"{target_id} implementation_revision mismatch: expected "
            f"{implementation_revision}, got "
            f"{target_result.get('implementation_revision')}"
        )
    if target_result.get("implementation_sha256") != implementation_sha256:
        raise AssemblyError(
            f"{target_id} implementation_sha256 mismatch: expected "
            f"{implementation_sha256}, got "
            f"{target_result.get('implementation_sha256')}"
        )
    suite_harnesses = suite_target["harnesses"]
    harness_ids = [str(harness["id"]) for harness in suite_harnesses]
    result_harnesses = object_index(
        target_result.get("harnesses"),
        "id",
        harness_ids,
        f"{target_id}.harnesses",
    )
    normalized_harnesses: list[dict[str, Any]] = []
    minimum_ratio = float(route_gate["minimum_ratio"])
    maximum_ratio = float(route_gate["maximum_ratio"])
    for suite_harness in suite_harnesses:
        harness_id = str(suite_harness["id"])
        result_harness = result_harnesses[harness_id]
        direction = suite_harness["metric_direction"]
        performance_unit = suite_harness.get("performance_unit")
        performance_source = suite_harness.get("performance_source")
        if not isinstance(performance_unit, str) or not performance_unit:
            raise AssemblyError(f"{target_id}.{harness_id}.performance_unit is invalid")
        if not isinstance(performance_source, str) or not performance_source:
            raise AssemblyError(
                f"{target_id}.{harness_id}.performance_source is invalid"
            )
        if result_harness.get("metric_direction") != direction:
            raise AssemblyError(f"{target_id}.{harness_id} metric direction mismatch")
        gates = result_harness.get("gates")
        if not isinstance(gates, dict):
            raise AssemblyError(f"{target_id}.{harness_id}.gates must be an object")
        failed = [
            gate
            for gate in STORED_DATA_ELIGIBILITY_GATES
            if gates.get(gate) is not True
        ]
        if failed:
            raise AssemblyError(
                f"{target_id}.{harness_id} failed data-eligibility gates: "
                f"{', '.join(failed)}"
            )
        stored_route_classification = gates.get(ATTRIBUTION_GATE)
        if type(stored_route_classification) is not bool:
            raise AssemblyError(
                f"{target_id}.{harness_id}.{ATTRIBUTION_GATE} must be a boolean"
            )
        validate_measurements(
            result_harness.get("measurements"), f"{target_id}.{harness_id}"
        )
        warmup_attestations = validate_warmup_evidence(
            result_harness.get("warmup_evidence"), target_id, harness_id
        )
        measurement_rows = result_harness["measurements"]
        comparisons = {
            family["id"]: comparison_medians(
                measurement_rows,
                str(direction),
                family,
                f"{target_id}.{harness_id}",
            )
            for family in families
        }
        route_ratio = float(
            comparisons["compiler_route"]["execution_cost_ratio_median"]
        )
        route_equivalent = minimum_ratio <= route_ratio <= maximum_ratio
        if stored_route_classification is not route_equivalent:
            raise AssemblyError(
                f"{target_id}.{harness_id} stored compiler route classification "
                f"{stored_route_classification} disagrees with recomputed "
                f"classification {route_equivalent} at ratio {route_ratio:.6g}"
            )
        normalized = {
            "id": harness_id,
            "metric_direction": direction,
            "rss_work_model": rss_work_model,
            "rss_comparison_eligibility": rss_eligibility,
            "performance_unit": performance_unit,
            "performance_source": performance_source,
            "gates": {
                **{gate: True for gate in DATA_ELIGIBILITY_GATES},
                ATTRIBUTION_GATE: route_equivalent,
            },
            "warmup_attestations": warmup_attestations,
            "comparison_families": comparisons,
            "compiler_route_attribution": {
                "accepted_interval": [minimum_ratio, maximum_ratio],
                "classification": (
                    "within_predeclared_interval"
                    if route_equivalent
                    else "outside_predeclared_interval"
                ),
                "median_cost_ratio": route_ratio,
            },
            "measurements": normalized_measurements(result_harness),
        }
        normalized_harnesses.append(normalized)
    pass_count = sum(
        harness["gates"][ATTRIBUTION_GATE] for harness in normalized_harnesses
    )
    fail_count = len(normalized_harnesses) - pass_count
    normalized = {
        "id": target_id,
        "rss_work_model": rss_work_model,
        "rss_comparison_eligibility": rss_eligibility,
        "source_commit": expected_commit,
        "implementation_revision": implementation_revision,
        "implementation_sha256": implementation_sha256,
        "compiler_route_attribution": attribution_counts(
            pass_count, fail_count, minimum_ratio, maximum_ratio
        ),
        "comparison_families": aggregate_comparison_families(
            normalized_harnesses,
            families,
            source_level="harness",
        ),
        "harnesses": normalized_harnesses,
    }
    normalized["compact_provenance"] = compact_target_provenance(
        target_result, target_id
    )
    return normalized


def assemble(suite_path: Path, targets_dir: Path, output: Path) -> Path:
    suite_path = suite_path.resolve()
    targets_dir = targets_dir.resolve()
    suite = load_object(suite_path)
    suite_targets = suite.get("targets")
    if not isinstance(suite_targets, list):
        raise AssemblyError("suite.targets must be a list")
    route_gate = suite.get("compiler_route_equivalence_gate")
    if not isinstance(route_gate, dict):
        raise AssemblyError("suite compiler route equivalence gate must be an object")
    try:
        minimum_ratio = float(route_gate["minimum_ratio"])
        maximum_ratio = float(route_gate["maximum_ratio"])
    except (KeyError, TypeError, ValueError) as error:
        raise AssemblyError(
            "suite compiler route equivalence bounds must be numeric"
        ) from error
    if (
        not math.isfinite(minimum_ratio)
        or not math.isfinite(maximum_ratio)
        or minimum_ratio <= 0.0
        or minimum_ratio > maximum_ratio
    ):
        raise AssemblyError("suite compiler route equivalence bounds are invalid")
    implementation = suite.get("implementation")
    if not isinstance(implementation, dict):
        raise AssemblyError("suite implementation contract must be an object")
    implementation_revision = implementation.get("git_revision")
    implementation_sha256 = implementation.get("canonical_sha256")
    if (
        not isinstance(implementation_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", implementation_revision) is None
    ):
        raise AssemblyError("suite implementation git revision must be a full commit")
    if (
        not isinstance(implementation_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", implementation_sha256) is None
    ):
        raise AssemblyError("suite implementation digest must be SHA-256")
    analysis_amendments = suite.get("analysis_amendments")
    if not isinstance(analysis_amendments, list) or not analysis_amendments:
        raise AssemblyError("suite analysis amendment record is missing")
    if not isinstance(analysis_amendments[0], dict):
        raise AssemblyError("suite analysis amendment record is invalid")
    original_manifest_sha256 = analysis_amendments[0].get(
        "original_campaign_manifest_sha256"
    )
    if (
        not isinstance(original_manifest_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", original_manifest_sha256) is None
    ):
        raise AssemblyError("suite original campaign manifest digest is invalid")
    families = comparison_contracts(suite)
    targets = []
    for suite_target in suite_targets:
        if not isinstance(suite_target, dict):
            raise AssemblyError("every suite target must be an object")
        target_id = str(suite_target["id"])
        target_result = load_object(targets_dir / f"{target_id}.json")
        targets.append(
            normalized_target(
                suite_target,
                target_result,
                route_gate,
                implementation_revision,
                implementation_sha256,
                families,
            )
        )
    pass_count = sum(
        target["compiler_route_attribution"]["pass_count"] for target in targets
    )
    fail_count = sum(
        target["compiler_route_attribution"]["fail_count"] for target in targets
    )
    candidate = {
        "schema_version": 1,
        "suite_id": suite["suite_id"],
        "status": (
            "complete" if fail_count == 0 else "complete_with_attribution_limits"
        ),
        "implementation_revision": implementation_revision,
        "implementation_sha256": implementation_sha256,
        "suite_manifest_sha256": sha256_file(suite_path),
        "analysis_amendments": analysis_amendments,
        "original_campaign_manifest_sha256": original_manifest_sha256,
        "compiler_route_attribution": attribution_counts(
            pass_count, fail_count, minimum_ratio, maximum_ratio
        ),
        "comparison_families": aggregate_comparison_families(
            targets,
            families,
            source_level="target",
        ),
        "targets": targets,
    }
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(output)
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--targets-dir", type=Path, default=DEFAULT_TARGETS_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = assemble(args.suite, args.targets_dir, args.output)
    except AssemblyError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
