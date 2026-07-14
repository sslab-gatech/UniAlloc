# Cross-allocator large-page evaluation: lifetime-guided THP versus global policies

> Canonical design record and qualifier/paper slide guide:
> [`lifetime-aware-thp-design-and-presentation.md`](lifetime-aware-thp-design-and-presentation.md)

## Conclusion

On a 512 MiB translation-stress trace pinned to one CPU and NUMA node on this
host, UniAlloc's **selective lifetime THP** reduces dependent-touch latency by
**10.37%** relative to the same lifetime layout and trace with process THP
forcibly disabled. The paired-bootstrap 95% CI is **[+6.72%, +14.05%]**. Maximum
effective resident memory increases by **0.334%** `[+0.309%, +0.358%]`. This pair
changes only physical THP eligibility, directly supporting the mechanism claim
that lifetime hints guide which cohorts receive THP backing.

In the same campaign, the point estimate for process-level THP in mimalloc is
**+7.42%**. The jemalloc estimate is **+4.51%**, with an interval that crosses
zero. These values are incremental effects from each allocator's own on/off
pair: UniAlloc uses the semantic Rust probe, while mimalloc and jemalloc use the
allocator-neutral C probe. UniAlloc creates only **258 MiB** of actual THP
backing; the global policies in mimalloc and jemalloc each create **516 MiB**.
On the 50% long-lived trace, lifetime selection halves the THP footprint while
producing a 10.37% matched speedup in its own semantic pair. Valid cross-family
interpretation is limited to side-by-side incremental effects and backing cost.

The explicit HugeTLB pair for gperftools TCMalloc reaches **+37.00%** while using
**514 MiB** of pre-reserved HugeTLB backing. It represents a deterministic,
whole-heap, explicit-pool mechanism; UniAlloc represents an anonymous-memory,
selective, transparent-promotion mechanism. The slide should present them as
design points with different deployment costs.

## Slide-ready primary figures

### 1. Matched effect after enabling the large-page path

![Paired incremental effect of allocator large-page controls](figures/cross-allocator-large-page-20260714/incremental-effect.png)

- Editable version: [`incremental-effect.svg`](figures/cross-allocator-large-page-20260714/incremental-effect.svg)
- Chart data: [`incremental-effect.csv`](figures/cross-allocator-large-page-20260714/incremental-effect.csv)
- Recommended title: **Lifetime-guided THP delivers a 10.4% matched speedup with 0.3% peak-memory cost**
- Recommended footnote: *Within-pair deltas only: UniAlloc uses a semantic Rust
  probe; others share one neutral C probe. 20 paired blocks; 95%
  paired-bootstrap CI. gperftools uses pre-reserved HugeTLB; resident delta
  excludes unused pool capacity.*

### 2. Actual large-page backing

![Actual THP and HugeTLB backing](figures/cross-allocator-large-page-20260714/actual-backing.png)

- Editable version: [`actual-backing.svg`](figures/cross-allocator-large-page-20260714/actual-backing.svg)
- Chart data: [`actual-backing.csv`](figures/cross-allocator-large-page-20260714/actual-backing.csv)
- Recommended title: **Lifetime selection halves THP backing: 258 MiB versus 516 MiB global THP**
- Recommended footnote: *Same 512 MiB peak requested payload; cross-family
  comparison is backing-only. Backing comes from per-process `AnonHugePages` or
  `HugetlbPages`.*

### 3. Allocator-neutral endpoint (appendix)

![Allocator-neutral endpoint frontier](figures/cross-allocator-large-page-20260714/endpoint-frontier.png)

- Editable version: [`endpoint-frontier.svg`](figures/cross-allocator-large-page-20260714/endpoint-frontier.svg)
- Chart data: [`endpoint-frontier.csv`](figures/cross-allocator-large-page-20260714/endpoint-frontier.csv)
- This figure includes only the allocator-neutral family and supports endpoint
  comparison under the same C workload.
- UniAlloc uses the semantic Rust workload, so its raw endpoint remains in a
  separate table. The primary figure aligns the two families using each one's
  matched on/off delta.

## Core results

### Matched large-page effect

A positive speedup means the target is faster. A positive maximum-effective-
resident-memory change means the target uses more resident memory. Each interval
comes from 20 paired blocks and 20,000 bootstrap resamples.

