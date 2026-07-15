# Lifetime confidence + runtime death epoch: HugeTLB allocation evaluation

## Conclusion

**Classification and fragmentation control: GO; end-to-end performance: INCONCLUSIVE.**

The validation relationship proposed by the user is correct: the static side
provides a lifetime class and confidence, while the runtime derives the actual
lifetime label from an object's allocation epoch and deallocation epoch, then
computes TP/TN/FP/FN. The confidence threshold reduces the classified failure
rate under 5% symmetric error from **4.993%** to **0.581%**, with **86.012%**
coverage; under FP-heavy error, it reduces the classified failure rate from
**3.746%** to **0.430%**, with **87.007%** coverage. When `Unknown` values
produced by abstention are included in actual placement, the end-to-end failure
rates are **3.995%** and **2.874%**, respectively.

In an independent rolling-overlap experiment, epoch cohorts reduce HugeTLB
retained byte-epochs by **50%** in the critical release window, by **33.33%**
over the `target + reset` window, and by **20%** over the complete three-stage
cycle. Their allocation/deallocation lifecycle cost increases by **35.224%**,
with a bootstrap 95% interval of **[+34.002%, +36.436%]**. This result supports
the fragmentation mechanism in which epoch isolation reduces cross-stage page
pinning, while fast-path optimization and benefits on real applications remain
the next evidence gate.

## Mechanism: static speculation, runtime truth, and physical orchestration

```text
exact (callsite, type_id, module_id)
  -> profile-v2: {Ephemeral | LongLived, confidence 1..100}
  -> confidence threshold
       accepted -> lifetime_hint 1/2
       abstain  -> Unknown(0) -> existing allocator
  -> lifetime/size/alignment extent + exact 64 KiB identity region
  -> explicit phase advance
  -> death epoch truth + TP/TN/FP/FN
  -> optional epoch-cohort 2 MiB extent isolation
```

The static layer uses `unialloc-lifetime-profile-v2`. Each profile record
contains an exact `(callsite, type_id, module_id)`, a binary lifetime class, and
a `1..=100` confidence value. The compiler threshold accepts high-confidence
entries and abstains from low-confidence entries with
`profile_below_confidence_threshold`; missing entries, mismatched type/module
guards, duplicates, and malformed entries also become `Unknown`. The v1 profile
format remains compatible and is treated as confidence 100.

The runtime layer records `birth_epoch` on every routed allocation generation.
When deallocation occurs in the same epoch, the actual class is ephemeral; when
it crosses at least one explicit phase boundary, the actual class is
long-lived. A long-lived prediction is predictor-positive, and actual HugeTLB
backing is placement-positive, so the runtime reports both the predictor
confusion matrix and the actual-placement confusion matrix. A moved realloc
ends the old generation and creates a new generation; an in-place realloc
preserves the original birth epoch.

The epoch-cohort policy continues to use lifetime class to select
HugeTLB/ordinary backing and requires distinct birth epochs to use distinct
2 MiB extents. Available regions from an old epoch are detached from the
current allocation list, and a completely empty extent is unmapped
immediately. This constraint extends the physical page-orchestration signal
from "static object class" to "class + lifecycle stage."

## Statistical definitions

- **Object count** denotes allocation generations. One moved realloc
  contributes two generations, the old and the new; an in-place realloc remains
  the same generation.
- The **runtime byte confusion matrix** uses the allocator's rounded arena slot
  bytes. This definition corresponds to the physical page-occupancy granularity.
- The **effective placement byte confusion matrix** uses requested payload
  bytes. This definition includes `Unknown` fallback in end-to-end placement
  quality.
- **coverage** = statically classified generations / all allocation generations.
- **selective success/failure** is computed only over the classified set;
  success = `(TP+TN)/decided`.
- **end-to-end placement** covers all generations. An `Unknown` short fallback
  counts as TN, and an `Unknown` long fallback counts as FN.
