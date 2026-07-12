# Evaluation Rust toolchains

This document defines toolchain provenance for UniAlloc evaluation.  The central
rule is that **repository default** and **paper exact** are different concepts.

Snapshot: 2026-07-12 after the implementation-first toolchain refresh.

## Canonical distinction

- Repository default: `nightly-2026-06-11`, read from `rust-toolchain`.
  It is the maintained compiler for current implementation work and is
  accepted-newer evidence, not exact historical paper-toolchain evidence.
- Paper exact: `nightly-2022-07-01`.
  It is used only for an explicitly requested historical reproduction and is
  paper-exact only when the effective toolchain equals this full dated name.
- A floating `nightly` alias is never paper-exact.  It may move without a source
  change; it is installed locally for compatibility checks but must not be used
  as paper-exact evidence.

Changing the repository default does not redefine the historical paper pin.

## Selection modes

| Request | Effective invocation | Provenance |
| --- | --- | --- |
| no flag / `repo` / `pinned` | `cargo +$(cat rust-toolchain) ...` | repo-default accepted-newer; currently `nightly-2026-06-11`; not paper-exact |
| `--rust-toolchain nightly-2026-06-11` | `cargo +nightly-2026-06-11 ...` | explicit accepted-newer; equivalent compiler to the current repo pin |
| `--rust-toolchain nightly-2022-07-01` | `cargo +nightly-2022-07-01 ...` | historical paper-exact, if installed |
| `--rust-toolchain stable` | `cargo +stable ...` | explicit non-repo compatibility evidence; not paper-exact |
| `--rust-toolchain system` / `none` | `cargo ...` | unpinned system toolchain; not paper-exact |
| `--rust-toolchain latest` / `nightly` | floating rustup nightly alias | explicit drifting override; locally installed, but never paper-exact |

`UNIALLOC_RUST_TOOLCHAIN` supplies the same workload-driver override.  Some
compiler probes use `--toolchain` rather than `--rust-toolchain`; pass the exact
dated value in either case.

## Provenance contract

Every performance record must distinguish at least:

- requested and effective toolchain;
- repository toolchain;
- selection source (`repo-rust-toolchain`, CLI, environment, or system);
- exact `rustc` and `cargo` versions;
- `paper_exact_toolchain`;
- accepted-newer/non-repo classification;
- claim-grade blockers.

`paper_exact_toolchain` is true only when the effective toolchain is exactly
`nightly-2022-07-01`.  Selecting the repository default is reproducible current
engineering evidence, but it must receive a non-paper-exact blocker for any
claim that requires historical-toolchain reproduction.

The canonical inner workload implementation is
`evaluation/scripts/paper_workload_driver.py`.  The inner workload and Docker
drivers now use the same exact comparison and preserve it in their records.
Matrix wrappers must continue forwarding that provenance without weakening it.
When auditing an older record, do not trust a boolean alone: compare the recorded
effective toolchain with the exact 2022 identifier.

Claim publication also binds toolchain evidence to repository fingerprint
schema 2.  The schema includes otherwise-gitignored local workload
configurations because they affect execution, but excludes documentation,
generated artifacts, and test-only evaluator scripts.  Claim/package
publication recomputes source identity immediately before writing so a source
change during a long run fails closed.

The std-bench list/build cache must also be keyed by the effective dated
toolchain and relevant source inputs.  A 2022 list or binary must not be reused
for the 2026 pin.

## Local inventory and disk policy

Current installed toolchains:

```text
stable-aarch64-apple-darwin
nightly-aarch64-apple-darwin
nightly-2021-02-19-aarch64-apple-darwin
nightly-2022-07-01-aarch64-apple-darwin
nightly-2026-06-11-aarch64-apple-darwin (active, default)
```

The repository pin resolves to:

```text
rustc 1.98.0-nightly (485ec3fbc 2026-06-10)
cargo 1.98.0-nightly (0b1123a48 2026-06-01)
```