| Pair | Large-page mechanism | Touch speedup (95% CI) | Max resident change (95% CI) | Actual backing | Decision |
|---|---|---:|---:|---:|---|
| UniAlloc lifetime THP on vs same layout forced off | selective anonymous THP | **+10.37%** `[+6.72%, +14.05%]` | +0.334% `[+0.309%, +0.358%]` | 258 MiB THP | improved |
| mimalloc THP on vs off | process-wide anonymous THP | **+7.42%** `[+3.75%, +9.73%]` | +0.842% `[+0.842%, +0.842%]` | 516 MiB THP | improved |
| jemalloc `thp:always` vs `thp:never` | allocator-wide anonymous THP | +4.51% `[-0.70%, +8.32%]` | +0.789% `[+0.659%, +0.903%]` | 516 MiB THP | inconclusive |
| gperftools HugeTLB vs anonymous default | explicit HugeTLB memfs | **+37.00%** `[+36.02%, +37.91%]` | +0.550% `[+0.418%, +0.701%]` | 514 MiB HugeTLB | improved |
| snmalloc default eligibility vs process THP disabled | OS-eligibility sensitivity | -1.53% `[-3.31%, -0.01%]` | approximately 0% `[-0.003%, +0.002%]` | 0 MiB in both | regressed |

snmalloc's public interface lets the Linux PAL determine page eligibility. This
row measures sensitivity to Linux process eligibility. Both modes show 0 MiB of
`AnonHugePages`, making it a zero-backing OS-sensitivity control.

### UniAlloc semantic family

| Case | Median touch | Median allocate/fault/free lifecycle | Max effective resident | Actual THP | Injected-noise classification failure | Policy-intent placement failure |
|---|---:|---:|---:|---:|---:|---:|
| default | 57.509 ns/touch | 1553.2 ns/allocation | 522.9 MiB | 0 MiB | 4.993% | 33.333% |
| ordinary arenas | 52.921 ns/touch | 1904.1 ns/allocation | 521.6 MiB | 0 MiB | 4.993% | 33.333% |
| lifetime layout / THP forced off | 53.262 ns/touch | 1907.4 ns/allocation | 521.5 MiB | 0 MiB | 4.993% | 4.993% |
| selective lifetime THP | **48.965 ns/touch** | **1298.9 ns/allocation** | 523.3 MiB | **258 MiB** | 4.993% | 4.993% |

`lifetime THP on` and `lifetime THP forced off` form the primary causal pair.
They use the same `long-thp` policy, metadata, layout, prediction trace, and
probe. The forced-off arm prevents the process from receiving THP with
`PR_SET_THP_DISABLE=1`; `PR_GET_THP_DISABLE=1` and `AnonHugePages=0` close the
control. The on arm must pass the actual-THP-backing gate. The default and
ordinary arms provide deployment endpoints and arena-layout controls. Their
policy-intent placement metric records their own placement semantics, while
classifier failure remains 4.993% in all four arms. The table's 4.993% result
comes from explicit 5% false-long/false-short noise injection, which locks the
classification input across all four arms. Held-out compiler accuracy belongs
to the subsequent real-application evaluation.

### UniAlloc internal ablation: separating layout and THP contributions

The following table uses the same 20 paired blocks and 20,000 bootstrap
resamples. Positive values indicate that the target improves a lower-is-better
metric; negative values indicate relative cost.

| Target comparison | Touch speedup | Allocate/fault/free lifecycle speedup | Steady resident reduction | Max resident reduction |
|---|---:|---:|---:|---:|
| selective THP vs same lifetime layout / THP off | **+10.37%** `[+6.72%, +14.05%]` | **+31.99%** `[+31.78%, +32.18%]` | -0.283% `[-0.327%, -0.233%]` | -0.334% `[-0.357%, -0.310%]` |
| selective THP vs ordinary arenas | **+8.55%** `[+4.28%, +12.90%]` | **+31.87%** `[+31.64%, +32.10%]` | -0.281% `[-0.326%, -0.230%]` | -0.332% `[-0.356%, -0.308%]` |
| selective THP vs default | **+16.90%** `[+13.50%, +20.38%]` | **+16.55%** `[+15.98%, +17.09%]` | **+8.09%** `[+7.61%, +8.59%]` | -0.072% `[-0.092%, -0.049%]` |
| same lifetime layout / THP off vs default | **+7.28%** `[+5.65%, +8.80%]` | -22.70% `[-23.36%, -22.06%]` | **+8.35%** `[+7.84%, +8.85%]` | +0.261% `[+0.251%, +0.272%]` |

