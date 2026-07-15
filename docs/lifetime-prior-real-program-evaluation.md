# Rust Lifetime Priors for Selective THP Allocation

## Status and current verdict

Rust ownership flow can identify a useful Long cohort in a complex real
program. The strongest completed result is the SWC Stage-A screen at allocator
revision `a7b9fe2c885074c0ac0607554dc08076b8c108de`: exact-layout return/escape
priors reached **96.15% decisive-outcome precision**, **96.31% decisive-byte
precision**, and **93.16% Long-byte recall**. Runtime validation remains the
authority for promotion, correction, and final page placement.

This evidence supports a classifier-opportunity claim. Stage B now adds one
fixed-work Polars result and one two-repeat SWC diagnostic. The current data
supports mechanism feasibility and leaves a real-program end-to-end performance
win unsupported.

The current presentation-safe statement is:

> On the pinned SWC workload, Rust MIR return/escape ownership priors identified
> 93.16% of observed Long bytes at 96.31% byte precision under the Stage-A
> process-wide pressure clock. Exact-layout abstention removed unvalidateable
> cohorts, and the runtime retained correction authority before selective THP
> placement.

## Mechanism

### 1. Compiler ownership features

The rustc pass associates a semantic heap allocation with its owner and exports
the following optimized-MIR facts:

- ownership-preserving moves and the final owner place;
- return, store, and opaque consuming-call escape sinks;
- exact, conditional, normal, and cleanup Drop topology;
- receiver ownership and owner-live opaque calls;
- loop membership, reachable backedges, yield, and await;
- requested size and alignment when rustc proves a concrete allocation layout.

The implementation is in
[`tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs`](../tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs).
The broader ground-truth contract is recorded in
[`docs/compiler-heap-lifetime-ground-truth.md`](compiler-heap-lifetime-ground-truth.md).

### 2. Advisory prior rules

The compiler emits a weak prior from ownership flow:

| MIR evidence | Advisory result | Confidence | Required safety bounds |
| --- | ---: | ---: | --- |
| Owner reaches the return place | Long | 70 | No local Drop, reachable backedge, yield, or await |
| Owner reaches an opaque consuming escape | Long | 70 | No local Drop, reachable backedge, yield, or await |
| Exact all-path local release | Short | 85 | Exact Drop plus no store, opaque owner-live call, backedge, yield, or await |
| Receiver-owned all-path local release | Short | 85 | One normal successor, no cleanup successor, exact unconditional Drop, and no nonlocal sink |
| Owner-live cleanup/unwind, opaque call, or unresolved control flow | Unknown | 0 | Runtime learns from the site |

An exact Drop by itself proves eventual release. Pressure-relative duration
also requires the all-path/effect bounds above. This distinction covers the
regression where a page-sized `Box` survives 16 MiB of intervening allocation
pressure before its eventual Drop. Any owner-live cleanup or unwind edge makes
the prior Unknown.

### 3. Exact-layout abstention

A deployed prior must identify the same cohort that the runtime can validate.
The compiler therefore routes a prior only when it exports a positive requested
size and a power-of-two alignment. Dynamic layouts, unresolved layouts, and
zero-sized allocations retain their ownership facts while their hint becomes
Unknown with an `_unjoinable_layout_unknown` basis.

Runtime observations use the separate exact key:

```text
(callsite, type_id, module_id, requested_size, align)
```

Numeric compiler identities require an exact five-field match. Generic
compiler identities use an authenticated, unique
`(callsite, module_id, requested_size, align)` match to one positive runtime
`type_id`, then become the same five-field key. The production
`AdaptiveSiteKey` remains unchanged, so dynamic-size callsites retain one
predictor identity instead of fragmenting into one predictor per requested
size.

The gate matters in practice. A pre-gate SWC screen contained one dominant
dynamic-layout Long prior whose roughly 52 MiB of executed outcomes were all
Short. The current SWC build reports 297 applied prior hints, 297 complete
layout keys, and zero incomplete layout keys.

### 4. Runtime validation and placement

The static hint seeds the adaptive site's votes and provisional cohort. A
provisional Long site remains on ordinary backing while the runtime validates
its pressure-relative lifetime. Four matching decisive samples confirm a
static prior; one contradictory decisive outcome rejects the provisional
placement immediately. An all-Unknown site uses the generic eight-sample
learning threshold.

The allocator then separates placement cohorts:

