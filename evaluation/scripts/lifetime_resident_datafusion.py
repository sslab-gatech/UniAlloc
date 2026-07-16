#!/usr/bin/env python3
"""Fail-closed natural resident-heap screen for pinned DataFusion 54.0.0.

The generated process uses uninstrumented input fixtures, asks DataFusion to
materialize a large join result, retains that DataFusion-produced ``MemTable``,
and repeatedly executes a fixed analytical query over it.  Lifetime inference
targets pinned DataFusion and Arrow dependency crates; the generated driver is
outside the compiler-claim boundary.  Runtime ground truth precedes every THP
or timing comparison.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import lifetime_prior_six_program_campaign as campaign  # noqa: E402


SCHEMA_VERSION = 1
DATAFUSION_REPOSITORY = "https://github.com/apache/datafusion.git"
DATAFUSION_VERSION = "54.0.0"
DATAFUSION_COMMIT = "45d943dfb8699dc9cb9ef2320e955b73e3e6c03b"
ARROW_VERSION = "58.3.0"
TOKIO_VERSION = "1.52.0"
DATAFUSION_CHECKOUT_NAME = "datafusion-54.0.0"
RUNNER_PACKAGE = "unialloc-lifetime-resident-datafusion"
RUNNER_CRATE = RUNNER_PACKAGE.replace("-", "_")
# Selected crates cover query planning/execution plus the Arrow kernels and
# buffers that DataFusion uses to materialize the resident table.  The direct
# force-extern wrapper supplies one UniAlloc rlib to these registry/path crates
# without mutating the pristine pinned checkout or Cargo registry.
TARGET_CRATES = (
    "datafusion",
    "datafusion_physical_plan",
    "datafusion_functions_aggregate",
    "datafusion_physical_expr",
    "arrow_select",
    "arrow_array",
    "arrow_buffer",
    "arrow_data",
    "arrow_row",
)
FORCE_LOAD_CRATES = (RUNNER_CRATE, *TARGET_CRATES)
RESULT_PREFIX = "UNIALLOC_LIFETIME_RESIDENT_DATAFUSION="

# 24 KiB keeps every intended Vec<u64> inside UniAlloc's 28,032-byte routed
# ceiling. The former 32 KiB geometry silently bypassed the lifetime arena.
ROWS_PER_BATCH = 3_072
COLUMNS_PER_TABLE = 4
TABLE_COUNT = 2
ROUTABLE_BUFFER_BYTES = ROWS_PER_BATCH * 8
DEFAULT_BATCHES_PER_TABLE = 256
DEFAULT_QUERY_ITERATIONS = 3_072
DEFAULT_TARGET_PARTITIONS = 2
DEFAULT_MINIMUM_SECONDS = 45.0
# A busy shared host can move the same fixed work a few seconds around the
# one-minute target.  The 45--70 second gate preserves a bounded macro window
# without wasting a successful minute-long process on a narrow cutoff.
DEFAULT_MAXIMUM_SECONDS = 70.0
DEFAULT_TIMEOUT_SECONDS = 300
MATCHED_BLOCK_COUNT = 3
DATAFUSION_ALLOCATOR_COMPATIBILITY_RULES = (
    {
        "package": "libc",
        "old_version": "0.2.183",
        "sections": ("dependencies", "build-dependencies"),
        "expected_occurrences": 2,
    },
)

FIXED_WORK_INVARIANT_FIELDS = (
    "batches_per_table",
    "rows_per_batch",
    "rows_per_table",
    "table_count",
    "columns_per_table",
    "routable_buffer_bytes",
    "resident_payload_bytes",
    "materialized_rows",
    "materialized_digest",
    "query_iterations",
    "target_partitions",
    "query_output_rows",
    "query_digest",
    "result_digest",
    "correctness",
)


class ContractError(RuntimeError):
    """A source, build, runtime, correctness, or claim gate failed closed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_remote(value: str) -> str:
    return value.strip().rstrip("/").removesuffix(".git")


def validate_checkout_identity(identity: Mapping[str, Any]) -> None:
    expected = {
        "repository": DATAFUSION_REPOSITORY,
        "source_ref": DATAFUSION_VERSION,
        "source_commit": DATAFUSION_COMMIT,
    }
    for field, value in expected.items():
        if identity.get(field) != value:
            raise ContractError(f"DataFusion {field} does not match the exact pin")
    if canonical_remote(str(identity.get("remote", ""))) != canonical_remote(
        DATAFUSION_REPOSITORY
    ):
        raise ContractError("DataFusion checkout remote does not match the pin")
    if identity.get("status") != "":
        raise ContractError("DataFusion checkout is dirty")
    if re.fullmatch(r"[0-9a-f]{40}", str(identity.get("source_tree", ""))) is None:
        raise ContractError("DataFusion source tree identity is missing")
    if re.fullmatch(
        r"[0-9a-f]{64}", str(identity.get("cargo_toml_sha256", ""))
    ) is None:
        raise ContractError("DataFusion Cargo.toml identity is missing")
    lock_digest = identity.get("cargo_lock_sha256")
    if lock_digest is not None and re.fullmatch(r"[0-9a-f]{64}", str(lock_digest)) is None:
        raise ContractError("DataFusion Cargo.lock identity is malformed")


def _git(checkout: Path, *arguments: str, timeout: int = 1_800) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=checkout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise ContractError(
            f"git {' '.join(arguments)} failed:\n"
            + result.stderr.decode(errors="replace")[-8_000:]
        )
    return result.stdout.decode().strip()


def ensure_pinned_checkout(checkout_root: Path) -> dict[str, Any]:
    checkout = checkout_root / DATAFUSION_CHECKOUT_NAME
    if not (checkout / ".git").is_dir():
        checkout_root.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                DATAFUSION_REPOSITORY,
                str(checkout),
            ],
            cwd=checkout_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=1_800,
        )
        if result.returncode != 0:
            raise ContractError(
                "DataFusion clone failed:\n"
                + result.stderr.decode(errors="replace")[-8_000:]
            )
        _git(checkout, "fetch", "--depth=1", "origin", DATAFUSION_COMMIT)
        _git(checkout, "checkout", "--detach", DATAFUSION_COMMIT)
    else:
        if _git(checkout, "status", "--short"):
            raise ContractError("refusing to alter a dirty DataFusion checkout")
        if _git(checkout, "rev-parse", "HEAD") != DATAFUSION_COMMIT:
            _git(checkout, "fetch", "--depth=1", "origin", DATAFUSION_COMMIT)
            _git(checkout, "checkout", "--detach", DATAFUSION_COMMIT)

    manifest = checkout / "Cargo.toml"
    if not manifest.is_file() or not (checkout / "datafusion/core/Cargo.toml").is_file():
        raise ContractError("pinned DataFusion checkout has an unexpected workspace layout")
    manifest_text = manifest.read_text(encoding="utf-8")
    if re.search(r'^version\s*=\s*"54\.0\.0"\s*$', manifest_text, re.MULTILINE) is None:
        raise ContractError("pinned DataFusion workspace version changed")
    lock = checkout / "Cargo.lock"
    identity = {
        "repository": DATAFUSION_REPOSITORY,
        "source_ref": DATAFUSION_VERSION,
        "source_commit": _git(checkout, "rev-parse", "HEAD"),
        "source_tree": _git(checkout, "rev-parse", "HEAD^{tree}"),
        "remote": _git(checkout, "remote", "get-url", "origin"),
        "status": _git(checkout, "status", "--short"),
        "checkout": str(checkout.resolve()),
        "cargo_toml_sha256": sha256_file(manifest),
        "cargo_lock_sha256": sha256_file(lock) if lock.is_file() else None,
    }
    validate_checkout_identity(identity)
    return identity