Installed components on the 2026 pin include `rust-src`, `rustc-dev`,
`llvm-tools`, `rustfmt`, `clippy`, and the Redox standard library target used by
current probes.

The retained `nightly-2021-02-19` supports the RustPython workload route.
`nightly-2022-07-01` is installed and is also used by the bounded current-source
Oxipng compiler-rewrite functionality smoke.  That use proves compatibility with
the pinned compiler; it does not turn the reduced smoke into a paper-performance
reproduction.  The floating nightly is installed for compatibility only and its
drifting identity remains unsuitable for paper-exact evidence.

The complete cleanup pass increased available disk space from about 18 GiB to
about 64 GiB.  Its final rebuildable-output cleanup removed about 1.6 GiB of
workspace target output, 450 MiB of nested R-Polars target output, the 50 MiB
downloaded Zig archive, and about 90 MiB of tcmalloc extracted/build state.

The following were preserved deliberately:

- extracted Zig toolchain, about 403 MiB, for Windows cross-target validation;
- canonical R-Polars CSV, about 466 MiB, for the next workload route;
- `evaluation/raw/`, about 4.0 GiB;
- `evaluation/results/`, about 95 MiB;
- `.omx/ultragoal/`, about 76 MiB.

Inspect rather than assume:

```sh
cat rust-toolchain
rustup toolchain list
rustc +$(cat rust-toolchain) --version
cargo +$(cat rust-toolchain) --version
rustup component list --toolchain "$(cat rust-toolchain)-$(rustc -vV | sed -n 's/^host: //p')" --installed
```

## Recommended current-source commands

Use the exact repository pin for formatting, tests, builds, and ordinary
engineering probes.  The final audit passed formatting, 604 hosted/default
library tests, 301 integration tests, 561 constrained fixed-heap tests, 686
combined metadata/PAC/hugepage tests, the 430-test std-bench surface, and the
workspace no-run gate.  Clippy exited 0 with warnings retained as warnings.

Core commands:

```sh
cargo +$(cat rust-toolchain) fmt --all -- --check
cargo +$(cat rust-toolchain) test -p unialloc --lib

cargo +$(cat rust-toolchain) test -p unialloc --lib \
  --no-default-features \
  --features fixed_heap,type_isolation,stats

cargo +$(cat rust-toolchain) test -p unialloc --lib \
  --features metadata_segregation,type_isolation,stats,hugepage,pac,force_initialize

cargo +$(cat rust-toolchain) test --workspace --no-run
```

`fixed_heap` must remain isolated from hosted default features.  Allocator
`bench_*` selectors are mutually exclusive, and Cargo feature lists use commas.

Repo-default workload-driver dry run:

```sh
PYTHONPYCACHEPREFIX=/tmp/unialloc-pycache \
python3 evaluation/scripts/paper_workload_driver.py \
  --dataset default_performance \
  --benchmark Collections \
  --allocator unialloc \
  --bench-filter vec::bench_with_capacity_1000 \
  --dry-run \
  --timeout 300
```

Explicitly dated current-toolchain dry run:

```sh
PYTHONPYCACHEPREFIX=/tmp/unialloc-pycache \
python3 evaluation/scripts/paper_workload_driver.py \
  --dataset default_performance \
  --benchmark Collections \
  --allocator unialloc \
  --bench-filter vec::bench_with_capacity_1000 \
  --rust-toolchain "$(cat rust-toolchain)" \
  --dry-run \
  --timeout 300
```

Filtered rows and dry runs are smoke/diagnostic evidence even when their
toolchain provenance is correct.  They do not constitute a complete
paper-performance matrix.

## Historical paper reproduction

Only use the exact dated selection:

```sh
rustup toolchain install nightly-2022-07-01
PYTHONPYCACHEPREFIX=/tmp/unialloc-pycache \
python3 evaluation/scripts/paper_workload_driver.py \
  --dataset default_performance \
  --benchmark Collections \
  --allocator unialloc \
  --rust-toolchain nightly-2022-07-01 \
  --timeout 300
```