- Cold and Short objects use ordinary-page extents;
- provisional Long objects use a Long cohort on ordinary backing;
- runtime-confirmed Long objects use the selective-THP path;
- exact-site runtime outcomes can correct the static prior and later demote a
  learned site.

This design lets Rust ownership reduce cold-start learning while runtime age
and extent state control physical backing. The adaptive allocator is in
[`unialloc/src/alloc_api/lifetime_hugepage.rs`](../unialloc/src/alloc_api/lifetime_hugepage.rs).

## Stage-A methodology

Stage A screens each pinned program for lifetime opportunity and static-feature
association. It builds an all-Unknown binary and a compiler-prior binary from
one frozen allocator/pass snapshot, runs each for a bounded 20--60 second
window, and forces trackable exact allocations onto ordinary pages.

The evaluation clock advances by requested bytes for every successful
allocation generation in the process, including raw, missing-identity, and
unsupported-layout allocations. Completed objects receive these labels:

```text
Short:    age < 2 MiB of later requested allocation pressure
Censored: 2 MiB <= age < 8 MiB
Long:     age >= 8 MiB
```

Live objects at snapshot time remain right-censored. The exporter keeps
outcomes, requested bytes, allocation hotness, and age at the exact five-field
site key. Guards fail closed on table loss, bypass, mapping/advice errors,
descendant processes, audit transport mismatch, or truncated snapshots. The
opportunity screen advances when a target supplies at least 2 MiB of Long
requested bytes or 8 MiB of runtime-confirmed Short requested bytes.

The campaign implementation and retained outputs are:

- [`evaluation/scripts/lifetime_prior_six_program_campaign.py`](../evaluation/scripts/lifetime_prior_six_program_campaign.py)
- [`evaluation/raw/lifetime-prior-stage-a-v2-swc-20260715/stage-a-results.json`](../evaluation/raw/lifetime-prior-stage-a-v2-swc-20260715/stage-a-results.json)
- [`evaluation/raw/lifetime-prior-stage-a-v2-polars-20260715/stage-a-results.json`](../evaluation/raw/lifetime-prior-stage-a-v2-polars-20260715/stage-a-results.json)
- [`evaluation/raw/lifetime-prior-stage-a-routed-v3-rustpython-20260715/stage-a-results.json`](../evaluation/raw/lifetime-prior-stage-a-routed-v3-rustpython-20260715/stage-a-results.json)
- [`evaluation/raw/lifetime-prior-stage-a-routed-v4-actix-20260715/stage-a-results.json`](../evaluation/raw/lifetime-prior-stage-a-routed-v4-actix-20260715/stage-a-results.json)
- [`evaluation/raw/lifetime-prior-stage-a-routed-small-20260715/stage-a-results.json`](../evaluation/raw/lifetime-prior-stage-a-routed-small-20260715/stage-a-results.json)

These `evaluation/raw` paths are the current source evidence and are ignored by
Git. A compact tracked evidence package remains pending; claims should retain
the raw path, source commit, allocator revision, and artifact digest until that
package is committed.

### Pinned program sources

| Program | Source reference | Fixed commit |
| --- | --- | --- |
| Oxipng | `v10.1.1` | `628e241e23f368097883807fa6e985ccf7c00357` |
| redb | `v4.1.0` | `6ed1f981ba4deab0b2adbdd7bccb46ec409b2191` |
| Polars | `py-1.42.1` | `0df0c25d4db895ec8cad773bedc4e98400e3135e` |
| SWC | `v1.15.43` | `73f0f386da64fe432975183f9ccf28429b52638a` |
| RustPython | `main@2026-07-13` | `a9c2c529b14199a7ae7893b9d82c8bebe6b17418` |
| Actix Web | `web-v4.14.0` | `696b1fed9c5b0147c37c70e0808cae5f63a5a4a0` |

## Stage-A results

The Polars, SWC, RustPython, and Actix rows use the exact-layout-gated allocator
revision `a7b9fe2c885074c0ac0607554dc08076b8c108de`. The Oxipng and redb rows use
the earlier `b0521a23f9f89de8bf28f505a7dbd599cbfb6cd2` screen and remain
feature-triage evidence pending exact-gate reruns.