- The runtime predictor matrix covers generations that enter the experimental
  arena and have an observable death epoch. Delayed frees and non-cohort
  mixed-epoch regions enter independent exclusion counts. Exclusions are 0 in
  both the formal main experiment and the rolling experiment.

Runtime truth in a real deployment must cover the profile candidate set. An
executable path is to collect the death-epoch distribution for every candidate
site with threshold 0 or shadow instrumentation, calibrate class/confidence,
and then raise the threshold. The current implementation provides an aggregate
runtime confusion matrix; per-site online updates remain future work. The
synthetic probe additionally knows the generation labels for `Unknown` values,
so it can report the complete effective-placement matrix.

## Experimental setup

### Classification, memory, and access experiment

| Item | Configuration |
|---|---|
| Host / pinning | AMD EPYC 9354; CPU 15; NUMA node 0 |
| HugeTLB pool | 4,096 x 2 MiB = 8 GiB; every claim-bearing case requires real HugeTLB with zero fallback |
| Per process | 262,144 simultaneously live 4 KiB objects; 50% long; 8 exact types / truth class |
| Phase trace | One persistent-long cohort and 3 ephemeral waves; 524,288 allocation generations per run |
| Scan | 4 warmup passes; 64 measured dependent-pointer passes |
| Repetitions | 10 fresh processes per case; prediction trace digests are exactly paired within each triplet |
| Confidence | Threshold 80; correct outcomes at confidence 90; incorrect outcomes at confidence 60; 10% confidence/outcome overlap |
| Error arms | 5% symmetric: long->short and short->long at 5% each; FP-heavy: short->long 5%, long->short 0% |

The 10% confidence/outcome overlap causes some correct predictions to abstain
and allows a small fraction of incorrect predictions through the threshold. It
constructs noisy but informative synthetic confidence; confidence still
contains 10% outcome-overlap noise.

### Rolling epoch-cohort experiment

| Item | Configuration |
|---|---|
| Host / pinning | CPU 10; NUMA node 0; real HugeTLB required |
| Geometry | One exact identity; 4 KiB slots; large cohort with 496 objects / 31 regions; bridge with 256 objects / 16 regions |
| Pair | Ordinary `long-huge` and `epoch-cohort` use the same seed, trace digest, and live-set geometry |
| Cycle | overlap -> target after releasing the large cohort -> reset; 5 warmup + 20 measured cycles |
| Repetitions | 30 randomized fresh-process pairs; 10,000 bootstrap resamples |

This geometry creates controlled overlap between the bridge cohort and the
preceding and following large cohorts. The two policies have identical live
byte-epochs; their difference comes from cross-epoch extent reuse and pinning.

## Classification success and failure rates

The FP/FN values below are allocation-generation counts aggregated over 10
runs. `confidence` and `confidence+epoch` use exactly the same prediction trace,
so their classification values are identical; epoch changes only the physical
extent cohort.

| Error model | Policy | Coverage | Selective success | Selective failure | Selective precision | Selective recall | End-to-end success | End-to-end failure | End-to-end recall | FP / FN |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 5% symmetric | binary | 100.000% | 95.007% | 4.993% | 86.376% | 95.012% | 95.007% | 4.993% | 95.012% | 196,420 / 65,374 |
| 5% symmetric | confidence | 86.012% | 99.419% | 0.581% | 98.281% | 99.415% | 96.005% | 3.995% | 85.514% | 19,605 / 6,596 |
| 5% symmetric | confidence + epoch | 86.012% | 99.419% | 0.581% | 98.281% | 99.415% | 96.005% | 3.995% | 85.514% | 19,605 / 6,596 |
| FP-heavy 5% | binary | 100.000% | 96.254% | 3.746% | 86.967% | 100.000% | 96.254% | 3.746% | 100.000% | 196,420 / 0 |
| FP-heavy 5% | confidence | 87.007% | 99.570% | 0.430% | 98.365% | 100.000% | 97.126% | 2.874% | 89.999% | 19,605 / 0 |
| FP-heavy 5% | confidence + epoch | 87.007% | 99.570% | 0.430% | 98.365% | 100.000% | 97.126% | 2.874% | 89.999% | 19,605 / 0 |

