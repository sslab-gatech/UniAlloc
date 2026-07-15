# Lifetime-guided Transparent Hugepage allocation: design and evaluation

## Decision

**Eager lifetime-guided THP: GO, suitable as the default deployment candidate and a qualifier/paper mechanism point.**

**Runtime-validated epoch collapse: MECHANISM GO; the current implementation has excessive lifecycle and transient-memory costs.**

UniAlloc now separates lifetime placement policy from the physical page backend. The
same Rust `(callsite, type_id, module_id, lifetime, confidence)` signal can select either
explicit 2 MiB HugeTLB or Linux anonymous THP. In the formal 80-process paired experiment,
long-only eager THP improved dependent-touch performance by **12.69%** over the system
default allocator, with a bootstrap 95% interval of **[+5.58%, +18.94%]**; relative to the
`MADV_NOHUGEPAGE` control with the same lifetime packing, it improved performance by
**8.45%**, with an interval of **[+2.39%, +13.65%]**. Steady THP backing coverage was
**100%**, with no application-reserved HugeTLB pool required.

The runtime death epoch also makes the static hint more useful: under 5% symmetric
classification error, delaying collapse until the phase boundary reduced placement
failure for the classified set from **4.990%** to **1.263%**, a **74.68%** reduction, and
eliminated false-positive placements; the confidence variant dropped from **0.582%** to
**0.151%**, a **74.13%** reduction. Synchronous collapse introduced **57.79%** allocator
lifecycle overhead and increased maximum effective resident memory by **41.50%**. The
current recommendation is therefore eager long-only THP, with epoch validation retained
as a research mechanism and a target for future asynchronous implementation.

This evidence is a synthetic paired allocator evaluation with `claim_grade=false`.
Profile coverage, held-out classifier calibration, throughput, and tail latency on real
Rust applications still require separate validation.

## Mechanism

### Page backend and lifetime policy are orthogonal

New interface:

```rust
pub enum LifetimePageBackend {
    ExplicitHugeTLB,
    TransparentHugepage,
}

lifetime_hugepage_configure_with_backend(policy, backend)
lifetime_hugepage_backend()
```

The existing `lifetime_hugepage_configure(policy)` retains explicit HugeTLB behavior, so
existing calls and compiler integration remain reproducible. The THP experiment explicitly
selects `LifetimePageBackend::TransparentHugepage`.

| Placement policy | THP behavior | Purpose |
|---|---|---|
| `SegregatedHugepage` | Classified objects enter a 2 MiB-aligned anonymous VMA with `MADV_HUGEPAGE` | All-THP upper control |
| `LongLivedHugepage` | Accepted long-lived objects use eager THP; ephemeral objects use a `MADV_NOHUGEPAGE` ordinary extent | Recommended deployment candidate |
| `EpochCohortHugepage` | Long-lived candidates start with `MADV_NOHUGEPAGE`; after surviving across an epoch with occupancy >=50%, the allocator executes `MADV_HUGEPAGE` + `MADV_COLLAPSE` | Runtime validation mechanism |
| `SegregatedOrdinary` | All experimental extents use `MADV_NOHUGEPAGE` | Page-size causal control |

The 50% occupancy threshold was preregistered for this feasibility study. It means that only
an extent dense enough for promotion and already surviving across a phase is promoted; it is
not claimed as a universally optimal value. The implementation performs no allocator-side
prefaulting. Normal object writes from the probe provide the resident workload, avoiding an
artificial RSS increase from extra page touches.

### How runtime validation corrects the static hint

```text
static exact identity + lifetime class + confidence
  -> eager path: accepted-long VMA becomes THP-eligible immediately
  -> validated path: accepted-long VMA starts NOHUGEPAGE
       -> object survives explicit epoch boundary
       -> extent live-slot occupancy >= 50%
       -> MADV_HUGEPAGE
       -> MADV_COLLAPSE
       -> point-in-time confirmed placement
  -> death epoch supplies actual short/long truth
  -> predictor and physical-placement TP/TN/FP/FN
```

False-long objects are released before the first boundary, so delayed collapse prevents them
from entering hugepage-positive placement. Low-occupancy survivors remain on ordinary pages;
they contribute to placement FN and the low-occupancy-skip metric. In the formal trace, this
policy filtered all false-positive placements to 0 while recall decreased slightly from the
static value of **95.016%** to the runtime-confirmed value of **94.946%**.

## Evidence conventions

