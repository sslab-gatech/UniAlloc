#!/usr/bin/env python3
"""Join compiler ownership features with independent per-site lifetime truth.

The runtime grain is the exact five-field allocator site key
``(callsite, type_id, module_id, requested_size, align)``. Compiler ownership
features are joined by the stable three-field semantic identity and remain
attached to every exact runtime size/alignment row. A compiler-declared exact
layout is checked against the runtime row; dynamic layouts remain explicit
layout abstentions.

The analysis keeps completed Short/Long outcomes, Long classifications made
while an allocation is still live, completed indeterminate outcomes, remaining
right-censored objects, and bypassed/unobserved allocations in separate
denominators. A live-survival observation remains cumulative provenance after
deallocation, so screening counts the disjoint sum ``completed long_outcomes +
current_live_survival_inflight``. Allocation counts, requested bytes, and
allocation-pressure span are allocation-hotness proxies. Access hotness is
unavailable.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Sequence


RUNTIME_SNAPSHOT_ABI_VERSION = 2
RUNTIME_SNAPSHOT_SOURCE = "unialloc-lifetime-adaptive-site-snapshot-v2"
LIVE_SURVIVAL_MINIMUM_AGE_BYTES = 8 * 1024 * 1024
PROVEN_SCOPED_HINT = 0xA101
BOUNDED_PROCESS_LONG_ORACLE_HINT = 0xA102
RUNTIME_PREFIX = "UNIALLOC_RUNTIME_LIFETIME_SITE_SNAPSHOT="
RUNTIME_STATS_PREFIX = "UNIALLOC_RUNTIME_LIFETIME_STATS="

RUNTIME_SUM_FIELDS = (
    "allocation_count",
    "allocation_requested_bytes",
    "allocation_payload_bytes",
    "tracked_allocations",
    "bypassed_allocations",
    "completed_outcomes",
    "short_outcomes",
    "long_outcomes",
    "censored_outcomes",
    "short_requested_bytes",
    "long_requested_bytes",
    "censored_requested_bytes",
    "total_completed_age_bytes",
    "tracked_inflight",
    "inflight_age_lower_bound_bytes",
    "live_survival_observations",
    "live_survival_requested_bytes",
    "live_survival_payload_bytes",
    "current_live_survival_inflight",
    "current_live_survival_inflight_requested_bytes",
    "current_live_survival_inflight_payload_bytes",
)

RUNTIME_LIVE_SURVIVAL_AGE_FIELDS = (
    "minimum_live_survival_age_bytes",
    "maximum_live_survival_age_bytes",
)

FEATURE_BOOLEAN_FIELDS = (
    "return_sink",
    "escape_sink",
    "store_sink",
    "allocation_in_natural_loop",
    "reachable_backedge_after_allocation",
    "function_has_yield_or_await",
    "reachable_yield_or_await",
    "receiver_owned_allocation",
    "exact_drop_path",
    "conditional_drop_path",
    "cleanup_drop_path",
)

FEATURE_COUNT_FIELDS = (
    "owner_move_count",
    "normal_successor_count",
    "cleanup_successor_count",
)

FEATURE_LIST_FIELDS = (
    "normal_drop_blocks",
    "cleanup_drop_blocks",
    "return_sink_blocks",
    "escape_sink_blocks",
    "store_sink_blocks",
    "reachable_normal_blocks",
    "reachable_cleanup_blocks",
)


class GroundTruthError(RuntimeError):
    """Raised when source evidence cannot satisfy the join contract."""


@dataclass(frozen=True, order=True)
class StaticSiteKey:
    callsite: int
    type_id: int
    module_id: int

    def as_dict(self) -> dict[str, int]:
        return {
            "callsite": self.callsite,
            "type_id": self.type_id,
            "module_id": self.module_id,
        }


@dataclass(frozen=True, order=True)
class RuntimeSiteKey:
    callsite: int
    type_id: int
    module_id: int
    requested_size: int
    align: int

    @property
    def static(self) -> StaticSiteKey:
        return StaticSiteKey(self.callsite, self.type_id, self.module_id)

    def as_dict(self) -> dict[str, int]:
        return {
            **self.static.as_dict(),
            "requested_size": self.requested_size,
            "align": self.align,
        }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def nonnegative_int(row: dict[str, Any], field: str) -> int:
    value = row.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GroundTruthError(f"runtime field {field} must be a nonnegative integer")
    return value


def positive_key_int(row: dict[str, Any], field: str) -> int:
    value = nonnegative_int(row, field)
    if value == 0:
        raise GroundTruthError(f"runtime key field {field} must be nonzero")
    return value


def validate_runtime_row(raw: dict[str, Any]) -> dict[str, Any]:
    key = RuntimeSiteKey(
        positive_key_int(raw, "callsite"),
        positive_key_int(raw, "type_id"),
        nonnegative_int(raw, "module_id"),
        positive_key_int(raw, "requested_size"),
        positive_key_int(raw, "align"),
    )
    if key.align & (key.align - 1):
        raise GroundTruthError(f"runtime alignment is not a power of two: {key.align}")

    minimum_payload_capacity = positive_key_int(raw, "minimum_payload_capacity")
    maximum_payload_capacity = positive_key_int(raw, "maximum_payload_capacity")
    if minimum_payload_capacity < key.requested_size:
        raise GroundTruthError("minimum payload capacity is smaller than requested size")
    if maximum_payload_capacity < minimum_payload_capacity:
        raise GroundTruthError("runtime payload-capacity interval is reversed")
    values = {field: nonnegative_int(raw, field) for field in RUNTIME_SUM_FIELDS}
    minimum_live_survival_age = nonnegative_int(
        raw, "minimum_live_survival_age_bytes"
    )
    maximum_live_survival_age = nonnegative_int(
        raw, "maximum_live_survival_age_bytes"
    )
    maximum_age = nonnegative_int(raw, "maximum_completed_age_bytes")
    first_pressure = nonnegative_int(raw, "first_allocation_pressure")
    last_pressure = nonnegative_int(raw, "last_allocation_pressure")
    latest_prediction = nonnegative_int(raw, "latest_prediction")
    latest_static_prior = nonnegative_int(raw, "latest_static_prior")
    predictor_flags_seen = nonnegative_int(raw, "predictor_flags_seen")
    predictor_placement_hint_bits_union = nonnegative_int(
        raw, "predictor_placement_hint_bits_union"
    )
    predictor_key_ambiguous = raw.get("predictor_key_ambiguous")
    if not isinstance(predictor_key_ambiguous, bool):
        raise GroundTruthError("runtime field predictor_key_ambiguous must be boolean")
    if latest_prediction not in {0, 1, 2} or latest_static_prior not in {0, 1, 2}:
        raise GroundTruthError(
            "runtime latest_prediction/latest_static_prior must be in 0..=2"
        )

    allocation_count = values["allocation_count"]
    tracked = values["tracked_allocations"]
    bypassed = values["bypassed_allocations"]
    completed = values["completed_outcomes"]
    inflight = values["tracked_inflight"]
    if allocation_count != tracked + bypassed:
        raise GroundTruthError("runtime tracked+bypassed allocation accounting does not close")
    if tracked != completed + inflight:
        raise GroundTruthError("runtime completed+inflight tracked accounting does not close")
    if completed != (
        values["short_outcomes"]
        + values["long_outcomes"]
        + values["censored_outcomes"]
    ):
        raise GroundTruthError("runtime completed outcome accounting does not close")
    exact_products = {
        "allocation_requested_bytes": allocation_count * key.requested_size,
        "short_requested_bytes": values["short_outcomes"] * key.requested_size,
        "long_requested_bytes": values["long_outcomes"] * key.requested_size,
        "censored_requested_bytes": values["censored_outcomes"] * key.requested_size,
    }
    for field, expected in exact_products.items():
        if values[field] != expected:
            raise GroundTruthError(
                f"runtime byte accounting mismatch for {field}: "
                f"expected {expected}, got {values[field]}"
            )
    minimum_payload_bytes = allocation_count * minimum_payload_capacity
    maximum_payload_bytes = allocation_count * maximum_payload_capacity
    if not minimum_payload_bytes <= values["allocation_payload_bytes"] <= maximum_payload_bytes:
        raise GroundTruthError(
            "runtime allocation payload bytes fall outside the exported capacity interval"
        )
    if maximum_age > values["total_completed_age_bytes"]:
        raise GroundTruthError("maximum completed age exceeds cumulative completed age")
    if allocation_count and first_pressure > last_pressure:
        raise GroundTruthError("runtime allocation pressure interval is reversed")
    if not inflight and values["inflight_age_lower_bound_bytes"]:
        raise GroundTruthError("runtime inflight age exists without inflight objects")

    live_survival = values["live_survival_observations"]
    current_live_survival = values["current_live_survival_inflight"]
    if live_survival > tracked:
        raise GroundTruthError("runtime live-survival observations exceed tracked objects")
    if current_live_survival > live_survival or current_live_survival > inflight:
        raise GroundTruthError("runtime current live-survival accounting is invalid")
    if live_survival > values["long_outcomes"] + current_live_survival:
        raise GroundTruthError(
            "runtime cumulative live-survival provenance exceeds known Long objects"
        )
    if values["live_survival_requested_bytes"] != live_survival * key.requested_size:
        raise GroundTruthError("runtime live-survival requested-byte accounting does not close")
    if (
        values["current_live_survival_inflight_requested_bytes"]
        != current_live_survival * key.requested_size
    ):
        raise GroundTruthError(
            "runtime current live-survival requested-byte accounting does not close"
        )
    live_survival_payload = values["live_survival_payload_bytes"]
    if not (
        live_survival * minimum_payload_capacity
        <= live_survival_payload
        <= live_survival * maximum_payload_capacity
    ):
        raise GroundTruthError("runtime live-survival payload bytes are invalid")
    current_live_survival_payload = values[
        "current_live_survival_inflight_payload_bytes"
    ]
    if not (
        current_live_survival * minimum_payload_capacity
        <= current_live_survival_payload
        <= current_live_survival * maximum_payload_capacity
    ):
        raise GroundTruthError("runtime current live-survival payload bytes are invalid")
    if current_live_survival_payload > live_survival_payload:
        raise GroundTruthError(
            "runtime current live-survival payload exceeds cumulative provenance"
        )
    if live_survival == 0:
        if minimum_live_survival_age or maximum_live_survival_age:
            raise GroundTruthError("runtime empty live-survival age interval is nonzero")
    elif not (
        LIVE_SURVIVAL_MINIMUM_AGE_BYTES
        <= minimum_live_survival_age
        <= maximum_live_survival_age
    ):
        raise GroundTruthError("runtime live-survival age interval violates the ABI")
    if (
        values["inflight_age_lower_bound_bytes"]
        < current_live_survival * LIVE_SURVIVAL_MINIMUM_AGE_BYTES
    ):
        raise GroundTruthError(
            "runtime inflight age lower bound omits a confirmed live survivor"
        )

    return {
        "key": key,
        "minimum_payload_capacity": minimum_payload_capacity,
        "maximum_payload_capacity": maximum_payload_capacity,
        **values,
        "minimum_live_survival_age_bytes": minimum_live_survival_age,
        "maximum_live_survival_age_bytes": maximum_live_survival_age,
        "maximum_completed_age_bytes": maximum_age,
        "pressure_span_bytes": max(0, last_pressure - first_pressure),
        "latest_prediction": latest_prediction,
        "latest_static_prior": latest_static_prior,
        "predictor_key_ambiguous": predictor_key_ambiguous,
        "predictor_flags_seen": predictor_flags_seen,
        "predictor_placement_hint_bits_union": predictor_placement_hint_bits_union,
    }


def new_runtime_aggregate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": row["key"],
        "minimum_payload_capacity": row["minimum_payload_capacity"],
        "maximum_payload_capacity": row["maximum_payload_capacity"],
        **{field: 0 for field in RUNTIME_SUM_FIELDS},
        "minimum_live_survival_age_bytes": 0,
        "maximum_live_survival_age_bytes": 0,
        "maximum_completed_age_bytes": 0,
        "total_pressure_span_bytes": 0,
        "maximum_pressure_span_bytes": 0,
        "snapshot_count": 0,
        "prediction_counts": Counter(),
        "static_prior_counts": Counter(),
        "predictor_key_ambiguous_snapshots": 0,
        "predictor_flags_seen": 0,
        "predictor_placement_hint_bits_union": 0,
    }


def merge_runtime_rows(rows: Iterable[dict[str, Any]]) -> dict[RuntimeSiteKey, dict[str, Any]]:
    merged: dict[RuntimeSiteKey, dict[str, Any]] = {}
    prediction_names = {0: "cold", 1: "short", 2: "long"}
    prior_names = {0: "unknown", 1: "scoped_or_advisory_short", 2: "advisory_long"}
    for raw in rows:
        row = validate_runtime_row(raw)
        key = row["key"]
        aggregate = merged.setdefault(key, new_runtime_aggregate(row))
        aggregate["minimum_payload_capacity"] = min(
            aggregate["minimum_payload_capacity"], row["minimum_payload_capacity"]
        )
        aggregate["maximum_payload_capacity"] = max(
            aggregate["maximum_payload_capacity"], row["maximum_payload_capacity"]
        )
        for field in RUNTIME_SUM_FIELDS:
            aggregate[field] += row[field]
        row_minimum_survival_age = row["minimum_live_survival_age_bytes"]
        if row_minimum_survival_age and (
            aggregate["minimum_live_survival_age_bytes"] == 0
            or row_minimum_survival_age
            < aggregate["minimum_live_survival_age_bytes"]
        ):
            aggregate["minimum_live_survival_age_bytes"] = row_minimum_survival_age
        aggregate["maximum_live_survival_age_bytes"] = max(
            aggregate["maximum_live_survival_age_bytes"],
            row["maximum_live_survival_age_bytes"],
        )
        aggregate["maximum_completed_age_bytes"] = max(
            aggregate["maximum_completed_age_bytes"], row["maximum_completed_age_bytes"]
        )
        aggregate["total_pressure_span_bytes"] += row["pressure_span_bytes"]
        aggregate["maximum_pressure_span_bytes"] = max(
            aggregate["maximum_pressure_span_bytes"], row["pressure_span_bytes"]
        )
        aggregate["snapshot_count"] += 1
        aggregate["prediction_counts"][prediction_names[row["latest_prediction"]]] += 1
        aggregate["static_prior_counts"][prior_names[row["latest_static_prior"]]] += 1
        aggregate["predictor_key_ambiguous_snapshots"] += int(
            row["predictor_key_ambiguous"]
        )
        aggregate["predictor_flags_seen"] |= row["predictor_flags_seen"]
        aggregate["predictor_placement_hint_bits_union"] |= row[
            "predictor_placement_hint_bits_union"
        ]
    return merged


def parse_json_values(path: Path) -> list[Any]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".jsonl":
        values = []
        for line_number, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise GroundTruthError(f"invalid JSONL {path}:{line_number}") from error
        return values
    try:
        return [json.loads(raw)]
    except json.JSONDecodeError:
        values = []
        for line in raw.splitlines():
            if not line.startswith(RUNTIME_PREFIX):
                continue
            try:
                values.append(json.loads(line[len(RUNTIME_PREFIX) :]))
            except json.JSONDecodeError as error:
                raise GroundTruthError(f"invalid runtime snapshot record in {path}") from error
        if not values:
            raise GroundTruthError(f"file contains no runtime snapshot JSON: {path}")
        return values


def runtime_envelopes(value: Any) -> list[dict[str, Any]]:
    envelopes: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if isinstance(value.get("rows"), list) and (
            value.get("source") == RUNTIME_SNAPSHOT_SOURCE
            or "abi_version" in value
        ):
            envelopes.append(value)
            return envelopes
        for key in (
            "runtime_lifetime_site_snapshot",
            "lifetime_adaptive_site_snapshot",
        ):
            nested = value.get(key)
            if isinstance(nested, dict):
                envelopes.extend(runtime_envelopes(nested))
        for key in ("measurements", "runs"):
            nested = value.get(key)
            if isinstance(nested, list):
                for item in nested:
                    envelopes.extend(runtime_envelopes(item))
    return envelopes


def prefixed_json_records(path: Path, prefix: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(prefix):
            continue
        try:
            record = json.loads(line[len(prefix) :])
        except json.JSONDecodeError as error:
            raise GroundTruthError(f"invalid prefixed JSON record in {path}") from error
        if not isinstance(record, dict):
            raise GroundTruthError(f"prefixed JSON record is not an object in {path}")
        records.append(record)
    return records


def force_track_collection_evidence(
    path: Path,
    rows: Sequence[dict[str, Any]],
    stats_records: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    reasons: list[str] = []
    if len(stats_records) != 1:
        reasons.append("requires exactly one runtime stats record")
        return {"path": str(path.resolve()), "verified": False, "reasons": reasons}
    stats = stats_records[0]
    required = (
        "adaptive_force_track_all",
        "adaptive_force_track_all_maximum_allocations",
        "adaptive_force_track_all_maximum_requested_bytes",
        "adaptive_force_track_all_admitted_allocations",
        "adaptive_force_track_all_admitted_requested_bytes",
        "adaptive_force_track_all_guard_bypasses",
        "adaptive_force_track_all_pressure_allocations",
        "adaptive_force_track_all_pressure_requested_bytes",
        "adaptive_force_track_all_raw_pressure_allocations",
        "adaptive_force_track_all_raw_pressure_requested_bytes",
        "adaptive_force_track_all_raw_reallocation_pressure_allocations",
        "adaptive_force_track_all_raw_reallocation_pressure_requested_bytes",
        "adaptive_pressure_bytes",
        "adaptive_cold_bypassed_allocations",
        "adaptive_short_bypassed_allocations",
        "adaptive_site_table_bypasses",
        "adaptive_observation_table_bypasses",
        "adaptive_observation_site_count",
        "adaptive_eligible_allocations",
        "adaptive_live_survival_registrations",
        "adaptive_live_survival_registration_bypasses",
        "adaptive_live_survival_scans",
        "adaptive_live_survival_slots_examined",
        "adaptive_live_survival_observations",
        "adaptive_live_survival_promotions",
        "adaptive_live_survival_stale_abstentions",
        "adaptive_long_observations",
        "thp_extent_mappings",
        "thp_advice_attempts",
        "thp_advice_successes",
        "thp_advice_errors",
        "nohugepage_advice_failures",
        "mapping_failures",
    )
    missing = [field for field in required if field not in stats]
    if missing:
        reasons.append("missing stats fields: " + ", ".join(missing))
        return {"path": str(path.resolve()), "verified": False, "reasons": reasons}
    if stats["adaptive_force_track_all"] is not True:
        reasons.append("force-track-all was disabled")
    for field in required:
        if field == "adaptive_force_track_all":
            continue
        value = stats[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            reasons.append(f"invalid nonnegative counter {field}")
    if reasons:
        return {"path": str(path.resolve()), "verified": False, "reasons": reasons}
    if (
        stats["adaptive_force_track_all_maximum_allocations"] == 0
        or stats["adaptive_force_track_all_maximum_requested_bytes"] == 0
    ):
        reasons.append("force-track-all guards were zero")
    zero_fields = (
        "adaptive_force_track_all_guard_bypasses",
        "adaptive_cold_bypassed_allocations",
        "adaptive_short_bypassed_allocations",
        "adaptive_site_table_bypasses",
        "adaptive_observation_table_bypasses",
        "adaptive_live_survival_stale_abstentions",
        "thp_extent_mappings",
        "thp_advice_attempts",
        "thp_advice_successes",
        "thp_advice_errors",
        "nohugepage_advice_failures",
        "mapping_failures",
    )
    reasons.extend(f"nonzero {field}" for field in zero_fields if stats[field] != 0)
    if not (
        stats["adaptive_live_survival_promotions"]
        <= stats["adaptive_live_survival_observations"]
        <= stats["adaptive_live_survival_registrations"]
    ):
        reasons.append("live-survival registration/observation accounting is invalid")
    if (
        stats["adaptive_live_survival_observations"] > 0
        and stats["adaptive_live_survival_scans"] == 0
    ):
        reasons.append("live-survival observations have no pressure scan")
    if (
        stats["adaptive_live_survival_slots_examined"]
        < stats["adaptive_live_survival_observations"]
    ):
        reasons.append("live-survival scan examined fewer slots than it classified")
    if (
        stats["adaptive_pressure_bytes"]
        != stats["adaptive_force_track_all_pressure_requested_bytes"]
    ):
        reasons.append("global requested-byte pressure clock does not close")
    if (
        stats["adaptive_force_track_all_pressure_allocations"]
        < stats["adaptive_force_track_all_admitted_allocations"]
        or stats["adaptive_force_track_all_pressure_requested_bytes"]
        < stats["adaptive_force_track_all_admitted_requested_bytes"]
    ):
        reasons.append("global pressure omits admitted exact-site allocations")
    raw_pressure_allocations = (
        stats["adaptive_force_track_all_raw_pressure_allocations"]
        + stats["adaptive_force_track_all_raw_reallocation_pressure_allocations"]
    )
    raw_pressure_bytes = (
        stats["adaptive_force_track_all_raw_pressure_requested_bytes"]
        + stats["adaptive_force_track_all_raw_reallocation_pressure_requested_bytes"]
    )
    if (
        raw_pressure_allocations
        > stats["adaptive_force_track_all_pressure_allocations"]
        or raw_pressure_bytes
        > stats["adaptive_force_track_all_pressure_requested_bytes"]
    ):
        reasons.append("raw pressure subsets exceed the global clock")
    allocation_count = sum(int(row["allocation_count"]) for row in rows)
    tracked_allocations = sum(int(row["tracked_allocations"]) for row in rows)
    requested_bytes = sum(int(row["allocation_requested_bytes"]) for row in rows)
    expected = {
        "adaptive_eligible_allocations": allocation_count,
        "adaptive_force_track_all_admitted_allocations": tracked_allocations,
        "adaptive_force_track_all_admitted_requested_bytes": requested_bytes,
        "adaptive_observation_site_count": len(rows),
        "adaptive_live_survival_observations": sum(
            int(row["live_survival_observations"]) for row in rows
        ),
        "adaptive_long_observations": sum(
            int(row["long_outcomes"])
            + int(row["current_live_survival_inflight"])
            for row in rows
        ),
    }
    reasons.extend(
        f"{field} disagrees with exact-site rows"
        for field, value in expected.items()
        if stats[field] != value
    )
    if any(int(row["bypassed_allocations"]) != 0 for row in rows):
        reasons.append("exact-site rows contain bypassed allocations")
    if (
        any(int(row["tracked_inflight"]) != 0 for row in rows)
        and stats["adaptive_live_survival_registration_bypasses"] != 0
    ):
        reasons.append(
            "live objects remain while the bounded live-survivor sampler bypassed registrations"
        )
    return {
        "path": str(path.resolve()),
        "verified": not reasons,
        "reasons": reasons,
        "maximum_allocations": stats["adaptive_force_track_all_maximum_allocations"],
        "maximum_requested_bytes": stats[
            "adaptive_force_track_all_maximum_requested_bytes"
        ],
        "pressure_allocations": stats[
            "adaptive_force_track_all_pressure_allocations"
        ],
        "pressure_requested_bytes": stats[
            "adaptive_force_track_all_pressure_requested_bytes"
        ],
        "raw_pressure_allocations": stats[
            "adaptive_force_track_all_raw_pressure_allocations"
        ],
        "raw_reallocation_pressure_allocations": stats[
            "adaptive_force_track_all_raw_reallocation_pressure_allocations"
        ],
    }


def load_runtime_files(
    paths: Sequence[Path],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    provenance: list[dict[str, str]] = []
    collection_files: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            raise GroundTruthError(f"runtime snapshot file is missing: {path}")
        provenance.append({"path": str(path.resolve()), "sha256": sha256_file(path)})
        found = 0
        path_rows: list[dict[str, Any]] = []
        for value in parse_json_values(path):
            for envelope in runtime_envelopes(value):
                if envelope.get("source") != RUNTIME_SNAPSHOT_SOURCE:
                    raise GroundTruthError(
                        f"unsupported runtime snapshot source "
                        f"{envelope.get('source')!r} in {path}"
                    )
                abi_version = envelope.get("abi_version")
                if abi_version != RUNTIME_SNAPSHOT_ABI_VERSION:
                    raise GroundTruthError(
                        f"unsupported runtime snapshot ABI {abi_version!r} in {path}"
                    )
                envelope_rows = envelope.get("rows")
                assert isinstance(envelope_rows, list)
                required = envelope.get("required_rows", len(envelope_rows))
                captured = envelope.get("captured_rows", len(envelope_rows))
                if envelope.get("truncated") is True or required != captured:
                    raise GroundTruthError(f"truncated runtime site snapshot in {path}")
                if captured != len(envelope_rows):
                    raise GroundTruthError(f"runtime captured-row count mismatch in {path}")
                for row in envelope_rows:
                    if not isinstance(row, dict):
                        raise GroundTruthError(f"non-object runtime site row in {path}")
                    rows.append(row)
                    path_rows.append(row)
                found += len(envelope_rows)
        if found == 0:
            raise GroundTruthError(f"runtime snapshot file contains no site rows: {path}")
        collection_files.append(
            force_track_collection_evidence(
                path,
                path_rows,
                prefixed_json_records(path, RUNTIME_STATS_PREFIX),
            )
        )
    return rows, provenance, {
        "force_track_all_verified": bool(collection_files)
        and all(record["verified"] for record in collection_files),
        "files": collection_files,
    }


def count_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 3:
        return "2-3"
    return "4+"


def destination_family(value: str) -> str:
    families = (
        ("Box<", "box"),
        ("Vec<", "vec"),
        ("Arc<", "arc"),
        ("Rc<", "rc"),
        ("String", "string"),
        ("HashMap<", "hash_map"),
        ("HashSet<", "hash_set"),
    )
    for marker, family in families:
        if marker in value:
            return family
    return "other"


def callee_family(value: str) -> str:
    lowered = value.lower()
    if "box" in lowered and "new" in lowered:
        return "box_new"
    if "with_capacity" in lowered:
        return "with_capacity"
    if "rawvec" in lowered or "raw_vec" in lowered:
        return "raw_vec"
    if "alloc" in lowered:
        return "allocator_call"
    return "other"


def compiler_hint_semantics(hint: int) -> str:
    if hint == PROVEN_SCOPED_HINT:
        return "proven_scoped_no_duration_claim"
    if hint == BOUNDED_PROCESS_LONG_ORACLE_HINT:
        return "bounded_process_long_oracle"
    if hint == 0:
        return "unknown"
    return "other_advisory"


def compiler_feature_set(row: dict[str, Any]) -> tuple[str, ...]:
    raw_features = row.get("lifetime_analysis_features")
    if not isinstance(raw_features, dict):
        raise GroundTruthError("compiler allocation row lacks lifetime_analysis_features")
    features: set[str] = set()
    for field in FEATURE_BOOLEAN_FIELDS:
        value = raw_features.get(field)
        if not isinstance(value, bool):
            raise GroundTruthError(f"compiler feature {field} must be boolean")
        if value:
            features.add(field)
    for field in FEATURE_COUNT_FIELDS:
        value = raw_features.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise GroundTruthError(f"compiler feature {field} must be nonnegative")
        features.add(f"{field}={count_bucket(value)}")
    for field in FEATURE_LIST_FIELDS:
        value = raw_features.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise GroundTruthError(f"compiler feature {field} must be a string list")
        features.add(f"{field}_count={count_bucket(len(value))}")
    for field in ("analysis_phase", "owner_place_basis", "requested_layout_basis"):
        value = raw_features.get(field)
        if not isinstance(value, str) or not value:
            raise GroundTruthError(f"compiler feature {field} must be a nonempty string")
        features.add(f"{field}={value}")
    features.add(f"destination_family={destination_family(str(row.get('destination_type') or ''))}")
    features.add(f"callee_family={callee_family(str(row.get('callee') or ''))}")
    features.add(f"hint_basis={str(row.get('lifetime_hint_basis') or '')}")
    features.add(f"hint_semantics={compiler_hint_semantics(int(row.get('lifetime_hint') or 0))}")
    return tuple(sorted(features))


def compiler_static_key(row: dict[str, Any]) -> StaticSiteKey:
    values = []
    for field in ("callsite", "type_id", "module_id"):
        value = row.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise GroundTruthError(f"compiler field {field} must be a positive integer")
        values.append(value)
    return StaticSiteKey(*values)


def compiler_exact_layout(row: dict[str, Any]) -> tuple[int, int] | None:
    features = row.get("lifetime_analysis_features")
    assert isinstance(features, dict)
    join_key = features.get("runtime_join_key")
    complete = features.get("runtime_join_key_complete")
    if not isinstance(join_key, dict) or not isinstance(complete, bool):
        raise GroundTruthError("compiler feature export lacks its runtime join-key contract")
    static = compiler_static_key(row)
    for field, expected in static.as_dict().items():
        if join_key.get(field) != expected:
            raise GroundTruthError(
                f"compiler feature join key disagrees with audit row for {field}"
            )
    size = join_key.get("requested_size_bytes")
    align = join_key.get("requested_align_bytes")
    if not complete:
        if size is not None or align is not None:
            raise GroundTruthError("incomplete compiler runtime join key carries a partial layout")
        return None
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or not isinstance(align, int)
        or isinstance(align, bool)
        or align <= 0
        or align & (align - 1)
    ):
        raise GroundTruthError("compiler exact layout must contain positive size/power-of-two align")
    return size, align


def compiler_record(row: dict[str, Any]) -> dict[str, Any]:
    key = compiler_static_key(row)
    exact_layout = compiler_exact_layout(row)
    return {
        "key": key,
        "features": compiler_feature_set(row),
        "exact_layout": exact_layout,
        "mir_function": str(row.get("mir_function") or ""),
        "destination_type": str(row.get("destination_type") or ""),
        "callee": str(row.get("callee") or ""),
        "lifetime_hint": int(row.get("lifetime_hint") or 0),
        "lifetime_hint_confidence": int(row.get("lifetime_hint_confidence") or 0),
        "lifetime_hint_basis": str(row.get("lifetime_hint_basis") or ""),
        "hint_semantics": compiler_hint_semantics(int(row.get("lifetime_hint") or 0)),
    }


def compiler_signature(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        record["features"],
        record["exact_layout"],
        record["mir_function"],
        record["destination_type"],
        record["callee"],
        record["lifetime_hint"],
        record["lifetime_hint_confidence"],
        record["lifetime_hint_basis"],
    )


def audit_files(roots: Sequence[Path]) -> list[Path]:
    files: set[Path] = set()
    for root in roots:
        if root.is_file():
            files.add(root.resolve())
        elif root.is_dir():
            files.update(path.resolve() for path in root.rglob("*.json"))
        else:
            raise GroundTruthError(f"compiler audit path is missing: {root}")
    return sorted(files)


def load_compiler_audits(
    roots: Sequence[Path],
) -> tuple[dict[StaticSiteKey, dict[str, Any]], list[dict[str, str]]]:
    records: dict[StaticSiteKey, dict[str, Any]] = {}
    provenance: list[dict[str, str]] = []
    files = audit_files(roots)
    if not files:
        raise GroundTruthError("compiler audit roots contain no JSON files")
    for path in files:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        candidates = document.get("rewrite_candidates") if isinstance(document, dict) else None
        if not isinstance(candidates, list):
            continue
        provenance.append({"path": str(path), "sha256": sha256_file(path)})
        for row in candidates:
            if not isinstance(row, dict):
                raise GroundTruthError(f"non-object compiler audit row in {path}")
            if row.get("lowering_kind") != "semantic_scope_enter_exit_rewrite":
                continue
            if not isinstance(row.get("lifetime_analysis_features"), dict):
                continue
            record = compiler_record(row)
            key = record["key"]
            previous = records.get(key)
            if previous is not None and compiler_signature(previous) != compiler_signature(record):
                raise GroundTruthError(f"ambiguous duplicate compiler identity {key} in {path}")
            records[key] = record
    if not records:
        raise GroundTruthError("compiler audits contain no lifetime-feature allocation rows")
    return records, provenance


def rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def percent(numerator: int, denominator: int) -> float | None:
    value = rate(numerator, denominator)
    return value * 100.0 if value is not None else None


def joined_runtime_record(
    app: str,
    aggregate: dict[str, Any],
    compiler: dict[str, Any] | None,
    total_allocations: int,
    total_requested_bytes: int,
    force_track_all_verified: bool,
) -> dict[str, Any]:
    key: RuntimeSiteKey = aggregate["key"]
    exact_layout_status = "unmatched_compiler_identity"
    if compiler is not None:
        exact = compiler["exact_layout"]
        if exact is None:
            exact_layout_status = "compiler_layout_abstained"
        elif exact != (key.requested_size, key.align):
            raise GroundTruthError(
                f"compiler exact layout {exact} disagrees with runtime key "
                f"{(key.requested_size, key.align)} for {key.static}"
            )
        else:
            exact_layout_status = "exact_five_field_match"

    unique_long_observations = (
        aggregate["long_outcomes"] + aggregate["current_live_survival_inflight"]
    )
    unique_long_requested_bytes = (
        aggregate["long_requested_bytes"]
        + aggregate["current_live_survival_inflight_requested_bytes"]
    )
    decisive_objects = aggregate["short_outcomes"] + unique_long_observations
    decisive_bytes = (
        aggregate["short_requested_bytes"] + unique_long_requested_bytes
    )
    completed = aggregate["completed_outcomes"]
    live_unclassified = (
        aggregate["tracked_inflight"] - aggregate["current_live_survival_inflight"]
    )
    return {
        "app": app,
        "runtime_key": key.as_dict(),
        "static_identity_joined": compiler is not None,
        "force_track_all_verified": force_track_all_verified,
        "exact_layout_join_status": exact_layout_status,
        "compiler": (
            {
                field: compiler[field]
                for field in (
                    "features",
                    "mir_function",
                    "destination_type",
                    "callee",
                    "lifetime_hint",
                    "lifetime_hint_confidence",
                    "lifetime_hint_basis",
                    "hint_semantics",
                )
            }
            if compiler is not None
            else None
        ),
        "runtime": {
            field: (
                dict(aggregate[field])
                if isinstance(aggregate[field], Counter)
                else aggregate[field]
            )
            for field in (
                "minimum_payload_capacity",
                "maximum_payload_capacity",
                *RUNTIME_SUM_FIELDS,
                *RUNTIME_LIVE_SURVIVAL_AGE_FIELDS,
                "maximum_completed_age_bytes",
                "total_pressure_span_bytes",
                "maximum_pressure_span_bytes",
                "snapshot_count",
                "prediction_counts",
                "static_prior_counts",
                "predictor_key_ambiguous_snapshots",
                "predictor_flags_seen",
                "predictor_placement_hint_bits_union",
            )
        },
        "derived": {
            "decisive_outcomes": decisive_objects,
            "decisive_requested_bytes": decisive_bytes,
            "unique_long_observations": unique_long_observations,
            "unique_long_requested_bytes": unique_long_requested_bytes,
            "long_outcome_share": rate(unique_long_observations, decisive_objects),
            "long_requested_byte_share": rate(
                unique_long_requested_bytes, decisive_bytes
            ),
            "completed_indeterminate_outcomes": aggregate["censored_outcomes"],
            "live_survival_confirmed_long_objects": aggregate[
                "current_live_survival_inflight"
            ],
            "live_survival_provenance_observations": aggregate[
                "live_survival_observations"
            ],
            "live_right_censored_objects": live_unclassified,
            "unobserved_bypassed_allocations": aggregate["bypassed_allocations"],
            "mean_completed_age_pressure_bytes": rate(
                aggregate["total_completed_age_bytes"], completed
            ),
            "mean_live_age_lower_bound_bytes": rate(
                aggregate["inflight_age_lower_bound_bytes"],
                aggregate["tracked_inflight"],
            ),
            "allocation_count_hotness_share": rate(
                aggregate["allocation_count"], total_allocations
            ),
            "allocation_requested_byte_hotness_share": rate(
                aggregate["allocation_requested_bytes"], total_requested_bytes
            ),
            "access_hotness": None,
        },
    }


def sum_joined(rows: Sequence[dict[str, Any]], field: str) -> int:
    return sum(int(row["runtime"][field]) for row in rows)


def sum_derived(rows: Sequence[dict[str, Any]], field: str) -> int:
    return sum(int(row["derived"][field]) for row in rows)


def feature_correlations(
    rows: Sequence[dict[str, Any]], *, minimum_feature_sites: int
) -> list[dict[str, Any]]:
    joined = [
        row
        for row in rows
        if row["static_identity_joined"]
        and int(row["derived"]["decisive_requested_bytes"]) > 0
    ]
    all_features = sorted(
        {
            feature
            for row in joined
            for feature in row["compiler"]["features"]
        }
    )
    correlations: list[dict[str, Any]] = []
    selection_conditioned = any(
        int(row["runtime"]["bypassed_allocations"]) > 0 for row in joined
    ) or any(not bool(row["force_track_all_verified"]) for row in joined)
    for feature in all_features:
        present = [row for row in joined if feature in row["compiler"]["features"]]
        absent = [row for row in joined if feature not in row["compiler"]["features"]]
        if len(present) < minimum_feature_sites:
            continue
        present_long = sum_derived(present, "unique_long_requested_bytes")
        present_short = sum_joined(present, "short_requested_bytes")
        absent_long = sum_derived(absent, "unique_long_requested_bytes")
        absent_short = sum_joined(absent, "short_requested_bytes")
        present_rate = rate(present_long, present_long + present_short)
        absent_rate = rate(absent_long, absent_long + absent_short)
        delta = (
            (present_rate - absent_rate) * 100.0
            if present_rate is not None and absent_rate is not None
            else None
        )
        lift = (
            present_rate / absent_rate
            if present_rate is not None and absent_rate not in {None, 0.0}
            else None
        )
        correlations.append(
            {
                "feature": feature,
                "present_runtime_sites": len(present),
                "absent_runtime_sites": len(absent),
                "present_decisive_requested_bytes": present_long + present_short,
                "present_long_requested_bytes": present_long,
                "present_long_requested_byte_share": present_rate,
                "absent_long_requested_byte_share": absent_rate,
                "long_share_difference_percentage_points": delta,
                "long_share_lift": lift,
                "selection_conditioned": selection_conditioned,
                "interpretation": (
                    "observational association conditioned on runtime-selected labels"
                    if selection_conditioned
                    else "observational association over fully tracked allocations"
                ),
            }
        )
    correlations.sort(
        key=lambda row: (
            row["long_share_difference_percentage_points"] is not None,
            row["long_share_difference_percentage_points"] or float("-inf"),
            row["present_decisive_requested_bytes"],
        ),
        reverse=True,
    )
    return correlations


def rank_long_sites(
    rows: Sequence[dict[str, Any]],
    *,
    minimum_decisive_outcomes: int,
    minimum_long_byte_share: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    volume = [row for row in rows if row["derived"]["unique_long_observations"] > 0]
    volume.sort(
        key=lambda row: (
            row["derived"]["unique_long_requested_bytes"],
            row["derived"]["unique_long_observations"],
            row["runtime"]["allocation_requested_bytes"],
        ),
        reverse=True,
    )
    candidates = [
        row
        for row in volume
        if row["derived"]["decisive_outcomes"] >= minimum_decisive_outcomes
        and (row["derived"]["long_requested_byte_share"] or 0.0)
        >= minimum_long_byte_share
    ]
    return volume, candidates


def application_summary(
    app: str,
    runtime: dict[RuntimeSiteKey, dict[str, Any]],
    compiler: dict[StaticSiteKey, dict[str, Any]],
    *,
    minimum_decisive_outcomes: int,
    minimum_long_byte_share: float,
    minimum_feature_sites: int,
    force_track_all_verified: bool = False,
) -> dict[str, Any]:
    total_allocations = sum(row["allocation_count"] for row in runtime.values())
    total_requested_bytes = sum(
        row["allocation_requested_bytes"] for row in runtime.values()
    )
    rows = [
        joined_runtime_record(
            app,
            aggregate,
            compiler.get(key.static),
            total_allocations,
            total_requested_bytes,
            force_track_all_verified,
        )
        for key, aggregate in sorted(runtime.items())
    ]
    matched = [row for row in rows if row["static_identity_joined"]]
    exact = [
        row for row in rows if row["exact_layout_join_status"] == "exact_five_field_match"
    ]
    layout_abstained = [
        row for row in rows if row["exact_layout_join_status"] == "compiler_layout_abstained"
    ]
    volume, candidates = rank_long_sites(
        rows,
        minimum_decisive_outcomes=minimum_decisive_outcomes,
        minimum_long_byte_share=minimum_long_byte_share,
    )
    runtime_static_keys = {key.static for key in runtime}
    bypassed_allocations = sum_joined(rows, "bypassed_allocations")
    tracked_allocations = sum_joined(rows, "tracked_allocations")
    return {
        "app": app,
        "runtime_exact_site_count": len(rows),
        "runtime_static_identity_count": len(runtime_static_keys),
        "compiler_static_identity_count": len(compiler),
        "matched_runtime_exact_site_count": len(matched),
        "exact_five_field_match_count": len(exact),
        "compiler_layout_abstention_runtime_site_count": len(layout_abstained),
        "unmatched_runtime_exact_site_count": len(rows) - len(matched),
        "compiler_identities_without_runtime_truth": len(set(compiler) - runtime_static_keys),
        "static_identity_join_coverage_percent": percent(len(matched), len(rows)),
        "matched_allocation_count_coverage_percent": percent(
            sum_joined(matched, "allocation_count"), total_allocations
        ),
        "matched_requested_byte_coverage_percent": percent(
            sum_joined(matched, "allocation_requested_bytes"), total_requested_bytes
        ),
        "label_collection": {
            "tracked_allocation_coverage_percent": percent(
                tracked_allocations, total_allocations
            ),
            "selection_conditioned": (
                not force_track_all_verified or bypassed_allocations > 0
            ),
            "force_track_all_verified": force_track_all_verified,
            "unbiased_prevalence_claim_eligible": (
                force_track_all_verified and bypassed_allocations == 0
            ),
        },
        "outcomes": {
            "allocation_count": sum_joined(rows, "allocation_count"),
            "allocation_requested_bytes": sum_joined(rows, "allocation_requested_bytes"),
            "tracked_allocations": sum_joined(rows, "tracked_allocations"),
            "bypassed_unobserved_allocations": sum_joined(rows, "bypassed_allocations"),
            "decisive_short_outcomes": sum_joined(rows, "short_outcomes"),
            "decisive_long_outcomes": sum_derived(
                rows, "unique_long_observations"
            ),
            "completed_long_outcomes": sum_joined(rows, "long_outcomes"),
            "live_survival_confirmed_long_objects": sum_joined(
                rows, "current_live_survival_inflight"
            ),
            "live_survival_provenance_observations": sum_joined(
                rows, "live_survival_observations"
            ),
            "completed_indeterminate_outcomes": sum_joined(rows, "censored_outcomes"),
            "tracked_inflight_objects": sum_joined(rows, "tracked_inflight"),
            "live_right_censored_objects": sum(
                int(row["derived"]["live_right_censored_objects"])
                for row in rows
            ),
            "decisive_short_requested_bytes": sum_joined(
                rows, "short_requested_bytes"
            ),
            "decisive_long_requested_bytes": sum_derived(
                rows, "unique_long_requested_bytes"
            ),
            "completed_long_requested_bytes": sum_joined(
                rows, "long_requested_bytes"
            ),
            "live_survival_confirmed_long_requested_bytes": sum_joined(
                rows, "current_live_survival_inflight_requested_bytes"
            ),
            "completed_indeterminate_requested_bytes": sum_joined(
                rows, "censored_requested_bytes"
            ),
            "live_inflight_age_lower_bound_bytes": sum_joined(
                rows, "inflight_age_lower_bound_bytes"
            ),
        },
        "runtime_sites": rows,
        "long_outcome_volume_ranking": volume,
        "long_dominant_candidates": candidates,
        "feature_correlations": feature_correlations(
            rows, minimum_feature_sites=minimum_feature_sites
        ),
    }


def aggregate_applications(
    applications: Sequence[dict[str, Any]], *, minimum_feature_sites: int
) -> dict[str, Any]:
    rows = [row for app in applications for row in app["runtime_sites"]]
    volume = [row for app in applications for row in app["long_outcome_volume_ranking"]]
    volume.sort(
        key=lambda row: (
            row["derived"]["unique_long_requested_bytes"],
            row["derived"]["unique_long_observations"],
        ),
        reverse=True,
    )
    candidates = [row for app in applications for row in app["long_dominant_candidates"]]
    candidates.sort(
        key=lambda row: (
            row["derived"]["unique_long_requested_bytes"],
            row["derived"]["unique_long_observations"],
        ),
        reverse=True,
    )
    total_allocations = sum_joined(rows, "allocation_count")
    tracked_allocations = sum_joined(rows, "tracked_allocations")
    bypassed_allocations = sum_joined(rows, "bypassed_allocations")
    return {
        "applications": [app["app"] for app in applications],
        "runtime_exact_site_count": len(rows),
        "matched_runtime_exact_site_count": sum(
            int(row["static_identity_joined"]) for row in rows
        ),
        "exact_five_field_match_count": sum(
            row["exact_layout_join_status"] == "exact_five_field_match" for row in rows
        ),
        "allocation_count": total_allocations,
        "allocation_requested_bytes": sum_joined(rows, "allocation_requested_bytes"),
        "decisive_short_outcomes": sum_joined(rows, "short_outcomes"),
        "decisive_long_outcomes": sum_derived(rows, "unique_long_observations"),
        "completed_long_outcomes": sum_joined(rows, "long_outcomes"),
        "live_survival_confirmed_long_objects": sum_joined(
            rows, "current_live_survival_inflight"
        ),
        "live_survival_provenance_observations": sum_joined(
            rows, "live_survival_observations"
        ),
        "completed_indeterminate_outcomes": sum_joined(rows, "censored_outcomes"),
        "tracked_inflight_objects": sum_joined(rows, "tracked_inflight"),
        "live_right_censored_objects": sum(
            int(row["derived"]["live_right_censored_objects"])
            for row in rows
        ),
        "bypassed_unobserved_allocations": bypassed_allocations,
        "label_collection": {
            "tracked_allocation_coverage_percent": percent(
                tracked_allocations, total_allocations
            ),
            "selection_conditioned": (
                not rows
                or any(not bool(row["force_track_all_verified"]) for row in rows)
                or bypassed_allocations > 0
            ),
            "force_track_all_verified": all(
                bool(row["force_track_all_verified"]) for row in rows
            ),
            "unbiased_prevalence_claim_eligible": (
                bool(rows)
                and all(bool(row["force_track_all_verified"]) for row in rows)
                and bypassed_allocations == 0
            ),
        },
        "long_outcome_volume_ranking": volume,
        "long_dominant_candidates": candidates,
        "feature_correlations": feature_correlations(
            rows, minimum_feature_sites=minimum_feature_sites
        ),
    }


def heuristic_promotion_decision(
    aggregate: dict[str, Any],
    *,
    minimum_decisive_outcomes: int,
    minimum_long_byte_share: float,
) -> dict[str, Any]:
    candidate_count = len(aggregate["long_dominant_candidates"])
    if candidate_count == 0:
        status = "ABSTAIN"
        reason = (
            "no exact runtime site passed the preregistered support and "
            "Long requested-byte dominance thresholds"
        )
    else:
        status = "CANDIDATES_REQUIRE_HELD_OUT_VALIDATION"
        reason = (
            "training-set candidates remain observational and require a held-out "
            "precision, recall, and performance evaluation before promotion"
        )
    return {
        "status": status,
        "production_static_rule_promoted": False,
        "eligible_training_candidate_count": candidate_count,
        "minimum_decisive_outcomes": minimum_decisive_outcomes,
        "minimum_long_requested_byte_share": minimum_long_byte_share,
        "reason": reason,
        "safe_runtime_action": "preserve Unknown placement for unvalidated sites",
    }


def resolve_paths(base: Path, values: Any, field: str) -> list[Path]:
    if not isinstance(values, list) or not values or not all(
        isinstance(value, str) and value for value in values
    ):
        raise GroundTruthError(f"manifest field {field} must be a nonempty path list")
    return [
        (Path(value) if Path(value).is_absolute() else base / value).resolve()
        for value in values
    ]


def analyze_manifest(
    manifest_path: Path,
    *,
    required_apps: set[str],
    minimum_decisive_outcomes: int,
    minimum_long_byte_share: float,
    minimum_feature_sites: int,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("applications") if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or not entries:
        raise GroundTruthError("manifest applications must be a nonempty list")
    base = manifest_path.resolve().parent
    applications: list[dict[str, Any]] = []
    provenance: dict[str, Any] = {
        "manifest": {
            "path": str(manifest_path.resolve()),
            "sha256": sha256_file(manifest_path),
        },
        "applications": {},
    }
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("app"), str):
            raise GroundTruthError("each manifest application must have an app name")
        app = entry["app"]
        if app in seen:
            raise GroundTruthError(f"duplicate manifest application {app}")
        seen.add(app)
        runtime_paths = resolve_paths(
            base, entry.get("runtime_site_snapshot_files"), "runtime_site_snapshot_files"
        )
        audit_roots = resolve_paths(
            base, entry.get("compiler_audit_roots"), "compiler_audit_roots"
        )
        runtime_rows, runtime_provenance, collection_evidence = load_runtime_files(
            runtime_paths
        )
        compiler_rows, compiler_provenance = load_compiler_audits(audit_roots)
        applications.append(
            application_summary(
                app,
                merge_runtime_rows(runtime_rows),
                compiler_rows,
                minimum_decisive_outcomes=minimum_decisive_outcomes,
                minimum_long_byte_share=minimum_long_byte_share,
                minimum_feature_sites=minimum_feature_sites,
                force_track_all_verified=collection_evidence[
                    "force_track_all_verified"
                ],
            )
        )
        provenance["applications"][app] = {
            "runtime_site_snapshots": runtime_provenance,
            "runtime_label_collection": collection_evidence,
            "compiler_audits": compiler_provenance,
        }
    missing = sorted(required_apps - seen)
    if missing:
        raise GroundTruthError("manifest lacks required applications: " + ", ".join(missing))
    applications.sort(key=lambda app: app["app"])
    aggregate = aggregate_applications(
        applications, minimum_feature_sites=minimum_feature_sites
    )
    return {
        "schema_version": 1,
        "source": "compiler-heap-lifetime-ground-truth-join",
        "join_contract": {
            "compiler_static_identity": ["callsite", "type_id", "module_id"],
            "runtime_exact_grain": [
                "callsite",
                "type_id",
                "module_id",
                "requested_size",
                "align",
            ],
            "compiler_exact_layout": (
                "checked only when requested_size_bytes and requested_align_bytes "
                "are concrete; dynamic layouts remain explicit abstentions"
            ),
            "ambiguous_compiler_identity": "fail closed",
        },
        "parameters": {
            "required_apps": sorted(required_apps),
            "minimum_decisive_outcomes": minimum_decisive_outcomes,
            "minimum_long_requested_byte_share": minimum_long_byte_share,
            "minimum_feature_sites": minimum_feature_sites,
        },
        "applications": applications,
        "aggregate": aggregate,
        "heuristic_promotion_decision": heuristic_promotion_decision(
            aggregate,
            minimum_decisive_outcomes=minimum_decisive_outcomes,
            minimum_long_byte_share=minimum_long_byte_share,
        ),
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
        "provenance": provenance,
        "claim_boundary": (
            "Completed outcomes and live-survival confirmation define pressure-relative "
            "Short/Long truth. Unique Long truth is completed long outcomes plus "
            "currently live confirmed survivors; cumulative live-survival observations "
            "remain provenance and are never added again. Completed indeterminate "
            "outcomes, remaining right-censored objects, and bypassed allocations stay "
            "outside decisive denominators. Compiler "
            "features are observational correlates. Exact Drop proves scope only; "
            "mem::forget/Box::leak tags remain a bounded process-long oracle. "
            "Allocation count, requested bytes, and pressure span proxy allocation "
            "hotness; access hotness is unavailable."
        ),
    }


def markdown_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def render_markdown(summary: dict[str, Any]) -> str:
    aggregate = summary["aggregate"]
    lines = [
        "# Compiler Heap-Lifetime Ground-Truth Analysis",
        "",
        "## Status",
        "",
        (
            f"Joined {aggregate['runtime_exact_site_count']} exact runtime sites across "
            f"{', '.join(aggregate['applications'])}."
        ),
        "",
        "## Application coverage",
        "",
        "| Application | Runtime sites | Static-feature matches | Exact layout matches | Matched requested bytes | Unique Long truth | Live censored |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for app in summary["applications"]:
        lines.append(
            "| {app} | {sites} | {matched} | {exact} | {coverage} | {long} | {live} |".format(
                app=app["app"],
                sites=app["runtime_exact_site_count"],
                matched=app["matched_runtime_exact_site_count"],
                exact=app["exact_five_field_match_count"],
                coverage=markdown_percent(app["matched_requested_byte_coverage_percent"]),
                long=app["outcomes"]["decisive_long_outcomes"],
                live=app["outcomes"]["live_right_censored_objects"],
            )
        )
    lines.extend(
        [
            "",
            "## Highest long-outcome byte volume",
            "",
            "| Application | Function | Requested size | Allocations | Unique Long truth | Long byte share | Requested-byte hotness |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in aggregate["long_outcome_volume_ranking"][:20]:
        compiler = row["compiler"] or {}
        lines.append(
            "| {app} | `{function}` | {size} | {allocations} | {long} | {share} | {hotness} |".format(
                app=row["app"],
                function=compiler.get("mir_function", "unmatched"),
                size=row["runtime_key"]["requested_size"],
                allocations=row["runtime"]["allocation_count"],
                long=row["derived"]["unique_long_observations"],
                share=markdown_percent(
                    (row["derived"]["long_requested_byte_share"] or 0.0) * 100.0
                ),
                hotness=markdown_percent(
                    (row["derived"]["allocation_requested_byte_hotness_share"] or 0.0)
                    * 100.0
                ),
            )
        )
    if not aggregate["long_outcome_volume_ranking"]:
        lines.append("| n/a | n/a | 0 | 0 | 0 | n/a | n/a |")
    lines.extend(
        [
            "",
            "## Strongest feature associations",
            "",
            "| Feature | Present sites | Decisive bytes | Present long share | Absent long share | Difference |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in aggregate["feature_correlations"][:20]:
        lines.append(
            "| `{feature}` | {sites} | {bytes} | {present} | {absent} | {delta} |".format(
                feature=row["feature"],
                sites=row["present_runtime_sites"],
                bytes=row["present_decisive_requested_bytes"],
                present=markdown_percent(
                    None
                    if row["present_long_requested_byte_share"] is None
                    else row["present_long_requested_byte_share"] * 100.0
                ),
                absent=markdown_percent(
                    None
                    if row["absent_long_requested_byte_share"] is None
                    else row["absent_long_requested_byte_share"] * 100.0
                ),
                delta=markdown_percent(row["long_share_difference_percentage_points"]),
            )
        )
    if not aggregate["feature_correlations"]:
        lines.append("| n/a | 0 | 0 | n/a | n/a | n/a |")
    lines.extend(
        [
            "",
            "## Evidence boundaries",
            "",
            "- The compiler join uses exact `(callsite, type_id, module_id)` identity; every runtime size/alignment row remains separate.",
            "- Concrete compiler layout claims receive an exact five-field check. Dynamic layouts remain explicit abstentions.",
            "- Unique Long truth is completed `long_outcomes + current_live_survival_inflight`; cumulative `live_survival_observations` is provenance and is never double counted.",
            "- Completed indeterminate outcomes, unconfirmed live right-censored objects, and bypassed allocations stay outside Short/Long denominators.",
            "- Feature correlations are observational. They rank hypotheses for a held-out classifier and carry no causal claim.",
            "- When any allocation is bypassed, outcome shares and feature associations are conditioned on the runtime-selected tracked stream. Population-prevalence claims require force-track-all evidence or valid inclusion-probability weighting.",
            "- Exact Drop establishes a scoped owner path and carries no Short-duration claim. `mem::forget`/`Box::leak` remain bounded oracle cases.",
            "- Allocation count, requested bytes, and pressure span are allocation-hotness proxies. Access hotness is unavailable.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--required-apps", default="ripgrep,fd,oxipng")
    parser.add_argument("--minimum-decisive-outcomes", type=int, default=3)
    parser.add_argument("--minimum-long-byte-share", type=float, default=0.8)
    parser.add_argument("--minimum-feature-sites", type=int, default=2)
    args = parser.parse_args(list(argv) if argv is not None else None)
    args.required_apps = {
        value.strip() for value in args.required_apps.split(",") if value.strip()
    }
    if not args.required_apps:
        parser.error("required apps must be nonempty")
    if args.minimum_decisive_outcomes <= 0 or args.minimum_feature_sites <= 0:
        parser.error("minimum decisive outcomes and feature sites must be positive")
    if not 0.0 <= args.minimum_long_byte_share <= 1.0:
        parser.error("minimum long byte share must be in [0, 1]")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    summary = analyze_manifest(
        args.manifest.resolve(),
        required_apps=args.required_apps,
        minimum_decisive_outcomes=args.minimum_decisive_outcomes,
        minimum_long_byte_share=args.minimum_long_byte_share,
        minimum_feature_sites=args.minimum_feature_sites,
    )
    write_json(args.output.resolve(), summary)
    markdown_path = args.markdown_output.resolve()
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(summary), encoding="utf-8")
    print(json.dumps(summary["aggregate"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