Confidence abstention reduces false positives by **90.019%** in both error
arms. In the symmetric arm, it also reduces false negatives by **89.910%**.
Selective failure decreases by **88.364%** and **88.528%**, respectively; after
counting abstained long objects as actual-placement FNs, end-to-end failure
still decreases by **19.985%** and **23.279%**, respectively. These two sets of
values distinguish "a purer classifier" from "the system ultimately placed the
object on the correct page." Abstained long generations enter ordinary
fallback, reducing end-to-end recall from **95.012%** to **85.514%** and from
**100.000%** to **89.999%**, respectively; this is the HugeTLB coverage cost of
reducing false-positive contamination.

## Memory, fragmentation, and timing controls

### Complete baseline matrix under correct labels

The confidence cases in the main table use 10% confidence overlap, so about 10%
of correct labels enter `Unknown` fallback. `binary` and `oracle` deliberately
use equivalent correct labels in this arm; the former is the baseline for the
error triplet, and the latter is the truth ceiling. Effective resident memory
is `VmRSS + HugetlbPages`.

| Policy | Static coverage | Steady effective resident | Steady arena retained | Steady arena slack | Peak HugeTLB | Alloc ns/generation | Dependent ns/touch |
|---|---:|---:|---:|---:|---:|---:|---:|
| raw default | — | 1,043.13 MiB | — | — | 0 MiB | 1,850.51 | 113.22 |
| ordinary segregated | 100% | 527.75 MiB | 512 MiB | 0 MiB | 0 MiB | 1,877.83 | 93.70 |
| all HugeTLB segregated | 100% | 527.74 MiB | 512 MiB | 0 MiB | 1,024 MiB | 395.14 | 106.84 |
| binary long->HugeTLB | 100% | 527.75 MiB | 512 MiB | 0 MiB | 512 MiB | 1,121.35 | 82.36 |
| confidence | 90.006% | 580.94 MiB | 462 MiB | 1.12 MiB | 462 MiB | 1,239.61 | 101.29 |
| confidence + epoch | 90.006% | 588.27 MiB | 462 MiB | 1.12 MiB | 462 MiB | 1,229.82 | 85.72 |
| oracle long->HugeTLB | 100% | 527.76 MiB | 512 MiB | 0 MiB | 512 MiB | 1,117.32 | 108.39 |

The steady effective resident reductions for ordinary, all-HugeTLB, binary,
and oracle relative to raw default are all about **49.4%**. This shared result
attributes the primary memory benefit to lifetime segregation and
complete-extent release. Binary/oracle reduce peak HugeTLB from 1,024 MiB under
all-HugeTLB to 512 MiB, demonstrating long-only HugeTLB placement. Confidence
further reduces accepted-long HugeTLB demand to 462 MiB while leaving abstained
long objects in the existing allocator; consequently, arena retained is lower,
while whole-process resident memory is higher than under the fully classified
512 MiB controls.

### Contamination and fragmentation under misclassification

| Error model | Policy | Steady resident | Steady retained | Steady slack | Peak HugeTLB | Retained byte-epochs |
|---|---|---:|---:|---:|---:|---:|
| 5% symmetric | binary | 952.60 MiB | 937 MiB | 425 MiB | 514 MiB | 2,811 MiB·epochs |
| 5% symmetric | confidence | 679.71 MiB | 520 MiB | 79.41 MiB | 442 MiB | 1,560 MiB·epochs |
| 5% symmetric | confidence + epoch | 687.11 MiB | 520 MiB | 79.41 MiB | 442 MiB | 1,560 MiB·epochs |
| FP-heavy 5% | binary | 553.78 MiB | 538 MiB | 26 MiB | 538 MiB | 1,614 MiB·epochs |
| FP-heavy 5% | confidence | 604.32 MiB | 465 MiB | 4.17 MiB | 465 MiB | 1,395 MiB·epochs |
| FP-heavy 5% | confidence + epoch | 611.68 MiB | 465 MiB | 4.17 MiB | 465 MiB | 1,395 MiB·epochs |