| Target | Window | Runtime opportunity | Executed static-prior outcomes | Current interpretation |
| --- | ---: | --- | --- | --- |
| Oxipng | 44.5 s | 17 sites; 0.50 MB Long and 443.64 MB Short | 100 allocations: 0 Long, 68 Short, 32 censored | Strong Short opportunity; observed Long prior is weak and pre-gate |
| redb | 27.0 s | 34 sites; 0.058 MB Long and 1.078 MB Short; opportunity gate failed | 3,973 allocations: 1 Long, 3,972 Short | Insufficient opportunity and poor return/escape-prior precision in this harness |
| Polars | 40.0 s | 54 sites; 6.09 MB Long, 8.75 MB Short, and 0.21 MB censored | 16,130 allocations at 9 exact prior sites: 11,291 Long and 4,839 Short | Mixed site behavior; runtime type identity carries useful corrective signal |
| SWC | 33.2 s | 87 sites; 75.34 MB Long, 225.20 MB Short, and 12.64 MB censored | 1,129,798 allocations at 55 exact prior sites: 904,381 Long, 36,216 Short, 189,201 censored | Strong Long cohort under the Stage-A clock |
| RustPython | 32.4 s | 99 sites; 178,520 B Long, 1,070,477 B Short, and 88,144 B censored; opportunity gate failed | 22 applied priors; 1 executed exact prior site with 10 Long and 5 censored allocations | Complete screen with low opportunity and narrow executed-prior coverage |
| Actix Web | 31.8 s | 23 sites; 1,574 B Long and 9,703,948,249 B Short | 15 applied priors; 1 executed exact prior site with one 64-B Long allocation | Strong Short opportunity with negligible executed Long-prior volume |

The RustPython and Actix executed-prior rows are retained in their respective
[`RustPython exact join`](../evaluation/raw/lifetime-prior-stage-a-routed-v3-rustpython-20260715/runs/rustpython/compiler-prior/measured/compiler-runtime-exact-join.json)
and
[`Actix exact join`](../evaluation/raw/lifetime-prior-stage-a-routed-v4-actix-20260715/runs/actix_web/compiler-prior/measured/compiler-runtime-exact-join.json)
artifacts.

### Strongest evidence: SWC

The exact runtime join matched 55 executed prior sites: 49 return-Long sites
and 6 escape-Long sites. Their decisive outcomes were:

```text
Long outcomes       = 904,381
Short outcomes      =  36,216
Censored outcomes   = 189,201
Long requested bytes  = 70,184,560
Short requested bytes =  2,686,176
All runtime Long bytes = 75,338,454
```

The resulting metrics are:

```text
Outcome precision = 904,381 / (904,381 + 36,216) = 96.15%
Byte precision    = 70,184,560 / (70,184,560 + 2,686,176) = 96.31%
Long-byte recall  = 70,184,560 / 75,338,454 = 93.16%
```

All 297 emitted priors had complete compiler layouts. Fifty-five sites executed
in the selected workload, while 242 remained unobserved. This is partial static
coverage with complete executed-prior accounting. The exact evidence is in
[`compiler-runtime-exact-join.json`](../evaluation/raw/lifetime-prior-stage-a-v2-swc-20260715/runs/swc/compiler-prior/measured/compiler-runtime-exact-join.json).

### Polars type-specific pattern

Polars shows why the compiler/runtime join includes the monomorphized type
identity. The same broad return-ownership pattern produced distinct runtime
outcomes:

| Exact type cohort | Outcomes | Requested bytes | Observation |
| --- | ---: | ---: | --- |
| `PrimitiveArray<i32>` and `PrimitiveArray<f64>` from `filter_with_bitmap` | 3,226 Long, 0 Short | 283,888 Long bytes | Stable Long cohort |
| `BinaryViewArrayGeneric<str>` and `BinaryViewArrayGeneric<[u8]>` | 0 Long, 4,839 Short | 619,392 Short bytes | Static return-Long prior needs runtime correction |
| Five physical-plan executor object types | 8,065 Long, 0 Short | 335,504 Long bytes | Stable Long plan cohort |

Across the nine matched prior sites, object precision was 70.0% and byte
precision was 50.0%. A function-level return rule alone loses this distinction;
the exact runtime type/site cohort recovers it. The rows are retained in
[`compiler-runtime-exact-join.json`](../evaluation/raw/lifetime-prior-stage-a-v2-polars-20260715/runs/polars/compiler-prior/measured/compiler-runtime-exact-join.json).

## Stage-A evidence boundary

