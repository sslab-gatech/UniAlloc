#!/usr/bin/env python3
"""Evaluate marker-free compiler heap-lifetime hints with matched THP controls.

The experiment builds one source fixture twice: once with heap-lifetime
inference disabled and once with exact inference enabled.  It then runs four
feature-parity arms in randomized blocks:

* ``default``: inference-disabled binary, lifetime policy disabled;
* ``runtime-adaptive``: inference-disabled binary, runtime-only adaptive policy;
* ``hints-ordinary``: inferred binary, exact lifetime segregation on ordinary pages;
* ``hints-thp``: the same inferred binary, proof-only compiler THP policy.

All aggregation uses Python's standard library so unit tests can validate the
evidence contract without requiring THP support or allocator builds.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import shutil
import statistics
import subprocess
import sys
import time
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = (
    ROOT
    / "tools"
    / "unialloc-rustc-pass"
    / "unialloc-rustc-mir-rewrite-dry-run.rs"
)
FIXTURE_SOURCE = (
    ROOT / "evaluation" / "fixtures" / "compiler_heap_lifetime_workload.rs"
)
RESULT_PREFIX = "UNIALLOC_COMPILER_HEAP_LIFETIME_RESULT="
GNU_TIME_PREFIX = "UNIALLOC_COMPILER_HEAP_LIFETIME_TIME"
GNU_TIME_FORMAT = GNU_TIME_PREFIX + "\t%e\t%U\t%S\t%M\t%F\t%R\t%x"
ALLOCATION_LOWERING_KIND = "semantic_scope_enter_exit_rewrite"
TARGET_CRATE = "compiler_heap_lifetime_fixture"
PROVEN_SCOPED_HINT = 0xA101
BOUNDED_PROCESS_LONG_ORACLE_HINT = 0xA102

PROVEN_BASIS_HINTS = {
    "automatic_heap_exact_mem_forget": BOUNDED_PROCESS_LONG_ORACLE_HINT,
    "automatic_heap_exact_box_leak": BOUNDED_PROCESS_LONG_ORACLE_HINT,
}

RUNTIME_COUNTER_FIELDS = (
    "semantic_total_allocations",
    "semantic_typed_allocations",
    "semantic_fallback_allocations",
    "routed_allocations",
    "routed_deallocations",
    "unknown_bypasses",
    "ordinary_extent_mappings",
    "thp_extent_mappings",
    "thp_advice_attempts",
    "thp_advice_successes",
    "thp_advice_errors",
    "thp_collapse_attempts",
    "thp_collapse_successes",
    "mapping_failures",
    "nohugepage_advice_failures",
    "compiler_inferred_unknown_or_unproven_bypasses",
    "compiler_inferred_proven_ephemeral_bypasses",
    "compiler_inferred_bypass_requested_bytes",
    "compiler_inferred_arena_lock_acquisitions",
    "compiler_inferred_direct_long_routes",
    "compiler_inferred_direct_long_routes_without_trailer",
    "compiler_inferred_direct_long_requested_bytes",
    "compiler_inferred_direct_long_slot_bytes",
    "compiler_inferred_direct_long_deallocations",
    "compiler_inferred_density_promotion_attempts",
    "compiler_inferred_density_promotion_successes",
    "compiler_inferred_density_promotion_errors",
)


class ExperimentError(RuntimeError):
    """Raised when evidence cannot satisfy the experiment contract."""


@dataclass(frozen=True)
class Arm:
    name: str
    build: str
    expected_policy: int


ARMS = (
    Arm("default", "baseline", 0),
    Arm("runtime-adaptive", "baseline", 5),
    Arm("hints-ordinary", "inferred", 7),
    Arm("hints-thp", "inferred", 6),
)
ARM_BY_NAME = {arm.name: arm for arm in ARMS}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ExperimentError(f"command timed out: {' '.join(command)}") from error
    if result.returncode != 0:
        raise ExperimentError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            + result.stderr.decode("utf-8", errors="replace")
        )
    return result


def parse_prefixed_json(text: str, prefix: str = RESULT_PREFIX) -> dict[str, Any] | None:
    parsed: dict[str, Any] | None = None
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            candidate = json.loads(line[len(prefix) :])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed = candidate
    return parsed


def parse_gnu_time(text: str) -> dict[str, int | float]:
    rows = [line for line in text.splitlines() if line.startswith(GNU_TIME_PREFIX + "\t")]
    if len(rows) != 1:
        raise ExperimentError(f"expected one GNU time row, found {len(rows)}")
    fields = rows[0].split("\t")
    if len(fields) != 8:
        raise ExperimentError(f"malformed GNU time row: {rows[0]!r}")
    try:
        return {
            "elapsed_seconds": float(fields[1]),
            "user_seconds": float(fields[2]),
            "system_seconds": float(fields[3]),
            "max_rss_kib": int(fields[4]),
            "major_faults": int(fields[5]),
            "minor_faults": int(fields[6]),
            "exit_status": int(fields[7]),
        }
    except ValueError as error:
        raise ExperimentError(f"non-numeric GNU time row: {rows[0]!r}") from error


def _candidate_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("callsite"),
        row.get("type_id"),
        row.get("module_id"),
        row.get("mir_function"),
        row.get("destination_place"),
    )


def is_heap_unknown_basis(basis: str) -> bool:
    return basis.startswith("automatic_heap_") and basis.endswith("_unknown")


def summarize_compiler_audits(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        raise ExperimentError(f"compiler audit root is missing: {root}")
    documents: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(root.rglob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(document, dict) and isinstance(document.get("rewrite_candidates"), list):
            documents.append((path, document))
    if not documents:
        raise ExperimentError(f"compiler audit root contains no rewrite audits: {root}")

    rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    enabled_files = 0
    for path, document in documents:
        compiler = document.get("compiler_pass")
        if isinstance(compiler, dict) and compiler.get(
            "automatic_heap_lifetime_inference_enabled"
        ) is True:
            enabled_files += 1
        for raw_row in document["rewrite_candidates"]:
            if not isinstance(raw_row, dict):
                raise ExperimentError(f"non-object compiler candidate in {path}")
            if raw_row.get("lowering_kind") != ALLOCATION_LOWERING_KIND:
                continue
            key = _candidate_key(raw_row)
            previous = rows.get(key)
            if previous is not None:
                comparable = (
                    "lifetime_hint",
                    "lifetime_hint_confidence",
                    "lifetime_hint_basis",
                )
                if any(previous.get(field) != raw_row.get(field) for field in comparable):
                    raise ExperimentError(f"conflicting compiler candidate {key} in {path}")
                continue
            rows[key] = raw_row

    basis_counts: Counter[str] = Counter()
    hint_counts: Counter[int] = Counter()
    proven = 0
    unknown = 0
    eventual_release_facts = 0
    for row in rows.values():
        basis = str(row.get("lifetime_hint_basis") or "")
        try:
            hint = int(row.get("lifetime_hint") or 0)
            confidence = int(row.get("lifetime_hint_confidence") or 0)
        except (TypeError, ValueError) as error:
            raise ExperimentError(f"compiler candidate has non-numeric hint fields: {row}") from error
        basis_counts[basis] += 1
        hint_counts[hint] += 1
        features = row.get("lifetime_analysis_features")
        if isinstance(features, dict) and features.get("exact_drop_path") is True:
            eventual_release_facts += 1
        expected = PROVEN_BASIS_HINTS.get(basis)
        if expected is not None:
            if hint != expected or confidence != 100:
                raise ExperimentError(
                    f"proven basis {basis} carried hint={hint}, confidence={confidence}"
                )
            proven += 1
        elif is_heap_unknown_basis(basis):
            if hint != 0 or confidence != 0:
                raise ExperimentError(
                    f"abstention basis {basis} carried hint={hint}, confidence={confidence}"
                )
            unknown += 1
        elif hint in {PROVEN_SCOPED_HINT, BOUNDED_PROCESS_LONG_ORACLE_HINT}:
            raise ExperimentError(
                f"unrecognized proven heap-lifetime basis {basis!r} carried hint={hint}"
            )

    candidates = len(rows)
    return {
        "schema_version": 1,
        "source": "compiler-heap-lifetime-audit-summary",
        "audit_root": str(root.resolve()),
        "audit_file_count": len(documents),
        "inference_enabled_audit_file_count": enabled_files,
        "candidate_allocation_site_count": candidates,
        "bounded_mechanism_site_count": proven,
        "proven_scoped_site_count": hint_counts[PROVEN_SCOPED_HINT],
        "eventual_release_fact_site_count": eventual_release_facts,
        "bounded_process_long_oracle_site_count": hint_counts[
            BOUNDED_PROCESS_LONG_ORACLE_HINT
        ],
        "heap_unknown_site_count": unknown,
        "bounded_mechanism_coverage": proven / candidates if candidates else 0.0,
        "basis_counts": dict(sorted(basis_counts.items())),
        "hint_counts": {str(key): value for key, value in sorted(hint_counts.items())},
        "audit_sha256": sha256_bytes(
            b"".join(path.name.encode() + b"\0" + path.read_bytes() for path, _ in documents)
        ),
    }


def validate_compiler_contract(
    baseline: dict[str, Any], inferred: dict[str, Any]
) -> None:
    if int(baseline.get("inference_enabled_audit_file_count", 0)) != 0:
        raise ExperimentError("inference-disabled build reports inference-enabled audit files")
    if int(inferred.get("inference_enabled_audit_file_count", 0)) <= 0:
        raise ExperimentError("inference-enabled build reports no inference-enabled audit files")
    baseline_hints = baseline.get("hint_counts", {})
    if int(baseline_hints.get(str(PROVEN_SCOPED_HINT), 0)) or int(
        baseline_hints.get(str(BOUNDED_PROCESS_LONG_ORACLE_HINT), 0)
    ):
        raise ExperimentError("inference-disabled build emitted proven heap-lifetime hints")
    inferred_bases = inferred.get("basis_counts", {})
    missing = [
        basis
        for basis in sorted(PROVEN_BASIS_HINTS)
        if int(inferred_bases.get(basis, 0)) <= 0
    ]
    if missing:
        raise ExperimentError(
            "inferred build missed required exact analysis patterns: " + ", ".join(missing)
        )
    if int(inferred.get("eventual_release_fact_site_count", 0)) < 2:
        raise ExperimentError("inferred build missed exact Drop analysis facts")
    if int(inferred.get("heap_unknown_site_count", 0)) <= 0:
        raise ExperimentError("inferred build did not preserve an ambiguous Unknown fallback")
    if int(inferred.get("bounded_mechanism_site_count", 0)) <= 0:
        raise ExperimentError("inferred build emitted no bounded mechanism sites")


def percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ExperimentError("cannot compute a percentile of an empty sample")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def paired_lower_is_better_effect(
    pairs: Sequence[tuple[float, float]], *, resamples: int, seed: int
) -> dict[str, Any]:
    """Return positive percentages when treatment reduces a lower-is-better metric."""

    if not pairs:
        raise ExperimentError("paired comparison contains no samples")
    if any(control <= 0 or treatment <= 0 for control, treatment in pairs):
        raise ExperimentError("paired comparison requires positive measurements")
    effects = [(control / treatment - 1.0) * 100.0 for control, treatment in pairs]
    estimate = statistics.median(effects)
    rng = random.Random(seed)
    bootstraps: list[float] = []
    for _ in range(resamples):
        selected = [effects[rng.randrange(len(effects))] for _ in effects]
        bootstraps.append(statistics.median(selected))
    return {
        "paired_samples": len(effects),
        "median_percent": estimate,
        "bootstrap_95_low_percent": percentile(bootstraps, 0.025),
        "bootstrap_95_high_percent": percentile(bootstraps, 0.975),
        "semantics": "positive means treatment reduced the lower-is-better metric",
    }


def _measured_rows(samples: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in samples if not bool(row.get("warmup"))]


def validate_sample_contract(samples: Sequence[dict[str, Any]]) -> str:
    measured = _measured_rows(samples)
    if not measured:
        raise ExperimentError("experiment contains no measured samples")
    checksums: set[str] = set()
    for row in measured:
        arm_name = str(row.get("arm"))
        if arm_name not in ARM_BY_NAME:
            raise ExperimentError(f"sample has unknown arm: {arm_name}")
        result = row.get("result")
        if not isinstance(result, dict) or result.get("passed") is not True:
            raise ExperimentError(f"sample {arm_name} has no passing workload result")
        if result.get("arm") != arm_name:
            raise ExperimentError(f"sample/result arm mismatch for {arm_name}")
        expected_policy = ARM_BY_NAME[arm_name].expected_policy
        if int(result.get("policy", -1)) != expected_policy:
            raise ExperimentError(
                f"sample {arm_name} policy mismatch: expected {expected_policy}, "
                f"got {result.get('policy')}"
            )
        if int(result.get("mapping_failures", -1)) != 0:
            raise ExperimentError(f"sample {arm_name} observed an arena mapping failure")
        if arm_name == "hints-ordinary" and int(
            result.get("nohugepage_advice_failures", -1)
        ) != 0:
            raise ExperimentError(
                "ordinary compiler-hint control observed a MADV_NOHUGEPAGE failure"
            )
        if arm_name in {"hints-ordinary", "hints-thp"}:
            direct_routes = int(result.get("compiler_inferred_direct_long_routes", 0))
            if direct_routes <= 0:
                raise ExperimentError(
                    f"sample {arm_name} transported no bounded process-long oracle hints"
                )
            if int(
                result.get("compiler_inferred_direct_long_deallocations", -1)
            ) != 0:
                raise ExperimentError(
                    f"sample {arm_name} deallocated a bounded process-long oracle object"
                )
        checksums.add(str(result.get("checksum")))
    if len(checksums) != 1:
        raise ExperimentError(f"correctness checksums differ across arms: {sorted(checksums)}")
    return next(iter(checksums))


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def summarize_samples(
    samples: Sequence[dict[str, Any]], *, bootstrap_resamples: int, seed: int
) -> dict[str, Any]:
    checksum = validate_sample_contract(samples)
    measured = _measured_rows(samples)
    per_arm: dict[str, Any] = {}
    for arm in ARMS:
        rows = [row for row in measured if row["arm"] == arm.name]
        if not rows:
            raise ExperimentError(f"missing measured samples for {arm.name}")
        results = [row["result"] for row in rows]
        totals = {
            field: sum(int(result.get(field, 0)) for result in results)
            for field in RUNTIME_COUNTER_FIELDS
        }
        per_arm[arm.name] = {
            "measured_runs": len(rows),
            "median_outer_wall_seconds": _median(
                [float(row["outer_wall_seconds"]) for row in rows]
            ),
            "median_gnu_wall_seconds": _median(
                [float(row["gnu_time"]["elapsed_seconds"]) for row in rows]
            ),
            "median_workload_seconds": _median(
                [float(result["workload_ns"]) / 1_000_000_000.0 for result in results]
            ),
            "median_allocation_seconds": _median(
                [float(result["allocation_ns"]) / 1_000_000_000.0 for result in results]
            ),
            "median_touch_seconds": _median(
                [float(result["touch_ns"]) / 1_000_000_000.0 for result in results]
            ),
            "median_max_rss_kib": _median(
                [float(row["gnu_time"]["max_rss_kib"]) for row in rows]
            ),
            "median_smaps_rss_kib": _median(
                [float(result.get("smaps_rss_kib", 0)) for result in results]
            ),
            "median_anon_hugepages_kib": _median(
                [float(result.get("smaps_anon_hugepages_kib", 0)) for result in results]
            ),
            "max_anon_hugepages_kib": max(
                int(result.get("smaps_anon_hugepages_kib", 0)) for result in results
            ),
            "max_pre_touch_anon_hugepages_kib": max(
                int(result.get("pre_touch_anon_hugepages_kib", 0)) for result in results
            ),
            "runtime_counter_totals": totals,
        }

    rows_by_block = {(int(row["block"]), str(row["arm"])): row for row in measured}
    measured_blocks = sorted({int(row["block"]) for row in measured})
    block_evidence: list[dict[str, Any]] = []
    backed_blocks: set[int] = set()
    for block in measured_blocks:
        thp_row = rows_by_block.get((block, "hints-thp"))
        ordinary_row = rows_by_block.get((block, "hints-ordinary"))
        if thp_row is None or ordinary_row is None:
            raise ExperimentError(f"block {block} lacks the matched THP/ordinary pair")
        thp_result = thp_row["result"]
        ordinary_result = ordinary_row["result"]
        thp_anon = int(thp_result.get("smaps_anon_hugepages_kib", 0))
        advice_acceptances = int(
            thp_result.get("compiler_inferred_density_promotion_successes", 0)
        )
        ordinary_anon = int(ordinary_result.get("smaps_anon_hugepages_kib", 0))
        ordinary_nohuge_failures = int(
            ordinary_result.get("nohugepage_advice_failures", 0)
        )
        reasons: list[str] = []
        if thp_anon <= 0:
            reasons.append("hints-thp sample has no resident AnonHugePages")
        if advice_acceptances <= 0:
            reasons.append("hints-thp sample has no MADV_HUGEPAGE acceptance")
        if ordinary_anon != 0:
            reasons.append("ordinary control has resident AnonHugePages")
        if ordinary_nohuge_failures != 0:
            reasons.append("ordinary control has MADV_NOHUGEPAGE failures")
        included = not reasons
        if included:
            backed_blocks.add(block)
        block_evidence.append(
            {
                "block": block,
                "included_in_thp_effects": included,
                "exclusion_reasons": reasons,
                "hints_thp_anon_hugepages_kib": thp_anon,
                "hints_thp_madvise_hugepage_acceptances": advice_acceptances,
                "ordinary_control_anon_hugepages_kib": ordinary_anon,
                "ordinary_control_nohugepage_advice_failures": ordinary_nohuge_failures,
            }
        )

    def pairs(
        control: str,
        treatment: str,
        path: tuple[str, ...],
        blocks: Sequence[int],
    ) -> list[tuple[float, float]]:
        values: list[tuple[float, float]] = []
        for block in blocks:
            control_row = rows_by_block.get((block, control))
            treatment_row = rows_by_block.get((block, treatment))
            if control_row is None or treatment_row is None:
                raise ExperimentError(f"block {block} lacks {control}/{treatment} pairing")

            def read(row: dict[str, Any]) -> float:
                value: Any = row
                for element in path:
                    value = value[element]
                return float(value)

            values.append((read(control_row), read(treatment_row)))
        return values

    comparisons: dict[str, Any] = {}
    definitions = (
        ("runtime_adaptive_vs_default", "default", "runtime-adaptive", False),
        ("compiler_layout_vs_default", "default", "hints-ordinary", False),
        (
            "compiler_hugepage_vs_runtime_adaptive",
            "runtime-adaptive",
            "hints-thp",
            True,
        ),
        ("thp_increment_vs_same_hints", "hints-ordinary", "hints-thp", True),
        ("full_feature_vs_default", "default", "hints-thp", True),
    )
    metrics = {
        "outer_wall_seconds": ("outer_wall_seconds",),
        "allocation_seconds": ("result", "allocation_ns"),
        "touch_seconds": ("result", "touch_ns"),
        "workload_seconds": ("result", "workload_ns"),
        "max_rss_kib": ("gnu_time", "max_rss_kib"),
    }
    for comparison_index, (name, control, treatment, requires_backing) in enumerate(
        definitions
    ):
        included_blocks = (
            sorted(backed_blocks) if requires_backing else measured_blocks
        )
        metric_summaries: dict[str, Any] = {}
        for metric_index, (metric, path) in enumerate(metrics.items()):
            raw_pairs = pairs(control, treatment, path, included_blocks)
            if metric in {"allocation_seconds", "touch_seconds", "workload_seconds"}:
                raw_pairs = [
                    (control_ns / 1_000_000_000.0, treatment_ns / 1_000_000_000.0)
                    for control_ns, treatment_ns in raw_pairs
                ]
            if raw_pairs:
                metric_summaries[metric] = paired_lower_is_better_effect(
                    raw_pairs,
                    resamples=bootstrap_resamples,
                    seed=seed + comparison_index * 101 + metric_index,
                )
            else:
                metric_summaries[metric] = {
                    "paired_samples": 0,
                    "status": "unavailable_without_backed_thp_pairs",
                }
        comparisons[name] = {
            "control": control,
            "treatment": treatment,
            "requires_per_block_thp_backing": requires_backing,
            "included_blocks": included_blocks,
            "excluded_blocks": (
                sorted(set(measured_blocks) - set(included_blocks))
                if requires_backing
                else []
            ),
            "metrics": metric_summaries,
        }

    excluded_blocks = sorted(set(measured_blocks) - backed_blocks)
    return {
        "schema_version": 1,
        "source": "compiler-heap-lifetime-experiment-summary",
        "correctness_checksum": checksum,
        "correctness_hashes_equal": True,
        "per_arm": per_arm,
        "comparisons": comparisons,
        "external_baseline_gate": {
            "google_tcmalloc_temeraire_hpaa": (
                "not_measured_in_this_harness_requires_separate_verified_"
                "fixed_revision_official_bazel_arm"
            ),
            "required_provenance": [
                "Temeraire/HPAA implementation identity",
                "Google TCMalloc git commit",
                "official Bazel build and link-time malloc target options",
            ],
            "gperftools_legacy_eligible_as_google_tcmalloc": False,
            "fallback_allowed": False,
        },
        "thp_gate": {
            "measured_block_count": len(measured_blocks),
            "backed_pair_count": len(backed_blocks),
            "included_blocks": sorted(backed_blocks),
            "excluded_blocks": excluded_blocks,
            "block_evidence": block_evidence,
            "madvise_acceptance_semantics": (
                "kernel accepted MADV_HUGEPAGE; actual THP backing requires "
                "nonzero smaps AnonHugePages"
            ),
            "claim_ready": bool(measured_blocks) and not excluded_blocks,
        },
    }


def read_toolchain() -> str:
    value = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    if not value:
        raise ExperimentError("rust-toolchain is empty")
    return value


def ensure_wrapper(path: Path, *, toolchain: str, timeout: int) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["RUSTC_BOOTSTRAP"] = "1"
    command = [
        "rustc",
        f"+{toolchain}",
        "--cfg",
        "unialloc_rustc_current",
        str(PASS_SOURCE),
        "-O",
        "-o",
        str(path),
    ]
    result = run_checked(command, cwd=ROOT, env=env, timeout=timeout)
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "source": str(PASS_SOURCE.resolve()),
        "source_sha256": sha256_file(PASS_SOURCE),
        "command": command,
        "stdout_sha256": sha256_bytes(result.stdout),
        "stderr_sha256": sha256_bytes(result.stderr),
        "built_by_harness": True,
    }


def bind_existing_wrapper(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ExperimentError(f"provided compiler wrapper does not exist: {path}")
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "source": str(PASS_SOURCE.resolve()),
        "source_sha256": sha256_file(PASS_SOURCE),
        "command": None,
        "built_by_harness": False,
    }


def create_fixture_crate(root: Path) -> Path:
    crate = root / "fixture-crate"
    source_dir = crate / "src"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "main.rs").write_bytes(FIXTURE_SOURCE.read_bytes())
    manifest = f'''[package]
name = "compiler-heap-lifetime-fixture"
version = "0.1.0"
edition = "2021"
publish = false

[[bin]]
name = "{TARGET_CRATE}"
path = "src/main.rs"

[dependencies]
unialloc = {{ path = {json.dumps(str((ROOT / "unialloc").resolve()))}, features = ["stats", "type_isolation", "lifetime_hugepage"] }}
'''
    (crate / "Cargo.toml").write_text(manifest, encoding="utf-8")
    shutil.copy2(ROOT / "Cargo.lock", crate / "Cargo.lock")
    return crate


def compiler_environment(
    *,
    wrapper: Path,
    sysroot: Path,
    audit_dir: Path,
    log_dir: Path,
    inference: bool,
    target_dir: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    library_dir = str((sysroot / "lib").resolve())
    existing_library_path = env.get("LD_LIBRARY_PATH", "")
    env.update(
        {
            "RUSTC_WRAPPER": str(wrapper.resolve()),
            "RUSTC_BOOTSTRAP": "1",
            "UNIALLOC_RUSTC_SYSROOT": str(sysroot.resolve()),
            "UNIALLOC_RUSTC_TARGET_CRATES": TARGET_CRATE,
            "UNIALLOC_REWRITE_AUDIT_DIR": str(audit_dir.resolve()),
            "UNIALLOC_PASS_LOG_DIR": str(log_dir.resolve()),
            "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "1",
            "UNIALLOC_DIRECT_LOCAL_METADATA_ABI": "1",
            "UNIALLOC_DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP": "1",
            "UNIALLOC_CONTINUE_COMPILATION": "1",
            "UNIALLOC_LOWERING_POLICY_FLAGS": "1",
            "UNIALLOC_AUTO_HEAP_LIFETIME_INFERENCE": "1" if inference else "0",
            "CARGO_INCREMENTAL": "0",
            "CARGO_TARGET_DIR": str(target_dir.resolve()),
            "LD_LIBRARY_PATH": library_dir
            + ((os.pathsep + existing_library_path) if existing_library_path else ""),
        }
    )
    return env


def build_fixture(
    *,
    crate: Path,
    mode: str,
    inference: bool,
    wrapper: Path,
    sysroot: Path,
    toolchain: str,
    output: Path,
    timeout: int,
) -> dict[str, Any]:
    audit_dir = output / "compiler-audits" / mode
    log_dir = output / "compiler-logs" / mode
    target_dir = output / "targets" / mode
    audit_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    env = compiler_environment(
        wrapper=wrapper,
        sysroot=sysroot,
        audit_dir=audit_dir,
        log_dir=log_dir,
        inference=inference,
        target_dir=target_dir,
    )
    command = [
        "cargo",
        f"+{toolchain}",
        "build",
        "--release",
        "--offline",
        "--manifest-path",
        str(crate / "Cargo.toml"),
    ]
    result = run_checked(command, cwd=crate, env=env, timeout=timeout)
    build_dir = output / "build-logs"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / f"{mode}.stdout.bin").write_bytes(result.stdout)
    (build_dir / f"{mode}.stderr.bin").write_bytes(result.stderr)
    binary = target_dir / "release" / TARGET_CRATE
    if not binary.is_file():
        raise ExperimentError(f"fixture build did not produce {binary}")
    audit = summarize_compiler_audits(audit_dir)
    return {
        "mode": mode,
        "inference": inference,
        "binary": str(binary.resolve()),
        "binary_sha256": sha256_file(binary),
        "command": command,
        "audit": audit,
        "stdout_sha256": sha256_bytes(result.stdout),
        "stderr_sha256": sha256_bytes(result.stderr),
    }


def command_prefix(cpu: int | None, numa_node: int | None) -> list[str]:
    if cpu is None and numa_node is None:
        return []
    numactl = shutil.which("numactl")
    if numactl and cpu is not None and numa_node is not None:
        return [numactl, f"--physcpubind={cpu}", f"--membind={numa_node}"]
    if cpu is not None and shutil.which("taskset"):
        return [str(shutil.which("taskset")), "-c", str(cpu)]
    raise ExperimentError("requested CPU/NUMA binding cannot be satisfied on this host")


def run_one(
    *,
    arm: Arm,
    binary: Path,
    block: int,
    order: int,
    warmup: bool,
    iterations: int,
    touch_passes: int,
    thp_settle_ms: int,
    workload_seed: int,
    runs_dir: Path,
    timeout: int,
    cpu: int | None,
    numa_node: int | None,
) -> dict[str, Any]:
    time_binary = Path("/usr/bin/time")
    if not time_binary.is_file():
        raise ExperimentError("GNU /usr/bin/time is required for RSS evidence")
    block_name = f"{'warmup' if warmup else 'block'}-{block:03d}"
    run_dir = runs_dir / block_name / f"{order:02d}-{arm.name}"
    run_dir.mkdir(parents=True, exist_ok=False)
    command = [
        *command_prefix(cpu, numa_node),
        str(binary),
        "--arm",
        arm.name,
        "--iterations",
        str(iterations),
        "--touch-passes",
        str(touch_passes),
        "--thp-settle-ms",
        str(thp_settle_ms),
        "--seed",
        str(workload_seed),
    ]
    time_path = run_dir / "gnu-time.txt"
    timed_command = [
        str(time_binary),
        "-f",
        GNU_TIME_FORMAT,
        "-o",
        str(time_path),
        *command,
    ]
    start = time.monotonic_ns()
    try:
        result = subprocess.run(
            timed_command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ExperimentError(f"workload timed out for {arm.name}") from error
    elapsed = time.monotonic_ns() - start
    (run_dir / "stdout.bin").write_bytes(result.stdout)
    (run_dir / "stderr.bin").write_bytes(result.stderr)
    if result.returncode != 0:
        raise ExperimentError(
            f"workload failed for {arm.name} ({result.returncode}):\n"
            + result.stderr.decode("utf-8", errors="replace")
        )
    workload = parse_prefixed_json(
        result.stdout.decode("utf-8", errors="replace")
        + "\n"
        + result.stderr.decode("utf-8", errors="replace")
    )
    if workload is None:
        raise ExperimentError(f"workload emitted no result record for {arm.name}")
    gnu_time = parse_gnu_time(time_path.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "arm": arm.name,
        "build": arm.build,
        "block": block,
        "order": order,
        "warmup": warmup,
        "command": command,
        "binary": str(binary),
        "binary_sha256": sha256_file(binary),
        "outer_wall_seconds": elapsed / 1_000_000_000.0,
        "gnu_time": gnu_time,
        "stdout_sha256": sha256_bytes(result.stdout),
        "stderr_sha256": sha256_bytes(result.stderr),
        "result": workload,
    }


def read_optional(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def git_record() -> dict[str, Any]:
    head = run_checked(["git", "rev-parse", "HEAD"], cwd=ROOT, timeout=30)
    status = run_checked(["git", "status", "--short"], cwd=ROOT, timeout=30)
    return {
        "head": head.stdout.decode().strip(),
        "status_short": status.stdout.decode("utf-8", errors="replace").splitlines(),
    }


def host_record(toolchain: str, sysroot: Path) -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "toolchain": toolchain,
        "sysroot": str(sysroot.resolve()),
        "thp_enabled": read_optional(Path("/sys/kernel/mm/transparent_hugepage/enabled")),
        "thp_defrag": read_optional(Path("/sys/kernel/mm/transparent_hugepage/defrag")),
        "pmd_size": read_optional(Path("/sys/kernel/mm/transparent_hugepage/hpage_pmd_size")),
        "meminfo_hugepages": [
            line
            for line in (read_optional(Path("/proc/meminfo")) or "").splitlines()
            if line.startswith(("HugePages_", "Hugepagesize:", "Hugetlb:"))
        ],
    }


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        default=time.strftime("compiler-heap-lifetime-%Y%m%d-%H%M%S"),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--toolchain", default=read_toolchain())
    parser.add_argument("--pass-binary", type=Path)
    parser.add_argument("--iterations", type=int, default=4096)
    parser.add_argument("--touch-passes", type=int, default=64)
    parser.add_argument("--thp-settle-ms", type=int, default=0)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=9)
    parser.add_argument("--bootstrap-resamples", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260715)
    parser.add_argument("--workload-seed", type=int, default=0x20260715)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--cpu", type=int)
    parser.add_argument("--numa-node", type=int)
    parser.add_argument("--require-thp", action="store_true")
    args = parser.parse_args(list(argv))
    if (
        args.iterations <= 0
        or args.touch_passes <= 0
        or args.thp_settle_ms < 0
        or args.warmups < 0
        or args.repeats <= 0
        or args.bootstrap_resamples <= 0
        or args.timeout <= 0
    ):
        parser.error(
            "iterations, touch passes, repeats, bootstrap resamples, and timeout "
            "must be positive; THP settle milliseconds must be nonnegative"
        )
    if args.output_dir is None:
        args.output_dir = ROOT / "evaluation" / "raw" / args.run_id
    return args


def require_fresh_directory(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise ExperimentError(f"output directory must be fresh: {path}")
    path.mkdir(parents=True, exist_ok=True)


def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output_dir.resolve()
    require_fresh_directory(output)
    if not FIXTURE_SOURCE.is_file() or not PASS_SOURCE.is_file():
        raise ExperimentError("fixture or compiler pass source is missing")

    sysroot_result = run_checked(
        ["rustc", f"+{args.toolchain}", "--print", "sysroot"],
        cwd=ROOT,
        timeout=args.timeout,
    )
    sysroot = Path(sysroot_result.stdout.decode().strip())
    if args.pass_binary is None:
        wrapper_record = ensure_wrapper(
            output / "tools" / "unialloc-rustc-wrapper",
            toolchain=args.toolchain,
            timeout=args.timeout,
        )
    else:
        wrapper_record = bind_existing_wrapper(args.pass_binary.resolve())
    wrapper = Path(wrapper_record["path"])
    crate = create_fixture_crate(output)

    builds = {
        "baseline": build_fixture(
            crate=crate,
            mode="baseline",
            inference=False,
            wrapper=wrapper,
            sysroot=sysroot,
            toolchain=args.toolchain,
            output=output,
            timeout=args.timeout,
        ),
        "inferred": build_fixture(
            crate=crate,
            mode="inferred",
            inference=True,
            wrapper=wrapper,
            sysroot=sysroot,
            toolchain=args.toolchain,
            output=output,
            timeout=args.timeout,
        ),
    }
    validate_compiler_contract(builds["baseline"]["audit"], builds["inferred"]["audit"])

    manifest = {
        "schema_version": 1,
        "source": "compiler-heap-lifetime-experiment-manifest",
        "run_id": args.run_id,
        "created_at_unix": int(time.time()),
        "git": git_record(),
        "host": host_record(args.toolchain, sysroot),
        "script": str(Path(__file__).resolve()),
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "fixture": str(FIXTURE_SOURCE.resolve()),
        "fixture_sha256": sha256_file(FIXTURE_SOURCE),
        "fixture_manifest_sha256": sha256_file(crate / "Cargo.toml"),
        "repository_lock_sha256": sha256_file(ROOT / "Cargo.lock"),
        "fixture_lock_sha256": sha256_file(crate / "Cargo.lock"),
        "wrapper": wrapper_record,
        "builds": builds,
        "configuration": {
            "iterations": args.iterations,
            "touch_passes": args.touch_passes,
            "thp_settle_ms": args.thp_settle_ms,
            "warmups": args.warmups,
            "repeats": args.repeats,
            "bootstrap_resamples": args.bootstrap_resamples,
            "seed": args.seed,
            "workload_seed": args.workload_seed,
            "cpu": args.cpu,
            "numa_node": args.numa_node,
            "require_thp": args.require_thp,
            "compiler_flag": "--unialloc-auto-heap-lifetime-inference",
            "compiler_environment": "UNIALLOC_AUTO_HEAP_LIFETIME_INFERENCE=1",
            "bounded_mechanism_hints": {
                "proven_scoped": PROVEN_SCOPED_HINT,
                "bounded_process_long_oracle": BOUNDED_PROCESS_LONG_ORACLE_HINT,
            },
            "runtime_policies": {
                "adaptive": "AdaptiveRuntimeHugepage (repr 5)",
                "ordinary": "CompilerInferredOrdinary (repr 7)",
                "thp": "CompilerInferredHugepage (repr 6)",
            },
        },
    }
    write_json(output / "manifest.json", manifest)

    rng = random.Random(args.seed)
    samples: list[dict[str, Any]] = []
    samples_path = output / "samples.jsonl"
    with samples_path.open("w", encoding="utf-8") as stream:
        phases = [(True, index) for index in range(args.warmups)] + [
            (False, index) for index in range(args.repeats)
        ]
        for warmup, block in phases:
            order = list(ARMS)
            rng.shuffle(order)
            for order_index, arm in enumerate(order):
                binary = Path(builds[arm.build]["binary"])
                sample = run_one(
                    arm=arm,
                    binary=binary,
                    block=block,
                    order=order_index,
                    warmup=warmup,
                    iterations=args.iterations,
                    touch_passes=args.touch_passes,
                    thp_settle_ms=args.thp_settle_ms,
                    workload_seed=args.workload_seed,
                    runs_dir=output / "runs",
                    timeout=args.timeout,
                    cpu=args.cpu,
                    numa_node=args.numa_node,
                )
                samples.append(sample)
                stream.write(json.dumps(sample, sort_keys=True) + "\n")
                stream.flush()

    summary = summarize_samples(
        samples,
        bootstrap_resamples=args.bootstrap_resamples,
        seed=args.seed,
    )
    summary["run_id"] = args.run_id
    summary["compiler_audits"] = {
        name: record["audit"] for name, record in builds.items()
    }
    summary["claim_boundary"] = (
        "The automatic compiler result is exact only for the audited fixture patterns. "
        "The ordinary/THP pair uses one inferred binary and matched lifetime geometry. "
        "A THP claim additionally requires the recorded actual-backing gate."
    )
    if args.require_thp and not summary["thp_gate"]["claim_ready"]:
        write_json(output / "summary.json", summary)
        raise ExperimentError("--require-thp was set but the actual-backing gate did not pass")
    write_json(output / "summary.json", summary)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        summary = run_experiment(args)
    except ExperimentError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
