#!/usr/bin/env python3
"""Build deterministic JSON and SVG evidence from the final raw UniAlloc A/B runs.

The repeated panels provide claim-shaped timing evidence.  The complete 468-leaf
pair is deliberately treated as a single-observation smoke screen.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1
FULL_LEAF_COUNT = 468
STRATIFIED_LEAF_COUNT = 57
ROBUST_FLOOR_NS_PER_ITER = 100.0
FULL_CAVEAT = (
    "The complete 468-leaf sweep contains one reported observation per binary and leaf. "
    "It serves as a completeness and gross-regression smoke screen; repeated panels "
    "provide claim-grade estimates."
)
CLASSIFICATION_CAVEAT = (
    "Classification describes whether the timed closure performs heap allocation. "
    "Attribution requires source or disassembly evidence; full-only leaves use a "
    "conservative name-based heuristic."
)

# Source-audited controls in the 57-leaf panel.  Their timed closures reuse
# existing storage or perform read/in-place work without allocating.
AUDITED_LAYOUT_CONTROLS = frozenset(
    {
        "binary_heap::bench_pop",
        "binary_heap::bench_push",
        "btree::map::find_seq_100",
        "btree::map::iter_10k",
        "btree::set::intersection_staggered_100_vs_100",
        "linked_list::bench_iter",
        "linked_list::bench_iter_mut_rev",
        "linked_list::bench_iter_rev",
        "slice::rotate_huge_by1234577_big",
        "slice::rotate_medium_by727_bytes",
        "str::ends_with_ascii_char::long_lorem_ipsum",
        "str::ends_with_unichar::long_lorem_ipsum",
        "str::rsplitn_space_char::long_lorem_ipsum",
        "str::split_space_char::long_lorem_ipsum",
        "str::starts_with_str::short_ascii",
        "vec::bench_dedup_slice_truncate_100000",
        "vec::bench_retain_100000",
        "vec_deque::bench_into_iter",
        "vec_deque::bench_iter_1000",
        "vec_deque::bench_into_iter_next_chunk",
        "vec_deque::bench_into_iter_try_fold",
        "vec_deque::bench_mut_iter_1000",
    }
)

AUDITED_ALLOCATION_SENSITIVE = frozenset(
    {
        "binary_heap::bench_find_smallest_1000",
        "binary_heap::bench_from_vec",
        "binary_heap::bench_into_sorted_vec",
        "btree::map::clone_fat_val_100_and_remove_half",
        "btree::map::insert_seq_10_000",
        "btree::set::clone_100_and_drain_half",
        "btree::set::clone_10k_and_remove_half",
        "linked_list::bench_collect_into",
        "linked_list::bench_push_front_pop_front",
        "slice::concat",
        "slice::join",
        "slice::random_inserts",
        "slice::sort_by_cached_key_lexicographic",
        "slice::sort_large_big",
        "slice::sort_large_descending",
        "slice::sort_small_big",
        "slice::sort_unstable_large_big",
        "slice::sort_unstable_large_ascending",
        "slice::sort_unstable_large_mostly_descending",
        "str::bench_join",
        "string::bench_from_str",
        "string::bench_insert_str_long",
        "string::bench_push_char_one_byte",
        "string::bench_push_char_two_bytes",
        "string::bench_push_str_one_byte",
        "string::bench_with_capacity",
        "string::from_utf8_lossy_100_invalid",
        "vec::bench_clone_from_10_0100_1000",
        "vec::bench_clone_from_10_1000_0100",
        "vec::bench_extend_from_slice_1000_1000",
        "vec::bench_flat_map_collect",
        "vec::bench_in_place_u128_1000_i0",
        "vec::bench_in_place_xu32_1000_i0",
        "vec::bench_map_regular",
        "vec::bench_chain_collect",
        "vec::bench_chain_extend_ref",
        "vec::bench_with_capacity_0010",
        "vec::bench_with_capacity_0100",
        "vec::bench_with_capacity_1000",
        "vec_deque::bench_grow_1025",
        "vec_deque::bench_into_iter_fold",
    }
)

# These source-audited leaves include substantial non-allocator hot loops whose
# placement changes when the raw GlobalAlloc wrapper shrinks.  Repeated A/B
# values remain visible, while allocator-path aggregates keep the confounder
# explicit and keeps allocator attribution evidence-bound.
AUDITED_LAYOUT_CONFOUNDED = frozenset(
    {
        "binary_heap::bench_from_vec",
        "slice::concat",
        "slice::sort_large_descending",
        "slice::sort_unstable_large_ascending",
        "vec_deque::bench_iter_1000",
        "vec_deque::bench_mut_iter_1000",
    }
)


def fail(message: str) -> "NoReturn":
    raise RuntimeError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    if not path.is_file():
        fail(f"input is not ready: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        fail(f"invalid JSON in {path}: {error}")


def finite_positive(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        fail(f"{field} must be numeric: {error}")
    if not math.isfinite(number) or number <= 0:
        fail(f"{field} must be finite and positive, got {value!r}")
    return number


def round_number(value: float, places: int = 6) -> float:
    result = round(float(value), places)
    return 0.0 if result == -0.0 else result


def median(values: Sequence[float]) -> float:
    if not values:
        fail("cannot take a median of an empty sequence")
    return float(statistics.median(values))


def percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        fail("cannot take a percentile of an empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def runtime_reduction_percent(base: float, candidate: float) -> float:
    return 100.0 * (base - candidate) / base


def speedup_percent(base: float, candidate: float) -> float:
    return 100.0 * (base / candidate - 1.0)


def normalize_label(value: Any) -> str:
    label = str(value).strip().lower()
    if label in {"base", "baseline"}:
        return "baseline"
    if label in {"variant", "candidate"}:
        return "candidate"
    fail(f"unsupported A/B label: {value!r}")


def family_of(benchmark: str) -> str:
    parts = benchmark.split("::")
    if parts[0] == "btree" and len(parts) >= 2:
        return "::".join(parts[:2])
    return parts[0]


def classify_benchmark(benchmark: str) -> dict[str, Any]:
    if benchmark in AUDITED_LAYOUT_CONTROLS:
        result: dict[str, Any] = {
            "kind": "layout/control",
            "basis": "source-audited timed closure has no heap allocation",
        }
    elif benchmark in AUDITED_ALLOCATION_SENSITIVE:
        result = {
            "kind": "allocation-sensitive",
            "basis": "source-audited timed closure allocates, reallocates, or deallocates",
        }
    else:
        result = {}

    if result:
        if benchmark in AUDITED_LAYOUT_CONFOUNDED:
            result["layout_confounded"] = True
            result["layout_evidence"] = (
                "timed non-allocator hot loop or shared machine function is sensitive "
                "to linked instruction placement"
            )
        return result

    lowered = benchmark.lower()
    family = family_of(benchmark)
    control_tokens = (
        "::iter",
        "bench_iter",
        "find_",
        "::find",
        "contains",
        "starts_with",
        "ends_with",
        "split",
        "search",
        "peek",
        "rotate",
        "reverse",
        "is_subset",
        "is_superset",
        "is_disjoint",
        "intersection",
        "difference",
        "range_",
    )
    sensitive_tokens = (
        "alloc",
        "capacity",
        "clone",
        "collect",
        "concat",
        "drain",
        "extend",
        "from_",
        "grow",
        "insert",
        "join",
        "new",
        "push",
        "remove",
        "replace",
        "reserve",
        "resize",
        "sort",
        "to_owned",
    )
    if any(token in lowered for token in sensitive_tokens):
        kind = "allocation-sensitive"
    elif family == "str" or any(token in lowered for token in control_tokens):
        kind = "layout/control"
    else:
        # Conservative default: allocator-facing collections are more likely to
        # allocate than pure scan families.  The report marks this as heuristic.
        kind = "allocation-sensitive"
    return {"kind": kind, "basis": "conservative name-family heuristic"}


def sample_summary(values: Sequence[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "samples_ns_per_iter": [round_number(value) for value in values],
        "median_ns_per_iter": round_number(median(values)),
        "min_ns_per_iter": round_number(min(values)),
        "max_ns_per_iter": round_number(max(values)),
    }


def leaf_from_samples(
    benchmark: str,
    baseline_by_rep: Mapping[int, float],
    candidate_by_rep: Mapping[int, float],
    source_role: str,
) -> dict[str, Any]:
    reps = sorted(baseline_by_rep)
    if reps != sorted(candidate_by_rep):
        fail(f"{source_role}: A/B repetition identities differ for {benchmark}")
    baseline = [baseline_by_rep[rep] for rep in reps]
    candidate = [candidate_by_rep[rep] for rep in reps]
    base_median = median(baseline)
    candidate_median = median(candidate)
    paired_reductions = [
        runtime_reduction_percent(baseline_by_rep[rep], candidate_by_rep[rep])
        for rep in reps
    ]
    paired_speedups = [
        speedup_percent(baseline_by_rep[rep], candidate_by_rep[rep]) for rep in reps
    ]
    classification = classify_benchmark(benchmark)
    return {
        "benchmark": benchmark,
        "family": family_of(benchmark),
        "classification": classification,
        "source": source_role,
        "measured_repetitions": len(reps),
        "repetition_ids": reps,
        "baseline": sample_summary(baseline),
        "candidate": sample_summary(candidate),
        "median_candidate_to_baseline_ratio": round_number(candidate_median / base_median),
        "median_runtime_reduction_percent": round_number(
            runtime_reduction_percent(base_median, candidate_median)
        ),
        "median_speedup_percent": round_number(speedup_percent(base_median, candidate_median)),
        "paired_runtime_reduction_percent": {
            "samples": [round_number(value) for value in paired_reductions],
            "median": round_number(median(paired_reductions)),
            "min": round_number(min(paired_reductions)),
            "max": round_number(max(paired_reductions)),
        },
        "paired_speedup_percent": {
            "samples": [round_number(value) for value in paired_speedups],
            "median": round_number(median(paired_speedups)),
            "min": round_number(min(paired_speedups)),
            "max": round_number(max(paired_speedups)),
        },
    }


def binary_identity(payload: Mapping[str, Any], role: str) -> dict[str, str | None]:
    path_key = "base" if role == "baseline" else "variant"
    sha_key = "base_sha256" if role == "baseline" else "variant_sha256"
    path_value = payload.get(path_key)
    sha_value = payload.get(sha_key)
    if sha_value is None and path_value:
        binary = Path(str(path_value))
        if binary.is_file():
            sha_value = sha256_file(binary)
    return {
        "path": str(path_value) if path_value is not None else None,
        "sha256": str(sha_value) if sha_value is not None else None,
    }


def load_repeated_campaign(
    path: Path,
    role: str,
    expected_repetitions: int,
    expected_leaf_count: int | None,
) -> dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        fail(f"{role}: expected an object with a rows array")
    if not payload.get("completed_at"):
        fail(f"{role}: campaign is incomplete (completed_at is absent)")
    declared_reps = payload.get("measured_repetitions")
    if declared_reps is not None and int(declared_reps) != expected_repetitions:
        fail(f"{role}: expected n={expected_repetitions}, got n={declared_reps}")

    declared_benchmarks = payload.get("benchmarks")
    if isinstance(declared_benchmarks, list):
        benchmarks = [str(value) for value in declared_benchmarks]
    else:
        benchmarks = sorted(
            {
                str(row.get("benchmark"))
                for row in payload["rows"]
                if isinstance(row, dict) and row.get("benchmark") is not None
            }
        )
    if not benchmarks or len(benchmarks) != len(set(benchmarks)):
        fail(f"{role}: benchmark inventory is empty or contains duplicates")
    if expected_leaf_count is not None and len(benchmarks) != expected_leaf_count:
        fail(f"{role}: expected {expected_leaf_count} leaves, got {len(benchmarks)}")

    grouped: dict[str, dict[str, dict[int, float]]] = defaultdict(
        lambda: {"baseline": {}, "candidate": {}}
    )
    for index, row in enumerate(payload["rows"]):
        if not isinstance(row, dict):
            fail(f"{role}: row {index} is not an object")
        if bool(row.get("warmup")):
            continue
        benchmark = str(row.get("benchmark", ""))
        if benchmark not in benchmarks:
            fail(f"{role}: measured row contains an undeclared leaf: {benchmark!r}")
        label = normalize_label(row.get("label"))
        try:
            rep = int(row.get("rep"))
        except (TypeError, ValueError):
            fail(f"{role}: invalid repetition id for {benchmark}")
        if rep in grouped[benchmark][label]:
            fail(f"{role}: duplicate {label} repetition {rep} for {benchmark}")
        grouped[benchmark][label][rep] = finite_positive(
            row.get("ns_per_iter"), f"{role}:{benchmark}:{label}:ns_per_iter"
        )

    leaves = []
    expected_ids = list(range(expected_repetitions))
    for benchmark in benchmarks:
        slots = grouped.get(benchmark)
        if slots is None:
            fail(f"{role}: missing measured rows for {benchmark}")
        for label in ("baseline", "candidate"):
            ids = sorted(slots[label])
            if ids != expected_ids:
                fail(
                    f"{role}: {benchmark} {label} repetitions are {ids}; "
                    f"expected {expected_ids}"
                )
        leaves.append(
            leaf_from_samples(benchmark, slots["baseline"], slots["candidate"], role)
        )

    return {
        "role": role,
        "input": {
            "path": str(path.resolve()),
            "sha256": sha256_file(path),
        },
        "protocol": {
            "started_at": payload.get("started_at"),
            "completed_at": payload.get("completed_at"),
            "cpu": payload.get("cpu"),
            "numa_node": payload.get("numa_node"),
            "measured_repetitions": expected_repetitions,
            "warmup_repetitions": payload.get("warmup_repetitions"),
            "fresh_process_per_leaf": payload.get("fresh_process_per_leaf"),
            "alternating_order": payload.get("alternating_order"),
        },
        "binaries": {
            "baseline": binary_identity(payload, "baseline"),
            "candidate": binary_identity(payload, "candidate"),
        },
        "leaf_count": len(leaves),
        "leaves": leaves,
    }


def load_full_pair(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("runs"), list):
        fail("full_468_single_pass: expected an object with a runs array")
    runs: dict[str, Mapping[str, Any]] = {}
    for run in payload["runs"]:
        if not isinstance(run, dict):
            fail("full_468_single_pass: a run is not an object")
        label = normalize_label(run.get("label"))
        if label in runs:
            fail(f"full_468_single_pass: duplicate {label} run")
        runs[label] = run
    if set(runs) != {"baseline", "candidate"}:
        fail("full_468_single_pass: both baseline and candidate runs are required")

    maps: dict[str, dict[str, float]] = {}
    for label, run in runs.items():
        if int(run.get("returncode", -1)) != 0:
            fail(f"full_468_single_pass: {label} returned {run.get('returncode')}")
        values = run.get("benchmarks_ns_per_iter")
        if not isinstance(values, dict):
            fail(f"full_468_single_pass: {label} omitted benchmarks_ns_per_iter")
        if int(run.get("leaf_count", -1)) != FULL_LEAF_COUNT or len(values) != FULL_LEAF_COUNT:
            fail(
                f"full_468_single_pass: {label} has {len(values)} leaves; "
                f"expected {FULL_LEAF_COUNT}"
            )
        maps[label] = {
            str(name): finite_positive(value, f"full:{label}:{name}")
            for name, value in values.items()
        }
    if set(maps["baseline"]) != set(maps["candidate"]):
        fail("full_468_single_pass: baseline and candidate inventories differ")

    leaves = []
    for benchmark in sorted(maps["baseline"]):
        base = maps["baseline"][benchmark]
        candidate = maps["candidate"][benchmark]
        leaves.append(
            {
                "benchmark": benchmark,
                "family": family_of(benchmark),
                "classification": classify_benchmark(benchmark),
                "baseline_ns_per_iter": round_number(base),
                "candidate_ns_per_iter": round_number(candidate),
                "candidate_to_baseline_ratio": round_number(candidate / base),
                "runtime_reduction_percent": round_number(
                    runtime_reduction_percent(base, candidate)
                ),
                "speedup_percent": round_number(speedup_percent(base, candidate)),
                "observation_count_per_binary": 1,
                "repeated_range_available": False,
            }
        )

    def run_provenance(run: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "binary": run.get("binary"),
            "binary_sha256": run.get("binary_sha256"),
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
            "wall_seconds": run.get("wall_seconds"),
            "minor_faults": run.get("minor_faults"),
            "major_faults": run.get("major_faults"),
            "max_rss_kib": run.get("max_rss_kib"),
        }

    return {
        "role": "full_468_single_pass",
        "input": {"path": str(path.resolve()), "sha256": sha256_file(path)},
        "purpose": payload.get("purpose"),
        "protocol": {
            "cpu": payload.get("cpu"),
            "numa_node": payload.get("numa_node"),
            "observations_per_binary_and_leaf": 1,
        },
        "runs": {
            "baseline": run_provenance(runs["baseline"]),
            "candidate": run_provenance(runs["candidate"]),
        },
        "leaf_count": len(leaves),
        "caveat": FULL_CAVEAT,
        "leaves": leaves,
    }


def identity_sha(campaign: Mapping[str, Any], role: str) -> str | None:
    if "binaries" in campaign:
        value = campaign["binaries"][role].get("sha256")
    else:
        value = campaign["runs"][role].get("binary_sha256")
    return str(value) if value else None


def validate_shared_binary_identity(campaigns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for role in ("baseline", "candidate"):
        by_campaign = {
            str(campaign["role"]): identity_sha(campaign, role) for campaign in campaigns
        }
        known = {value for value in by_campaign.values() if value}
        if len(known) != 1:
            fail(f"{role} binary SHA-256 differs or is missing across campaigns: {by_campaign}")
        result[role] = {"sha256": next(iter(known)), "by_campaign": by_campaign}
    return result


def extremum_entry(leaves: Sequence[Mapping[str, Any]], key: str, best: bool) -> dict[str, Any]:
    leaf = (max if best else min)(leaves, key=lambda item: float(item[key]))
    return {"benchmark": leaf["benchmark"], key: leaf[key]}


def aggregate_leaves(
    leaves: Sequence[Mapping[str, Any]], metric_key: str
) -> dict[str, Any]:
    if not leaves:
        return {
            "leaf_count": 0,
            "median_percent": None,
            "geometric_mean_percent": None,
            "min_percent": None,
            "max_percent": None,
            "faster_over_2_percent": 0,
            "within_2_percent": 0,
            "slower_over_2_percent": 0,
        }
    values = [float(leaf[metric_key]) for leaf in leaves]
    ratios = []
    for value in values:
        ratio = 1.0 + value / 100.0
        if ratio > 0:
            ratios.append(ratio)
    geometric = math.exp(sum(math.log(value) for value in ratios) / len(ratios)) - 1.0
    return {
        "leaf_count": len(leaves),
        "median_percent": round_number(median(values)),
        "geometric_mean_percent": round_number(100.0 * geometric),
        "min_percent": round_number(min(values)),
        "max_percent": round_number(max(values)),
        "p10_percent": round_number(percentile(values, 0.10)),
        "p90_percent": round_number(percentile(values, 0.90)),
        "faster_over_2_percent": sum(value > 2.0 for value in values),
        "within_2_percent": sum(abs(value) <= 2.0 for value in values),
        "slower_over_2_percent": sum(value < -2.0 for value in values),
        "best_leaf": extremum_entry(leaves, metric_key, True),
        "worst_leaf": extremum_entry(leaves, metric_key, False),
    }


def grouped_aggregates(
    leaves: Sequence[Mapping[str, Any]], metric_key: str, group_key: str
) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for leaf in leaves:
        if group_key == "classification":
            name = str(leaf["classification"]["kind"])
        else:
            name = str(leaf[group_key])
        groups[name].append(leaf)
    return [
        {group_key: name, **aggregate_leaves(groups[name], metric_key)}
        for name in sorted(groups)
    ]


def rank_leaves(
    leaves: Sequence[Mapping[str, Any]], metric_key: str, count: int = 10
) -> dict[str, Any]:
    def concise(leaf: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "benchmark": leaf["benchmark"],
            "family": leaf["family"],
            "classification": leaf["classification"]["kind"],
            metric_key: leaf[metric_key],
        }

    ordered = sorted(leaves, key=lambda item: (float(item[metric_key]), item["benchmark"]))
    return {
        "worst": [concise(leaf) for leaf in ordered[:count]],
        "best": [concise(leaf) for leaf in reversed(ordered[-count:])],
    }


def build_report(
    targeted: Mapping[str, Any],
    stratified: Mapping[str, Any],
    full: Mapping[str, Any],
    confirmatory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    repeated_campaigns = [stratified, targeted]
    if confirmatory is not None:
        repeated_campaigns.append(confirmatory)
    campaigns = [*repeated_campaigns, full]
    identities = validate_shared_binary_identity(campaigns)

    selected: dict[str, dict[str, Any]] = {}
    evidence_sources: dict[str, list[str]] = defaultdict(list)
    # Higher-repetition targeted measurements take precedence for overlaps.
    for campaign in repeated_campaigns:
        for leaf in campaign["leaves"]:
            evidence_sources[leaf["benchmark"]].append(campaign["role"])
            selected[leaf["benchmark"]] = dict(leaf)
    repeated = []
    for benchmark in sorted(selected):
        leaf = selected[benchmark]
        leaf["available_repeated_sources"] = sorted(
            evidence_sources[benchmark],
            key=lambda value: (
                {"confirmatory_n5": 0, "targeted_n5": 1, "stratified_n4": 2}.get(
                    value, 3
                ),
                value,
            ),
        )
        repeated.append(leaf)

    full_leaves = list(full["leaves"])
    allocator_path_evidence = [
        leaf
        for leaf in repeated
        if leaf["classification"]["kind"] == "allocation-sensitive"
        and not leaf["classification"].get("layout_confounded", False)
    ]
    layout_confounded_evidence = [
        leaf
        for leaf in repeated
        if leaf["classification"].get("layout_confounded", False)
    ]
    robust_full_leaves = [
        leaf
        for leaf in full_leaves
        if float(leaf["baseline_ns_per_iter"]) >= ROBUST_FLOOR_NS_PER_ITER
        and float(leaf["candidate_ns_per_iter"]) >= ROBUST_FLOOR_NS_PER_ITER
    ]
    repeated_metric = "median_speedup_percent"
    full_metric = "speedup_percent"
    return {
        "schema_version": SCHEMA_VERSION,
        "title": "UniAlloc raw-path ROI benchmark report",
        "metric_contract": {
            "primary": "speedup_percent",
            "formula": "100 * (baseline_ns_per_iter / candidate_ns_per_iter - 1)",
            "direction": "positive means the candidate is faster",
            "paired_range": "min and max of same-repetition A/B speedup percentages",
            "secondary_runtime_reduction_formula": (
                "100 * (baseline_ns_per_iter - candidate_ns_per_iter) / baseline_ns_per_iter"
            ),
        },
        "caveats": {
            "full_468_single_pass": FULL_CAVEAT,
            "classification": CLASSIFICATION_CAVEAT,
            "layout_sensitivity": (
                "Allocator-free controls can move when linked code layout changes; "
                "use repeated controls to distinguish allocator ROI from layout effects."
            ),
        },
        "provenance": {
            "inputs": [campaign["input"] | {"role": campaign["role"]} for campaign in campaigns],
            "binary_identities": identities,
            "selection_rule": (
                "confirmatory n5 supersedes targeted n5, which supersedes stratified n4, "
                "for overlapping repeated leaves; "
                "samples from different campaigns are never pooled"
            ),
            "classification_version": "source-audit-v1-with-conservative-full-heuristic",
        },
        "campaigns": {
            "targeted_n5": {key: value for key, value in targeted.items() if key != "leaves"},
            "stratified_n4": {key: value for key, value in stratified.items() if key != "leaves"},
            **(
                {
                    "confirmatory_n5": {
                        key: value
                        for key, value in confirmatory.items()
                        if key != "leaves"
                    }
                }
                if confirmatory is not None
                else {}
            ),
            "full_468_single_pass": {
                key: value for key, value in full.items() if key != "leaves"
            },
        },
        "repeated_evidence": {
            "leaf_count": len(repeated),
            "aggregate": aggregate_leaves(repeated, repeated_metric),
            "allocator_path_evidence": {
                "selection": (
                    "source-audited allocation-sensitive timed closures without known "
                    "linked-placement confounding"
                ),
                "aggregate": aggregate_leaves(
                    allocator_path_evidence, repeated_metric
                ),
                "ranked_leaves": rank_leaves(
                    allocator_path_evidence, repeated_metric
                ),
                "leaves": allocator_path_evidence,
            },
            "layout_confounded_evidence": {
                "selection": (
                    "source/disassembly-audited leaves with linked-placement-sensitive "
                    "non-allocator hot loops or shared machine functions"
                ),
                "aggregate": aggregate_leaves(
                    layout_confounded_evidence, repeated_metric
                ),
                "ranked_leaves": rank_leaves(
                    layout_confounded_evidence, repeated_metric
                ),
                "leaves": layout_confounded_evidence,
            },
            "by_classification": grouped_aggregates(
                repeated, repeated_metric, "classification"
            ),
            "by_family": grouped_aggregates(repeated, repeated_metric, "family"),
            "ranked_leaves": rank_leaves(repeated, repeated_metric),
            "leaves": repeated,
        },
        "full_468_single_pass": {
            "leaf_count": len(full_leaves),
            "observation_count_per_binary_and_leaf": 1,
            "repeated_range_available": False,
            "caveat": FULL_CAVEAT,
            "aggregate": aggregate_leaves(full_leaves, full_metric),
            "by_classification": grouped_aggregates(
                full_leaves, full_metric, "classification"
            ),
            "by_family": grouped_aggregates(full_leaves, full_metric, "family"),
            "ranked_leaves": rank_leaves(full_leaves, full_metric),
            "robust_floor_ns_per_iter": ROBUST_FLOOR_NS_PER_ITER,
            "robust": {
                "selection": (
                    "baseline and candidate observations are both at least "
                    f"{ROBUST_FLOOR_NS_PER_ITER:g} ns/iter"
                ),
                "aggregate": aggregate_leaves(robust_full_leaves, full_metric),
                "by_classification": grouped_aggregates(
                    robust_full_leaves, full_metric, "classification"
                ),
                "by_family": grouped_aggregates(
                    robust_full_leaves, full_metric, "family"
                ),
                "ranked_leaves": rank_leaves(robust_full_leaves, full_metric),
                "leaves": robust_full_leaves,
            },
            "leaves": full_leaves,
        },
    }


def fmt_percent(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):+.{digits}f}%"


def xml_text(value: Any) -> str:
    return html.escape(str(value), quote=True)


def shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def heat_color(value: float, limit: float = 50.0) -> str:
    clipped = max(-limit, min(limit, value)) / limit
    neutral = (235, 238, 240)
    target = (31, 130, 82) if clipped >= 0 else (190, 55, 55)
    weight = abs(clipped) ** 0.65
    rgb = tuple(round(neutral[i] * (1 - weight) + target[i] * weight) for i in range(3))
    return "#%02x%02x%02x" % rgb


def svg_text(
    parts: list[str], x: float, y: float, text: str, *, size: int = 14,
    weight: int = 400, fill: str = "#17212b", anchor: str = "start",
) -> None:
    parts.append(
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}">{xml_text(text)}</text>'
    )


def svg_rect(
    parts: list[str], x: float, y: float, width: float, height: float,
    *, fill: str, stroke: str = "none", radius: float = 0,
) -> None:
    parts.append(
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" '
        f'rx="{radius:.1f}" fill="{fill}" stroke="{stroke}"/>'
    )


def metric_x(value: float, center: float, half_width: float, limit: float = 50.0) -> float:
    return center + half_width * max(-limit, min(limit, value)) / limit


def render_svg(report: Mapping[str, Any]) -> str:
    width = 1480
    repeated = list(report["repeated_evidence"]["leaves"])
    full = list(report["full_468_single_pass"]["leaves"])
    family_rows = list(report["full_468_single_pass"]["robust"]["by_family"])
    repeated_row_height = 24
    repeated_height = sum(
        34 + repeated_row_height * sum(
            leaf["classification"]["kind"] == kind for leaf in repeated
        )
        for kind in ("allocation-sensitive", "layout/control")
    )
    family_height = 34 + 27 * len(family_rows)
    heat_rows = math.ceil(len(full) / 26)
    full_height = max(family_height + 80, 500)
    height = 390 + repeated_height + full_height + 500
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="UniAlloc raw-path ROI benchmark report">',
        "<title>UniAlloc raw-path ROI benchmark report</title>",
        "<desc>Repeated A/B medians and ranges plus a single-observation 468-leaf smoke heatmap.</desc>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<style>text{font-family:Inter,Segoe UI,Arial,sans-serif}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}</style>',
    ]

    svg_text(parts, 60, 58, "UniAlloc raw-path ROI", size=30, weight=750)
    svg_text(
        parts, 60, 87,
        "Repeated A/B medians with paired min–max ranges; positive values mean faster",
        size=15, fill="#52606d",
    )

    repeated_agg = report["repeated_evidence"]["aggregate"]
    allocator_path_agg = report["repeated_evidence"]["allocator_path_evidence"]["aggregate"]
    full_agg = report["full_468_single_pass"]["aggregate"]
    robust_full_agg = report["full_468_single_pass"]["robust"]["aggregate"]
    classification_map = {
        row["classification"]: row
        for row in report["repeated_evidence"]["by_classification"]
    }
    cards = [
        (
            "Repeated leaves",
            str(report["repeated_evidence"]["leaf_count"]),
            f"median {fmt_percent(repeated_agg['median_percent'])}",
        ),
        (
            "Unconfounded allocator path",
            fmt_percent(allocator_path_agg.get("median_percent")),
            (
                f"n={allocator_path_agg.get('leaf_count', 0)} · range "
                f"{fmt_percent(allocator_path_agg.get('min_percent'), 0)}…"
                f"{fmt_percent(allocator_path_agg.get('max_percent'), 0)}"
            ),
        ),
        (
            "Layout / control",
            fmt_percent(classification_map.get("layout/control", {}).get("median_percent")),
            f"n={classification_map.get('layout/control', {}).get('leaf_count', 0)}",
        ),
        (
            "Full floor-filtered (>=100 ns)",
            fmt_percent(robust_full_agg["median_percent"]),
            (
                f"n={robust_full_agg['leaf_count']} · raw "
                f"{fmt_percent(full_agg['min_percent'], 0)}…"
                f"{fmt_percent(full_agg['max_percent'], 0)}"
            ),
        ),
    ]
    card_y = 112
    card_w = 325
    for index, (label, value, detail) in enumerate(cards):
        x = 60 + index * 350
        svg_rect(parts, x, card_y, card_w, 105, fill="#f5f7f9", stroke="#d9e0e6", radius=8)
        svg_text(parts, x + 18, card_y + 28, label, size=13, weight=650, fill="#52606d")
        svg_text(parts, x + 18, card_y + 63, value, size=25, weight=750)
        svg_text(parts, x + 18, card_y + 88, detail, size=12, fill="#52606d")

    caveat_y = 235
    svg_rect(parts, 60, caveat_y, 1360, 82, fill="#fff5d6", stroke="#e1b84b", radius=7)
    svg_text(parts, 78, caveat_y + 27, "PROMINENT LIMIT", size=13, weight=750, fill="#805b00")
    svg_text(parts, 78, caveat_y + 50, FULL_CAVEAT, size=14, weight=600, fill="#684b00")
    svg_text(parts, 78, caveat_y + 70, "Rows marked [layout] have source/disassembly evidence of linked-placement sensitivity.", size=13, fill="#684b00")

    y = 365
    svg_text(parts, 60, y, "Repeated evidence", size=23, weight=750)
    svg_text(parts, 1405, y, "scale clipped at ±50%", size=12, fill="#6b7785", anchor="end")
    y += 25
    label_x = 72
    bar_left = 610
    bar_width = 500
    center = bar_left + bar_width / 2
    value_x = 1130
    source_x = 1365
    svg_text(parts, bar_left, y, "slower", size=11, fill="#9b3434")
    svg_text(parts, center, y, "0", size=11, fill="#6b7785", anchor="middle")
    svg_text(parts, bar_left + bar_width, y, "faster", size=11, fill="#18794e", anchor="end")
    y += 12

    for kind, heading in (
        ("allocation-sensitive", "Allocation-sensitive timed closures"),
        ("layout/control", "Layout / control timed closures"),
    ):
        rows = [leaf for leaf in repeated if leaf["classification"]["kind"] == kind]
        rows.sort(key=lambda leaf: (float(leaf["median_speedup_percent"]), leaf["benchmark"]))
        aggregate = classification_map.get(kind, {})
        y += 22
        svg_rect(parts, 60, y - 18, 1360, 28, fill="#eaf0f4", radius=4)
        svg_text(parts, 72, y + 2, heading, size=14, weight=750)
        svg_text(
            parts, 1405, y + 2,
            f"n={len(rows)} · median {fmt_percent(aggregate.get('median_percent'))}",
            size=12, fill="#52606d", anchor="end",
        )
        y += 18
        for row_index, leaf in enumerate(rows):
            row_y = y + row_index * repeated_row_height
            if row_index % 2:
                svg_rect(parts, 60, row_y - 14, 1360, repeated_row_height, fill="#fafbfc")
            benchmark = str(leaf["benchmark"])
            layout_confounded = bool(leaf["classification"].get("layout_confounded"))
            display_name = f"{benchmark} [layout]" if layout_confounded else benchmark
            display = shorten(display_name, 64)
            tooltip = (
                f"{benchmark}: linked-placement-sensitive"
                if layout_confounded
                else benchmark
            )
            parts.append(f"<g><title>{xml_text(tooltip)}</title>")
            svg_text(parts, label_x, row_y + 3, display, size=11)
            svg_rect(parts, bar_left, row_y - 7, bar_width, 8, fill="#edf0f2", radius=3)
            parts.append(
                f'<line x1="{center:.1f}" y1="{row_y-9:.1f}" x2="{center:.1f}" '
                f'y2="{row_y+3:.1f}" stroke="#8b96a0" stroke-width="1"/>'
            )
            speed = float(leaf["median_speedup_percent"])
            range_data = leaf["paired_speedup_percent"]
            low = float(range_data["min"])
            high = float(range_data["max"])
            x_low = metric_x(low, center, bar_width / 2)
            x_high = metric_x(high, center, bar_width / 2)
            parts.append(
                f'<line x1="{x_low:.1f}" y1="{row_y-3:.1f}" x2="{x_high:.1f}" '
                f'y2="{row_y-3:.1f}" stroke="#263746" stroke-width="1.4"/>'
            )
            x_value = metric_x(speed, center, bar_width / 2)
            x0, x1 = sorted((center, x_value))
            svg_rect(
                parts, x0, row_y - 6, max(1.5, x1 - x0), 6,
                fill=heat_color(speed), radius=2,
            )
            parts.append(
                f'<circle cx="{x_value:.1f}" cy="{row_y-3:.1f}" r="3.2" '
                f'fill="{heat_color(speed)}" stroke="#ffffff" stroke-width="1"/>'
            )
            svg_text(
                parts, value_x, row_y + 3,
                f"{fmt_percent(speed)}  [{fmt_percent(low)}, {fmt_percent(high)}]",
                size=11, weight=600,
            )
            source = {
                "confirmatory_n5": "confirmatory n5",
                "targeted_n5": "targeted n5",
                "stratified_n4": "stratified n4",
            }.get(str(leaf["source"]), str(leaf["source"]))
            svg_text(parts, source_x, row_y + 3, source, size=10, fill="#6b7785", anchor="end")
            parts.append("</g>")
        y += repeated_row_height * len(rows)

    y += 40
    svg_text(parts, 60, y, "Full 468-leaf single-observation smoke", size=23, weight=750)
    y += 30
    full_start_y = y
    svg_text(parts, 60, y, "Floor-filtered family median speedup (both >=100 ns/iter)", size=15, weight=700)
    y += 22
    family_bar_left = 245
    family_bar_width = 420
    family_center = family_bar_left + family_bar_width / 2
    for index, row in enumerate(family_rows):
        row_y = y + index * 27
        svg_text(parts, 72, row_y + 4, str(row["family"]), size=12)
        svg_text(parts, 225, row_y + 4, f"n={row['leaf_count']}", size=9, fill="#6b7785", anchor="end")
        svg_rect(parts, family_bar_left, row_y - 6, family_bar_width, 9, fill="#edf0f2", radius=3)
        parts.append(
            f'<line x1="{family_center:.1f}" y1="{row_y-8:.1f}" x2="{family_center:.1f}" '
            f'y2="{row_y+5:.1f}" stroke="#8b96a0"/>'
        )
        value = float(row["median_percent"])
        family_low = float(row["min_percent"])
        family_high = float(row["max_percent"])
        low_x = metric_x(family_low, family_center, family_bar_width / 2)
        high_x = metric_x(family_high, family_center, family_bar_width / 2)
        parts.append(
            f'<line x1="{low_x:.1f}" y1="{row_y-2:.1f}" x2="{high_x:.1f}" '
            f'y2="{row_y-2:.1f}" stroke="#263746" stroke-width="1.2"/>'
        )
        value_x_pos = metric_x(value, family_center, family_bar_width / 2)
        x0, x1 = sorted((family_center, value_x_pos))
        svg_rect(parts, x0, row_y - 5, max(1.5, x1 - x0), 7, fill=heat_color(value), radius=2)
        svg_text(
            parts, 675, row_y + 4,
            f"{fmt_percent(value)} [{fmt_percent(family_low)}, {fmt_percent(family_high)}]",
            size=10, weight=650,
        )

    heat_x = 815
    heat_y = full_start_y + 24
    svg_text(parts, heat_x, full_start_y, "468-leaf heatmap (alphabetical; one observation each)", size=15, weight=700)
    cell_w = 22
    cell_h = 17
    gap = 2
    for index, leaf in enumerate(full):
        col = index % 26
        row = index // 26
        x = heat_x + col * (cell_w + gap)
        cell_y = heat_y + row * (cell_h + gap)
        value = float(leaf["speedup_percent"])
        parts.append(
            f'<rect x="{x:.1f}" y="{cell_y:.1f}" width="{cell_w}" height="{cell_h}" '
            f'rx="2" fill="{heat_color(value)}"><title>{xml_text(leaf["benchmark"])}: '
            f'{xml_text(fmt_percent(value, 2))}</title></rect>'
        )
    legend_y = heat_y + heat_rows * (cell_h + gap) + 17
    for index, value in enumerate((-50, -25, 0, 25, 50)):
        x = heat_x + index * 75
        svg_rect(parts, x, legend_y, 18, 12, fill=heat_color(float(value)), radius=2)
        svg_text(parts, x + 24, legend_y + 11, fmt_percent(value, 0), size=10, fill="#52606d")

    rank_y = max(
        y + 27 * len(family_rows) + 40,
        legend_y + 60,
    )
    svg_text(parts, 60, rank_y, "Raw single-pass extremes (diagnostic only)", size=18, weight=750)
    rank_y += 25
    worst = report["full_468_single_pass"]["ranked_leaves"]["worst"][:6]
    best = report["full_468_single_pass"]["ranked_leaves"]["best"][:6]
    svg_text(parts, 72, rank_y, "Worst", size=13, weight=700, fill="#9b3434")
    svg_text(parts, 755, rank_y, "Best", size=13, weight=700, fill="#18794e")
    rank_y += 21
    for index in range(max(len(worst), len(best))):
        row_y = rank_y + index * 24
        if index < len(worst):
            item = worst[index]
            svg_text(parts, 72, row_y, shorten(item["benchmark"], 68), size=11)
            svg_text(parts, 700, row_y, fmt_percent(item["speedup_percent"]), size=11, weight=700, anchor="end")
        if index < len(best):
            item = best[index]
            svg_text(parts, 755, row_y, shorten(item["benchmark"], 68), size=11)
            svg_text(parts, 1385, row_y, fmt_percent(item["speedup_percent"]), size=11, weight=700, anchor="end")

    footer_y = rank_y + max(len(worst), len(best)) * 24 + 38
    svg_rect(parts, 60, footer_y - 20, 1360, 112, fill="#f5f7f9", stroke="#d9e0e6", radius=6)
    svg_text(parts, 75, footer_y + 2, "Provenance", size=13, weight=750)
    binary_ids = report["provenance"]["binary_identities"]
    svg_text(parts, 75, footer_y + 27, f"baseline SHA-256  {binary_ids['baseline']['sha256']}", size=10, fill="#52606d")
    svg_text(parts, 75, footer_y + 48, f"candidate SHA-256 {binary_ids['candidate']['sha256']}", size=10, fill="#52606d")
    input_items = [
        f"{item['role']} {item['sha256'][:12]}…"
        for item in report["provenance"]["inputs"]
    ]
    rebased_validation = report["provenance"].get("rebased_source_validation")
    if rebased_validation is not None:
        input_items.append(
            f"rebased_validation {rebased_validation['input']['sha256'][:12]}…"
        )
    input_text = " · ".join(input_items)
    svg_text(parts, 75, footer_y + 70, f"input hashes: {input_text}", size=10, fill="#52606d")
    svg_text(parts, 75, footer_y + 90, CLASSIFICATION_CAVEAT, size=10, fill="#52606d")
    parts.append("</svg>\n")
    return "\n".join(parts)


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--targeted",
        type=Path,
        default=Path("/tmp/unialloc-raw-final-targeted-ab-n5.json"),
    )
    parser.add_argument(
        "--stratified",
        type=Path,
        default=Path("/tmp/unialloc-raw-final-stratified-ab-n4.json"),
    )
    parser.add_argument(
        "--full-pair",
        type=Path,
        default=Path("/tmp/unialloc-raw-final-full/pair.json"),
    )
    parser.add_argument(
        "--confirmatory",
        type=Path,
        help="optional fresh-process n=5 panel for full-sweep extremes",
    )
    parser.add_argument(
        "--rebased-validation",
        type=Path,
        help="optional machine-readable validation of the final rebased binaries",
    )
    parser.add_argument(
        "--report-json",
        type=Path,
        default=Path("/tmp/unialloc-raw-roi-report.json"),
    )
    parser.add_argument(
        "--report-svg",
        type=Path,
        default=Path("/tmp/unialloc-raw-roi-report.svg"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    targeted = load_repeated_campaign(args.targeted, "targeted_n5", 5, None)
    stratified = load_repeated_campaign(
        args.stratified, "stratified_n4", 4, STRATIFIED_LEAF_COUNT
    )
    full = load_full_pair(args.full_pair)
    confirmatory = (
        load_repeated_campaign(args.confirmatory, "confirmatory_n5", 5, None)
        if args.confirmatory is not None
        else None
    )
    report = build_report(targeted, stratified, full, confirmatory)
    if args.rebased_validation is not None:
        validation = read_json(args.rebased_validation)
        if not isinstance(validation, dict):
            fail("rebased validation must be a JSON object")
        report["provenance"]["rebased_source_validation"] = {
            "input": {
                "path": str(args.rebased_validation.resolve()),
                "sha256": sha256_file(args.rebased_validation),
            },
            "evidence": validation,
        }
    report_bytes = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    svg_bytes = render_svg(report).encode("utf-8")
    atomic_write(args.report_json, report_bytes)
    atomic_write(args.report_svg, svg_bytes)
    print(
        json.dumps(
            {
                "report_json": str(args.report_json.resolve()),
                "report_json_sha256": hashlib.sha256(report_bytes).hexdigest(),
                "report_svg": str(args.report_svg.resolve()),
                "report_svg_sha256": hashlib.sha256(svg_bytes).hexdigest(),
                "repeated_leaf_count": report["repeated_evidence"]["leaf_count"],
                "full_leaf_count": report["full_468_single_pass"]["leaf_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        raise SystemExit(f"error: {error}")