Stage A uses the evaluation-only
`process_wide_requested_generation_bytes` clock. Production adaptive placement
uses `eligible_exact_site_payload_capacity`. The two clocks age objects at
different rates, and force tracking changes allocator work. Stage-A precision,
recall, wall time, RSS, and THP residency therefore stay in the diagnostic and
opportunity scope.

Additional current boundaries:

- SWC, RustPython, and Actix use Criterion-controlled iteration counts and
  remain diagnostic performance targets.
- Polars completed fixed-work Stage B with matching output identity. Oxipng and
  redb remain fixed-work candidates.
- The completed real-program matches in this snapshot exercise Long priors.
  Application-scale Short-prior evidence remains open.
- SWC supplies the strong result; redb supplies a counterexample, Polars
  supplies mixed type-specific behavior, RustPython supplies low opportunity
  and narrow executed coverage, and Actix supplies a Short-dominated workload
  with negligible executed Long-prior volume.
- SWC now proves process-level resident THP backing in a production-clock
  diagnostic. Per-VMA allocator attribution and fragmentation reduction remain
  open. Polars produced zero THP activity and stays outside THP causal claims.

## Stage B: measured production-clock results

### Keep/drop verdict

**KEEP the Rust lifetime classifier and compiler/runtime validation mechanism as
a presentation point. DROP a real-program end-to-end performance-win claim and
a general THP-benefit claim from the current evidence.**

The strongest supported story is SWC classifier/mechanism feasibility: the
compiler priors remain accurate under the production clock, the runtime routes
hundreds of thousands of confirmed Long allocations, and the selective policy
creates resident THPs. Polars supplies the fixed-work counterweight: every
decisive compiler Long prior was Short, runtime correction prevented Long
routing, and both policy-5 endpoints realized zero THPs.

Stage B reused the paired frozen Stage-A binaries with force tracking disabled,
the production `eligible_exact_site_payload_capacity` clock, pinned CPU,
exclusive measurement locking, and balanced arm order. The five arms were
`default`, runtime-only ordinary, compiler-prior ordinary, runtime-only
selective THP, and compiler-prior selective THP.

Raw result artifacts:

- [`Polars Stage-B results`](../evaluation/raw/lifetime-prior-stage-b-v2-polars-20260715/stage-b-results.json)
- [`SWC warmup-bounded Stage-B results`](../evaluation/raw/lifetime-prior-stage-b-v3-swc-warmup5-20260715/stage-b-results.json)

The tracked, source-digest-bound evidence package is
[`docs/evidence/lifetime-prior-real-program-20260715/`](evidence/lifetime-prior-real-program-20260715/).
Its single [`summary.json`](evidence/lifetime-prior-real-program-20260715/summary.json)
retains the six Stage-A rows, all twenty fixed-work Polars Stage-B samples, and
all ten SWC diagnostic Stage-B samples. The slide-ready figures and long-form
CSV tables are under
[`docs/figures/lifetime-prior-real-program-20260715/`](figures/lifetime-prior-real-program-20260715/).

### Polars fixed-work result

Polars completed four balanced repeats with equal work and matching output
identity. The ordinary comparisons satisfy the fixed-work gates. The policy-5
arms are measured policy endpoints with no realized THP.

| Arm | Median seconds/work | Median peak RSS KiB | Long routes | THP extents/advice | Peak `AnonHugePages` | Interpretation |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `default` | 0.019826094 | 157,480 | 0 | 0 / 0 | 0 | Reference |
| `adaptive-ordinary-all-unknown` | 0.019955901 | 157,736 | 0 | 0 / 0 | 0 | Runtime ordinary control |
| `adaptive-ordinary-compiler-prior` | 0.019799502 | 157,920 | 0 | 0 / 0 | 0 | Compiler-prior ordinary result |
| `adaptive-selective-thp-all-unknown` | 0.019777546 | 157,784 | 0 | 0 / 0 | 0 | Policy-5 endpoint; THP causal comparison BLOCKED |
| `adaptive-selective-thp-compiler-prior` | 0.019793392 | 157,900 | 0 | 0 / 0 | 0 | Policy-5 endpoint; THP causal comparison BLOCKED |

The paired fixed-work results are:

- Runtime ordinary versus default: **-0.751% speed improvement**, which is a
  **0.751% slowdown**, and **-0.168% peak-RSS reduction**, which is a 0.168%
  RSS increase.
- Compiler-prior ordinary versus runtime ordinary: **+0.889% median speed** and
  **-0.129% peak-RSS reduction**, which is a 0.129% RSS increase.