WORKLOAD_SOURCE = r'''
use std::collections::BTreeMap;
use std::env;
use std::hint::black_box;
use std::sync::Arc;
use std::time::Instant;

use arrow::array::{Array, ArrayRef, UInt64Array};
use arrow::datatypes::{DataType, Field, Schema, SchemaRef};
use arrow::record_batch::RecordBatch;
use datafusion::datasource::MemTable;
use datafusion::prelude::{SessionConfig, SessionContext};

const ROWS_PER_BATCH: usize = 3072;
const COLUMNS_PER_TABLE: usize = 4;
const TABLE_COUNT: usize = 2;
const ROUTABLE_BUFFER_BYTES: usize = ROWS_PER_BATCH * 8;
const MATERIALIZE_SQL: &str = "SELECT f.join_key, f.group_id, f.value, f.selector, d.category, d.weight, d.active FROM facts f JOIN dimensions d ON f.join_key = d.join_key";
const SQL: &str = "SELECT group_id, COUNT(*) AS row_count, SUM(value) AS value_sum, SUM(weight) AS weight_sum FROM resident_rows WHERE selector < 8 AND active = 1 AND category < 8 GROUP BY group_id ORDER BY group_id";

type AnyResult<T> = Result<T, Box<dyn std::error::Error + Send + Sync>>;

fn positive_argument(value: Option<String>, name: &str) -> usize {
    let parsed = value
        .unwrap_or_else(|| panic!("missing {name}"))
        .parse::<usize>()
        .unwrap_or_else(|_| panic!("invalid {name}"));
    assert!(parsed > 0, "{name} must be positive");
    parsed
}

fn mix(digest: u64, value: u64) -> u64 {
    (digest ^ value).wrapping_mul(0x100000001b3)
}

fn fact_schema() -> SchemaRef {
    Arc::new(Schema::new(vec![
        Field::new("join_key", DataType::UInt64, false),
        Field::new("group_id", DataType::UInt64, false),
        Field::new("value", DataType::UInt64, false),
        Field::new("selector", DataType::UInt64, false),
    ]))
}

fn dimension_schema() -> SchemaRef {
    Arc::new(Schema::new(vec![
        Field::new("join_key", DataType::UInt64, false),
        Field::new("category", DataType::UInt64, false),
        Field::new("weight", DataType::UInt64, false),
        Field::new("active", DataType::UInt64, false),
    ]))
}

#[inline(never)]
fn fact_batch(schema: &SchemaRef, batch_index: usize) -> AnyResult<RecordBatch> {
    let base = batch_index.checked_mul(ROWS_PER_BATCH).expect("fact row offset");
    let keys = (base..base + ROWS_PER_BATCH).map(|key| key as u64);
    let columns: Vec<ArrayRef> = vec![
        Arc::new(UInt64Array::from_iter_values(keys.clone())),
        Arc::new(UInt64Array::from_iter_values(keys.clone().map(|key| key % 64))),
        Arc::new(UInt64Array::from_iter_values(keys.clone().map(|key| (key * 3 + 7) % 1009))),
        Arc::new(UInt64Array::from_iter_values(keys.map(|key| (key * 11 + 5) % 32))),
    ];
    Ok(RecordBatch::try_new(schema.clone(), columns)?)
}

#[inline(never)]
fn dimension_batch(schema: &SchemaRef, batch_index: usize) -> AnyResult<RecordBatch> {
    let base = batch_index.checked_mul(ROWS_PER_BATCH).expect("dimension row offset");
    let keys = (base..base + ROWS_PER_BATCH).map(|key| key as u64);
    let columns: Vec<ArrayRef> = vec![
        Arc::new(UInt64Array::from_iter_values(keys.clone())),
        Arc::new(UInt64Array::from_iter_values(keys.clone().map(|key| (key * 7 + 1) % 16))),
        Arc::new(UInt64Array::from_iter_values(keys.clone().map(|key| (key * 5 + 3) % 97))),
        Arc::new(UInt64Array::from_iter_values(keys.map(|key| u64::from(key % 5 != 0)))),
    ];
    Ok(RecordBatch::try_new(schema.clone(), columns)?)
}

fn expected_aggregates(rows: usize) -> BTreeMap<u64, (u64, u128, u128)> {
    let mut expected = BTreeMap::new();
    for key in 0..rows {
        let selector = (key * 11 + 5) % 32;
        let active = key % 5 != 0;
        let category = (key * 7 + 1) % 16;
        if selector >= 8 || !active || category >= 8 {
            continue;
        }
        let group = (key % 64) as u64;
        let entry = expected.entry(group).or_insert((0_u64, 0_u128, 0_u128));
        entry.0 += 1;
        entry.1 += ((key * 3 + 7) % 1009) as u128;
        entry.2 += ((key * 5 + 3) % 97) as u128;
    }
    expected
}

fn validate_materialized(
    batches: &[RecordBatch],
    expected_rows: usize,
) -> AnyResult<(usize, usize, u64)> {
    let expected_names = [
        "join_key", "group_id", "value", "selector", "category", "weight", "active",
    ];
    let mut seen = vec![false; expected_rows];
    let mut observed_rows = 0_usize;
    let mut array_memory_bytes = 0_usize;
    for batch in batches {
        let names: Vec<&str> = batch
            .schema_ref()
            .fields()
            .iter()
            .map(|field| field.name().as_str())
            .collect();
        if names != expected_names {
            return Err(format!("unexpected materialized schema: {names:?}").into());
        }
        let columns: Vec<&UInt64Array> = batch
            .columns()
            .iter()
            .map(|column| {
                column
                    .as_any()
                    .downcast_ref::<UInt64Array>()
                    .ok_or("materialized column is not UInt64")
            })
            .collect::<Result<_, _>>()?;
        for row in 0..batch.num_rows() {
            let key = usize::try_from(columns[0].value(row))?;
            if key >= expected_rows || seen[key] {
                return Err(format!("invalid or duplicate materialized key {key}").into());
            }
            seen[key] = true;
            let expected = [
                key as u64,
                (key % 64) as u64,
                ((key * 3 + 7) % 1009) as u64,
                ((key * 11 + 5) % 32) as u64,
                ((key * 7 + 1) % 16) as u64,
                ((key * 5 + 3) % 97) as u64,
                u64::from(key % 5 != 0),
            ];
            if columns
                .iter()
                .enumerate()
                .any(|(column, values)| values.value(row) != expected[column])
            {
                return Err(format!("materialized row mismatch at key {key}").into());
            }
            observed_rows += 1;
        }
        array_memory_bytes = array_memory_bytes
            .checked_add(batch.get_array_memory_size())
            .expect("resident array memory size");
    }
    if observed_rows != expected_rows || seen.iter().any(|value| !value) {
        return Err(format!(
            "materialized row count mismatch: observed={observed_rows} expected={expected_rows}"
        )
        .into());
    }
    let mut digest = 0xcbf29ce484222325_u64;
    for key in 0..expected_rows {
        for value in [
            key as u64,
            (key % 64) as u64,
            ((key * 3 + 7) % 1009) as u64,
            ((key * 11 + 5) % 32) as u64,
            ((key * 7 + 1) % 16) as u64,
            ((key * 5 + 3) % 97) as u64,
            u64::from(key % 5 != 0),
        ] {
            digest = mix(digest, value);
        }
    }
    Ok((observed_rows, array_memory_bytes, digest))
}

fn cell(batch: &RecordBatch, column: usize, row: usize) -> AnyResult<String> {
    Ok(arrow::util::display::array_value_to_string(
        batch.column(column).as_ref(),
        row,
    )?)
}

fn verify_output(
    batches: &[RecordBatch],
    expected: &BTreeMap<u64, (u64, u128, u128)>,
) -> AnyResult<(u64, usize)> {
    let mut actual = BTreeMap::new();
    let mut observed_order = Vec::new();
    for batch in batches {
        let output_schema = batch.schema();
        let names: Vec<&str> = output_schema
            .fields()
            .iter()
            .map(|field| field.name().as_str())
            .collect();
        if names != ["group_id", "row_count", "value_sum", "weight_sum"] {
            return Err(format!("unexpected aggregate schema: {names:?}").into());
        }
        for row in 0..batch.num_rows() {
            let group: u64 = cell(batch, 0, row)?.parse()?;
            let values = (
                cell(batch, 1, row)?.parse::<u64>()?,
                cell(batch, 2, row)?.parse::<u128>()?,
                cell(batch, 3, row)?.parse::<u128>()?,
            );
            if actual.insert(group, values).is_some() {
                return Err(format!("duplicate aggregate group {group}").into());
            }
            observed_order.push(group);
        }
    }
    let expected_order: Vec<u64> = expected.keys().copied().collect();
    if observed_order != expected_order || &actual != expected {
        return Err(format!("aggregate mismatch: actual={actual:?} expected={expected:?}").into());
    }
    let mut digest = 0xcbf29ce484222325_u64;
    for (&group, &(count, value_sum, weight_sum)) in expected {
        digest = mix(digest, group);
        digest = mix(digest, count);
        digest = mix(digest, u64::try_from(value_sum)?);
        digest = mix(digest, u64::try_from(weight_sum)?);
    }
    Ok((digest, actual.len()))
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> AnyResult<()> {
    let mut arguments = env::args().skip(1);
    let batches_per_table = positive_argument(arguments.next(), "batches_per_table");
    let query_iterations = positive_argument(arguments.next(), "query_iterations");
    let target_partitions = positive_argument(arguments.next(), "target_partitions");
    assert!(arguments.next().is_none(), "unexpected extra argument");
    assert_eq!(ROUTABLE_BUFFER_BYTES, 24 * 1024);

    let total_started = Instant::now();
    let facts_schema = fact_schema();
    let dimensions_schema = dimension_schema();
    let mut fact_batches = Vec::with_capacity(batches_per_table);
    let mut dimension_batches = Vec::with_capacity(batches_per_table);
    for batch_index in 0..batches_per_table {
        fact_batches.push(fact_batch(&facts_schema, batch_index)?);
        dimension_batches.push(dimension_batch(&dimensions_schema, batch_index)?);
    }
    let rows_per_table = batches_per_table.checked_mul(ROWS_PER_BATCH).expect("row count");
    let fact_table = Arc::new(MemTable::try_new(facts_schema, vec![fact_batches])?);
    let dimension_table = Arc::new(MemTable::try_new(dimensions_schema, vec![dimension_batches])?);

    let config = SessionConfig::new()
        .with_target_partitions(target_partitions)
        .with_batch_size(ROWS_PER_BATCH)
        .with_enforce_batch_size_in_joins(true);
    let ctx = SessionContext::new_with_config(config);
    ctx.register_table("facts", fact_table.clone())?;
    ctx.register_table("dimensions", dimension_table.clone())?;

    // DataFusion performs the join and Arrow output materialization.  The
    // resulting batches, rather than driver-created seed columns, define the
    // resident lifetime cohort under study.
    let resident_batches = ctx.sql(MATERIALIZE_SQL).await?.collect().await?;
    assert!(!resident_batches.is_empty(), "materialization returned no batches");
    let (materialized_rows, resident_array_memory_bytes, materialized_digest) =
        validate_materialized(&resident_batches, rows_per_table)?;
    let resident_batch_count = resident_batches.len();
    let resident_payload_bytes = rows_per_table
        .checked_mul(7 * std::mem::size_of::<u64>())
        .expect("resident payload bytes");
    let resident_schema = resident_batches[0].schema();
    let resident_table = Arc::new(MemTable::try_new(
        resident_schema,
        vec![resident_batches],
    )?);
    ctx.register_table("resident_rows", resident_table.clone())?;
    drop(ctx.deregister_table("facts")?);
    drop(ctx.deregister_table("dimensions")?);
    drop(fact_table);
    drop(dimension_table);

    // Build the independent oracle once. Recomputing it inside the query loop
    // would add O(rows * iterations) allocator-independent work to the measured
    // section and dilute the DataFusion execution signal.
    let expected = expected_aggregates(rows_per_table);
    let resident_build_seconds = total_started.elapsed().as_secs_f64();
    let query_started = Instant::now();
    let mut query_digest = None;
    let mut result_digest = 0xcbf29ce484222325_u64;
    let mut query_output_rows = 0_usize;
    for iteration in 0..query_iterations {
        let output = ctx.sql(SQL).await?.collect().await?;
        assert!(!output.is_empty(), "fixed query returned no output batches");
        let (digest, output_rows) = verify_output(&output, &expected)?;
        if let Some(expected) = query_digest {
            assert_eq!(digest, expected, "query result changed across iterations");
        } else {
            query_digest = Some(digest);
            query_output_rows = output_rows;
        }
        result_digest = mix(result_digest, iteration as u64);
        result_digest = mix(result_digest, digest);
        black_box(&output);
    }
    assert_eq!(resident_table.batches.len(), 1);
    black_box((&ctx, &resident_table));
    let query_seconds = query_started.elapsed().as_secs_f64();
    let total_seconds = total_started.elapsed().as_secs_f64();
    println!(
        "@@RESULT_PREFIX@@{{\"schema_version\":1,\"correctness\":true,\"batches_per_table\":{},\"rows_per_batch\":{},\"rows_per_table\":{},\"table_count\":{},\"columns_per_table\":{},\"routable_buffer_bytes\":{},\"resident_payload_bytes\":{},\"resident_array_memory_bytes\":{},\"resident_batch_count\":{},\"materialized_rows\":{},\"materialized_digest\":\"{:016x}\",\"query_iterations\":{},\"target_partitions\":{},\"query_output_rows\":{},\"query_digest\":\"{:016x}\",\"result_digest\":\"{:016x}\",\"resident_build_seconds\":{:.9},\"query_seconds\":{:.9},\"total_seconds\":{:.9}}}",
        batches_per_table,
        ROWS_PER_BATCH,
        rows_per_table,
        TABLE_COUNT,
        COLUMNS_PER_TABLE,
        ROUTABLE_BUFFER_BYTES,
        resident_payload_bytes,
        resident_array_memory_bytes,
        resident_batch_count,
        materialized_rows,
        materialized_digest,
        query_iterations,
        target_partitions,
        query_output_rows,
        query_digest.expect("at least one query"),
        result_digest,
        resident_build_seconds,
        query_seconds,
        total_seconds,
    );
    Ok(())
}
'''


