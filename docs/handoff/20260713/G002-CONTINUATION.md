# G002 continuation handoff -- 2026-07-13

## Repository and goal state

- Branch: `g002-functional-correctness-20260713`
- Remote: `git@github.com:sslab-gatech/UniAlloc.git`
- Base HEAD: `364d786fe92f400f3cc36b11bf947af3b48f535c`
- Source state: validated commit candidate based on that HEAD
- Active goal: `G002-unialloc-functional-correctness-and`
- Continued objective: real-world Type Isolation coverage, performance, RSS,
  and bounded runtime optimization
- Superseded goal: `G001-users-hqzhao-downloads-rust-alloc-pa`

G002 remains the implementation-first goal. The G001 paper-performance matrix,
including the 168-cell/1008-run campaign, remains explicitly deferred. The 20
accepted G001 records are read-only historical diagnostics and support no
current-source allocator comparison or publication-grade percentage.

The copied OMX goal snapshot preserves the pre-completion `in_progress` state as
a historical aggregate. This handoff and `continuation-state.json` record the
validated completion state for the current working tree.
The local `.omx/` directory remains excluded because it contains runtime state
and reproducible/raw artifacts rather than required source.

## Current functional closure

The working tree closes the lifecycle-capacity and recovery-admission defects
documented by the earlier WIP handoff.

1. **Exact lifecycle history:** each secondary history bucket retains a recent
   eight-way window keyed by the exact address, with an independent epoch on
   every entry.
   A ninth distinct exact key displaces the oldest `Missing` record back to
   raw-only epoch-zero semantics. Exact keys and per-entry epochs prevent an
   unrelated same-home address from inheriting a stale-release rejection. A raw
   reclaim that presents a known generation and observes `Absent` is admitted as
   `Tracked`, preserving generation-aware ownership without keeping unbounded
   release history.
2. **Recovery and realloc ordering:** reallocating paths carry the observation
   made before recovery lookup through tag preflight, reclaim admission,
   mutation, rollback, and terminal release. Admission precedes observable
   counters, compiler-selector consumption, replacement publication, and old-
   record consumption. A release followed by exact-address reuse therefore
   cannot authorize a stale operation on the replacement generation.

The exact-generation ABA regressions cover all three public realloc surfaces:

- `GlobalAlloc::realloc`;
- `SemanticAlloc::realloc_with_split_metadata`;
- the split-metadata FFI realloc entry point.

Each regression pauses after an exact recovery lookup, releases generation A,
reuses the same address for generation B with a different identity, resumes the
stale realloc, and requires rejection without changing generation B's payload,
recovery record, fallback counters, or validation counters.

The reviewed realloc repairs also consume the old recovery record exactly once
after a successful in-place realloc, prevent unrecorded old storage from being
reattributed to a recovery-required active or compiler-auto scope, and compile
the alignment-changing grow path under `quarantine`. The local-tag regression
covers an active
`TYPE_ISOLATED | MEMORY_TAGGING` allocation with no recovery record. A corrupted
old tag is rejected before replacement publication; the old payload and record
state remain available for repair and retry.

Admission tokens bind their pointer, recovery metadata can be previewed without
mutating counters, compiler auto-metadata is consumed only after durable
admission, and zero-size/zero-old-layout realloc paths use the same ordering
rules.

## Final real-world Type Isolation diagnostic

The final matrix builds pinned ripgrep, fd, and Oxipng sources through native,
jemalloc `0.5.4`, mimalloc `0.1.25`, UniAlloc without semantic rewriting,
actual-MIR `typed_plain`, stats-free `typeiso_perf`, and statistics-enabled
`typeiso_coverage` routes. fd's native source already selects jemalloc `0.5.4`;
its native and explicit jemalloc rows are route controls.

All runs were pinned to physical CPU 6 and NUMA node 0. ripgrep and fd used full
inputs, two warmups, and 9 and 7 measured repetitions. Oxipng used its quick
input, one warmup, and 5 measured repetitions. Each cell below is
`median wall seconds / median peak RSS KiB`.

| Application | Native | jemalloc | mimalloc | UniAlloc | `typed_plain` | `typeiso_perf` | `typeiso_coverage` |
|---|---:|---:|---:|---:|---:|---:|---:|
| ripgrep | `0.086309096 / 5120` | `0.086702833 / 6144` | `0.088868793 / 11812` | `0.090066579 / 6144` | `0.092411192 / 6144` | `0.092586986 / 6144` | `0.094015251 / 6144` |
| fd | `0.136296730 / 6144` | `0.136304143 / 6144` | `0.129672706 / 18872` | `0.167541836 / 6144` | `0.464682945 / 6144` | `0.465833677 / 6144` | `0.491091350 / 6144` |
| Oxipng | `1.712608439 / 46400` | `1.727841580 / 49088` | `1.634668906 / 71180` | `1.736975509 / 44260` | `1.766903750 / 44508` | `1.754456338 / 44524` | `1.754579386 / 44640` |