Every compiler-prior production run recorded **0 static true positives, 132
static false positives, 9 runtime prior corrections, and 0 Long routes**. Both
selective-policy arms recorded zero Long routes, THP extents, advice attempts,
advice successes, and resident `AnonHugePages` in every sample. All Polars THP
comparisons are therefore **causally BLOCKED**. The +0.889% ordinary timing
delta is a narrow measured result with adverse classifier outcomes and a small
RSS increase; it supplies no THP or end-to-end lifetime-aware win.

### SWC warmup-bounded diagnostic

SWC completed two balanced repeats. Criterion warm-up samples were excluded
before backing validation; every timed-phase ordinary sample had zero resident
THP and every timed-phase selective sample had positive resident THP. Each THP
sample recorded 42 THP extents, 42 successful advice operations, and 12 MiB
peak resident `AnonHugePages`.

| Arm | Median Criterion time/iteration | Median peak RSS KiB | Approximate Long routes | THP extents | Peak resident THP |
| --- | ---: | ---: | ---: | ---: | ---: |
| `default` | 4.3116 ms | 33,118 | 0 | 0 | 0 |
| `adaptive-ordinary-all-unknown` | 4.4006 ms | 28,866 | 327,000 | 0 | 0 |
| `adaptive-ordinary-compiler-prior` | 4.3932 ms | 29,034 | 339,000 | 0 | 0 |
| `adaptive-selective-thp-all-unknown` | 4.3617 ms | 37,028 | 326,000 | 42 | 12 MiB |
| `adaptive-selective-thp-compiler-prior` | 4.3325 ms | 37,428 | 339,000 | 42 | 12 MiB |

The paired diagnostic medians are:

- Runtime THP versus runtime ordinary: **+0.882% speed** and **28.30% higher
  peak RSS**.
- Compiler THP versus runtime THP: **+0.667% speed**.
- Compiler THP versus compiler ordinary: **+1.379% speed**.
- Full compiler-prior THP stack versus default: **-0.486% speed improvement**,
  which is a **0.486% slowdown**, with **13.03% higher peak RSS**.

Compiler-prior arms reached approximately **98.06% production static-prior
accuracy** and routed about **339,000 Long allocations**, compared with about
**327,000** for runtime-only arms. The process-level timed-phase backing gate
passed for all four THP pairs. Allocator-VMA residency attribution remains
unmeasured.

SWC remains a Criterion diagnostic with `n=2`; its dynamic iteration count and
repeat count keep every timing and RSS delta outside a presentation-grade
performance claim. The 28.30% THP RSS increase and the 0.486% full-stack
slowdown give the current end-to-end direction.

### Why a longer run may repeat Short outcomes

Pressure age is computed independently for each object:

```text
object age = pressure at deallocation - pressure at allocation
```

Each new allocation starts a new age interval. Repeating a workload for ten
minutes can therefore produce many copies of the same object that each die
before 2 MiB of later allocation pressure. The result is more Short evidence.
An object becomes Long when that specific allocation stays live across at
least 8 MiB of later production-clock payload pressure. Longer process wall
time raises sample count and confidence; per-object retention determines the
lifetime class.

### Presentation boundary

The current presentation point is:

> Rust ownership flow supplies a useful Long prior on SWC, exact-layout
> abstention makes the prior runtime-joinable, and runtime validation turns the
> prior into confirmed selective THP placement. SWC demonstrates classifier
> and mechanism feasibility. Current fixed-work evidence leaves a real-program
> end-to-end performance win unsupported.

Keep these measured facts in backup material:

- SWC Stage A: 96.15% outcome precision, 96.31% byte precision, and 93.16%
  Long-byte recall under the diagnostic process-wide clock.
- SWC Stage B: about 98.06% production prior accuracy, 42 THP extents, and
  12 MiB resident THP per selective sample; `n=2` Criterion diagnostic.
- Polars Stage B: +0.889% compiler-prior ordinary timing versus runtime
  ordinary, alongside 0/132 TP/FP, 9 corrections, zero Long routes, zero THP,
  and a 0.129% RSS increase.

Exclude these claims from the main presentation:

- a general real-world speedup from lifetime-aware allocation;
- a fixed-work THP performance benefit;
- an RSS or fragmentation reduction;
- a claim that longer wall-clock execution makes repeatedly short objects
  Long.