def generated_runner_source() -> str:
    return (
        WORKLOAD_SOURCE.replace("@@RESULT_PREFIX@@", RESULT_PREFIX).strip()
        + "\n\n"
        + campaign.allocator_instrumentation_source().strip()
        + "\n"
    )


def generated_manifest(*, datafusion_checkout: Path, allocator_snapshot: Path) -> str:
    del allocator_snapshot
    datafusion_core = datafusion_checkout / "datafusion/core"
    return (
        f'[package]\nname = "{RUNNER_PACKAGE}"\n'
        'version = "0.0.0"\nedition = "2024"\npublish = false\n\n'
        "[dependencies]\n"
        f"datafusion = {{ path = {json.dumps(str(datafusion_core.resolve()))}, "
        'default-features = false, features = ["sql"] }\n'
        f'arrow = "={ARROW_VERSION}"\n'
        f'tokio = {{ version = "={TOKIO_VERSION}", features = ["macros", "rt-multi-thread"] }}\n'
        "\n[workspace]\n"
    )


def _mix_digest(digest: int, value: int) -> int:
    return ((digest ^ value) * 0x100000001B3) & ((1 << 64) - 1)


@functools.lru_cache(maxsize=16)
def expected_aggregate_rows(rows_per_table: int) -> tuple[tuple[int, int, int, int], ...]:
    values: dict[int, list[int]] = {}
    for key in range(rows_per_table):
        selector = (key * 11 + 5) % 32
        active = key % 5 != 0
        category = (key * 7 + 1) % 16
        if selector >= 8 or not active or category >= 8:
            continue
        group = key % 64
        row = values.setdefault(group, [0, 0, 0])
        row[0] += 1
        row[1] += (key * 3 + 7) % 1009
        row[2] += (key * 5 + 3) % 97
    return tuple((group, *values[group]) for group in sorted(values))


def expected_digests(*, rows_per_table: int, query_iterations: int) -> tuple[str, str]:
    query_digest = 0xCBF29CE484222325
    rows = expected_aggregate_rows(rows_per_table)
    for row in rows:
        for value in row:
            query_digest = _mix_digest(query_digest, value)
    result_digest = 0xCBF29CE484222325
    for iteration in range(query_iterations):
        result_digest = _mix_digest(result_digest, iteration)
        result_digest = _mix_digest(result_digest, query_digest)
    return f"{query_digest:016x}", f"{result_digest:016x}"


@functools.lru_cache(maxsize=16)
def expected_materialized_digest(rows_per_table: int) -> str:
    digest = 0xCBF29CE484222325
    for key in range(rows_per_table):
        for value in (
            key,
            key % 64,
            (key * 3 + 7) % 1009,
            (key * 11 + 5) % 32,
            (key * 7 + 1) % 16,
            (key * 5 + 3) % 97,
            int(key % 5 != 0),
        ):
            digest = _mix_digest(digest, value)
    return f"{digest:016x}"