The legacy toolchain is now present, but it is not a standing prerequisite for
current-source tests.  The local R-Polars workload configuration names
`nightly-2022-07-01`; running that route still depends on its external workload
assets and compatibility checks.  A newer successful R-Polars run must be
labeled accepted-newer rather than paper-exact.

## Docker Collections runner

`evaluation/scripts/paper_collections_docker_driver.py` forwards the requested
toolchain to the inner workload driver and applies the same exact-2022
classification.  Keep build volumes separated by the effective dated
toolchain, for example:

```text
unialloc-collections-target-nightly-2022-07-01
unialloc-collections-target-nightly-2026-06-11
```

The image must contain the requested toolchain or be able to install it.  The
outer record does not promote the current repo pin to paper-exact; the inner
record's effective toolchain and blocker set remain the final provenance check.

## Platform retargeting probes

`collect-platform-smoke` accepts `--rust-toolchain` for one evaluator process.
Use an exact dated value rather than editing repository state:

```sh
PYTHONPYCACHEPREFIX=/tmp/unialloc-pycache \
python3 evaluation/scripts/evaluate.py collect-platform-smoke \
  --platforms redox \
  --rust-toolchain "$(cat rust-toolchain)" \
  --no-import \
  --timeout 180
```

A toolchain override changes only the build/check compiler.  It does not turn a
host smoke, target `cargo check`, marker transcript, or fixture image into real
target boot/runtime evidence.

## Current rustc_driver compatibility

The 2026 pin has `rustc-dev`, `rust-src`, and `llvm-tools` installed.  The MIR
rewrite path uses the `unialloc_rustc_current` compatibility surface for modern
nightlies while retaining the legacy source path for deliberate 2022 builds.
The legacy path is not considered currently verified while the 2022 toolchain
is absent.

Standalone current-path compile:

```sh
RUSTC_BOOTSTRAP=1 rustc +$(cat rust-toolchain) \
  --cfg unialloc_rustc_current \
  tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs \
  -o /tmp/unialloc-rustc-mir-rewrite-dry-run-current
```

Focused probes should likewise use the exact repo pin:

```sh
python3 evaluation/scripts/evaluate.py collect-rustc-driver-mir-semantic-scope-probe \
  --toolchain "$(cat rust-toolchain)" \
  --timeout 600

python3 evaluation/scripts/evaluate.py collect-rustc-driver-direct-allocator-mir-probe \
  --toolchain "$(cat rust-toolchain)" \
  --timeout 600

python3 evaluation/scripts/evaluate.py collect-rustc-driver-mir-cross-thread-hint-probe \
  --toolchain "$(cat rust-toolchain)" \
  --timeout 600
```

These are implementation/functionality gates, not the slow paper-performance
matrix.  Historical artifacts under `evaluation/raw/` remain useful porting
evidence but must not be represented as current-source results without a
matching source fingerprint.

## PAC and arm64e note

Rustup lists `arm64e-apple-darwin` but does not ship a prebuilt arm64e standard
library.  PAC evidence therefore separates three lanes: the host Rust allocator
passes through the safe software fallback and typed side-cache reuse; an external
arm64e C ABI probe observes hardware context binding and wrong-context rejection;
and the allocator has a dedicated `no_std` arm64e runtime route.  The last route
now uses `tools/pac-nostd-contract/Cargo.toml`, which depends on UniAlloc as a
normal dependency so std-only benchmark/test dependencies cannot enter its
target graph.  It builds `core`, `alloc`, and `panic_abort` from `rust-src`
instead of requiring a prebuilt arm64e standard library.

The allocator `no_std` runtime probe validates metadata authentication and typed
side-cache reuse, but it does not prove the optional C006 cost matrix.  Treat old
`evaluation/results/pac_metadata_direct_probe_audit.json` files as historical
unless they are rebound after source changes.