Under symmetric error, confidence reduces retained byte-epochs/steady retained
by **44.504%** relative to binary and reduces steady slack by **81.315%**. Under
FP-heavy error, retained decreases by **13.569%** and slack decreases by
**83.954%**. The higher FP-heavy steady resident reflects the RSS cost of
returning `Unknown` values to the existing allocator; retained arena capacity
and HugeTLB footprint still decrease together. This is the explicit space
tradeoff of confidence abstention.

### Timing and PMU

The point median `ns/touch` values in the table lack a stable monotonic
relationship. The paired bootstrap results likewise leave direct latency open:
the improvement interval for oracle long->HugeTLB relative to
ordinary-segregated is **[-15.19%, +2.16%]**; under 5% symmetric error, the
interval for confidence+epoch relative to binary is **[-10.64%, +13.84%]**.
These intervals cross zero, so the timing verdict for the main experiment is
**INCONCLUSIVE**.

Whole-process `perf stat -r 5` diagnostics with the same seed on CPU 15 show:

| Pair | cycles | L1 dTLB misses | L2 dTLB misses | 4 KiB reload L2 misses | page faults |
|---|---:|---:|---:|---:|---:|
| ordinary-segregated -> correct binary | -6.175% | -20.323% | -80.511% | -85.424% | — |
| 5% symmetric binary -> confidence | +23.033% | — | +85.789% | — | +80.635% |

The first row supports the translation mechanism of long-only HugeTLB. The
second row shows that aggressive abstention exchanges lower misplacement and
page pinning for more ordinary/default pages, increasing whole-process cycles,
TLB misses, and faults. The PMU scope includes allocation, first touch, warmup,
scan, phase release, and teardown; its role is directional mechanism diagnosis,
while the main paired timing CI remains responsible for the latency verdict.
The average running ratio for TLB events is about 62%, and perf applied scaling;
the running ratio for the page-fault event is 100%.

## Independent incremental effect of epoch cohorts

In the persistent-long main experiment, confidence and confidence+epoch have
identical allocator-retained, HugeTLB, and ordinary byte-epoch counters. The
long cohort in that trace remains live throughout, so it provides little power
to identify additional reclaim value from epoch isolation. The rolling-overlap
experiment specifically creates this identification window.

| Window | `long-huge` retained byte-epochs | `epoch-cohort` retained byte-epochs | Reduction | Live byte-epochs change |
|---|---:|---:|---:|---:|
| overlap | 80 MiB·epochs | 80 MiB·epochs | 0% | 0% |
| target: after releasing the preceding large cohort | 80 MiB·epochs | 40 MiB·epochs | **50.00%** | 0% |
| reset | 40 MiB·epochs | 40 MiB·epochs | 0% | 0% |
| target + reset | 120 MiB·epochs | 80 MiB·epochs | **33.33%** | 0% |
| full cycle | 200 MiB·epochs | 160 MiB·epochs | **20.00%** | 0% |

All 30 pairs have the same ratio, and their prediction trace, parameters, live
set, and checksum are paired; the runtime validates 19,296 generations per
sample, all 19,296 as TP, with 0 exclusions, HugeTLB fallbacks, mapping
failures, and release failures; the global 4,096-page pool is fully restored
after each process exits. In the target window, bridge objects pin 4 MiB under
ordinary long-huge and 2 MiB under epoch-cohort. This difference comes directly
from cohort extent isolation.

The cost is equally clear: epoch-cohort allocation time increases by
**46.692%**, release time by **19.408%**, and combined lifecycle ns/object by
**35.224%**. The relative cost of an explicit epoch advance is about 7.90x; the
absolute median total time for 60 measured advances is 13.77 us. The current
implementation is a research path with a global lock and fixed table;
`FRAGMENTATION-GO` applies to the mechanism, and `INCONCLUSIVE` applies to the
performance benefit.

## Boundary with the closest prior work

