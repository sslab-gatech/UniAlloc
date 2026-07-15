# Compiler Heap-Lifetime Ground-Truth Pipeline

## Status

This document records the ground-truth-first design for learning which compiler
ownership features predict pressure-relative heap lifetime in real Rust
programs. The joiner, synthetic regressions, and source-bound ripgrep, fd, and
Oxipng force-track campaign are complete. The machine-readable
[`ground-truth-summary.json`](evidence/compiler-lifetime-ground-truth-20260715/ground-truth-summary.json)
and the rendered
[`ground-truth-analysis.md`](evidence/compiler-lifetime-ground-truth-20260715/ground-truth-analysis.md)
contain the preserved evidence.

Current result status: **force-track verified with 100% tracked-allocation and
static-identity join coverage; the observed features provide exploratory
associations and support continued classifier abstention. A production Long
heuristic remains unsubstantiated.**

The controlled Drop/forget/leak fixture remains bounded mechanism evidence in
`docs/compiler-heap-lifetime-inference.md`. It does not supply the labels used
by this pipeline.

## Core decision

Rust ownership facts become classifier inputs. Runtime allocation survival
supplies the labels.

- Exact local or move-chain Drop is an Unknown lifetime fact. It proves an
  eventual owner release path and carries no Short-duration claim.
- Exact `mem::forget` and `Box::leak` provide bounded process-long oracle cases
  for mechanism validation. The compiler transports these cases as `0xA102`;
  real-program Long discovery still comes from runtime outcomes.
- Runtime completed outcomes determine pressure-relative Short and Long labels.
- Completed outcomes inside the indeterminate age interval remain
  indeterminate.
- Objects still live at snapshot time remain right-censored and contribute only
  age lower bounds.
- Allocations bypassed by sampling have no outcome label.

This separation prevents compiler syntax from becoming its own ground truth.
Adaptive sampled runs estimate the policy-selected tracked stream. Population
prevalence and classifier-accuracy claims use the evaluation-only force-track-
all mode described below.

## Compiler/runtime split

Rust source lifetimes describe reference validity and ownership ordering. They
leave unspecified how many bytes of later allocation pressure an owned heap
object survives. A local in `main`, a loop-carried owner, and a small
helper-local can all have an exact Drop path while spanning radically different
amounts of allocator pressure.

The compiler should therefore supply semantic structure that would otherwise
require extra allocator instrumentation:

- exact allocation identity and concrete layout when proven;
- exact/conditional/cleanup Drop topology and owner moves;
- return, store, escape, loop, backedge, receiver, and yield/await evidence;
- in a later cohort policy, a stable owner/drop-scope identifier for grouping
  objects likely to retire together.

The runtime layer can stay thin. It validates pressure-relative survival at
site grain, corrects static priors, observes current extent density, and enables
THP only for dense Long candidates. Bounded mechanism-oracle cases can use
compiler-directed cohorts immediately; production uncertain cases receive
sampled validation. This split uses Rust ownership information directly while
retaining the dynamic evidence required for page-placement decisions.

## Runtime evidence contract

The allocator exports ABI v1 through:

```rust
lifetime_hugepage_adaptive_site_snapshot(
    &mut [LifetimeAdaptiveSiteSnapshot],
) -> usize
```

Callers allocate
`LIFETIME_ADAPTIVE_SITE_SNAPSHOT_CAPACITY` rows before the workload and verify
the returned required count fits the buffer. Truncated snapshots fail the
analysis.

Claim-bearing label collection additionally enables:

```rust
lifetime_hugepage_adaptive_site_force_track_all_enable(
    1_000_000,
    4 * 1024 * 1024 * 1024,
)
```

This evaluation-only submode preserves the production `AdaptiveSiteKey` and
learned predictor unchanged, including flags, placement hint, rounded payload
capacity, and alignment. A separate `AdaptiveObservationKey` groups exact
observations by requested layout; dynamic-size callsites therefore retain one
production learning identity while exporting distinct size rows. Every
trackable exact allocation uses ordinary-page trailer geometry regardless of
the learned placement prediction. The fixed allocation-count and
requested-byte guards bound collection overhead. A guard bypass, site-table
loss, observation-table loss, mapping failure, `MADV_NOHUGEPAGE` failure, or
any THP mapping makes the label-collection run fail closed.

