# Historical G001 evaluation continuation status

> **Historical G001 note (superseded 2026-07-12).** This document preserves the
> paper-reproduction audit as historical context; it is no longer the active
> execution objective. The formal campaign stopped by explicit user objective
> change after 20 accepted source-bound records. Those records remain
> historical/diagnostic evidence and do not support a paper performance claim.
> G001 remains incomplete and is marked superseded, not complete. Active G002
> work is implementation-first: allocator/compiler correctness, runnable
> functional coverage, platform readiness, and profile-guided optimization.
> C001/C003/C004/C005 and the C006 performance percentage are
> `deferred_by_explicit_user_scope_change`; reduced benchmarks are diagnostic
> only. For current execution status use `.omx/ultragoal/goals.json`,
> `.omx/ultragoal/ledger.jsonl`, and the live repository HEAD rather than the
> historical queue below.

This is the compact historical continuation document for the superseded
paper-reproduction goal.  It records that audit's source identity, validation,
evidence posture, and remaining claim queue.  Per-run history belongs in `evaluation/raw/` and
`.omx/ultragoal/ledger.jsonl` rather than in this file.

Snapshot: 2026-07-09 after the final audit and fail-closed claim refresh.

## Historical G001 objective and source identity

- Durable goal `G001-users-hqzhao-downloads-rust-alloc-pa` remains
  `in_progress`.  The implementation audit did not complete or narrow it.  It
  is now superseded by G002, not complete; this subsection preserves the
  pre-supersession snapshot.
- Local continuation branch: `g001-paper-refactor-evaluation-20260709`.
- Pre-audit base: `341aed9aa77c` (`Update README.md`).
- Final local commit hashes are not duplicated here; use
  `git log --oneline --decorate` as the source of truth.
- Repository Rust pin: `nightly-2026-06-11`.
- This branch is local work.  Do not push it as part of the audit/cleanup pass.

The final claim check used repository source-fingerprint schema 2:

```text
source_digest = 222726711f3c31515d6073a060af45fc6d910a063af4db759fd877ee3989c08e
source_stable_during_claim_check = true
```

The digest is content-scoped rather than commit-scoped.  It includes source,
evaluation logic, and otherwise-gitignored local workload configurations that
affect execution.  It excludes documentation, generated evidence, build output,
external dependency/checkouts, and test-only evaluation scripts.  Therefore
these documentation edits do not invalidate the recorded source digest, while a
change to claim-affecting implementation or workload configuration does.

Refresh branch and source identity before a later handoff or evidence run:

```sh
git branch --show-current
git rev-parse HEAD
git status --short
cat rust-toolchain
```

## Evidence tiers

Use these tiers consistently:

1. **Current-source validation**: a build, test, or probe executed against the
   current source and exact toolchain, with command, result, and source identity.
   This proves implementation behavior, not a paper result.
2. **Historical or diagnostic evidence**: partial matrices, host-only smoke,
   fixture transcripts, imported legacy logs, and filtered performance rows.
   These remain useful for debugging but cannot close a paper claim.
3. **Source-bound claim-grade evidence**: complete required coverage, real
   target execution where required, reproducible provenance, verified artifact
   hashes, and a matching source fingerprint.  Only this tier closes a claim.

Marker-only transcripts, placeholder artifacts, fixture kernel trees,
unvalidated cached summaries, and host/target provenance mismatches remain
diagnostic even when their schemas are otherwise complete.

## Historical G001 provenance gates completed in this audit

The evaluation framework now fails closed across the claim publication path:

- compiler runtime evidence is bound to its manifest and repository source;
- saved compiler audits are compared with a freshly recomputed claim-critical
  snapshot, including semantic basis and evidence hashes;
- cached dataset metrics and claim state are recomputed from verified raw bytes
  and manifests rather than trusted as cached scalar values;
- claim-check publication detects repository changes between computation and
  write;
- compiler-package publication performs the same prepublication source-stability
  check;
- schema-2 source fingerprints include ignored local workload configurations
  while excluding test-only evaluator scripts;
- workload and Docker drivers classify only `nightly-2022-07-01` as the exact
  historical paper toolchain;
- platform evidence gates reject smoke-only, fixture, implausible-image,
  unrecognized-runner, and source-unbound entries.

These controls explain why previously reported C002 and C007 passes are now
correctly missing rather than silently reused.

## Historical G001 claim posture before supersession

This subsection preserves the fail-closed `2026-07-09` G001 snapshot.  It is not
the current G002 status: C002 functional/compiler evidence and C006 PAC
functionality are tracked separately, while the paper-performance claims are
`deferred_by_explicit_user_scope_change` rather than pass or fail.

`evaluation/results/claim_check_current.json` was generated at
`2026-07-09T21:15:35.066717Z` with `overall=fail`:

- seven claims are `missing`;
- zero claims pass;
- all six required claims are missing;
- optional C006 is also missing.

| Claim | Status | Remaining evidence |
| --- | --- | --- |
| C001 default performance | missing, required | Complete every required workload/allocator cell and publish a complete source-bound claim-grade manifest. |
| C002 compiler type coverage | missing, required | Recollect/package compiler coverage and runtime evidence with schema-2 source binding, fresh semantic snapshots, and complete declared SHA-256 values. Historical 430/430 coverage evidence remains diagnostic until republished. |
| C003 type-isolation cost | missing, required | Existing mechanism probes do not replace the complete type-isolation performance matrix. |
| C004 metadata-segregation speedup | missing, required | Complete the paper-shaped metadata-segregation workload/allocator matrix. |
| C005 hugepage-metadata speedup | missing, required | Combine real hugepage backing evidence with the complete performance matrix. |
| C006 PAC cost | missing, optional | Hardware PAC functionality is validated, but the optional cost matrix remains absent. |
| C007 cross-platform retargeting | missing, required | Recollect real, source-bound Windows, macOS, Rust-for-Linux, BlogOS, and Redox evidence; fixtures and smoke-only rows remain rejected. |

