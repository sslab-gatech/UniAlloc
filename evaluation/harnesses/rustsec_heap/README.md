# Pinned RustSec heap-safety harnesses

This directory contains the repository-owned execution inputs for the expanded
RustSec heap-safety corpus. The authoritative machine-readable catalog is
`evaluation/config/rustsec_heap_harnesses.json`; the 40-advisory classification
manifest is `evaluation/config/rustsec_heap_security_corpus.json`.

## Inventory and counting rule

The current source-pinned inventory has **45 scenarios covering all 40 distinct
advisories (100% of the 40-case corpus)**:

| Source class | Scenarios | Distinct advisory cases |
|---|---:|---:|
| Pinned Rudra-PoC sources | 18 | 18 |
| Repository harness bundle | 27 | 22 |
| Total source-pinned inventory | 45 | 40 |

The 27 repository scenarios consist of 24 mechanically adapted
(`mechanical_adapter`) scenarios grounded in published, upstream, or advisory
sources and 3 derived (`derived_adapter`) scenarios. Two derived scenarios are
cross-identity reuse experiments; the third is a minimal calamine CFB adapter.
`mechanical_adapter` records provenance and adaptation; it makes no
verbatim-source claim.

Scenario and advisory counts are deliberately separate. RSH-017 contributes
two secp256k1 scenarios, RSH-018 contributes three arenavec scenarios, and
RSH-002 and RSH-008 each contribute a derived reuse experiment beside a
published-witness adapter. The classification corpus contains 40 advisories,
and every advisory now has a pinned source-ready path.

The harness catalog remains `claim_grade=false`; every scenario stays inside
that boundary. Source readiness supplies inputs for experiments. Security
efficacy requires reproduced vulnerable baselines, valid patched controls,
matched allocator arms, exact compiler/runtime coverage, repeated outcomes
where appropriate, and retained raw artifacts.

## Directory and integrity contract

Each `RSH-NNN/` directory contains one or more harness sources plus exact lock
files:

```text
RSH-NNN/
  Cargo.lock.vulnerable
  Cargo.lock.patched
  main.rs or named scenario sources
  patched.rs                 # when the control needs a harness override
  patches/*.patch            # when no upstream patched crate is available
```

The catalog binds every repository source, lock file, source override, and
local patch to a SHA-256 digest. It binds each archive-backed vulnerable or
patched crate input to an exact version, crates.io URL, byte count, archive
SHA-256 value, and upstream commit.
Cases without an upstream patched release reuse the pinned vulnerable archive
and add a scenario-specific hashed local patch. Local patch metadata records
the expected hashes of the resulting subject files. The audit also checks that
paths remain under the matching case directory and that catalog cases agree
with the 40-case corpus.

Some upstream reproducers require one shared mechanical subject adapter on both
archive arms. A `hashed_subject_patch` records the patch and each variant's
expected post-patch source hashes. The chttp adapter exposes the internal buffer
module and disables curl's default TLS features so the pinned slim build image
does not require unrelated OpenSSL headers. Both archive arms receive the same
two edits; the upstream chttp version change remains the only
vulnerable-versus-patched semantic difference.

Run the audit before materializing or executing a case:

```bash
python3 evaluation/scripts/audit_rustsec_heap_corpus.py
```

For source verification against local upstream snapshots:

```bash
python3 evaluation/scripts/audit_rustsec_heap_corpus.py \
  --rustsec-db /path/to/advisory-db \
  --rudra-poc /path/to/Rudra-PoC \
  --output evaluation/results/rustsec_heap_corpus_audit.json
```

## Local environment layout

The validated host uses `nightly-2026-06-11` with `rust-src`, `rustc-dev`,
`llvm-tools`, `miri`, `rustfmt`, and `clippy`, plus Clang 18 and Docker. Install
the pinned Rust components with:

```bash
rustup toolchain install nightly-2026-06-11 \
  --component rust-src \
  --component rustc-dev \
  --component llvm-tools \
  --component miri \
  --component rustfmt \
  --component clippy
```

RSH-031 additionally uses `nightly-2022-07-01` with `miri` and `rust-src` for
its historical layout-mismatch oracle. Old Miri setup requires `xargo 0.3.26`.
The runner keeps allocator diagnostic arms on `nightly-2026-06-11` and records
the effective toolchain and flag policy per arm.

The harness and experiment runners use this default verified archive cache:

```text
${XDG_CACHE_HOME:-$HOME/.cache}/unialloc/rustsec-heap/archives/
```

The current host keeps the pinned source snapshots under one adjacent cache
root, with stable symlinks for audit commands:

```text
${XDG_CACHE_HOME:-$HOME/.cache}/unialloc/rustsec-sources/
  advisory-db -> advisory-db-9f3e138091487e69144f536d36976e427a7a3307/
  rudra-poc   -> rudra-poc-6226dd030fffbed5601099cb0e24f73e4150a7f5/
```

This separates immutable source/archive inputs from ignored experiment outputs
under `evaluation/raw/<run-id>/`. The runner places compiler wrappers, the
single force-loaded UniAlloc rlib, per-arm targets, logs, and manifests inside
that run directory. Each run therefore owns its compiler/build artifacts
independently.

## Runner

Listing is the default action and performs no download or execution:

```bash
python3 evaluation/scripts/run_rustsec_heap_harness.py --action list
```

Materialize an exact project in an empty retained work directory:

```bash
python3 evaluation/scripts/run_rustsec_heap_harness.py \
  --action materialize \
  --scenario RSH-002-published \
  --variant vulnerable \
  --allow-download \
  --work-dir /tmp/unialloc-rsh-002-vulnerable
```

Materialization validates the crate archive's size and SHA-256, rejects unsafe
archive members, verifies and applies any cataloged patch file, copies the
hashed harness source and exact lock file, and emits
`materialization.json`. Omitting `--allow-download` makes a missing archive a
hard error; a previously cached archive is still verified before use.

Run a contained compile check:

```bash
python3 evaluation/scripts/run_rustsec_heap_harness.py \
  --action check \
  --scenario RSH-002-published \
  --variant vulnerable \
  --allow-download
```

The check path uses this digest-pinned compile-smoke image:

```text
rust:1.85-slim@sha256:9f841bbe9e7d8e37ceb96ed907265a3a0df7f44e3737d0b100e7907a679acb36
```

It fetches locked transitive dependencies first through the only container with
write access to the shared Cargo cache. The offline check mounts that cache
read-only, creates a disposable tmpfs `CARGO_HOME`, and invokes
`cargo check --locked` with networking disabled. Fetch and check each run in a
uniquely named container with a host wall-clock timeout and forced cleanup. Set
that deadline with `--build-timeout`. This proves materialization and compile
readiness under the smoke environment; it provides no oracle execution.

Run an ordinary native harness only after reviewing its source and oracle:

```bash
python3 evaluation/scripts/run_rustsec_heap_harness.py \
  --action run-native \
  --scenario RSH-016-advisory \
  --variant vulnerable \
  --allow-download \
  --execute-unsafe \
  --timeout 30
```

`run-native` requires both the action and the `--execute-unsafe` opt-in. The
runtime container uses no network, a read-only root filesystem, UID/GID 65534,
all capabilities dropped, `no-new-privileges`, a no-exec temporary filesystem,
CPU/memory/PID limits, and a hard deadline. The in-container deadline escalates
from `TERM` to `KILL` after two seconds; a host-side fallback force-removes the
named container if the Docker call exceeds its grace period. Fetch, offline
build, and runtime containers all use the same named-container timeout/cleanup
contract. Offline build receives the shared Cargo cache read-only. Only the
built binary directory reaches the runtime container, also read-only. The build
phase remains inside the Docker daemon trust boundary and processes historical
crate build logic; use a disposable VM when that boundary is insufficient.

## Matched allocator experiment runner

`evaluation/scripts/run_rustsec_heap_experiment.py` turns one catalog scenario
into system, raw UniAlloc, `typed_plain`, and `typeiso` arms for both vulnerable
and patched archives. Preflight performs materialization, source transformation,
allocator-topology validation, and toolchain checks without executing build
scripts or witnesses:

```bash
python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --action preflight \
  --scenario RSH-002-derived-reuse \
  --repetitions 2 \
  --cache "${XDG_CACHE_HOME:-$HOME/.cache}/unialloc/rustsec-heap" \
  --allow-download \
  --output-dir evaluation/raw/rustsec-heap-preflight
```

Host execution requires a second explicit action and opt-in:

```bash
python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --action run \
  --scenario RSH-002-derived-reuse \
  --repetitions 10 \
  --cache "${XDG_CACHE_HOME:-$HOME/.cache}/unialloc/rustsec-heap" \
  --allow-download \
  --output-dir evaluation/raw/rustsec-heap-rsh002 \
  --execute-unsafe
```