Force-track-all also replaces the predictor-sampled age clock with a
process-wide requested-byte pressure clock. Every successful allocation
generation advances this clock before label-admission checks, including exact
semantic allocations, semantic allocations with missing identity, unsupported
layouts, allocations after a collection guard is reached, and raw
`GlobalAlloc` allocations. A successful address-moving reallocation starts a
new allocation generation and advances the clock by its new requested size:
semantic moves re-enter semantic allocation, and raw moves use the dedicated
raw-reallocation hook. An in-place reallocation preserves the existing
generation. Pressure-only events age already tracked objects while producing
no fabricated exact site row. Dedicated total, raw-allocation, and
raw-moving-reallocation counters make clock coverage auditable.

Each exported observation row uses the separate exact runtime grain:

```text
(callsite, type_id, module_id, requested_size, align)
```

The snapshot records:

- minimum/maximum allocator payload capacity, allocation count, requested
  bytes, allocator payload bytes, tracked allocations, and bypassed
  allocations;
- completed Short, Long, and indeterminate outcomes with requested-byte totals;
- cumulative and maximum completed allocation-pressure age;
- live tracked objects with a cumulative pressure-age lower bound;
- first/last allocation pressure, latest adaptive prediction and static prior;
- whether multiple production predictor keys contributed to the exact runtime
  row, plus observed predictor flags and the placement-hint bit union.

The joiner fails closed unless the following accounting identities hold:

```text
allocation_count = tracked_allocations + bypassed_allocations
tracked_allocations = completed_outcomes + tracked_inflight
completed_outcomes = short_outcomes + long_outcomes + censored_outcomes
allocation_requested_bytes = allocation_count * requested_size
outcome_requested_bytes = outcome_count * requested_size
allocation_count * minimum_payload_capacity
  <= allocation_payload_bytes
  <= allocation_count * maximum_payload_capacity
```

`censored_outcomes` names completed observations in the indeterminate pressure
interval. `tracked_inflight` names live right-censored objects and supplies age
lower bounds only. The report keeps these denominators separate and excludes
live rows from completed Short/Long accuracy denominators.

The ordinary adaptive policy remains prediction-sampled: Cold sites cap
inflight training objects, learned Short sites sample periodically, and learned
Long sites stay fully tracked. Rows from that mode support telemetry smoke tests
and hypothesis generation. They carry no application-wide prevalence or
classifier-accuracy claim.

## Compiler feature contract

The rustc audit exports `lifetime_analysis_features` at the static semantic
identity:

```text
(callsite, type_id, module_id)
```

The feature vector includes:

- owner-place basis and ownership move count;
- return, escape, and store sinks;
- natural-loop membership and reachable backedges;
- function and reachable yield/await evidence;
- receiver ownership;
- exact, conditional, and cleanup Drop paths;
- normal/cleanup reachability and successor counts;
- a concrete requested size/alignment only when rustc proves the layout.

The compiler triple joins to every exact runtime size/alignment row. This
preserves dynamic-size sites without collapsing their runtime distributions.
When the compiler exports a concrete layout, the joiner additionally requires
an exact five-field match. Dynamic layouts remain explicit layout abstentions.
Conflicting compiler rows for one static identity fail closed.

## Ground-truth aggregation

For every exact runtime site, the analysis reports:

- decisive Short/Long object and requested-byte counts;
- Long object share and Long requested-byte share;
- completed indeterminate outcomes;
- live right-censored objects and mean age lower bound;
- bypassed/unobserved allocations;
- mean/maximum completed pressure age;
- allocation-count and requested-byte hotness shares;
- total and maximum allocation-pressure span.

Access hotness is unavailable. Allocation count, requested bytes, and pressure
span are explicitly labeled allocation-hotness proxies.

Two Long rankings are produced:

1. observed Long requested-byte volume, without a dominance threshold;
2. preregistered Long-dominant candidates with minimum decisive support and a
   minimum Long requested-byte share.

The default candidate parameters are three decisive outcomes and an 80% Long
requested-byte share. They are evaluation parameters rather than universal
lifetime definitions.

## Feature association analysis