`typeiso_coverage` timing is performance-ineligible because statistics are
enabled. Performance variants disable statistics. All variants produce the
same output hash within each application. The primary incremental comparison is
`typeiso_perf / typed_plain`; the native comparison includes the allocator,
compiler rewrite, recovery bookkeeping, and Type Isolation policy.

| Application | Type Isolation / typed plain | Incremental time | Type Isolation / native | End-to-end time | Typed / total allocation events | Event coverage |
|---|---:|---:|---:|---:|---:|---:|
| ripgrep | `1.001902302050` | `+0.190230%` | `1.072737292950` | `+7.273729%` | `2266/22725` | `9.97%` (`997` bp) |
| fd | `1.002476380966` | `+0.247638%` | `3.417790558878` | `+241.779056%` | `616262/831549` | `74.11%` (`7411` bp) |
| Oxipng | `0.992955240488` | `-0.704476%` | `1.024435182057` | `+2.443518%` | `9661/10368` | `93.18%` (`9318` bp) |

Type Isolation peak-RSS ratios versus typed plain / native are
`1.000000000000 / 1.200000000000` for ripgrep,
`1.000000000000 / 1.000000000000` for fd, and
`1.000359485935 / 0.959568965517` for Oxipng.

Coverage is typed allocation events divided by total allocation events for the
exact application revision and input. It is a per-application metric with no
source-line, Rust-type, byte, or universal-program denominator. Its denominator
differs from the paper's 72.17% result. This matrix is a source-bound diagnostic;
the original paper reproduction and publication-grade inference remain
deferred.

The measured artifact implementation-bundle SHA-256 is
`7e98e63ce2fbeccc361ea57bd26773ccdb02664b83d772f0475161c980c55929`.
The current matrix-runner implementation-bundle SHA-256 is
`94ede1223c7b348639a7041a40a5b1840a6840cc18b4dbcf7d0f7a6ef8bb2cf4`.
The runner changed only to reject stale or modified reused binaries after the
measurements; the allocator and MIR pass sources used by the measured binaries
are unchanged. The measured MIR pass source SHA-256 is
`ae7dd0da2368c670323287647c94ce5a90069b6f2e4a3d51b298d48cb9a5ac63`.
The authoritative result files are the three
`evaluation/raw/realworld-nightly-20260713/type-isolation-*-final/results-pinned-final.json`
artifacts.

Two final optimizations retain the lifecycle contract:

1. Hosted automatic-allocation records stay in their pointer-derived home shard
   and home overflow. A sticky legacy marker enables exhaustive search only for
   supported old or test-only non-home inline state; `fixed_heap` retains its
   bounded cross-shard inline behavior. Active-shard mask updates occur only on
   live-count transitions from zero to one and one to zero.
2. Generic raw allocation misses publish the authoritative strict lifecycle
   state once and complete semantic admission through an after-publication path.
   Cache hits and guarded mappings retain semantic publication, and raw backend
   alias rejection stays at the strict publication boundary.

On the pinned fd full input, the pre-optimization to final medians change from
`0.611635718 s` to `0.464682945 s` for `typed_plain` (`-24.026192%`) and from
`0.573481469 s` to `0.465833677 s` for `typeiso_perf` (`-18.770928%`). Both
remain at `6144 KiB` median peak RSS, and event coverage remains `74.11%`. The
native route moves by `-0.218373%`. This is a source-bound directional result
for the fd input, with no stable cross-workload speedup claim.

Ordinary compiler semantic scopes request conservative cross-thread recovery;
explicit `_local` scopes retain local placement. An exact historical `Absent`
observation owns a lease until admission finishes, eviction skips leased
entries, and observation drop releases the lease. The bounded history has eight
ways; simultaneous leases on all eight ways take the fail-stop availability
path.

## Verified allocator matrix

The allocator's private rseq self-registration tests must start with glibc's
automatic registration disabled:

```bash
GLIBC_TUNABLES=glibc.pthread.rseq=0 \
  cargo test -p unialloc --lib --features type_isolation -- --test-threads=1

GLIBC_TUNABLES=glibc.pthread.rseq=0 \
  cargo test -p unialloc --lib --features type_isolation

GLIBC_TUNABLES=glibc.pthread.rseq=0 \
  cargo test -p unialloc --lib --no-default-features \
  --features fixed_heap,type_isolation \
  -- --test-threads=1

GLIBC_TUNABLES=glibc.pthread.rseq=0 \
  cargo test -p unialloc --lib --no-default-features \
  --features fixed_heap,type_isolation

GLIBC_TUNABLES=glibc.pthread.rseq=0 \
  cargo test -p unialloc --lib --features type_isolation,quarantine \
  -- --test-threads=1

GLIBC_TUNABLES=glibc.pthread.rseq=0 \
  cargo test -p unialloc --lib --features type_isolation,quarantine

GLIBC_TUNABLES=glibc.pthread.rseq=0 cargo test
```