| System | Established mechanism | Supportable incremental contribution from this experiment | Numerical comparison boundary |
|---|---|---|---|
| [LLAMA, ASPLOS 2020](https://colinraffel.com/publications/asplos2020learning.pdf) | Allocation-context lifetime prediction, multi-timescale lifetime classes, 2 MiB lifetime placement, and dynamic feedback/misprediction recovery | Rust exact `(callsite,type,module)` profile binding, compiler metadata continuity, confidence abstention, and a runtime death-epoch confusion matrix | LLAMA uses production service code and traces; this experiment uses fixed geometry and synthetic confidence. The fragmentation/accuracy values from the two experiments use different workloads and denominators. |
| [TEMERAIRE, OSDI 2021](https://www.usenix.org/system/files/osdi21-hunter.pdf) | Hugepage packing, tail donation, and subrelease based on size, page fullness, and longest-free-range state | Semantic lifetime/confidence/epoch as an orchestration input beyond size/fullness, with rolling-cohort isolation of cross-stage pinning | TEMERAIRE is evaluated in a production allocator across multiple applications; this experiment is a single-size-class allocator microprobe. A direct percentage comparison lacks a shared baseline. |

The paper point should therefore be positioned as a **Rust compiler/runtime
contract for validated lifetime-guided hugepage orchestration**. Prior work has
already established lifetime-aware hugepage placement, dynamic error recovery,
and fullness-aware packing. The new evidence from this experiment is that an
exact Rust site can carry class + confidence, a runtime death epoch can quantify
its correctness, confidence can control contamination, and an epoch can control
cross-stage physical page reuse.

## Defensible statements and open boundaries

### Defensible

- Runtime death epochs provide closed predictor/placement TP/TN/FP/FN for
  explicit-phase workloads; confusion and allocator-accounting closure pass for
  every formal sample.
- Under 5% synthetic error, the confidence threshold substantially improves
  selective accuracy and continues to reduce end-to-end placement failure
  after abstention is included.
- Confidence reduces false-positive contamination, retained arena capacity,
  and slack under both error models.
- Epoch cohorts reduce target-window and full-cycle retained byte-epochs in a
  rolling trace with the same live set.
- Every claim-bearing mapping uses real 2 MiB HugeTLB, fallback is 0, and
  the persistent pool has 4,096 free pages both before and after the experiments.

### Open boundaries

- Profile confidence uses synthetic calibration; the current data do not come
  from training and held-out validation on real Rust workloads.
- Runtime epoch truth depends on a semantically meaningful phase boundary.
  Service workloads require a request, transaction, GC-like epoch, or time-window
  definition.
- The current matrix fixes 4 KiB slots, single-host NUMA pinning, and a limited
  number of exact types; multi-size-class behavior, cross-thread caches,
  concurrent phases, and long-running service traces remain to be evaluated.
- Explicit HugeTLB `munmap` returns pages to the persistent pool; returning
  ordinary Linux RAM requires pool shrink, surplus, or a THP path.
- The rolling microprobe measures a +35.224% lifecycle cost. Real-application
  throughput, tail latency, RSS/PSS, HugeTLB coverage, and combined PMU benefits
  determine the final performance conclusion.

## Evidence sources

- `docs/evidence/lifetime-confidence-epoch-final-20260714/main-summary.json`
- `docs/evidence/lifetime-confidence-epoch-final-20260714/main-samples.jsonl`
- `docs/evidence/lifetime-confidence-epoch-final-20260714/rolling-summary.json`
- `docs/evidence/lifetime-confidence-epoch-final-20260714/rolling-samples.jsonl`
- `docs/evidence/lifetime-confidence-epoch-final-20260714/perf/perf5-manifest.json`
- `docs/evidence/lifetime-confidence-epoch-final-20260714/perf/perf5-*.stdout`

The strongest presentation order is: **static class+confidence -> runtime
death-epoch truth -> selective and end-to-end error rates ->
contamination/slack reduction -> rolling epoch reclaim -> explicit lifecycle
cost and real-workload gate.**
