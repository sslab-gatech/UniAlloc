#!/usr/bin/env python3
"""Run a fail-closed resident Tantivy lifetime ground-truth and backing screen.

The workload keeps one in-memory index, one reader, and one searcher alive while
deterministic document batches are committed and queried.  This is an
The force-track process uses ordinary pages to export ground truth.  One
selective adaptive process then decides whether physical THP backing exists;
every performance claim field remains false.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import lifetime_prior_six_program_campaign as campaign  # noqa: E402


SCHEMA_VERSION = 1
TOPIC_COUNT = 64
TOP_DOC_LIMIT = 8
TANTIVY_REPOSITORY = "https://github.com/quickwit-oss/tantivy.git"
TANTIVY_VERSION = "0.26.1"
TANTIVY_COMMIT = "d8f4c0b703120ed98f06297724dc1522df6019b9"
TANTIVY_CHECKOUT_NAME = "lifetime-resident-tantivy-0.26.1"
RUNNER_PACKAGE = "unialloc-lifetime-resident-tantivy"
RUNNER_CRATE = RUNNER_PACKAGE.replace("-", "_")
TANTIVY_TARGET_CRATES = (
    "tantivy",
    "tantivy_stacker",
    "tantivy_common",
    "tantivy_columnar",
    "tantivy_query_grammar",
    "tantivy_bitpacker",
    "tantivy_tokenizer_api",
    # The pinned workspace package is literally `ownedbytes`; Cargo does not
    # expose it as `tantivy_ownedbytes`.
    "ownedbytes",
    "tantivy_sstable",
)
TARGET_CRATES = (RUNNER_CRATE, *TANTIVY_TARGET_CRATES)
RESULT_PREFIX = "UNIALLOC_LIFETIME_RESIDENT_TANTIVY="
FIXED_WORK_INVARIANT_FIELDS = (
    "batches",
    "documents_per_batch",
    "queries_per_batch",
    "documents_indexed",
    "commits",
    "query_phases",
    "query_evaluations",
    "matched_documents",
    "final_num_docs",
    "result_digest",
    "correctness",
)
# A controlled NoMergePolicy calibration completed 216 batches in 16 seconds
# on the selective ordinary arm. 256 keeps the same geometry and enters the
# preregistered 20--60s window without relying on background merge work.
DEFAULT_BATCHES = 256
DEFAULT_DOCUMENTS_PER_BATCH = 2_048
DEFAULT_QUERIES_PER_BATCH = 128
DEFAULT_MINIMUM_SECONDS = 20.0
DEFAULT_MAXIMUM_SECONDS = 60.0
DEFAULT_TIMEOUT_SECONDS = 600


class ContractError(RuntimeError):
    """A provenance, correctness, or measurement boundary failed closed."""


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
        "repository": TANTIVY_REPOSITORY,
        "source_ref": TANTIVY_VERSION,
        "source_commit": TANTIVY_COMMIT,
    }
    for field, value in expected.items():
        if identity.get(field) != value:
            raise ContractError(f"Tantivy {field} does not match the exact pin")
    if canonical_remote(str(identity.get("remote", ""))) != canonical_remote(
        TANTIVY_REPOSITORY
    ):
        raise ContractError("Tantivy checkout remote does not match the pinned repository")
    if identity.get("status") != "":
        raise ContractError("Tantivy checkout is dirty")
    if re.fullmatch(r"[0-9a-f]{40}", str(identity.get("source_tree", ""))) is None:
        raise ContractError("Tantivy source tree identity is missing")
    if re.fullmatch(
        r"[0-9a-f]{64}", str(identity.get("cargo_toml_sha256", ""))
    ) is None:
        raise ContractError("Tantivy Cargo.toml identity is missing")
    if identity.get("cargo_lock_tracked") is not False:
        raise ContractError("Tantivy upstream lockfile contract changed")


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
    checkout = checkout_root / TANTIVY_CHECKOUT_NAME
    if not (checkout / ".git").is_dir():
        checkout_root.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                TANTIVY_REPOSITORY,
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
                "Tantivy clone failed:\n"
                + result.stderr.decode(errors="replace")[-8_000:]
            )
        _git(checkout, "fetch", "--depth=1", "origin", TANTIVY_COMMIT)
        _git(checkout, "checkout", "--detach", TANTIVY_COMMIT)
    else:
        if _git(checkout, "status", "--short"):
            raise ContractError("refusing to alter a dirty Tantivy checkout")
        head = _git(checkout, "rev-parse", "HEAD")
        if head != TANTIVY_COMMIT:
            _git(checkout, "fetch", "--depth=1", "origin", TANTIVY_COMMIT)
            _git(checkout, "checkout", "--detach", TANTIVY_COMMIT)

    cargo_lock = checkout / "Cargo.lock"
    cargo_manifest = checkout / "Cargo.toml"
    if not cargo_manifest.is_file():
        raise ContractError("pinned Tantivy checkout lacks Cargo.toml")
    if cargo_lock.exists():
        raise ContractError("pinned Tantivy source unexpectedly gained Cargo.lock")
    manifest_text = cargo_manifest.read_text(encoding="utf-8")
    if re.search(r'^version\s*=\s*"0\.26\.1"\s*$', manifest_text, re.MULTILINE) is None:
        raise ContractError("pinned Tantivy manifest version changed")
    identity = {
        "repository": TANTIVY_REPOSITORY,
        "source_ref": TANTIVY_VERSION,
        "source_commit": _git(checkout, "rev-parse", "HEAD"),
        "source_tree": _git(checkout, "rev-parse", "HEAD^{tree}"),
        "remote": _git(checkout, "remote", "get-url", "origin"),
        "status": _git(checkout, "status", "--short"),
        "checkout": str(checkout.resolve()),
        "cargo_lock_tracked": False,
        "cargo_lock_sha256": None,
        "cargo_toml_sha256": sha256_file(cargo_manifest),
    }
    validate_checkout_identity(identity)
    return identity


WORKLOAD_SOURCE = r'''
use std::env;
use std::hint::black_box;
use std::time::Instant;
use tantivy::collector::{Count, TopDocs};
use tantivy::merge_policy::NoMergePolicy;
use tantivy::query::QueryParser;
use tantivy::schema::{Field, Schema, TantivyDocument, Value, INDEXED, STORED, TEXT};
use tantivy::{Index, IndexReader, IndexWriter, ReloadPolicy, Searcher};

const TOPIC_COUNT: usize = 64;
const TOP_DOC_LIMIT: usize = 8;
const WRITER_MEMORY_BYTES: usize = 50_000_000;

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

fn generated_document(doc_id: u64, phase: u64, id_field: Field, title: Field, body: Field) -> TantivyDocument {
    let topic = doc_id as usize % TOPIC_COUNT;
    let shard = (doc_id / TOPIC_COUNT as u64) % 97;
    let title_text = format!("topic{topic} resident phase{phase} shard{shard}");
    let body_text = format!(
        "stable generated document topic{topic} cohort{} shard{shard} alpha beta gamma delta epsilon",
        doc_id % 31,
    );
    let mut document = TantivyDocument::default();
    document.add_u64(id_field, doc_id);
    document.add_text(title, title_text);
    document.add_text(body, body_text);
    document
}

fn add_batch(
    writer: &IndexWriter,
    batch_index: usize,
    documents_per_batch: usize,
    id_field: Field,
    title: Field,
    body: Field,
) -> tantivy::Result<()> {
    let start = batch_index
        .checked_mul(documents_per_batch)
        .expect("document offset overflow");
    for offset in 0..documents_per_batch {
        let doc_id = (start + offset) as u64;
        writer.add_document(generated_document(
            doc_id,
            batch_index as u64,
            id_field,
            title,
            body,
        ))?;
    }
    Ok(())
}

fn query_phase(
    searcher: &Searcher,
    parser: &QueryParser,
    id_field: Field,
    batch_index: usize,
    queries_per_batch: usize,
    mut digest: u64,
) -> tantivy::Result<(u64, u64)> {
    let mut matched_documents = 0u64;
    for query_index in 0..queries_per_batch {
        let topic = (batch_index + query_index) % TOPIC_COUNT;
        let query = parser.parse_query(&format!("topic{topic}"))?;
        let (top_docs, count) = searcher.search(
            &*query,
            &(TopDocs::with_limit(TOP_DOC_LIMIT).order_by_score(), Count),
        )?;
        assert!(count > 0, "deterministic topic query returned no documents");
        matched_documents = matched_documents.saturating_add(count as u64);
        digest = mix(digest, batch_index as u64);
        digest = mix(digest, query_index as u64);
        digest = mix(digest, count as u64);
        digest = mix(digest, top_docs.len() as u64);
        for (score, address) in top_docs {
            assert!(score.is_finite(), "top-document score is not finite");
            let document: TantivyDocument = searcher.doc(address)?;
            let doc_id = document
                .get_first(id_field)
                .and_then(|field_value| field_value.as_value().as_u64())
                .expect("stored document lacks its deterministic id");
            assert_eq!(doc_id as usize % TOPIC_COUNT, topic);
            // Background segment merges can choose different equally-scored
            // members of TopDocs without changing the query result. Exercise
            // document retrieval and validate every returned document, while
            // keeping the correctness digest tied to deterministic counts.
            black_box((doc_id, score));
        }
    }
    Ok((matched_documents, digest))
}

fn main() -> tantivy::Result<()> {
    let mut arguments = env::args().skip(1);
    let batches = positive_argument(arguments.next(), "batches");
    let documents_per_batch = positive_argument(arguments.next(), "documents_per_batch");
    let queries_per_batch = positive_argument(arguments.next(), "queries_per_batch");
    assert!(arguments.next().is_none(), "unexpected extra argument");
    let expected_documents = batches
        .checked_mul(documents_per_batch)
        .expect("fixed document count overflow");

    let started = Instant::now();
    let mut schema_builder = Schema::builder();
    let id_field = schema_builder.add_u64_field("doc_id", INDEXED | STORED);
    let title = schema_builder.add_text_field("title", TEXT | STORED);
    let body = schema_builder.add_text_field("body", TEXT);
    let schema = schema_builder.build();
    let index = Index::create_in_ram(schema);
    let mut writer: IndexWriter =
        index.writer_with_num_threads(1, WRITER_MEMORY_BYTES)?;
    // Keep each deterministic commit as one resident immutable segment. This
    // removes allocator-dependent background-merge scheduling from matched
    // runs and deliberately models a resident read-heavy index snapshot set.
    writer.set_merge_policy(Box::new(NoMergePolicy));

    add_batch(&writer, 0, documents_per_batch, id_field, title, body)?;
    writer.commit()?;
    let reader: IndexReader = index
        .reader_builder()
        .reload_policy(ReloadPolicy::Manual)
        .try_into()?;
    let parser = QueryParser::for_index(&index, vec![title, body]);
    let mut searcher: Searcher = reader.searcher();
    assert_eq!(searcher.num_docs() as usize, documents_per_batch);
    let (mut matched_documents, mut digest) = query_phase(
        &searcher,
        &parser,
        id_field,
        0,
        queries_per_batch,
        0xcbf29ce484222325,
    )?;

    for batch_index in 1..batches {
        // The previous immutable Searcher remains live while the next batch is built.
        black_box(searcher.num_docs());
        add_batch(
            &writer,
            batch_index,
            documents_per_batch,
            id_field,
            title,
            body,
        )?;
        writer.commit()?;
        reader.reload()?;
        searcher = reader.searcher();
        assert_eq!(
            searcher.num_docs() as usize,
            (batch_index + 1) * documents_per_batch,
        );
        let phase = query_phase(
            &searcher,
            &parser,
            id_field,
            batch_index,
            queries_per_batch,
            digest,
        )?;
        matched_documents = matched_documents.saturating_add(phase.0);
        digest = phase.1;
    }

    writer.wait_merging_threads()?;
    reader.reload()?;
    searcher = reader.searcher();
    let final_num_docs = searcher.num_docs() as usize;
    assert_eq!(final_num_docs, expected_documents);
    digest = mix(digest, final_num_docs as u64);
    black_box((&index, &reader, &searcher, matched_documents));
    let elapsed_seconds = started.elapsed().as_secs_f64();
    println!(
        "@@RESULT_PREFIX@@{{\"schema_version\":1,\"correctness\":true,\"batches\":{},\"documents_per_batch\":{},\"queries_per_batch\":{},\"documents_indexed\":{},\"commits\":{},\"query_phases\":{},\"query_evaluations\":{},\"matched_documents\":{},\"final_num_docs\":{},\"result_digest\":\"{:016x}\",\"elapsed_seconds\":{:.9}}}",
        batches,
        documents_per_batch,
        queries_per_batch,
        expected_documents,
        batches,
        batches,
        batches * queries_per_batch,
        matched_documents,
        final_num_docs,
        digest,
        elapsed_seconds,
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


def generated_manifest(*, tantivy_checkout: Path, allocator_snapshot: Path) -> str:
    unialloc = campaign.matrix.cargo_path_dependency(
        allocator_snapshot / "unialloc", ("lifetime_hugepage",)
    )
    return (
        '[package]\nname = "unialloc-lifetime-resident-tantivy"\n'
        'version = "0.0.0"\nedition = "2024"\npublish = false\n\n'
        "[dependencies]\n"
        f"tantivy = {{ path = {json.dumps(str(tantivy_checkout.resolve()))} }}\n"
        + unialloc
        + "\n\n[workspace]\n"
    )


def parse_result_record(
    stdout: str,
    *,
    batches: int,
    documents_per_batch: int,
    queries_per_batch: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.startswith(RESULT_PREFIX):
            continue
        try:
            value = json.loads(line[len(RESULT_PREFIX) :])
        except json.JSONDecodeError as error:
            raise ContractError("Tantivy result record is invalid JSON") from error
        if isinstance(value, dict):
            rows.append(value)
    expected_documents = batches * documents_per_batch
    expected_queries = batches * queries_per_batch
    expected_matched = expected_matched_documents(
        batches=batches,
        documents_per_batch=documents_per_batch,
        queries_per_batch=queries_per_batch,
    )
    expected_digest = expected_correctness_digest(
        batches=batches,
        documents_per_batch=documents_per_batch,
        queries_per_batch=queries_per_batch,
    )
    if len(rows) != 1:
        raise ContractError("Tantivy emitted an invalid number of result records")
    row = rows[0]
    expected = {
        "schema_version": SCHEMA_VERSION,
        "correctness": True,
        "batches": batches,
        "documents_per_batch": documents_per_batch,
        "queries_per_batch": queries_per_batch,
        "documents_indexed": expected_documents,
        "commits": batches,
        "query_phases": batches,
        "query_evaluations": expected_queries,
        "matched_documents": expected_matched,
        "final_num_docs": expected_documents,
        "result_digest": expected_digest,
    }
    if any(row.get(field) != value for field, value in expected.items()):
        raise ContractError("Tantivy fixed-work result does not match the command")
    elapsed = row.get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed <= 0:
        raise ContractError("Tantivy workload elapsed time is invalid")
    return row


def _mix_digest(digest: int, value: int) -> int:
    return ((digest ^ value) * 0x100000001B3) & ((1 << 64) - 1)


def _query_match_count(total_documents: int, topic: int) -> int:
    quotient, remainder = divmod(total_documents, TOPIC_COUNT)
    return quotient + int(topic < remainder)


def expected_matched_documents(
    *, batches: int, documents_per_batch: int, queries_per_batch: int
) -> int:
    matched = 0
    for batch_index in range(batches):
        total_documents = (batch_index + 1) * documents_per_batch
        for query_index in range(queries_per_batch):
            topic = (batch_index + query_index) % TOPIC_COUNT
            matched += _query_match_count(total_documents, topic)
    return matched


def expected_correctness_digest(
    *, batches: int, documents_per_batch: int, queries_per_batch: int
) -> str:
    digest = 0xCBF29CE484222325
    for batch_index in range(batches):
        total_documents = (batch_index + 1) * documents_per_batch
        for query_index in range(queries_per_batch):
            topic = (batch_index + query_index) % TOPIC_COUNT
            count = _query_match_count(total_documents, topic)
            for value in (batch_index, query_index, count, min(TOP_DOC_LIMIT, count)):
                digest = _mix_digest(digest, value)
    digest = _mix_digest(digest, batches * documents_per_batch)
    return f"{digest:016x}"


def validate_fixed_work_match(
    observed: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    if any(
        observed.get(field) != expected.get(field)
        for field in FIXED_WORK_INVARIANT_FIELDS
    ):
        raise ContractError("Tantivy backing smoke fixed work diverged from ground truth")


def adaptive_thp_backing_gate(
    mechanism: Mapping[str, Any], procfs: Mapping[str, Any]
) -> dict[str, Any]:
    reasons: list[str] = []
    if mechanism["thp_advice_attempts"] <= 0:
        reasons.append("no candidate crossed the runtime-confirmed density gate")
    if mechanism["thp_collapse_successes"] <= 0:
        reasons.append("no synchronous THP collapse succeeded")
    if procfs["peak_anon_hugepages_kib"] <= 0:
        reasons.append("no sampled physical anonymous THP backing")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "sample_count": procfs["sample_count"],
        "peak_anon_hugepages_kib": procfs["peak_anon_hugepages_kib"],
        "thp_advice_attempts": mechanism["thp_advice_attempts"],
        "thp_collapse_successes": mechanism["thp_collapse_successes"],
        "claim_boundary": (
            "this single smoke only unlocks a later matched campaign; every measured "
            "pair still requires its own ordinary-zero/THP-positive backing check"
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


def build_runner(
    *,
    raw_dir: Path,
    checkout: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    wrapper: Path,
    jobs: int,
    timeout: int,
) -> dict[str, Any]:
    crate_root = raw_dir / "build-work" / "tantivy-runner"
    tantivy_source = raw_dir / "build-work" / "tantivy-source"
    if crate_root.exists():
        shutil.rmtree(crate_root)
    if tantivy_source.exists():
        shutil.rmtree(tantivy_source)
    campaign._copy_checkout(Path(str(checkout["checkout"])), tantivy_source)
    dependency = campaign._stage_a_dependency(snapshot)
    patched_manifests = campaign._inject_workspace_dependencies(
        tantivy_source,
        TANTIVY_TARGET_CRATES,
        dependency,
    )
    (crate_root / "src").mkdir(parents=True)
    source = generated_runner_source()
    manifest = generated_manifest(
        tantivy_checkout=tantivy_source,
        allocator_snapshot=Path(str(snapshot["path"])),
    )
    source_path = crate_root / "src" / "main.rs"
    manifest_path = crate_root / "Cargo.toml"
    source_path.write_text(source, encoding="utf-8")
    manifest_path.write_text(manifest, encoding="utf-8")
    campaign.matrix.add_spin_patch(manifest_path, campaign.matrix.find_cached_spin())
    manifest = manifest_path.read_text(encoding="utf-8")

    audit_dir = raw_dir / "build" / "compiler-audits"
    pass_log_dir = raw_dir / "build" / "pass-logs"
    target_dir = raw_dir / "build" / "cargo-target"
    temporary_dir = raw_dir / "build" / "tmp"
    for path in (audit_dir, pass_log_dir, target_dir, temporary_dir):
        path.mkdir(parents=True, exist_ok=True)
    environment = campaign._compiler_build_environment(
        "compiler-prior",
        wrapper=wrapper,
        audit_dir=audit_dir,
        pass_log_dir=pass_log_dir,
        target_dir=target_dir,
        target_crates=TARGET_CRATES,
        temporary_dir=temporary_dir,
    )
    build_environment = campaign.compiler_build_environment_provenance(environment)
    _run_command(
        ["cargo", f"+{campaign.TOOLCHAIN}", "generate-lockfile"],
        cwd=crate_root,
        env=environment,
        timeout=timeout,
    )
    lock_path = crate_root / "Cargo.lock"
    if not lock_path.is_file():
        raise ContractError("generated runner Cargo.lock is missing")
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
            "Tantivy runner build failed:\n"
            + build["stderr"].decode(errors="replace")[-8_000:]
        )
    binary = Path(campaign._cargo_artifact(build["stdout"], RUNNER_PACKAGE))
    if not binary.is_file():
        raise ContractError("Cargo did not emit the Tantivy runner binary")

    arm = campaign.ARM_BY_NAME["force-track-compiler-prior-diagnostic"]
    audit = campaign.summarize_compiler_prior_audits(audit_dir)
    campaign.validate_build_audit_for_arm(arm, audit, TARGET_CRATES)
    compiler_sites = campaign.export_compiler_site_features(audit_dir)
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
        "compiler_environment": build_environment,
        "compiler_audit": audit,
        "compiler_sites_path": str(compiler_sites_path.resolve()),
        "compiler_sites_sha256": sha256_file(compiler_sites_path),
        "compiler_sites": campaign.compiler_site_export_summary(compiler_sites),
        "target_crates": list(TARGET_CRATES),
        "tantivy_build_source": str(tantivy_source.resolve()),
        "injected_workspace_manifests": [
            {
                "path": str(Path(str(path)).resolve()),
                "sha256": sha256_file(Path(str(path))),
            }
            for path in patched_manifests
        ],
    }
    campaign.write_json(build_dir / "build.json", record)
    return record


def run_screen(
    *,
    raw_dir: Path,
    build: Mapping[str, Any],
    batches: int,
    documents_per_batch: int,
    queries_per_batch: int,
    minimum_seconds: float,
    maximum_seconds: float,
    timeout: int,
    sample_interval: float,
) -> dict[str, Any]:
    command = [
        str(build["binary"]),
        str(batches),
        str(documents_per_batch),
        str(queries_per_batch),
    ]
    arm = campaign.ARM_BY_NAME["force-track-compiler-prior-diagnostic"]
    environment = campaign._runtime_environment(arm)
    artifact_dir = raw_dir / "run"
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
        raise ContractError("Tantivy ground-truth process failed:\n" + stderr[-8_000:])
    if process["single_process_guard"].get("passed") is not True:
        raise ContractError("Tantivy workload spawned a descendant process")
    wall_seconds = float(process["wall_seconds"])
    if not minimum_seconds <= wall_seconds <= maximum_seconds:
        raise ContractError(
            f"Tantivy screen duration {wall_seconds:.3f}s is outside "
            f"{minimum_seconds:.1f}--{maximum_seconds:.1f}s"
        )
    output = parse_result_record(
        stdout,
        batches=batches,
        documents_per_batch=documents_per_batch,
        queries_per_batch=queries_per_batch,
    )
    stats = campaign.runtime_lifetime.parse_runtime_stats(stderr)
    if stats is None:
        raise ContractError("Tantivy process emitted no lifetime stats")
    sites = campaign.runtime_lifetime.parse_runtime_site_rows(stderr)
    campaign.validate_runtime_evidence(arm, stats, sites)
    fragmentation = campaign.parse_fragmentation(stderr)
    mechanism = campaign.parse_mechanism(stderr)
    if not process["smaps_samples"]:
        raise ContractError("Tantivy process emitted no smaps samples")
    compiler_export = json.loads(
        Path(str(build["compiler_sites_path"])).read_text(encoding="utf-8")
    )
    exact_join = campaign.join_compiler_runtime_sites(compiler_export, sites)
    site_summary = campaign.summarize_screening_sites(sites)
    record = {
        **{key: value for key, value in process.items() if key != "smaps_samples"},
        "success": True,
        "classification": "diagnostic-ground-truth-only",
        "performance_claim": False,
        "performance_claim_eligible": False,
        "production_accuracy_claim_eligible": False,
        "stage_a_pressure_basis": "process_wide_requested_generation_bytes",
        "production_pressure_basis": "eligible_exact_site_payload_capacity",
        "runtime_arm": arm.name,
        "runtime_policy": stats["policy"],
        "force_track": stats["adaptive_force_track_all"],
        "fixed_work": output,
        "runtime_stats": stats,
        "runtime_site_summary": site_summary,
        "opportunity_gate": campaign.opportunity_gate(site_summary),
        "fragmentation": fragmentation,
        "mechanism": mechanism,
        "procfs": campaign.summarize_proc_samples(process["smaps_samples"]),
        "runtime_sites_path": str((artifact_dir / "runtime-sites.json").resolve()),
        "compiler_runtime_exact_join_path": str(
            (artifact_dir / "compiler-runtime-exact-join.json").resolve()
        ),
        "compiler_runtime_exact_join": campaign.compact_compiler_runtime_exact_join(
            exact_join
        ),
        "smaps_samples_path": str((artifact_dir / "smaps-samples.json").resolve()),
    }
    campaign.write_json(artifact_dir / "runtime-stats.json", stats)
    campaign.write_json(artifact_dir / "runtime-sites.json", sites)
    campaign.write_json(artifact_dir / "mechanism.json", mechanism)
    campaign.write_json(artifact_dir / "compiler-runtime-exact-join.json", exact_join)
    campaign.write_json(artifact_dir / "smaps-samples.json", process["smaps_samples"])
    campaign.write_json(artifact_dir / "run.json", record)
    return record


def run_adaptive_thp_backing_smoke(
    *,
    raw_dir: Path,
    build: Mapping[str, Any],
    expected_fixed_work: Mapping[str, Any],
    batches: int,
    documents_per_batch: int,
    queries_per_batch: int,
    minimum_seconds: float,
    maximum_seconds: float,
    timeout: int,
    sample_interval: float,
) -> dict[str, Any]:
    command = [
        str(build["binary"]),
        str(batches),
        str(documents_per_batch),
        str(queries_per_batch),
    ]
    arm = campaign.ARM_BY_NAME["adaptive-selective-thp-compiler-prior"]
    environment = campaign._runtime_environment(arm)
    artifact_dir = raw_dir / "adaptive-thp-backing-smoke"
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
        raise ContractError("Tantivy adaptive THP smoke failed:\n" + stderr[-8_000:])
    if process["single_process_guard"].get("passed") is not True:
        raise ContractError("Tantivy adaptive THP smoke spawned a descendant process")
    wall_seconds = float(process["wall_seconds"])
    if not minimum_seconds <= wall_seconds <= maximum_seconds:
        raise ContractError(
            f"Tantivy adaptive THP smoke duration {wall_seconds:.3f}s is outside "
            f"{minimum_seconds:.1f}--{maximum_seconds:.1f}s"
        )
    output = parse_result_record(
        stdout,
        batches=batches,
        documents_per_batch=documents_per_batch,
        queries_per_batch=queries_per_batch,
    )
    validate_fixed_work_match(output, expected_fixed_work)
    stats = campaign.runtime_lifetime.parse_runtime_stats(stderr)
    if stats is None:
        raise ContractError("Tantivy adaptive THP smoke emitted no lifetime stats")
    sites = campaign.runtime_lifetime.parse_runtime_site_rows(stderr)
    campaign.validate_runtime_evidence(arm, stats, sites)
    fragmentation = campaign.parse_fragmentation(stderr)
    mechanism = campaign.parse_mechanism(stderr)
    if not process["smaps_samples"]:
        raise ContractError("Tantivy adaptive THP smoke emitted no smaps samples")
    procfs = campaign.summarize_proc_samples(process["smaps_samples"])
    backing_gate = adaptive_thp_backing_gate(mechanism, procfs)
    record = {
        **{key: value for key, value in process.items() if key != "smaps_samples"},
        "success": True,
        "classification": "physical-backing-gate-only",
        "performance_claim": False,
        "performance_claim_eligible": False,
        "matched_timing_campaign_allowed": backing_gate["passed"],
        "runtime_arm": arm.name,
        "fixed_work": output,
        "runtime_stats": stats,
        "runtime_site_summary": campaign.summarize_screening_sites(sites),
        "fragmentation": fragmentation,
        "mechanism": mechanism,
        "procfs": procfs,
        "backing_gate": backing_gate,
        "runtime_sites_path": str((artifact_dir / "runtime-sites.json").resolve()),
        "smaps_samples_path": str((artifact_dir / "smaps-samples.json").resolve()),
    }
    campaign.write_json(artifact_dir / "runtime-stats.json", stats)
    campaign.write_json(artifact_dir / "runtime-sites.json", sites)
    campaign.write_json(artifact_dir / "mechanism.json", mechanism)
    campaign.write_json(artifact_dir / "smaps-samples.json", process["smaps_samples"])
    campaign.write_json(artifact_dir / "run.json", record)
    return record


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
        "source": "unialloc-lifetime-resident-tantivy-evaluator-v1",
        "files": files,
        "digest": campaign.canonical_json_sha256(files),
    }


def plan(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign": "lifetime-resident-tantivy-ground-truth-v1",
        "classification": "diagnostic-ground-truth-only",
        "execution_performed": False,
        "performance_claim": False,
        "source": {
            "repository": TANTIVY_REPOSITORY,
            "version": TANTIVY_VERSION,
            "commit": TANTIVY_COMMIT,
        },
        "fixed_work": {
            "batches": args.batches,
            "documents_per_batch": args.documents_per_batch,
            "queries_per_batch": args.queries_per_batch,
            "documents_indexed": args.batches * args.documents_per_batch,
            "query_evaluations": args.batches * args.queries_per_batch,
            "one_persistent_process": True,
            "one_in_memory_index": True,
            "reader_and_searcher_resident_while_indexing": True,
            "background_merges_disabled": True,
            "resident_segment_count": args.batches,
            "deterministic_generated_documents": True,
        },
        "duration_boundary_seconds": {
            "minimum": args.minimum_seconds,
            "maximum": args.maximum_seconds,
            "timeout": args.timeout,
        },
        "runtime": {
            "arm": "force-track-compiler-prior-diagnostic",
            "policy": 8,
            "backing": "ordinary",
            "performance_claim_eligible": False,
        },
        "adaptive_thp_backing_smoke": {
            "enabled": not args.skip_adaptive_thp_smoke,
            "arm": "adaptive-selective-thp-compiler-prior",
            "single_sample": True,
            "performance_claim_eligible": False,
        },
        "evidence_required": [
            "exact source pin and clean tree",
            "compiler rewrite audit and exact site export",
            "runtime stats and exact sites",
            "compiler/runtime exact join",
            "fixed operation counts and correctness digest",
            "single-process guard",
            "smaps samples and fragmentation record",
        ],
        "evaluator_provenance": evaluator_provenance(),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=ROOT / "evaluation/raw/lifetime-resident-tantivy-ground-truth",
    )
    parser.add_argument(
        "--checkout-root",
        type=Path,
        default=ROOT / "evaluation/external/_checkouts",
    )
    parser.add_argument("--allocator-revision")
    parser.add_argument("--batches", type=int, default=DEFAULT_BATCHES)
    parser.add_argument(
        "--documents-per-batch", type=int, default=DEFAULT_DOCUMENTS_PER_BATCH
    )
    parser.add_argument(
        "--queries-per-batch", type=int, default=DEFAULT_QUERIES_PER_BATCH
    )
    parser.add_argument("--minimum-seconds", type=float, default=DEFAULT_MINIMUM_SECONDS)
    parser.add_argument("--maximum-seconds", type=float, default=DEFAULT_MAXIMUM_SECONDS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--sample-interval", type=float, default=0.5)
    parser.add_argument("--jobs", type=int, default=min(16, os.cpu_count() or 1))
    parser.add_argument("--build-timeout", type=int, default=3_600)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-adaptive-thp-smoke", action="store_true")
    parser.set_defaults(performance_claim=False)
    args = parser.parse_args(argv)
    if any(
        value <= 0
        for value in (
            args.batches,
            args.documents_per_batch,
            args.queries_per_batch,
            args.timeout,
            args.sample_interval,
            args.jobs,
            args.build_timeout,
        )
    ):
        parser.error("fixed-work and resource values must be positive")
    if not (
        20.0 <= args.minimum_seconds <= args.maximum_seconds <= 60.0
        and args.timeout <= campaign.HARD_PROCESS_CAP_SECONDS
    ):
        parser.error("duration boundary must stay within 20--60 seconds")
    return args


def execute(args: argparse.Namespace) -> dict[str, Any]:
    raw_dir = args.raw_dir.resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    checkout = ensure_pinned_checkout(args.checkout_root.resolve())
    snapshot = campaign.psr.snapshot_allocator(
        raw_dir,
        revision=args.allocator_revision,
        current_working_tree=args.allocator_revision is None,
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
    run = run_screen(
        raw_dir=raw_dir,
        build=build,
        batches=args.batches,
        documents_per_batch=args.documents_per_batch,
        queries_per_batch=args.queries_per_batch,
        minimum_seconds=args.minimum_seconds,
        maximum_seconds=args.maximum_seconds,
        timeout=args.timeout,
        sample_interval=args.sample_interval,
    )
    backing_smoke = None
    if not args.skip_adaptive_thp_smoke:
        backing_smoke = run_adaptive_thp_backing_smoke(
            raw_dir=raw_dir,
            build=build,
            expected_fixed_work=run["fixed_work"],
            batches=args.batches,
            documents_per_batch=args.documents_per_batch,
            queries_per_batch=args.queries_per_batch,
            minimum_seconds=args.minimum_seconds,
            maximum_seconds=args.maximum_seconds,
            timeout=args.timeout,
            sample_interval=args.sample_interval,
        )
    result = {
        "schema_version": SCHEMA_VERSION,
        "campaign": "lifetime-resident-tantivy-ground-truth-v1",
        "success": True,
        "classification": "diagnostic-ground-truth-only",
        "performance_claim": False,
        "performance_claim_eligible": False,
        "source": checkout,
        "allocator_snapshot": snapshot,
        "build": build,
        "run": run,
        "adaptive_thp_backing_smoke": backing_smoke,
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
            "campaign": "lifetime-resident-tantivy-ground-truth-v1",
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
                "wall_seconds": result["run"]["wall_seconds"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