Observed results:

| Configuration | Execution | Result |
|---|---|---:|
| Hosted + Type Isolation | Serial | `732/732` passed |
| Hosted + Type Isolation | Parallel | `732/732` passed |
| `fixed_heap` + Type Isolation | Serial | `602/602` passed |
| `fixed_heap` + Type Isolation | Parallel | `602/602` passed |
| `quarantine` + Type Isolation | Serial | `738/738` passed |
| `quarantine` + Type Isolation | Parallel | `738/738` passed |
| Standard repository test | Default execution | Exit `0`; `semantic_std` `12/12`, `std_bench` `430/430` |

The default-parallel `semantic_std` suite also completed `30/30` repeated runs.

Linux rseq itself permits libc-managed registration. The remote host's default
registration conflicts only with the allocator's private self-registration
tests, so the explicit `GLIBC_TUNABLES` setting is their reproducible execution
contract. The production allocation hot path makes no rseq call. The final
real-world matrix retains default libc-managed rseq and sets no glibc tunable.

## Paper-independent execution

The evaluator doctor reports ready while the external `rust-alloc-paper`
checkout is absent. The quick end-to-end diagnostic passes `3/3`. After
installing `rustc-dev`, `rust-src`, and LLVM tools, the realistic multi-module
actual-rustc probe builds, runs, and validates. The Python evaluation suite most
recently passes `610/610` in the final verification run. The MIR pass helper
binary passes `19/19`, the pass-side Python discovery passes `80/80`, and the
real-world matrix harness passes `27/27`.
The paper checkout is not a G002 completion prerequisite. Commands that extract
paper text or reproduce paper tables retain an explicit paper dependency and
fail with a bounded missing-input result.

Publication-grade performance reproduction remains deferred. The final
three-application Type Isolation matrix is a current-source diagnostic for
implementation decisions.

## Claim boundary

The current evidence supports allocator functional correctness for the covered
hosted, `fixed_heap`, and `quarantine` paths; bounded exact-key lifecycle
history; exact-generation ABA rejection on the three tested realloc surfaces;
pre-mutation tag rejection for the tested alignment-changing path; standard-
library semantic tests; and one realistic multi-module actual-rustc probe.

The evidence leaves these claims open:

- universal UAF or double-free prevention;
- stale-address detection after an exact generation ages out of its eight-way
  history bucket;
- resistance to forged or compromised semantic metadata;
- universal compiler rewrite and lifecycle coverage;
- runtime closure on every external platform;
- publication-grade performance or paper percentage reproduction.

The real-world matrix adds exact-source per-application timing, RSS, and
allocation-event coverage diagnostics. It supplies no cross-workload aggregate,
universal compiler coverage, source/type/byte coverage, or paper-equivalent
denominator.

## Completed continuation gate

The final gate includes formatting and diff checks, workspace checks, scoped
lint, hosted/fixed/quarantine allocator matrices, the standard repository test,
the `610/610` Python suite, the repository CJK scan, the finite `430/430`
inventory, and the realistic actual-rustc probe. The final diff digest binds the
parent-to-commit source patch, including new files and excluding only
`continuation-state.json` so that the state can carry the digest.

## Historical source bindings

- `f8966ca` validates hidden `slice::IterMut<u8>.zip(Vec<u8>)` ownership
  transfer and fail-closed Drop provenance.
- `c820338` is the earlier WIP type-isolation lifecycle checkpoint and is not a
  completion point.
- `6b73a55` adds bounded diagnostic evaluation tooling and source-binding tests.
- `9072fb3` adds the fail-first regression for cold `Released` lifecycle
  capacity exhaustion.
- `364d786` records the cross-machine branch inventory and is the base HEAD for
  the current working tree.

The earlier clean source-bound Oxipng one-shot remains a historical functional
artifact documented in `docs/presentation-outline.md`; its source binding stays
unchanged. The new three-application matrix has separate current implementation
and pass digests. Neither artifact supports paper timing, universal compiler
coverage, or a publication-grade paper percentage.

## Local-only exclusions

Generated binaries, `.omx/` runtime state, external raw evaluation data,
credentials, and `target/` remain outside source control. The separate
preservation branches for the G001 freeze and no-std PAC runner remain historical
or WIP inputs and are not merged into G002 by this handoff.