This decomposition separates the contribution into two layers. Lifetime-directed
layout reduces steady resident memory by 8.35% relative to default while making
the allocate/fault/free lifecycle 22.70% slower. Selective THP accelerates that
lifecycle by 31.99% relative to the same-layout off arm. The final result versus
default combines a 16.55% allocate/fault/free lifecycle speedup with an 8.09%
steady-resident reduction. This metric includes the first-byte fault for every
object in the allocation path, plus free, wave, and teardown time. Maximum
resident memory over the complete process differs from default by 0.072%. The
full ablation data is stored in
[`unialloc-ablation-effects.csv`](evidence/cross-allocator-large-page-final-20260714/unialloc-ablation-effects.csv).

### Allocator-neutral family

| Case | Median touch | Max effective resident | THP coverage / HugeTLB backing |
|---|---:|---:|---:|
| glibc default | **31.701 ns/touch** | 518.3 MiB | 0 |
| mimalloc off | 59.714 ns/touch | 517.8 MiB | 0 |
| mimalloc THP | 54.126 ns/touch | 522.1 MiB | 516 MiB; 99.99% coverage |
| jemalloc off | 58.935 ns/touch | 536.2 MiB | 0 |
| jemalloc THP | 54.210 ns/touch | 541.2 MiB | 516 MiB; 97.18% coverage |
| gperftools anonymous | 59.381 ns/touch | 524.1 MiB | 0 |
| gperftools HugeTLB | 36.950 ns/touch | 526.4 MiB | 514 MiB HugeTLB |
| snmalloc eligible | 59.859 ns/touch | 522.5 MiB | 0 |
| snmalloc process THP disabled | 59.255 ns/touch | 522.5 MiB | 0 |

glibc default records the lowest touch time on this allocator-neutral endpoint.
The matched pairs answer the incremental-effect question for large-page
controls; the endpoint table gives the overall result for this fixed malloc/free
workload. Keeping both views prevents page-size mechanism effects from becoming
a general allocator ranking.

## Evidence boundaries for the two families

### UniAlloc semantic causal family

The Rust probe keys predictions by `(callsite, type_id, module_id)`, records the
lifetime/confidence controls, and passes the thresholded discrete lifetime hint
to the allocator. That discrete hint controls the physical placement of long
and short cohorts; continuous confidence remains outside the allocator ABI and
allocator reuse identity.
The primary pair keeps both the semantic trace and lifetime-directed layout
identical, changing only THP eligibility. This family answers: **Given Rust
lifetime hints, does selective THP backing provide an additional benefit?**

### Allocator-neutral family

One C malloc/free binary runs the same seed, checksum, object order,
pointer-chasing, and waves for every allocator. Each allocator's on/off arms use
only the corresponding mechanism exposed by its public configuration. This
family answers: **How large is the matched effect of each general-purpose
allocator's large-page control on the same neutral trace?**

Absolute `ns/touch` values across families reflect both the Rust semantic-arena
path and the C malloc ABI path. Cross-family comparisons in this document use
only matched percentage effects, actual backing footprints, and deployment
mechanisms; raw endpoint rankings remain closed within each family. The two
probe families also preserve their own wave semantics: Rust
`ephemeral_waves=2` means an initial cohort plus one extra cohort, while C
`waves=2` means an initial cohort plus two extra cohorts. This difference further
limits cross-family interpretation to incremental effects and backing trade-offs.

## Allocator controls and actual-backing contract