Linux delegates THP promotion and demotion to the kernel and provides `MADV_HUGEPAGE`,
`MADV_NOHUGEPAGE`, and `MADV_COLLAPSE` controls; see the
[Linux Transparent Hugepage documentation](https://docs.kernel.org/admin-guide/mm/transhuge.html)
for complete semantics. This evaluation uses the following strict boundaries:

- `MADV_HUGEPAGE` success indicates VMA eligibility; `/proc/self/smaps_rollup`
  `AnonHugePages` for the process proves actual backing.
- `/proc/vmstat` `thp_fault_alloc` and `thp_collapse_alloc` provide system-wide
  corroboration but do not provide process attribution on their own.
- Claim-bearing THP and ordinary-control runs require host mode `madvise` and require
  `smaps_rollup` to be readable and parseable.
- Eager THP requires backing coverage >=95% across all steady THP-backend VMAs.
- Epoch THP requires backing coverage >=95% across all steady collapse-confirmed extents;
  proactively skipped sparse candidates are reported separately as backend-VMA coverage.
- Collapse accounting requires `eligible = attempts + advice failures` and
  `attempts = successes + collapse failures`.
- Effective resident memory is `VmRSS + HugetlbPages`, sampled at the initial peak,
  after the first epoch, at each live-wave peak, and at steady state; the maximum is used.
- Allocator lifecycle includes the first synchronous epoch promotion; `/proc` evidence-read
  time is measured separately and subtracted from lifecycle time.

Explicit HugeTLB uses a persistent reserved pool; THP uses dynamic promotion of ordinary
anonymous memory. See the
[Linux HugeTLB documentation](https://docs.kernel.org/admin-guide/mm/hugetlbpage.html)
for HugeTLB pool semantics.

## Experimental setup

| Item | Configuration |
|---|---|
| Host | 2-socket AMD EPYC 9354; 64 cores / 128 logical CPUs |
| Kernel | Linux 6.8.0-111-generic x86_64 |
| THP | Global `madvise`; defrag `madvise`; 2 MiB PMD size |
| Pinning | CPU 15; NUMA memory node 0 |
| HugeTLB control | 4,096 x 2 MiB persistent pages; all explicit samples require zero fallback |
| Process workload | 262,144 initial 4 KiB objects; 50% long-lived; 8 exact types / truth class |
| Trace | Persistent-long cohort + 3 ephemeral waves; 524,288 allocation generations |
| Prediction noise | False-long 5%; false-short 5%; confidence outcome overlap 10% |
| Confidence | Threshold 80; correct 90; error 60; Unknown rate 0 |
| Touch | 4 warmup passes; 64 dependent-pointer measured passes |
| Matrix | 8 cases x 10 fresh processes = 80 samples; randomized order within each repeat |
| Pairing | The same seed and prediction-trace digest within each repeat |

Eight cases:

1. `system-default`
2. `ordinary-no-thp`
3. `all-thp`
4. `static-long-thp`
5. `confidence-thp`
6. `static-epoch-thp`
7. `confidence-epoch-thp`
8. `explicit-hugetlb`

Reproduction:

```bash
PYTHONHASHSEED=0 python3 \
  evaluation/scripts/lifetime_thp_allocator_experiment.py \
  --run-id lifetime-thp-final-20260714 \
  --objects 262144 --slot-bytes 4096 --types-per-truth 8 \
  --long-fraction 0.5 --false-long-rate 0.05 --false-short-rate 0.05 \
  --confidence-threshold 80 --correct-confidence 90 --error-confidence 60 \
  --confidence-overlap-rate 0.10 --unknown-rate 0.0 \
  --ephemeral-waves 3 --warmup-passes 4 --passes 64 --repeats 10 \
  --numa-node 0 --cpu 15 --seed 20260714 --timeout 900
```

Tracked evidence:

- `docs/evidence/lifetime-thp-final-20260714/manifest.json`
- `docs/evidence/lifetime-thp-final-20260714/samples.jsonl`
- `docs/evidence/lifetime-thp-final-20260714/summary.json`
- `docs/evidence/lifetime-thp-final-20260714/verification.txt`
- `docs/evidence/lifetime-thp-final-20260714/host-before.txt`
- `docs/evidence/lifetime-thp-final-20260714/host-after.txt`

## Actual backing and THP demand

| Case | Median AnonHugePages delta | Steady strict coverage | THP collapse | Description |
|---|---:|---:|---:|---|
| `all-thp` | 1,028 MiB | 100% | - | All classified placements use THP |
| `static-long-thp` | 514 MiB | 100% | - | Long-only eager THP |
| `confidence-thp` | 442 MiB | 100% | - | Low-confidence objects abstain |
| `static-epoch-thp` | 512 MiB | 100% confirmed; 99.61% steady VMA | 256/256 median, 0 failures | Sparse candidates stay on 4 KiB pages |
| `confidence-epoch-thp` | 440 MiB | 100% confirmed; 99.55% steady VMA | 220/220 median, 0 failures | Confidence + runtime survival |
| `ordinary-no-thp` | 0 | 100% no-THP gate | - | `MADV_NOHUGEPAGE` control |
| `explicit-hugetlb` | 0 AnonHugePages | Real HugeTLB; 0 fallback | - | Deterministic control |

Long-only eager THP reduced THP footprint from **1,028 MiB** under all-THP to
**514 MiB, a 50% reduction**. Confidence further reduced it to 442 MiB; this additional
reduction comes from the abstention-coverage tradeoff. The global HugeTLB pool had 4,096
free pages both before and after the experiment, and the THP arms themselves consumed no
persistent pool.

## Classification and runtime-confirmed placement

All static/epoch/explicit cases use exactly the same paired prediction trace. The
classification rate in the table covers classified generations; confidence coverage
explicitly reports abstention.

| Policy | Classification coverage | Static success / failure | Runtime placement success / failure | Runtime precision / recall |
|---|---:|---:|---:|---:|
| static + explicit HugeTLB | 100% | 95.010% / 4.990% | 95.010% / 4.990% | 86.388% / 95.016% |
| static + epoch THP | 100% | 95.010% / 4.990% | **98.737% / 1.263%** | **100% / 94.946%** |
| confidence + epoch THP | 86.013% | 99.418% / 0.582% | **99.849% / 0.151%** | **100% / 99.398%** |

The median false-positive prediction in the static trace was 19,623 generations; runtime
placement FP under epoch THP was **0**. In the confidence trace, the median static FP was
1,955.5; runtime placement FP was also **0**. Low-occupancy skips and false-short
classification create a small number of FNs, so precision rises to 100% while recall remains
close to the original static value.

This result supports the proposed design: static analysis provides lifetime/confidence,
the runtime validates the actual lifetime through the death epoch, and the validated result
drives physical page promotion. Runtime truth depends on a semantically meaningful phase
boundary; service workloads need request, transaction, or time-window definitions.

## Performance and memory

Bootstrap intervals are based on 10 paired repeats. Positive improvement means that the
target is better; resident-memory values in the table use the complete wave-peak convention.

### Recommended path: static long-only eager THP

| Baseline | Dependent touch | Allocator lifecycle | Max effective resident | Steady effective resident |
|---|---:|---:|---:|---:|
| system default | **+12.69%** `[+5.58%, +18.94%]` | **+3.58%** `[+2.52%, +4.66%]` | +0.11% | **+8.46%** `[+7.75%, +9.09%]` |
| ordinary no-THP | **+8.45%** `[+2.39%, +13.65%]` | **+28.32%** `[+27.71%, +28.95%]` | -0.15% | -0.13% |
| explicit HugeTLB | -0.73% `[-6.86%, +5.03%]` | **+1.69%** `[+1.10%, +2.23%]` | approximately equal | approximately equal |

The touch interval between THP and explicit HugeTLB crosses zero, indicating comparable
access performance on this trace. THP retains dynamic ordinary-memory semantics and avoids
application-side persistent-pool provisioning. The touch and lifecycle improvements relative
to the ordinary control confirm the large-page translation/fault mechanism; nearly identical
steady resident memory shows that the page backend itself creates no fragmentation reduction.

Both static long-only and the ordinary control retain **937 MiB** of arena capacity and
**425 MiB** of steady slack. Lifetime/identity packing determines extent reclamation and
fragmentation; THP determines backing and translation behavior.

### Confidence eager THP

The dependent-touch interval for confidence eager THP relative to the ordinary control is
`[-7.63%, +6.44%]`, leaving the direct-latency result open; allocator lifecycle increases by
**4.68%**. Steady effective resident memory decreases by **28.64%**, arena retained falls
from 937 MiB to 520 MiB, and slack falls from 425 MiB to 79.4 MiB. This memory result comes
primarily from confidence abstention reducing incorrect cohort contamination; abstained
objects return to the existing allocator.

### Runtime-validated epoch THP

| Case vs ordinary no-THP | Dependent touch | Allocator lifecycle | Max effective resident | Steady effective resident |
|---|---:|---:|---:|---:|
| static epoch THP | **+11.94%** `[+3.17%, +19.45%]` | **-57.79%** | **-41.50%** | -0.69% |
| confidence epoch THP | **+13.62%** `[+9.37%, +19.09%]` | **-50.94%** | **-8.38%** | **+27.98%** |

Negative values here indicate overhead / resident growth. The median first promotion was
170.36 ms for static epoch and 155.04 ms for confidence epoch. Synchronous `MADV_COLLAPSE`
occurs under the arena lock. Epoch cohorts also prevent a new wave from reusing an old cohort
extent, raising the median maximum effective resident memory for static epoch to **1,470 MiB**,
compared with approximately **1,041 MiB** for the ordinary control.

Runtime validation substantially improves placement precision, and the current synchronous
implementation has a clear cost. The highest-value next step is to enqueue surviving extents
for promotion, perform only a state transition at the phase boundary, and let a background
thread collapse them in batches. Safe canonical-bucket reuse or a memory budget should also
limit the wave peak caused by cohort isolation.

## Boundary relative to the closest prior work

| System | Established mechanism | Defensible contribution of this implementation |
|---|---|---|
| [LLAMA, ASPLOS 2020](https://colinraffel.com/publications/asplos2020learning.pdf) | Allocation-context lifetime prediction, multiple lifetime classes, 2 MiB placement, and dynamic recovery from misprediction | Exact Rust identity/profile binding, confidence abstention, death-epoch confusion matrix, and THP advice/backing evidence contract |
| [TEMERAIRE, OSDI 2021](https://www.usenix.org/system/files/osdi21-hunter.pdf) | Fullness/longest-free-range hugepage packing, tail donation, and subrelease | Semantic lifetime/confidence/epoch as packing inputs; survival-gated physical promotion |
| [TCMalloc warehouse-scale study, ASPLOS 2024](https://people.csail.mit.edu/delimitrou/papers/2024.asplos.memory.pdf) | Short/long span separation in dedicated hugepage sets; fleet-scale throughput/memory/TLB evaluation | Compiler-to-allocator Rust contract and runtime placement validation; current evidence is a single-machine synthetic allocator trace |

Prior work uses different workloads, allocators, fragmentation denominators, and hardware,
so a direct "improvement over" calculation lacks a shared baseline. This experiment can state
its own controls: long-only THP reduces THP footprint by 50% relative to all-THP; improves
dependent-touch performance by 8.45% relative to the ordinary control; and reduces selective
placement failure by approximately 74% through runtime epoch validation. Cross-paper numbers
remain background context.

## Defensible claims

- UniAlloc's lifetime policy can select a real anonymous THP backend on the production
  allocator path while preserving exact identity, fallback, and symmetric deallocation
  provenance.
- On this host's 80-sample paired trace, eager long-only THP achieves 100% steady backing,
  halves THP footprint, and improves dependent-touch performance relative to the ordinary
  no-THP control.
- Static lifetime + runtime death epoch can keep false-long predictions on ordinary pages,
  eliminating false-positive THP placement in this trace.
- Confidence and runtime validation separately control classifier contamination and physical
  promotion, with closed accounting for coverage, success/failure, and precision/recall.
- Advice, point-in-time collapse, and steady actual backing use separate metrics; memory,
  timing, and byte-epoch accounting all pass strict gates.

## Open boundaries and recommendation

- Profile confidence and the error model are synthetic; real Rust workloads require training /
  held-out validation and per-site calibration.
- The current workload fixes a 4 KiB slot, one NUMA node, and an explicit phase boundary;
  multiple size classes, cross-thread service traces, and long-term THP split behavior remain
  unvalidated.
- `/proc/vmstat` provides system-wide corroboration; process attribution comes from smaps.
  The host had unrelated background work during the experiment. CPU pinning, case
  randomization, paired seeds, and bootstrap intervals reduce its noise impact.
- The 50% promotion threshold requires a 25%/50%/75% sweep; a complete policy also needs a
  memory budget and a collapse-latency budget.
- Eager THP is the main path for the next stage:

```rust
lifetime_hugepage_configure_with_backend(
    LifetimeHugepagePolicy::LongLivedHugepage,
    LifetimePageBackend::TransparentHugepage,
)
```

The core qualifier/paper statement should be **Rust compiler/runtime contract for
lifetime-guided THP placement and survival-validated promotion**. Deployment results should
use eager THP; runtime epoch results demonstrate that online validation can turn a hint into
more precise physical placement, with asynchronous promotion as the next engineering
optimization.