Boolean and categorical compiler features are one-hot encoded. Count features
use stable buckets (`0`, `1`, `2-3`, `4+`). For each feature, the report
compares the Long requested-byte share among sites with the feature against
the share among matched sites without it.

The output includes support sites, decisive requested bytes, percentage-point
difference, and lift. These are observational associations used to rank
candidate classifier features. Held-out evaluation is required before any
predictive or causal claim.

## Real-application campaign

The required applications are:

| Application | Workload role | Status |
|---|---|---|
| ripgrep | repeated corpus search | force-track verified; 117/117 exact sites joined |
| fd | repeated metadata-tree traversal | force-track verified; 37/37 exact sites joined |
| Oxipng | image optimization with retained internal state | force-track verified; 21/21 exact sites joined |

Runtime truth collection uses inference-disabled metadata so compiler hints do
not alter the label collection policy. Compiler feature audits come from a
separate source-bound build and join offline by exact identity.

The `typeiso_coverage` runtime variant enables force-track-all with the fixed
guards above and requires ordinary-page evidence. The `typeiso_perf` variant
retains production adaptive sampling and never enables force-track-all.

Each runtime snapshot file uses this envelope:

```json
{
  "source": "unialloc-lifetime-adaptive-site-snapshot-v1",
  "abi_version": 1,
  "required_rows": 1,
  "captured_rows": 1,
  "truncated": false,
  "rows": []
}
```

The analysis manifest is:

```json
{
  "schema_version": 1,
  "applications": [
    {
      "app": "ripgrep",
      "runtime_site_snapshot_files": ["runtime/ripgrep.json"],
      "compiler_audit_roots": ["audits/ripgrep"]
    },
    {
      "app": "fd",
      "runtime_site_snapshot_files": ["runtime/fd.json"],
      "compiler_audit_roots": ["audits/fd"]
    },
    {
      "app": "oxipng",
      "runtime_site_snapshot_files": ["runtime/oxipng.json"],
      "compiler_audit_roots": ["audits/oxipng"]
    }
  ]
}
```

Run the joiner with:

```bash
uv run python evaluation/scripts/compiler_heap_lifetime_ground_truth.py \
  --manifest evaluation/raw/<run-id>/ground-truth-manifest.json \
  --output evaluation/raw/<run-id>/ground-truth-summary.json \
  --markdown-output evaluation/raw/<run-id>/ground-truth-analysis.md
```

Focused validation requires no allocator build or THP support:

```bash
uv run python evaluation/scripts/test_compiler_heap_lifetime_ground_truth.py
```

## External comparator gate

This pipeline measures labels and compiler-feature associations; it carries no
allocator-prior-work performance comparison. The fail-closed helper
`evaluation/scripts/google_tcmalloc_bazel_probe.py` pins
`github.com/google/tcmalloc` commit
`12f255231938d30493186b0a037feedd70f5a1c1` (Bazel Central Registry release
`0.0.0-20250927-12f2552`). It builds target
`//:cross_allocator_large_page_workload_google_tcmalloc` with official
link-time malloc target `@com_google_tcmalloc//tcmalloc` and records HPAA source
hashes, symbol proof, revision, and build options. The current host has Bazel
9.2.0, and the generic helper build is **VERIFIED**: the linked binary contains
the expected Temeraire/HPAA and strong `malloc` symbols, and the matched
system-control binary uses the same source and compile options. This
verification establishes comparator identity and build provenance. A
real-application modern TCMalloc arm remains fail-closed **BLOCKED** until a
verified application binary is linked through the same official target.
gperftools 2.18.1 remains `gperftools-legacy`, is historical evidence only, and
never substitutes for the Google TCMalloc/Temeraire baseline.