Linux defines `MADV_HUGEPAGE` as an eligibility hint and lets the kernel decide
whether anonymous memory receives THP backing; `PR_SET_THP_DISABLE` provides a
process-level disable control. See the
[Linux Transparent Hugepage documentation](https://docs.kernel.org/admin-guide/mm/transhuge.html)
for the semantics and limitations. This experiment uses `AnonHugePages` from
`/proc/self/smaps_rollup` to establish actual THP backing, `HugetlbPages` from
`/proc/self/status` to establish actual HugeTLB backing, and
`VmRSS + HugetlbPages` to calculate effective resident memory.

| Allocator / case | Formal control | Fixed conditions | Official basis |
|---|---|---|---|
| UniAlloc | selected VMA `MADV_HUGEPAGE`; off arm `PR_SET_THP_DISABLE=1` | same lifetime layout and trace | [Linux THP](https://docs.kernel.org/admin-guide/mm/transhuge.html) |
| mimalloc 3.3.2 | `MIMALLOC_ALLOW_THP=1/0` | large OS pages and reserved huge pages disabled; purge threshold fixed | [v3.3.2 README](https://github.com/microsoft/mimalloc/blob/v3.3.2/readme.md), [options API](https://microsoft.github.io/mimalloc/group__options.html) |
| jemalloc 5.3.x | `thp:always` / `thp:never` | `metadata_thp:disabled` | [jemalloc `opt.thp`](https://jemalloc.net/jemalloc.3.html#opt.thp) |
| gperftools TCMalloc 2.18.1 | `TCMALLOC_MEMFS_MALLOC_PATH` | `TCMALLOC_MEMFS_DISABLE_FALLBACK=1` in HugeTLB arm | [TCMalloc docs](https://gperftools.github.io/gperftools/tcmalloc.html), [memfs allocator source](https://github.com/gperftools/gperftools/blob/gperftools-2.18.1/src/memfs_malloc.cc) |
| snmalloc 0.2.27 | Linux process eligibility on/off | same snmalloc shared shim | [Linux PAL](https://github.com/microsoft/snmalloc/blob/main/src/snmalloc/pal/pal_linux.h) |

In this campaign, "TCMalloc" specifically means **gperftools TCMalloc 2.18.1**.
Google TCMalloc's HugePage-aware allocator (Temeraire/HPAA) uses a different
implementation and configuration system; see the
[Google TCMalloc design](https://google.github.io/tcmalloc/design.html),
[Temeraire](https://google.github.io/tcmalloc/temeraire.html), and the
[tuning guide](https://google.github.io/tcmalloc/tuning.html). A Google TCMalloc
HPAA comparison remains follow-up work.

## Experimental method

| Item | Formal configuration |
|---|---|
| Host | AMD EPYC 9354; 2 sockets; 128 logical CPUs |
| Kernel | Linux 6.8.0-111-generic x86_64 |
| THP host mode | `always [madvise] never`; defrag `madvise`; PMD 2 MiB |
| HugeTLB pool | 4,096 x 2 MiB; gperftools arm strict zero fallback |
| Pinning | CPU 15; NUMA node 0 |
| Working set | 131,072 x 4 KiB = 512 MiB peak requested payload |
| Lifetime mix | 50% long / 50% short; 5% false-long + 5% false-short |
| Trace | 2 ephemeral waves; 2 warmup pointer passes; 32 measured dependent-pointer passes |
| Sampling | 1 discarded warmup block + 20 measured randomized complete blocks |
| Uncertainty | paired geometric-mean effect; 20,000 bootstrap resamples; fixed seed 20260714 |
| Backing gates | THP from `AnonHugePages`; HugeTLB from `HugetlbPages`; off arms require zero backing |

Each measured block contains 4 semantic cases and 9 neutral cases. Neutral cases
share a checksum; semantic on/off cases share metadata, trace topology, and
classification counts. `summary.json` records `invariants_passed=true` and
`claim_grade=true`, meaning every preregistered backing, pairing, and integrity
gate passed for this synthetic benchmark.

## Classification and runtime validation

This cross-allocator campaign uses the static `long-thp` policy and injects
synthetic prediction noise with
`--false-long-rate 0.05 --false-short-rate 0.05`. The observed median
classification failure is **4.993%** in every UniAlloc arm. This value is a
synthetic robustness/control input near 5%; it does not yet measure the
compiler's empirical accuracy on held-out Rust programs. The primary on/off pair
shares exactly the same injected errors. The observed **+10.37%** effect therefore
comes from physical THP eligibility while classification success remains
identical in both arms.

Runtime validation was measured in a separate synthetic epoch experiment.
Runtime survival gating is not active in the **+10.37%** cross-allocator pair,
which uses the eager static `long-thp` policy.
Death-epoch survival validation reduces the injected static placement failure
from **4.990%** to **1.263%**, a **74.68%** reduction, and eliminates false-positive
placement. The result, synchronous-collapse cost, and evidence boundary are
recorded in [`lifetime-thp-evaluation.md`](lifetime-thp-evaluation.md). The eager
static pair quantifies the physical-THP performance increment; the separate
epoch campaign evaluates survival validation as a precision mechanism.

## Presentation script

### Slide 1 - Causal effect

> We compare each allocator with its own large-page path disabled. UniAlloc keeps
> the lifetime layout and trace identical and changes only process THP eligibility.
> Selective lifetime THP improves dependent-touch latency by 10.4%, with a 95%
> interval of 6.7% to 14.1%, at a 0.33% maximum-resident-memory cost.

Speaker note: mimalloc global THP reaches 7.4%; jemalloc's interval crosses zero;
gperftools explicit HugeTLB reaches 37.0% under a separate explicit-pool
deployment mechanism.

### Slide 2 - Selectivity

> The lifetime hint changes where large pages are spent. UniAlloc backs only the
> selected long-lived cohort: 258 MiB of THP, versus 516 MiB under global THP in
> mimalloc and jemalloc. On this 50/50 trace, semantic selection halves large-page
> backing while the semantic on/off pair records a 10.4% matched speedup.

Speaker note: per-process procfs readings verify 0 MiB backing in every off arm.
The values represent actual physical backing rather than a successful `madvise`
return value.

### Slide 3 - Research contribution and boundary

> The contribution is a compiler/runtime-to-allocator contract: static Rust
> lifetime identity selects candidate VMAs, runtime survival can validate the
> prediction, and the allocator spends THP only on accepted cohorts. The current
> evidence isolates all three links: a shared injected 5% prediction-noise
> control (4.993% observed), a separate synthetic runtime reduction to 1.263%,
> and a 10.4% matched THP speedup. Held-out compiler accuracy remains future work.

Speaker note: the current data comes from one host and a synthetic
translation-stress trace. The next evidence layer is held-out classification on
real Rust services, tail latency, long-run fragmentation, and THP splits.

## Defensible claims and boundaries

### Directly supported by the current data

- On this host and trace, lifetime-guided selective THP provides a **10.37%**
  dependent-touch speedup over the exact same-policy off control; the entire 95%
  CI is above zero.
- The selective policy uses **258 MiB** of THP; mimalloc process THP and jemalloc
  allocator THP each use **516 MiB**. The THP-backing footprint falls by 50% on
  this 50/50 trace.
- The matched transparent-THP point estimates for UniAlloc, mimalloc, and
  jemalloc are 10.37%, 7.42%, and 4.51%, respectively. Each quantifies the
  on/off effect within its own family, and the jemalloc result remains labeled
  inconclusive.
- gperftools explicit HugeTLB provides the larger 37.00% effect with an
  explicitly reserved pool, whole-heap backing, and a strict zero-fallback
  contract.
- Actual backing, classification error, and timing are recorded separately;
  the physical-backing claim uses only procfs measurements.

### Scope of subsequent evidence

- Real Rust applications: held-out lifetime classification, throughput, P99
  latency, page faults, and TLB counters.
- Multiple size classes, multithreading, cross-NUMA execution, and long-run
  fragmentation, THP splits, and reclaim.
- Google TCMalloc HPAA, THP=`always` host mode, different kernels, and different
  2 MiB coverage.
- Performance-counter attribution; this campaign's headline uses elapsed
  dependent-pointer latency and procfs backing evidence.

## Evidence index

- Formal summary: [`summary.json`](evidence/cross-allocator-large-page-final-20260714/summary.json)
- Case table: [`summary.csv`](evidence/cross-allocator-large-page-final-20260714/summary.csv)
- Paired effects: [`toggle-effects.csv`](evidence/cross-allocator-large-page-final-20260714/toggle-effects.csv)
- UniAlloc internal ablations: [`unialloc-ablation-effects.csv`](evidence/cross-allocator-large-page-final-20260714/unialloc-ablation-effects.csv)
- Full parsed samples: [`samples.jsonl`](evidence/cross-allocator-large-page-final-20260714/samples.jsonl)
- Exact command and library hashes: [`runner-manifest.json`](evidence/cross-allocator-large-page-final-20260714/runner-manifest.json)
- Third-party build provenance: [`library-build-provenance.json`](evidence/cross-allocator-large-page-final-20260714/library-build-provenance.json)
- Host before / after: [`host-before.txt`](evidence/cross-allocator-large-page-final-20260714/host-before.txt), [`host-after.txt`](evidence/cross-allocator-large-page-final-20260714/host-after.txt)
- Verification record: [`verification.txt`](evidence/cross-allocator-large-page-final-20260714/verification.txt)
- Evidence hashes: [`manifest.json`](evidence/cross-allocator-large-page-final-20260714/manifest.json)
- Figure hashes: [`figures/.../manifest.json`](figures/cross-allocator-large-page-20260714/manifest.json)

For a qualifier or paper presentation, use the first two figures as the primary
result: **10.4% matched speedup, 0.3% peak-memory cost, and 50% lower THP
backing**. Keep gperftools HugeTLB as the explicit upper control, jemalloc as an
inconclusive interval, and snmalloc as the zero-backing sensitivity control.