def parse_result_record(
    stdout: str,
    *,
    batches_per_table: int,
    query_iterations: int,
    target_partitions: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.startswith(RESULT_PREFIX):
            continue
        try:
            value = json.loads(line[len(RESULT_PREFIX) :])
        except json.JSONDecodeError as error:
            raise ContractError("DataFusion result record is invalid JSON") from error
        if isinstance(value, dict):
            rows.append(value)
    if len(rows) != 1:
        raise ContractError("DataFusion emitted an invalid number of result records")
    rows_per_table = batches_per_table * ROWS_PER_BATCH
    resident_payload_bytes = rows_per_table * 7 * 8
    query_digest, result_digest = expected_digests(
        rows_per_table=rows_per_table, query_iterations=query_iterations
    )
    expected = {
        "schema_version": SCHEMA_VERSION,
        "correctness": True,
        "batches_per_table": batches_per_table,
        "rows_per_batch": ROWS_PER_BATCH,
        "rows_per_table": rows_per_table,
        "table_count": TABLE_COUNT,
        "columns_per_table": COLUMNS_PER_TABLE,
        "routable_buffer_bytes": ROUTABLE_BUFFER_BYTES,
        "resident_payload_bytes": resident_payload_bytes,
        "materialized_rows": rows_per_table,
        "materialized_digest": expected_materialized_digest(rows_per_table),
        "query_iterations": query_iterations,
        "target_partitions": target_partitions,
        "query_output_rows": len(expected_aggregate_rows(rows_per_table)),
        "query_digest": query_digest,
        "result_digest": result_digest,
    }
    row = rows[0]
    if any(row.get(field) != value for field, value in expected.items()):
        raise ContractError("DataFusion fixed-work result does not match the command")
    for field in ("resident_array_memory_bytes", "resident_batch_count"):
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ContractError(f"DataFusion {field} is invalid")
    if int(row["resident_array_memory_bytes"]) < resident_payload_bytes:
        raise ContractError("DataFusion resident Arrow memory is smaller than its payload")
    for field in ("resident_build_seconds", "query_seconds", "total_seconds"):
        elapsed = row.get(field)
        if (
            not isinstance(elapsed, (int, float))
            or isinstance(elapsed, bool)
            or not math.isfinite(float(elapsed))
            or float(elapsed) <= 0
        ):
            raise ContractError(f"DataFusion {field} is invalid")
    if float(row["total_seconds"]) < (
        float(row["resident_build_seconds"]) + float(row["query_seconds"])
    ) * 0.99:
        raise ContractError("DataFusion phase timers are internally inconsistent")
    return row


def validate_fixed_work_match(
    observed: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    if any(
        observed.get(field) != expected.get(field)
        for field in FIXED_WORK_INVARIANT_FIELDS
    ):
        raise ContractError("DataFusion fixed work diverged across runtime arms")


def adaptive_thp_backing_gate(
    mechanism: Mapping[str, Any], smaps_samples: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    if not smaps_samples:
        raise ContractError("adaptive THP smoke has no procfs samples")
    procfs = campaign.summarize_proc_samples(smaps_samples)
    reasons: list[str] = []
    if int(mechanism.get("thp_advice_attempts", 0)) <= 0:
        reasons.append("no candidate crossed the runtime-confirmed density gate")
    if int(mechanism.get("thp_collapse_successes", 0)) <= 0:
        reasons.append("no synchronous THP collapse succeeded")
    if not procfs["anon_hugepages_observed"]:
        reasons.append("this adaptive run has no sampled physical anonymous THP backing")
    positive_samples = sum(int(row.get("anon_hugepages_kib", 0)) > 0 for row in smaps_samples)
    if positive_samples != len(smaps_samples):
        reasons.append(
            "adaptive THP backing is absent from one or more operation-window samples"
        )
    return {
        "passed": not reasons,
        "reasons": reasons,
        "sample_count": len(smaps_samples),
        "positive_backing_sample_count": positive_samples,
        "peak_anon_hugepages_kib": procfs["peak_anon_hugepages_kib"],
        "thp_advice_attempts": int(mechanism.get("thp_advice_attempts", 0)),
        "thp_collapse_successes": int(mechanism.get("thp_collapse_successes", 0)),
        "claim_boundary": (
            "this run-level gate only unlocks the explicit three-pair follow-on; "
            "each measured ordinary/THP process is gated separately"
        ),
    }


def measured_pair_backing_gate(
    ordinary_samples: Sequence[Mapping[str, Any]],
    thp_samples: Sequence[Mapping[str, Any]],
    *,
    ordinary_mechanism: Mapping[str, Any],
    thp_mechanism: Mapping[str, Any],
) -> dict[str, Any]:
    if not ordinary_samples or not thp_samples:
        raise ContractError("measured pair lacks procfs samples")
    ordinary_values = [int(row.get("anon_hugepages_kib", -1)) for row in ordinary_samples]
    thp_values = [int(row.get("anon_hugepages_kib", -1)) for row in thp_samples]
    reasons: list[str] = []
    if any(value < 0 for value in (*ordinary_values, *thp_values)):
        reasons.append("one or more procfs samples omit AnonHugePages")
    if any(value > 0 for value in ordinary_values):
        reasons.append("ordinary process has sampled anonymous THP backing")
    if not all(value > 0 for value in thp_values):
        reasons.append(
            "selective-THP backing is absent from one or more operation-window samples"
        )
    ordinary_attempts = int(ordinary_mechanism.get("thp_advice_attempts", 0))
    ordinary_collapses = int(ordinary_mechanism.get("thp_collapse_successes", 0))
    ordinary_mappings = int(ordinary_mechanism.get("thp_extent_mappings", 0))
    thp_attempts = int(thp_mechanism.get("thp_advice_attempts", 0))
    thp_collapses = int(thp_mechanism.get("thp_collapse_successes", 0))
    if ordinary_attempts or ordinary_collapses or ordinary_mappings:
        reasons.append("ordinary process reports a THP mechanism action")
    if thp_attempts <= 0:
        reasons.append("selective-THP process reports no advice attempt")
    if thp_collapses <= 0:
        reasons.append("selective-THP process reports no successful collapse")
    ordinary_positive_samples = sum(value > 0 for value in ordinary_values)
    thp_positive_samples = sum(value > 0 for value in thp_values)
    return {
        "passed": not reasons,
        "reasons": reasons,
        "ordinary_sample_count": len(ordinary_values),
        "thp_sample_count": len(thp_values),
        "ordinary_positive_backing_sample_count": ordinary_positive_samples,
        "thp_positive_backing_sample_count": thp_positive_samples,
        "ordinary_peak_anon_hugepages_kib": max(ordinary_values),
        "thp_peak_anon_hugepages_kib": max(thp_values),
        "ordinary_thp_advice_attempts": ordinary_attempts,
        "ordinary_thp_collapse_successes": ordinary_collapses,
        "ordinary_thp_extent_mappings": ordinary_mappings,
        "thp_advice_attempts": thp_attempts,
        "thp_collapse_successes": thp_collapses,
        "claim_boundary": (
            "both processes and every operation-window sample in this measured "
            "block are gated independently"
        ),
    }


LIVE_SURVIVAL_FIELD_PREFIXES = (
    "adaptive_live_survival_",
    "adaptive_survival_",
    "live_survival_",
)


def live_survival_counters(*records: Mapping[str, Any]) -> dict[str, int]:
    """Preserve live-survivor counters without binding to an in-flight schema."""
    counters: dict[str, int] = {}
    for record in records:
        for field, value in record.items():
            if field.startswith(LIVE_SURVIVAL_FIELD_PREFIXES):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ContractError(f"live-survival counter is invalid: {field}")
                if field in counters and counters[field] != value:
                    raise ContractError(f"live-survival counter disagrees: {field}")
                counters[field] = value
    return dict(sorted(counters.items()))


def validate_process_wide_routing(
    stats: Mapping[str, Any],
    sites: Sequence[Mapping[str, Any]],
    *,
    batches_per_table: int,
) -> dict[str, int]:
    """Prove that several process-wide GlobalAlloc sites entered the arena."""
    del batches_per_table
    unsupported = stats.get("unsupported_layout_bypasses")
    if isinstance(unsupported, bool) or not isinstance(unsupported, int):
        raise ContractError("unsupported-layout bypass telemetry is missing")
    target_rows = [row for row in sites if int(row.get("allocation_count", 0)) > 0]
    observed_allocations = sum(int(row["allocation_count"]) for row in target_rows)
    observed_requested_bytes = sum(
        int(row["allocation_requested_bytes"]) for row in target_rows
    )
    if len(target_rows) < 4:
        raise ContractError("DataFusion process produced fewer than four runtime sites")
    if observed_allocations < 1_024:
        raise ContractError("DataFusion process produced too few routed allocations")
    if any(
        int(row.get(field, 0)) == 0
        for row in target_rows
        for field in ("callsite", "type_id", "module_id")
    ):
        raise ContractError("DataFusion process has an incomplete exact identity")
    return {
        "site_count": len(target_rows),
        "allocation_count": observed_allocations,
        "allocation_requested_bytes": observed_requested_bytes,
        "unsupported_layout_bypasses": unsupported,
        "distinct_requested_size_count": len(
            {int(row["requested_size"]) for row in target_rows}
        ),
    }


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise ContractError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            + result.stderr.decode(errors="replace")[-8_000:]
        )
    return result


def verify_generated_lock(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    packages = document.get("package")
    if not isinstance(packages, list):
        raise ContractError("generated Cargo.lock has no package array")

    def exact(name: str, version: str) -> dict[str, Any]:
        matches = [
            row
            for row in packages
            if row.get("name") == name and row.get("version") == version
        ]
        if len(matches) != 1:
            raise ContractError(
                f"generated Cargo.lock must contain exactly {name} {version}"
            )
        return matches[0]

    datafusion = exact("datafusion", DATAFUSION_VERSION)
    if datafusion.get("source") is not None:
        raise ContractError("DataFusion resolved from a registry instead of the pinned path")
    arrow = exact("arrow", ARROW_VERSION)
    source = str(arrow.get("source", ""))
    checksum = str(arrow.get("checksum", ""))
    if not source.startswith("registry+") or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise ContractError("Arrow did not resolve as an exact checksummed registry package")
    runner = exact(RUNNER_PACKAGE, "0.0.0")
    if runner.get("source") is not None:
        raise ContractError("generated runner unexpectedly has an external source")
    cargo_unialloc = [row for row in packages if row.get("name") == "unialloc"]
    if cargo_unialloc:
        raise ContractError(
            "generated Cargo.lock contains UniAlloc in addition to the force-loaded rlib"
        )
    return {
        "verified": True,
        "sha256": sha256_file(path),
        "package_count": len(packages),
        "cargo_graph_unialloc_package_count": 0,
        "datafusion": {"version": datafusion["version"], "source": None},
        "arrow": {
            "version": arrow["version"],
            "source": arrow["source"],
            "checksum": arrow["checksum"],
        },
    }


def validate_dependency_compiler_scope(
    audit: Mapping[str, Any], compiler_sites: Mapping[str, Any]
) -> dict[str, Any]:
    """Require a multi-crate dependency audit and exclude the generated driver."""
    audited = {
        str(value).replace("-", "_") for value in audit.get("audited_crates", [])
    }
    expected = {value.replace("-", "_") for value in TARGET_CRATES}
    if audited != expected:
        raise ContractError(
            "DataFusion audited crate set does not exactly match the dependency allowlist"
        )
    if RUNNER_CRATE in audited:
        raise ContractError("generated DataFusion runner entered the compiler-claim scope")
    site_count = int(compiler_sites.get("site_count", 0))
    hinted = int(compiler_sites.get("applied_prior_hinted_count", 0))
    if site_count < 4:
        raise ContractError("DataFusion dependency compiler scope has fewer than four sites")
    if hinted <= 0:
        raise ContractError("DataFusion dependency compiler scope transported no lifetime prior")
    return {
        "target_crates": sorted(expected),
        "target_crate_count": len(expected),
        "generated_runner_excluded": True,
        "compiler_site_count": site_count,
        "applied_prior_hinted_count": hinted,
        "complete_join_key_count": int(compiler_sites.get("complete_join_key_count", 0)),
    }


def validate_dependency_compiler_runtime_join(
    exact_join: Mapping[str, Any],
) -> dict[str, Any]:
    """Require and summarize executed exact dependency lifetime priors."""
    rows = exact_join.get("rows")
    if not isinstance(rows, list):
        raise ContractError("DataFusion compiler/runtime join rows are missing")
    if exact_join.get("static_prior_join_success") is not True:
        raise ContractError("DataFusion static-prior compiler/runtime join failed")
    reported_matches = int(exact_join.get("matched_applied_prior_site_count", 0))
    if reported_matches <= 0:
        raise ContractError("DataFusion executed no exact applied lifetime prior")
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("matched") is True
        and row.get("applied_prior_hint") is True
        and isinstance(row.get("runtime"), dict)
        and int(row["runtime"].get("allocation_count", 0)) > 0
    ]
    if len(matches) != reported_matches:
        raise ContractError("DataFusion applied-prior join summary disagrees with its rows")
    return {
        "matched_site_count": len(matches),
        "matched_applied_prior_site_count": len(matches),
        "allocation_count": sum(int(row["runtime"]["allocation_count"]) for row in matches),
        "long_outcomes": sum(int(row["runtime"].get("long_outcomes", 0)) for row in matches),
        "short_outcomes": sum(int(row["runtime"].get("short_outcomes", 0)) for row in matches),
        "censored_outcomes": sum(
            int(row["runtime"].get("censored_outcomes", 0)) for row in matches
        ),
    }


def prepare_datafusion_compatible_allocator_snapshot(
    *,
    raw_dir: Path,
    checkout: Path,
    baseline: Mapping[str, Any],
    timeout: int,
) -> dict[str, Any]:
    """Copy the allocator snapshot and align only its exact libc build pin.

    DataFusion 54 resolves libc 0.2.186 while the production allocator snapshot
    intentionally pins 0.2.183. Cargo cannot place both semver-compatible libc
    patch releases in one graph. The shared campaign helper performs the
    provenance-preserving rewrite in a copied snapshot and leaves production
    inputs untouched.
    """
    previous = campaign.TARGET_SNAPSHOT_DEPENDENCY_REWRITES.get("datafusion")
    campaign.TARGET_SNAPSHOT_DEPENDENCY_REWRITES["datafusion"] = (
        DATAFUSION_ALLOCATOR_COMPATIBILITY_RULES
    )
    try:
        return campaign.prepare_target_compatible_allocator_snapshot(
            "datafusion",
            checkout=checkout,
            raw_dir=raw_dir,
            baseline=baseline,
            timeout=timeout,
        )
    finally:
        if previous is None:
            campaign.TARGET_SNAPSHOT_DEPENDENCY_REWRITES.pop("datafusion", None)
        else:
            campaign.TARGET_SNAPSHOT_DEPENDENCY_REWRITES["datafusion"] = previous


def build_lifetime_force_load(
    *, raw_dir: Path, snapshot: Mapping[str, Any], jobs: int, timeout: int
) -> dict[str, Any]:
    """Build the one UniAlloc rlib force-loaded into driver and dependencies."""
    build_root = raw_dir / "build/force-load-lifetime-hugepage"
    target_dir = build_root / "target"
    shutil.rmtree(build_root, ignore_errors=True)
    build_root.mkdir(parents=True)
    command = [
        "cargo",
        f"+{campaign.TOOLCHAIN}",
        "build",
        "--release",
        "--locked",
        "--manifest-path",
        str((Path(str(snapshot["path"])) / "unialloc/Cargo.toml").resolve()),
        "--lib",
        "--features",
        "lifetime_hugepage",
        "--target-dir",
        str(target_dir.resolve()),
        "--jobs",
        str(jobs),
    ]
    environment = os.environ.copy()
    environment["CARGO_INCREMENTAL"] = "0"
    result = campaign.matrix.execute(
        command, cwd=ROOT, env=environment, timeout=timeout
    )
    (build_root / "build.stdout").write_bytes(result["stdout"])
    (build_root / "build.stderr").write_bytes(result["stderr"])
    candidates = sorted((target_dir / "release/deps").glob("libunialloc-*.rlib"))
    if result["timed_out"] or result["exit_code"] != 0 or len(candidates) != 1:
        raise ContractError(
            "lifetime force-load rlib build failed:\n"
            + result["stderr"].decode(errors="replace")[-8_000:]
        )
    rlib = candidates[0].resolve()
    return {
        "success": True,
        "features": ["lifetime_hugepage"],
        "rlib": str(rlib),
        "rlib_sha256": sha256_file(rlib),
        "dependency_dir": str(rlib.parent),
        "command": command,
    }


def ensure_lifetime_force_load_wrapper(path: Path) -> Path:
    """Use a separate force-load allowlist while MIR targets exclude the driver."""
    source = campaign.matrix.FORCE_LOAD_WRAPPER_SOURCE.replace(
        'os.environ.get("UNIALLOC_RUSTC_TARGET_CRATES", "")',
        'os.environ.get("UNIALLOC_FORCE_LOAD_CRATES", "")',
    )
    source = source.replace(
        "selected = crate_name(arguments) in targets\nif selected:\n",
        "current_crate = crate_name(arguments)\n"
        "selected = current_crate in targets\n"
        "if not dependency_dir.is_dir():\n"
        "    fail(f\"missing force-load dependency directory: {dependency_dir}\")\n"
        "arguments.extend([\"-L\", f\"dependency={dependency_dir}\"])\n"
        f"if current_crate == {RUNNER_CRATE!r}:\n"
        "    arguments.extend([\"-C\", \"panic=abort\"])\n"
        "if selected:\n",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path.resolve()


def prepare_fresh_build_directories(paths: Sequence[Path]) -> None:
    """Remove inputs Cargo cannot fingerprint, then recreate empty directories."""
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True)


def build_runner(
    *,
    raw_dir: Path,
    checkout: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    wrapper: Path,
    jobs: int,
    timeout: int,
) -> dict[str, Any]:
    crate_root = raw_dir / "build-work" / "datafusion-runner"
    if crate_root.exists():
        shutil.rmtree(crate_root)
    (crate_root / "src").mkdir(parents=True)
    source = generated_runner_source()
    manifest = generated_manifest(
        datafusion_checkout=Path(str(checkout["checkout"])),
        allocator_snapshot=Path(str(snapshot["path"])),
    )
    source_path = crate_root / "src/main.rs"
    manifest_path = crate_root / "Cargo.toml"
    source_path.write_text(source, encoding="utf-8")
    manifest_path.write_text(manifest, encoding="utf-8")
    campaign.matrix.add_spin_patch(manifest_path, campaign.matrix.find_cached_spin())
    manifest = manifest_path.read_text(encoding="utf-8")

    audit_dir = raw_dir / "build/compiler-audits"
    pass_log_dir = raw_dir / "build/pass-logs"
    target_dir = raw_dir / "build/cargo-target"
    temporary_dir = raw_dir / "build/tmp"
    prepare_fresh_build_directories(
        (audit_dir, pass_log_dir, target_dir, temporary_dir)
    )
    force_load = build_lifetime_force_load(
        raw_dir=raw_dir, snapshot=snapshot, jobs=jobs, timeout=timeout
    )
    force_wrapper = ensure_lifetime_force_load_wrapper(
        raw_dir / "build/tools/unialloc-lifetime-force-load-wrapper"
    )
    environment = campaign._compiler_build_environment(
        "compiler-prior",
        wrapper=force_wrapper,
        audit_dir=audit_dir,
        pass_log_dir=pass_log_dir,
        target_dir=target_dir,
        target_crates=TARGET_CRATES,
        temporary_dir=temporary_dir,
    )
    environment["UNIALLOC_FORCE_LOAD_CRATES"] = ",".join(FORCE_LOAD_CRATES)
    environment = campaign.matrix.force_load_environment(
        environment, driver_wrapper=wrapper, force_load=force_load
    )
    # Marker-free owner/return analysis runs before MIR optimization. Direct
    # allocator-call replacement uses the optimized provider and would disable
    # that phase; this runner needs semantic scopes only.
    environment["UNIALLOC_ACTUAL_MIR_REWRITE"] = "0"
    build_environment = campaign.compiler_build_environment_provenance(environment)
    build_environment["actual_allocator_call_rewrite"] = False
    build_environment["marker_free_preoptimization_required"] = True
    _run_command(
        ["cargo", f"+{campaign.TOOLCHAIN}", "generate-lockfile"],
        cwd=crate_root,
        env=environment,
        timeout=timeout,
    )
    lock_path = crate_root / "Cargo.lock"
    if not lock_path.is_file():
        raise ContractError("generated DataFusion runner Cargo.lock is missing")
    lock = verify_generated_lock(lock_path)
    build_command = [
        "cargo",
        f"+{campaign.TOOLCHAIN}",
        "build",
        "--release",
        "--locked",
        "--message-format=json-render-diagnostics",
        "--jobs",
        str(jobs),
    ]
    build = campaign.matrix.execute(
        build_command,
        cwd=crate_root,
        env=environment,
        timeout=timeout,
    )
    build_dir = raw_dir / "build"
    (build_dir / "build.stdout").write_bytes(build["stdout"])
    (build_dir / "build.stderr").write_bytes(build["stderr"])
    if build["timed_out"] or build["exit_code"] != 0:
        raise ContractError(
            "DataFusion runner build failed:\n"
            + build["stderr"].decode(errors="replace")[-8_000:]
        )
    binary = Path(campaign._cargo_artifact(build["stdout"], RUNNER_PACKAGE))
    if not binary.is_file():
        raise ContractError("Cargo did not emit the DataFusion runner binary")

    arm = campaign.ARM_BY_NAME["force-track-compiler-prior-diagnostic"]
    audit = campaign.summarize_compiler_prior_audits(audit_dir)
    campaign.validate_build_audit_for_arm(arm, audit, TARGET_CRATES)
    compiler_sites = campaign.export_compiler_site_features(audit_dir)
    dependency_compiler_scope = validate_dependency_compiler_scope(audit, compiler_sites)
    compiler_sites_path = build_dir / "compiler-sites.json"
    campaign.write_json(compiler_sites_path, compiler_sites)
    record = {
        "success": True,
        "command": build_command,
        "cwd": str(crate_root.resolve()),
        "binary": str(binary.resolve()),
        "binary_sha256": sha256_file(binary),
        "source_path": str(source_path.resolve()),
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
        "cargo_lock_path": str(lock_path.resolve()),
        "cargo_lock_sha256": sha256_file(lock_path),
        "cargo_lock": lock,
        "compiler_environment": build_environment,
        "compiler_audit": audit,
        "dependency_compiler_scope": dependency_compiler_scope,
        "force_load": force_load,
        "force_load_crates": list(FORCE_LOAD_CRATES),
        "force_load_wrapper": str(force_wrapper),
        "force_load_wrapper_sha256": sha256_file(force_wrapper),
        "compiler_sites_path": str(compiler_sites_path.resolve()),
        "compiler_sites_sha256": sha256_file(compiler_sites_path),
        "compiler_sites": campaign.compiler_site_export_summary(compiler_sites),
        "target_crates": list(TARGET_CRATES),
        "rewrite_boundary": (
            "only pinned DataFusion/Arrow dependency crates are compiler-instrumented; "
            "the generated input driver is explicitly excluded"
        ),
    }
    campaign.write_json(build_dir / "build.json", record)
    return record


def run_command(
    binary: Path,
    *,
    batches_per_table: int,
    query_iterations: int,
    target_partitions: int,
) -> list[str]:
    return [
        str(binary),
        str(batches_per_table),
        str(query_iterations),
        str(target_partitions),
    ]


def conservative_query_window_samples(
    samples: Sequence[Mapping[str, Any]],
    *,
    fixed_work: Mapping[str, Any],
    process_wall_seconds: float,
) -> tuple[list[Mapping[str, Any]], dict[str, float]]:
    """Select the clock intersection guaranteed to lie inside the query phase.

    Procfs sampling starts before ``main`` while Rust timers start inside
    ``main``.  ``wall - query`` is therefore an upper bound on query start and
    ``resident_build + query`` is a lower bound on query end.  Their
    intersection conservatively excludes startup and shutdown without relying
    on an unmeasured clock offset.
    """
    resident_build = float(fixed_work["resident_build_seconds"])
    query = float(fixed_work["query_seconds"])
    lower = float(process_wall_seconds) - query
    upper = resident_build + query
    if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
        raise ContractError("DataFusion process and Rust phase clocks do not overlap")
    selected = [
        row
        for row in samples
        if lower <= float(row.get("elapsed_seconds", -1.0)) <= upper
    ]
    if not selected:
        raise ContractError("DataFusion query window emitted no conservative smaps samples")
    return selected, {
        "conservative_start_seconds": lower,
        "conservative_end_seconds": upper,
        "process_wall_seconds": float(process_wall_seconds),
    }


def run_one(
    *,
    raw_dir: Path,
    artifact_name: str,
    build: Mapping[str, Any],
    arm_name: str,
    batches_per_table: int,
    query_iterations: int,
    target_partitions: int,
    minimum_seconds: float,
    maximum_seconds: float,
    timeout: int,
    sample_interval: float,
) -> dict[str, Any]:
    binary = Path(str(build["binary"]))
    command = run_command(
        binary,
        batches_per_table=batches_per_table,
        query_iterations=query_iterations,
        target_partitions=target_partitions,
    )
    arm = campaign.ARM_BY_NAME[arm_name]
    environment = campaign._runtime_environment(arm)
    environment.update(
        {
            "RAYON_NUM_THREADS": str(target_partitions),
            "TOKIO_WORKER_THREADS": "2",
        }
    )
    artifact_dir = raw_dir / artifact_name
    process = campaign.execute_monitored_process(
        command,
        cwd=ROOT,
        env=environment,
        artifact_dir=artifact_dir,
        timeout=timeout,
        sample_interval=sample_interval,
    )
    stdout = Path(str(process["stdout_path"])).read_text(
        encoding="utf-8", errors="replace"
    )
    stderr = Path(str(process["stderr_path"])).read_text(
        encoding="utf-8", errors="replace"
    )
    if process["timed_out"] or process["exit_code"] != 0:
        raise ContractError(f"DataFusion {arm_name} process failed:\n" + stderr[-8_000:])
    if process["single_process_guard"].get("passed") is not True:
        raise ContractError("DataFusion workload spawned a descendant process")
    fixed_work = parse_result_record(
        stdout,
        batches_per_table=batches_per_table,
        query_iterations=query_iterations,
        target_partitions=target_partitions,
    )
    query_seconds = float(fixed_work["query_seconds"])
    if not minimum_seconds <= query_seconds <= maximum_seconds:
        raise ContractError(
            f"DataFusion query window {query_seconds:.3f}s is outside "
            f"{minimum_seconds:.1f}--{maximum_seconds:.1f}s"
        )
    stats = campaign.runtime_lifetime.parse_runtime_stats(stderr)
    if stats is None:
        raise ContractError("DataFusion process emitted no lifetime stats")
    sites = campaign.runtime_lifetime.parse_runtime_site_rows(stderr)
    campaign.validate_runtime_evidence(arm, stats, sites)
    process_wide_routing = None
    if arm.name != "default":
        process_wide_routing = validate_process_wide_routing(
            stats,
            sites,
            batches_per_table=batches_per_table,
        )
    fragmentation = campaign.parse_fragmentation(stderr)
    mechanism = campaign.parse_mechanism(stderr)
    samples = process["smaps_samples"]
    query_samples, query_window_bounds = conservative_query_window_samples(
        samples,
        fixed_work=fixed_work,
        process_wall_seconds=float(process["wall_seconds"]),
    )
    record = {
        **{key: value for key, value in process.items() if key != "smaps_samples"},
        "success": True,
        "performance_claim": False,
        "performance_claim_eligible": False,
        "runtime_arm": arm.name,
        "runtime_policy": stats["policy"],
        "fixed_work": fixed_work,
        "runtime_stats": stats,
        "runtime_site_summary": campaign.summarize_screening_sites(sites),
        "process_wide_routing": process_wide_routing,
        "fragmentation": fragmentation,
        "mechanism": mechanism,
        "live_survival_counters": live_survival_counters(stats, mechanism),
        "procfs": campaign.summarize_proc_samples(query_samples),
        "query_window_sample_count": len(query_samples),
        "query_window_clock_bounds": query_window_bounds,
        "runtime_sites_path": str((artifact_dir / "runtime-sites.json").resolve()),
        "smaps_samples_path": str(
            (artifact_dir / "query-window-smaps-samples.json").resolve()
        ),
    }
    campaign.write_json(artifact_dir / "runtime-stats.json", stats)
    campaign.write_json(artifact_dir / "runtime-sites.json", sites)
    campaign.write_json(artifact_dir / "mechanism.json", mechanism)
    campaign.write_json(artifact_dir / "smaps-samples.json", samples)
    campaign.write_json(artifact_dir / "query-window-smaps-samples.json", query_samples)
    campaign.write_json(artifact_dir / "run.json", record)
    return record


def run_ground_truth(
    *,
    raw_dir: Path,
    build: Mapping[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    record = run_one(
        raw_dir=raw_dir,
        artifact_name="ground-truth-ordinary",
        build=build,
        arm_name="force-track-compiler-prior-diagnostic",
        batches_per_table=args.batches_per_table,
        query_iterations=args.query_iterations,
        target_partitions=args.target_partitions,
        minimum_seconds=args.minimum_seconds,
        maximum_seconds=args.maximum_seconds,
        timeout=args.timeout,
        sample_interval=args.sample_interval,
    )
    sites = json.loads(Path(record["runtime_sites_path"]).read_text(encoding="utf-8"))
    compiler_export = json.loads(
        Path(str(build["compiler_sites_path"])).read_text(encoding="utf-8")
    )
    exact_join = campaign.join_compiler_runtime_sites(compiler_export, sites)
    dependency_exact_join = validate_dependency_compiler_runtime_join(exact_join)
    exact_join_path = raw_dir / "ground-truth-ordinary/compiler-runtime-exact-join.json"
    campaign.write_json(exact_join_path, exact_join)
    record["classification"] = "force-tracked-ordinary-ground-truth"
    record["stage_a_pressure_basis"] = "process_wide_requested_generation_bytes"
    record["production_pressure_basis"] = "eligible_exact_site_payload_capacity"
    record["opportunity_gate"] = campaign.opportunity_gate(
        record["runtime_site_summary"]
    )
    record["compiler_runtime_exact_join_path"] = str(exact_join_path.resolve())
    record["compiler_runtime_exact_join"] = campaign.compact_compiler_runtime_exact_join(
        exact_join
    )
    record["dependency_compiler_runtime_exact_join"] = dependency_exact_join
    campaign.write_json(raw_dir / "ground-truth-ordinary/run.json", record)
    return record


def run_adaptive_smoke(
    *,
    raw_dir: Path,
    build: Mapping[str, Any],
    ground_truth: Mapping[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    record = run_one(
        raw_dir=raw_dir,
        artifact_name="adaptive-thp-backing-smoke",
        build=build,
        arm_name="adaptive-selective-thp-compiler-prior",
        batches_per_table=args.batches_per_table,
        query_iterations=args.query_iterations,
        target_partitions=args.target_partitions,
        minimum_seconds=args.minimum_seconds,
        maximum_seconds=args.maximum_seconds,
        timeout=args.timeout,
        sample_interval=args.sample_interval,
    )
    validate_fixed_work_match(record["fixed_work"], ground_truth["fixed_work"])
    samples = json.loads(Path(record["smaps_samples_path"]).read_text(encoding="utf-8"))
    gate = adaptive_thp_backing_gate(record["mechanism"], samples)
    record.update(
        {
            "classification": "adaptive-thp-physical-backing-gate",
            "backing_gate": gate,
            "matched_campaign_allowed": gate["passed"],
        }
    )
    campaign.write_json(raw_dir / "adaptive-thp-backing-smoke/run.json", record)
    return record


def run_matched_pairs(
    *,
    raw_dir: Path,
    build: Mapping[str, Any],
    expected_fixed_work: Mapping[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    pairs: list[dict[str, Any]] = []
    for block in range(1, MATCHED_BLOCK_COUNT + 1):
        order = (
            ("adaptive-ordinary-compiler-prior", "adaptive-selective-thp-compiler-prior")
            if block % 2
            else ("adaptive-selective-thp-compiler-prior", "adaptive-ordinary-compiler-prior")
        )
        rows = {
            arm_name: run_one(
                raw_dir=raw_dir,
                artifact_name=f"matched/block-{block:02d}/{arm_name}",
                build=build,
                arm_name=arm_name,
                batches_per_table=args.batches_per_table,
                query_iterations=args.query_iterations,
                target_partitions=args.target_partitions,
                minimum_seconds=args.minimum_seconds,
                maximum_seconds=args.maximum_seconds,
                timeout=args.timeout,
                sample_interval=args.sample_interval,
            )
            for arm_name in order
        }
        ordinary = rows["adaptive-ordinary-compiler-prior"]
        thp = rows["adaptive-selective-thp-compiler-prior"]
        validate_fixed_work_match(ordinary["fixed_work"], expected_fixed_work)
        validate_fixed_work_match(thp["fixed_work"], expected_fixed_work)
        ordinary_samples = json.loads(
            Path(ordinary["smaps_samples_path"]).read_text(encoding="utf-8")
        )
        thp_samples = json.loads(
            Path(thp["smaps_samples_path"]).read_text(encoding="utf-8")
        )
        gate = measured_pair_backing_gate(
            ordinary_samples,
            thp_samples,
            ordinary_mechanism=ordinary["mechanism"],
            thp_mechanism=thp["mechanism"],
        )
        pairs.append(
            {
                "block": block,
                "order": list(order),
                "ordinary": ordinary,
                "selective_thp": thp,
                "backing_gate": gate,
                "query_ratio_thp_over_ordinary": (
                    float(thp["fixed_work"]["query_seconds"])
                    / float(ordinary["fixed_work"]["query_seconds"])
                ),
                "total_ratio_thp_over_ordinary": (
                    float(thp["fixed_work"]["total_seconds"])
                    / float(ordinary["fixed_work"]["total_seconds"])
                ),
                "wall_ratio_thp_over_ordinary": (
                    float(thp["wall_seconds"]) / float(ordinary["wall_seconds"])
                ),
                "peak_rss_ratio_thp_over_ordinary": (
                    int(thp["procfs"]["peak_rss_kib"])
                    / int(ordinary["procfs"]["peak_rss_kib"])
                ),
            }
        )
    all_backed = all(pair["backing_gate"]["passed"] for pair in pairs)
    query_ratios = [pair["query_ratio_thp_over_ordinary"] for pair in pairs]
    total_ratios = [pair["total_ratio_thp_over_ordinary"] for pair in pairs]
    wall_ratios = [pair["wall_ratio_thp_over_ordinary"] for pair in pairs]
    rss_ratios = [pair["peak_rss_ratio_thp_over_ordinary"] for pair in pairs]
    return {
        "schema_version": SCHEMA_VERSION,
        "classification": "preliminary-three-pair-mechanism-follow-on",
        "pair_count": MATCHED_BLOCK_COUNT,
        "pairs": pairs,
        "all_pairs_backing_verified": all_backed,
        "mechanism_contrast_backing_eligible": all_backed,
        "performance_claim_eligible": False,
        "presentation_claim_eligible": False,
        "median_query_ratio_thp_over_ordinary": statistics.median(query_ratios),
        "median_total_ratio_thp_over_ordinary": statistics.median(total_ratios),
        "median_wall_ratio_thp_over_ordinary": statistics.median(wall_ratios),
        "median_peak_rss_ratio_thp_over_ordinary": statistics.median(rss_ratios),
        "claim_boundary": (
            "three fresh-process pairs are a preliminary direction check; a "
            "preregistered larger matched campaign is required for a stable estimate"
        ),
    }


def evaluator_provenance() -> dict[str, Any]:
    paths = {
        "runner": Path(__file__).resolve(),
        "lifetime_campaign_helper": Path(campaign.__file__).resolve(),
        "runtime_export_helper": Path(campaign.runtime_lifetime.__file__).resolve(),
        "compiler_build_helper": Path(campaign.psr.__file__).resolve(),
        "matrix_helper": Path(campaign.matrix.__file__).resolve(),
    }
    files = {
        name: {"path": str(path), "sha256": sha256_file(path)}
        for name, path in paths.items()
    }
    return {
        "source": "unialloc-lifetime-resident-datafusion-evaluator-v1",
        "files": files,
        "digest": campaign.canonical_json_sha256(files),
    }


def plan(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign": "lifetime-resident-datafusion-v1",
        "execution_performed": False,
        "performance_claim": False,
        "source": {
            "repository": DATAFUSION_REPOSITORY,
            "version": DATAFUSION_VERSION,
            "commit": DATAFUSION_COMMIT,
            "arrow_version": ARROW_VERSION,
        },
        "fixed_work": {
            "one_process": True,
            "child_processes_allowed": False,
            "memtables": TABLE_COUNT,
            "batches_per_table": args.batches_per_table,
            "rows_per_batch": ROWS_PER_BATCH,
            "resident_payload_bytes": (
                args.batches_per_table
                * ROWS_PER_BATCH
                * 7
                * 8
            ),
            "routable_buffer_bytes": ROUTABLE_BUFFER_BYTES,
            "query_iterations": args.query_iterations,
            "target_partitions": args.target_partitions,
            "materialization": "DataFusion join projection retained as resident_rows",
            "query": "selective hash aggregate and ordered output over resident_rows",
            "compiler_target_crates": list(TARGET_CRATES),
            "generated_runner_compiler_target": False,
        },
        "stages": [
            "one force-tracked ordinary ground-truth process",
            "one adaptive THP smoke only after the opportunity gate",
            (
                "three matched ordinary/THP blocks only after physical backing"
                if args.matched_three_pairs
                else "matched campaign disabled"
            ),
        ],
        "evaluator_provenance": evaluator_provenance(),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=ROOT / "evaluation/raw/lifetime-resident-datafusion",
    )
    parser.add_argument(
        "--checkout-root",
        type=Path,
        default=ROOT / "evaluation/external/_checkouts",
    )
    parser.add_argument("--allocator-revision")
    parser.add_argument(
        "--batches-per-table", type=int, default=DEFAULT_BATCHES_PER_TABLE
    )
    parser.add_argument(
        "--query-iterations", type=int, default=DEFAULT_QUERY_ITERATIONS
    )
    parser.add_argument(
        "--target-partitions", type=int, default=DEFAULT_TARGET_PARTITIONS
    )
    parser.add_argument("--minimum-seconds", type=float, default=DEFAULT_MINIMUM_SECONDS)
    parser.add_argument("--maximum-seconds", type=float, default=DEFAULT_MAXIMUM_SECONDS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--jobs", type=int, default=min(16, os.cpu_count() or 1))
    parser.add_argument("--build-timeout", type=int, default=7_200)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-adaptive-thp-smoke", action="store_true")
    parser.add_argument("--matched-three-pairs", action="store_true")
    parser.set_defaults(performance_claim=False)
    args = parser.parse_args(argv)
    positive = (
        args.batches_per_table,
        args.query_iterations,
        args.target_partitions,
        args.minimum_seconds,
        args.maximum_seconds,
        args.timeout,
        args.sample_interval,
        args.jobs,
        args.build_timeout,
    )
    if any(value <= 0 for value in positive):
        parser.error("fixed-work and resource values must be positive")
    if not (
        args.minimum_seconds <= args.maximum_seconds <= DEFAULT_MAXIMUM_SECONDS
        and args.timeout <= campaign.HARD_PROCESS_CAP_SECONDS
    ):
        parser.error("duration boundary must stay within the 70-second screen")
    return args


def execute(args: argparse.Namespace) -> dict[str, Any]:
    raw_dir = args.raw_dir.resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    checkout = ensure_pinned_checkout(args.checkout_root.resolve())
    baseline_snapshot = campaign.psr.snapshot_allocator(
        raw_dir,
        revision=args.allocator_revision,
        current_working_tree=args.allocator_revision is None,
    )
    snapshot = prepare_datafusion_compatible_allocator_snapshot(
        raw_dir=raw_dir,
        checkout=Path(str(checkout["checkout"])),
        baseline=baseline_snapshot,
        timeout=args.build_timeout,
    )
    wrapper = campaign.psr.build_mir_driver(raw_dir, snapshot, args.build_timeout)
    build = build_runner(
        raw_dir=raw_dir,
        checkout=checkout,
        snapshot=snapshot,
        wrapper=wrapper,
        jobs=args.jobs,
        timeout=args.build_timeout,
    )
    ground_truth = run_ground_truth(raw_dir=raw_dir, build=build, args=args)

    adaptive_smoke = None
    opportunity = ground_truth["opportunity_gate"]
    if opportunity["passed"] and not args.skip_adaptive_thp_smoke:
        adaptive_smoke = run_adaptive_smoke(
            raw_dir=raw_dir,
            build=build,
            ground_truth=ground_truth,
            args=args,
        )

    matched = None
    if args.matched_three_pairs:
        if adaptive_smoke is None or not adaptive_smoke["backing_gate"]["passed"]:
            raise ContractError(
                "three-pair follow-on requested before a passing adaptive backing gate"
            )
        matched = run_matched_pairs(
            raw_dir=raw_dir,
            build=build,
            expected_fixed_work=ground_truth["fixed_work"],
            args=args,
        )
    result = {
        "schema_version": SCHEMA_VERSION,
        "campaign": "lifetime-resident-datafusion-v1",
        "success": True,
        "classification": "ground-truth-first-resident-heap-screen",
        "performance_claim": False,
        "mechanism_contrast_backing_eligible": bool(
            matched is not None and matched["mechanism_contrast_backing_eligible"]
        ),
        "performance_claim_eligible": False,
        "source": checkout,
        "allocator_snapshot": snapshot,
        "build": build,
        "ground_truth": ground_truth,
        "adaptive_thp_smoke": adaptive_smoke,
        "matched_three_pairs": matched,
        "evaluator_provenance": evaluator_provenance(),
    }
    campaign.write_json(raw_dir / "result.json", result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    raw_dir = args.raw_dir.resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        value = plan(args)
        campaign.write_json(raw_dir / "plan.json", value)
        print(json.dumps({"success": True, "plan": str(raw_dir / "plan.json")}, sort_keys=True))
        return 0
    try:
        result = execute(args)
    except Exception as error:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "campaign": "lifetime-resident-datafusion-v1",
            "success": False,
            "performance_claim": False,
            "error_type": type(error).__name__,
            "reason": str(error),
        }
        campaign.write_json(raw_dir / "failure.json", failure)
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "success": True,
                "result": str((raw_dir / "result.json").resolve()),
                "opportunity_gate": result["ground_truth"]["opportunity_gate"],
                "backing_gate": (
                    result["adaptive_thp_smoke"]["backing_gate"]
                    if result["adaptive_thp_smoke"] is not None
                    else None
                ),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