The separate five-pair
[`cross-allocator summary`](evidence/cross-allocator-modern-tcmalloc-20260715/summary.json)
confirms the fixed-revision Google TCMalloc identity. Temeraire/HPAA physical
backing was 0/5, leaving both the hugepage-mechanism and performance-causality
gates false. The semantic probe's within-allocator placement-potential results
and presentation figures are recorded in
[`compiler-heap-lifetime-inference.md`](compiler-heap-lifetime-inference.md#external-comparator-gate).

## Real-program results

The three force-track runs exported 175 exact runtime sites and joined every
site to a compiler static identity. Rustc supplied 58 exact five-field layout
matches; the remaining 117 sites retained explicit dynamic-layout
abstentions. All 74,507 tracked allocations completed, covering 25,063,584
requested bytes with zero bypasses and zero live objects at snapshot time.

### Coverage and censoring

| Metric | ripgrep | fd | Oxipng | Aggregate |
|---|---:|---:|---:|---:|
| Exact runtime sites | 117 | 37 | 21 | 175 |
| Static-feature join coverage | 100% | 100% | 100% | 100% |
| Exact five-field layout matches | 29 | 25 | 4 | 58 |
| Dynamic-layout abstentions | 88 | 12 | 17 | 117 |
| Matched requested-byte coverage | 100% | 100% | 100% | 100% |
| Tracked allocations | 2,266 | 62,591 | 9,650 | 74,507 |
| Allocation requested bytes | 73,430 | 1,870,950 | 23,119,204 | 25,063,584 |
| Decisive Short outcomes | 2,266 | 62,338 | 9,635 | 74,239 |
| Decisive Long outcomes | 0 | 151 | 14 | 165 |
| Completed indeterminate outcomes | 0 | 102 | 1 | 103 |
| Live right-censored objects | 0 | 0 | 0 | 0 |
| Bypassed/unobserved allocations | 0 | 0 | 0 | 0 |

### Static-hint decision coverage

All 74,404 decisive objects carried an abstaining static runtime hint. The
compiler/runtime feature join still achieved 100% site coverage; classification
coverage remained zero because the audited real-program sites exported
Unknown. The confusion matrix therefore contains no classified decisions:

| Metric | Count |
|---|---:|
| True positive | 0 |
| True negative | 0 |
| False positive | 0 |
| False negative | 0 |
| Abstained decisive objects | 74,404 |

At the preregistered gate of at least three decisive outcomes and at least 80%
Long requested-byte share, **zero sites qualified as Long-dominant
candidates**. The observed 165 Long outcomes remain useful labels for feature
hypothesis generation.

Presentation-ready ground-truth overview:
[SVG](figures/compiler-lifetime-ground-truth-20260715/ground-truth-overview.svg),
[PNG](figures/compiler-lifetime-ground-truth-20260715/ground-truth-overview.png),
and [source CSV](figures/compiler-lifetime-ground-truth-20260715/ground-truth-overview.csv).

### Exploratory feature associations

The table reports the Long requested-byte share within sites where each feature
is present. These are observational correlations over three workloads.

| Feature present | Runtime sites | Decisive requested bytes | Long requested bytes | Long byte share |
|---|---:|---:|---:|---:|
| Owner place unresolved | 13 | 2,661 | 682 | 25.63% |
| `Box::new` allocation | 58 | 164,184 | 2,384 | 1.452% |
| Store sink | 152 | 1,947,148 | 4,066 | 0.209% |
| Reachable loop backedge | 10 | 23,118,276 | 300 | 0.0013% (rounds to 0.00%) |
| Cleanup Drop path | 18 | 11,888 | 0 | 0% |
| Receiver-owned allocation | 10 | 23,118,179 | 0 | 0% |

The unresolved-owner association has only 2,661 decisive bytes across 13 sites.
The remaining intuitive signals are weak or absent: `Box::new` reaches 1.452%,
store sinks reach 0.209%, and loop, cleanup-Drop, and receiver signals are near
zero. This evidence is inadequate for freezing a production heuristic. The
next classifier must be preregistered from these hypotheses and evaluated on
held-out applications before it controls performance placement.

### Held-out work

| Evaluation item | Status |
|---|---|
| Frozen classifier on separate applications | **TBD** |
| Classifier-enabled real-program allocator performance | **TBD** |
| Real-program THP backing and fragmentation effect | **TBD** |

## Claim boundary

This campaign establishes real-program pressure-relative outcome distributions,
exact compiler/runtime identity coverage, and observational feature
associations under loss-free force-track-all. Adaptive sampled runs remain
selection-conditioned. Access hotness, universal lifetime thresholds, causal
feature effects, held-out prediction accuracy, THP benefit, and fragmentation
reduction remain future claim targets. Those claims require a learned
classifier frozen from this corpus and evaluated on separate applications and
performance runs.