The system arm keeps the cataloged ASan/Miri oracle. The three allocator arms
use native execution and serve as allocator diagnostics. The Type Isolation
arms force-load one pinned UniAlloc rlib into both the historical subject crate
and the harness, record compiler audits for both targets, and reject a second
Cargo-resolved UniAlloc identity. Each Type Isolation arm build starts from a
reset target so compiler audits regenerate for the current source, lock, tool,
wrapper, and rlib fingerprints.

The runner detects subject-owned `#[global_allocator]` topology before
substitution. The `system` arm preserves the subject allocator. The
`unialloc`, `typed_plain`, and `typeiso` arms become expected unsupported arms
when injecting UniAlloc would create ambiguous or duplicate allocator ownership.
RSH-030 is the canonical case: the fixed subject intentionally owns MiMalloc, so
allocator-substitution arms are topology-unsupported while the system arm
remains the valid oracle path. RSH-028 has a separate dependency-topology
boundary: exact historical and current libc pins conflict in the direct
raw-UniAlloc route, while its force-loaded `typed_plain` and `typeiso` routes
remain supported.

Every arm records source and binary hashes, commands, allowlisted environment,
per-repetition logs, tool findings, native diagnostics, runtime statistics, and
`claim_grade=false`. Clean allocator runs must contain valid UniAlloc runtime
statistics. Early crashes, sanitizer findings, assertions, or timeouts record
runtime statistics as unavailable. Compiler audit pass status validates only
target-crate presence and force-load topology; critical vulnerable-site coverage
is a separate efficacy gate and keeps matched-arm mitigation claims ineligible
until validated. A clean exit remains an observation. It provides zero
mitigation evidence by itself. This runner executes historical crate build
logic and unsafe witnesses on the host, so claim-grade work belongs in a
disposable VM with the stronger boundary described above.

Run the complete repository-materializable matrix through the sweep wrapper:

```bash
CARGO_HOME="$HOME/.cache/unialloc/rustsec-heap/cargo" \
uv run python evaluation/scripts/run_rustsec_heap_sweep.py \
  --action run \
  --cache "$HOME/.cache/unialloc/rustsec-heap" \
  --output-dir evaluation/raw/rustsec-heap-full-sweep \
  --scenario-jobs 1 \
  --jobs 4 \
  --repetitions 2 \
  --build-timeout 1200 \
  --run-timeout 600 \
  --execute-unsafe
```

The wrapper requires a fresh complete matrix, binds resume records to all
relevant hashes, and checks input drift at the end. The 2026-07-14 run completed
27 scenarios, 216 terminal arms, 352 runtime executions, and 32 expected
compile-rejection arms with zero unexpected outcomes. Its eight planned
topology exclusions comprise the two raw-UniAlloc RSH-028 arms and the six
allocator-substitution RSH-030 arms. Every result remains `claim_grade=false`.

## Sanitizer, Miri, and environment-specific oracles

The native runner does not substitute for ASan or Miri. The catalog records the
required tool, cargo profile, Rust flags, environment variables, and vulnerable
and patched outcome oracles for each scenario. Sanitizer work is pinned to
`nightly-2026-06-11`, rustc `1.98.0-nightly (485ec3fbc 2026-06-10)`, on
`x86_64-unknown-linux-gnu`; compile smoke uses the separate Rust 1.85 image.

Keep sanitizer/Miri observations separate from matched allocator-efficacy
measurements because instrumentation can change allocation, layout, scheduling,
and failure timing. RSH-017 additionally requires Clang-instrumented C code,
RSH-021 requires Unix virtual memory behavior, RSH-030 has an
environment-sensitive alignment oracle, and RSH-032 requires
`-Copt-level=0 -Coverflow-checks=off -Zub-checks=no` before ASan flags so the
optimizer preserves the witness. RSH-030 intentionally retains 100,000
allocations of 4,096
bytes, approximately 391 MiB of payload plus allocator overhead, to preserve
the upstream witness shape; give that case at least the cataloged 1 GiB runtime
limit. A clean exit or generic signal provides zero security classification by
itself. Judge each run against the cataloged vulnerable and patched-control
oracles and retain the raw command, logs, exit status, binary hash, toolchain
identity, and allocator/compiler coverage artifacts.