The refreshed `evaluation/results/overclaim_worklist.json` was generated at
`2026-07-09T21:15:35.849368Z`.  It contains seven actionable claims, 249 missing
requirements in total, and 210 missing requirements across required claims.

Historical G001 status inputs:

- `evaluation/results/claim_check_current.json`
- `evaluation/results/overclaim_worklist.json`
- `evaluation/results/compiler_coverage_evidence_audit.json`
- `evaluation/results/platform_matrix_audit.json`

## Validation completed for the historical G001 audit

The final audit validation used `nightly-2026-06-11` and completed:

- Python evaluator suite: 421 tests passed;
- Rust formatting: passed;
- hosted/default library surface: 604 tests passed;
- integration-test surface: 301 tests passed;
- std-bench surface: 430 tests passed;
- constrained `fixed_heap` configuration: 561 tests passed;
- metadata-segregation/type-isolation/stats/hugepage/PAC/force-init
  configuration: 686 tests passed;
- workspace all-target compile/no-run gate: passed;
- Clippy: exit status 0, with warnings retained as warnings;
- runtime smokes: `small_heap`, threaded `platform_allocator_workload`, real
  rustc-pass probes, and the PAC check passed.
- Rust-for-Linux bridge state regression: 3 tests passed, including partial
  initialization retry and foreign-ready allocator rejection.

Historical validation commands retained for provenance (not the active G002
execution plan):

```sh
cargo +$(cat rust-toolchain) fmt --all -- --check

cargo +$(cat rust-toolchain) test -p unialloc --lib

cargo +$(cat rust-toolchain) test -p unialloc --lib \
  --no-default-features \
  --features fixed_heap,type_isolation,stats

cargo +$(cat rust-toolchain) test -p unialloc --lib \
  --features metadata_segregation,type_isolation,stats,hugepage,pac,force_initialize

cargo +$(cat rust-toolchain) test --workspace --no-run

PYTHONPYCACHEPREFIX=/tmp/unialloc-pycache \
  python3 -m unittest discover -s evaluation/scripts -p 'test_*.py'

git diff --check
git diff --cached --check
```

Runtime smoke commands:

```sh
cargo +$(cat rust-toolchain) run -p unialloc --example small_heap \
  --no-default-features --features fixed_heap,type_isolation,stats

cargo +$(cat rust-toolchain) run -p unialloc \
  --example platform_allocator_workload \
  --features type_isolation,stats -- \
  --threads 4 --iters 64 --max-len 1024
```

Allocator `bench_*` selector features are mutually exclusive and must be tested
one at a time.  Cargo feature lists are comma-separated.  `fixed_heap` validation
uses `--no-default-features` so it is not mixed with hosted TLS/rseq defaults.
`git diff --check` does not inspect untracked files, so run
`git diff --cached --check` after intentional staging.

After a later claim-affecting source change, refresh both status artifacts with:

```sh
PYTHONPYCACHEPREFIX=/tmp/unialloc-pycache \
python3 evaluation/scripts/evaluate.py overclaim-worklist \
  --source current \
  --refresh-claim-check \
  --allow-incomplete
```

## Toolchain and disk outcome

Retained toolchains, each with a current purpose:

- `nightly-2026-06-11-aarch64-apple-darwin`: repository implementation and
  evaluation default;
- `nightly-2021-02-19-aarch64-apple-darwin`: RustPython workload route;
- `stable-aarch64-apple-darwin`: compatibility/support checks.

The unused floating nightly was removed.  `nightly-2022-07-01` is absent and is
available only as an on-demand historical paper-reproduction input.

Disk cleanup increased available space from about 18 GiB to about 64 GiB.  The
final cleanup additionally removed:

- about 1.6 GiB of workspace `target/` output;
- about 450 MiB of nested R-Polars target output;
- the 50 MiB downloaded Zig archive after extraction;
- about 90 MiB of rebuildable tcmalloc extracted/build state.

Preserved intentionally:

- `evaluation/raw/`: about 4.0 GiB of research/evidence artifacts;
- `evaluation/results/`: about 95 MiB of normalized audits/results;
- `.omx/ultragoal/`: about 76 MiB of durable goal evidence;
- extracted Zig toolchain: about 403 MiB, required for Windows cross-target work;
- canonical R-Polars CSV: about 466 MiB, required by the next evaluation route.

Do not delete unique raw evidence to gain space.  Rebuildable targets may be
cleaned again only after confirming no collector, compiler, emulator, benchmark,
or Docker process is using them.

## Historical G001 continuation queue (superseded; do not execute as G002 plan)

1. Recollect and republish C002 compiler coverage/runtime evidence under the
   schema-2 provenance and integrity gates.
2. Collect real C007 evidence for all five target environments; do not promote
   fixtures or smoke-only rows.
3. Complete the required C001, C003, C004, and C005 paper-shaped performance
   matrices.
4. Collect optional C006 PAC cost evidence after the required queue, or when a
   PAC-specific investigation needs it.
5. Refresh claim check/worklist after every claim-affecting source or evidence
   publication, review the results, and append the evidence to the durable
   ledger.

The stop condition remains the original G001 objective: a reproducible
current-source allocator and evaluation framework that supports the paper claims
without overclaiming.  G001 remains `in_progress`; a passing smoke, partial
matrix, clean commit, or local branch organization is not completion.
