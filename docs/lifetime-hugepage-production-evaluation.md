# Lifetime-guided HugeTLB payload allocation: integrated design and evaluation

## Decision

**GO as a qualifier and paper mechanism point. Production-workload benefit remains a follow-on claim.**

UniAlloc now has an exact, fail-closed rustc profile transport for semantic
allocation and Drop scopes, plus a production allocator path that consumes the
same lifetime class and reclaims complete 2 MiB extents at a phase boundary.
The compiler transport and allocator consumption paths are execution-tested
separately in this evidence package. The integrated allocator oracle experiment
cut peak HugeTLB demand by 50% and steady effective resident memory by 49.52%.
Whole-process PMU measurements reduced L2 dTLB misses by 96.07%. The paired
dependent-touch result was a 3.04% improvement with a 95% bootstrap interval of
`[-4.07%, +9.57%]`, so the direct latency result remains inconclusive.

The presentable result is a compatible compiler/runtime placement contract with
measured allocator-side reclamation and translation effects. A single-binary
compiler-profile-to-HugeTLB gate and a trained classifier on real Rust
applications are the next evidence gates.

## Claim, mechanism, evidence, boundary, next experiment

| Layer | Statement |
|---|---|
| Claim | Rust semantic identity can make a lifetime hint actionable for HugeTLB payload placement while preserving conservative fallback and exact reuse boundaries. |
| Mechanism | Exact `(callsite, type_id, module_id)` profile lookup selects `Unknown`, `Ephemeral`, or `LongLived`; 2 MiB extents are bucketed by lifetime/size/alignment; 32 × 64 KiB regions preserve exact `(type, module, flags, lifetime, placement)` identity. |
| Evidence | 180/180 production-path matrix samples passed; peak HugeTLB demand fell from 512 to 256 pages; steady effective resident memory fell by 49.52%; L2 dTLB misses fell by 96.07%; every arena mapping and identity region was released. |
| Boundary | Labels are manual oracle or injected-error labels. Compiler profile transport and production payload consumption are separately execution-tested. A single binary connecting both paths, plus real-workload classifier coverage, accuracy, throughput, and memory benefit, remain open. |
| Next experiment | Connect the profiled compiler rewrite to the production HugeTLB allocator in one binary, then generate profiles from conservative MIR ownership/drop/escape proofs plus allocation-context training and run application workloads with per-site accuracy, coverage, and end-to-end performance. |

## Implemented path

### Exact compiler profile

The rustc MIR rewrite driver accepts:

```text
--unialloc-lifetime-profile <path>
UNIALLOC_LIFETIME_PROFILE=<path>
```

Profile format:

```text
unialloc-lifetime-profile-v1
# callsite type_id module_id class
0xd082983f56011871 0xefa40a42f3015a14 0xfa02c02b1264f343 ephemeral
0xd670bc4f00ad57ef 0xefa40a42f3015a14 0xfa02c02b1264f343 long-lived
```

The lookup key is exact `(callsite, type_id, module_id)`. Missing entries,
stale type/module guards, malformed rows, invalid classes, duplicate keys, and
invalid headers produce `Unknown(0)`. A profile presence overrides an
invocation-wide hint, including on misses. Audit output records the selection
basis, match/miss counts, source path, raw-byte FNV fingerprint, and parser
failures.

A two-pass process test collects the allocation and Drop identities, creates a
profile, performs the actual MIR rewrite against a dependency-free runtime stub,
executes the binary, and observes only the requested long-lived hint. Ownership
transfer rewrites preserve the existing metadata instead of consuming a new
profile row.

### Payload allocator

Feature: `lifetime_hugepage = ["type_isolation"]`.

| Policy | Ephemeral | Long-lived | Unknown |
|---|---|---|---|
| `Disabled` | existing allocator | existing allocator | existing allocator |
| `SegregatedOrdinary` | ordinary 2 MiB extents | separate ordinary 2 MiB extents | existing allocator |
| `LongLivedHugepage` | ordinary 2 MiB extents | 2 MiB HugeTLB extents | existing allocator |
| `SegregatedHugepage` | HugeTLB extents | separate HugeTLB extents | existing allocator |

The placement lookup runs before semantic TLS cache reuse. Guard-page precedence
remains intact. Ordinary experimental mappings receive `MADV_NOHUGEPAGE`, and
claim-bearing HugeTLB runs require zero fallback.

Each extent has one coarse `(lifetime, size class, alignment class)` identity.
Its 32 fixed 64 KiB regions allow different exact Rust identities to co-pack.
One live region belongs to one exact identity. A region becomes assignable to a
new identity after its last slot is released. Empty extents are checked and
immediately unmapped.

Address provenance selects the release backend after semantic scope exit and on
a foreign thread. Semantic realloc preserves placement metadata; fitting
same-identity realloc stays in place and a lifetime-class change moves with a
payload copy. Delayed-free eviction and an explicit current-thread phase flush
perform terminal arena release. A TLS guard ignores same-thread phase-flush
reentry.

### Accounting contract

The probe hard-gates these relationships for peak, steady, and final snapshots:

```text
current extents = HugeTLB extents + ordinary extents
live objects = ephemeral objects + long-lived objects
retained bytes = current extents × 2 MiB
routed allocations = bump allocations + reuse hits
routed allocations = routed deallocations at teardown
identity-region assignments = identity-region releases at teardown
```

Memory slack is reported as separate components:

```text
reusable unassigned region bytes
  = unassigned 64 KiB regions in retained extents
assigned-region slack bytes
  = assigned region capacity - live rounded slots
retained slack bytes
  = reusable unassigned bytes + assigned-region slack
```

`stranded_bytes` remains a compatibility alias for retained slack. Fragmentation
claims use retained bytes and the two explicit slack components.

## Experimental configuration

| Item | Configuration |
|---|---|
| Host | 2-socket AMD EPYC 9354, 64 cores / 128 logical CPUs, 2 NUMA nodes |
| Kernel | Linux 6.8.0-111-generic x86_64 |
| HugeTLB pool | 4,096 × 2 MiB = 8 GiB persistent pool; node 0: 2,722 pages; node 1: 1,374 pages |
| Pinning | CPU 0 and NUMA memory node 0 |
| Workload | 262,144 × 4 KiB objects = 1 GiB peak payload; 50% long-lived; two ephemeral waves |
| Scan | Four warmup passes; 64 measured dependent-pointer passes |
| Matrix | 12 cases × 15 fresh processes = 180 samples, randomized within repeat |
| Error sweep | Symmetric 0.1%, 1%, 5%, and 50% shuffled labels |
| Baselines | policy-off, ordinary-segregated, all-HugeTLB, long-only HugeTLB, exact identity, lifetime-only identity |
| Memory denominator | `VmRSS + HugetlbPages`, because explicit hugetlb memory has separate `/proc` accounting |
| Integrity | HugeTLB fallback is fatal; mapping, accounting, release, and global-pool restoration are hard gates |

The global pool provides eight times the 1 GiB peak requirement of this
integrated matrix; the CPU-0-pinned node-0 pool provides 5.32 times the peak.
`munmap` returns explicit pages to the persistent HugeTLB pool. Shrinking
the persistent pool is the separate operation that returns this reserved RAM to
the ordinary page allocator. See the [Linux HugeTLB documentation](https://docs.kernel.org/admin-guide/mm/hugetlbpage.html)
and [`/proc` memory accounting documentation](https://docs.kernel.org/filesystems/proc.html).

Reproduction:

```bash
uv run python evaluation/scripts/lifetime_hugepage_allocator_experiment.py \
  --run-id lifetime-hugepage-production-final-20260714-0408 \
  --objects 262144 --slot-bytes 4096 --types-per-truth 1 \
  --long-fraction 0.5 --error-rates 0.001,0.01,0.05 \
  --include-shuffled --include-lifetime-only-baseline \
  --ephemeral-waves 2 --warmup-passes 4 --passes 64 --repeats 15 \
  --numa-node 0 --cpu 0 --seed 20260714
```

Tracked evidence:

- `docs/evidence/lifetime-hugepage-production-final-20260714/manifest.json`
- `docs/evidence/lifetime-hugepage-production-final-20260714/production-samples.jsonl`
- `docs/evidence/lifetime-hugepage-production-final-20260714/production-summary.json`
- `docs/evidence/lifetime-hugepage-production-final-20260714/type-diversity-summary.json`
- `docs/evidence/lifetime-hugepage-production-final-20260714/perf/`

The manifest records source commit `079711f`, commands, host configuration,
artifact hashes, and the synthetic-label evidence boundary.

## Results

### Correct oracle labels

| Metric | Baseline | Lifetime-tiered | Effect |
|---|---:|---:|---:|
| Peak explicit HugeTLB pages | all-HugeTLB: 512 | long-only HugeTLB: 256 | 50.00% reduction |
| Steady effective resident memory | policy-off: 1,066,076 KiB | long-only HugeTLB: 538,200 KiB | 49.52% reduction |
| Steady retained arena capacity | peak: 1 GiB | after ephemeral phase: 512 MiB | 50.00% returned |
| HugeTLB fallbacks | 0 | 0 | hard gate passed |
| Pool free pages, before → after | 4,096 | 4,096 | fully restored |

The ordinary-segregated control reached the same 49.52% steady resident
reduction. This control attributes the memory result to phase/lifetime
segregation and complete-extent release. HugeTLB placement supplies the
translation effect.

### Misclassification sensitivity and exact identity

The exact synthetic mode assigns distinct Rust-like type IDs to the long and
ephemeral cohorts. The lifetime-only control removes that identity boundary.
It measures the value of exact identity when program type cohorts correlate
with object residence.

| Symmetric label error | Exact retained | Lifetime-only retained | Retained reduction | Exact retained slack | Lifetime-only slack | Slack reduction |
|---:|---:|---:|---:|---:|---:|---:|
| 0.1% | 530 MiB | 724 MiB | 26.80% | 18 MiB | 212 MiB | 91.51% |
| 1% | 676 MiB | 1,022 MiB | 33.86% | 164 MiB | 510 MiB | 67.84% |
| 5% | 1,024 MiB | 1,026 MiB | 0.19% | 512 MiB | 514 MiB | 0.39% |
| 50% shuffled | 1,026 MiB | 1,026 MiB | 0% | 514 MiB | 514 MiB | 0% |

Exact identity materially bounds low-rate contamination. The benefit collapses
at 5% random per-object error in this adversarial trace. This curve provides a
classifier-quality requirement: strong allocation-context accuracy or an
online promotion/deadline mechanism is required before broad workload claims.

Most exact-mode slack in the error cases is fully unassigned 64 KiB capacity
inside extents pinned by a sparse misclassified cohort. Assigned-region internal
slack stays at 64 KiB at the median. The reported decomposition keeps reusable
capacity visible and avoids presenting every retained byte as identity-pinned
internal fragmentation.

### Dependent-touch timing

| Comparison | Paired geometric-mean improvement | Bootstrap 95% interval | Decision |
|---|---:|---:|---|
| long-only HugeTLB vs ordinary-segregated | 3.04% | `[-4.07%, +9.57%]` | inconclusive |

Median `ns/touch` was 91.247 for long-only HugeTLB and 92.038 for
ordinary-segregated. The paired interval crosses zero, so the paper point should
use the PMU result as mechanism evidence and keep direct latency open.

### Whole-process PMU check

Five `perf stat` repetitions used 256 measured pointer-chain passes. All events
ran at 100%. Counters include allocation, first touch, warmup, measured scan,
phase release, and teardown.

| Counter | Ordinary-segregated | Long-only HugeTLB | Reduction |
|---|---:|---:|---:|
| cycles | 15.877 B | 14.428 B | 9.13% |
| instructions | 4.473 B | 3.597 B | 19.58% |
| page faults | 396,534 | 265,718 | 32.99% |
| all L2 dTLB misses | 36,352,418 | 1,427,294 | 96.07% |
| 4 KiB L2 dTLB misses | 35,610,654 | 952,838 | 97.32% |
| 2 MiB L2 dTLB misses | 387,559 | 224,369 | 42.11% |

The whole-process scope supports the expected translation mechanism. The
dependent-touch loop has a separate paired timing result, which remains
inconclusive.

### Low-occupancy type diversity

A separate 100-process structural sweep used 320 objects and 1, 8, 32, 40, or
64 exact types per truth cohort.

| Types per cohort | Exact peak / steady extents | Lifetime-only peak / steady extents |
|---:|---:|---:|
| 1 | 2 / 1 | 2 / 1 |
| 8 | 2 / 1 | 2 / 1 |
| 32 | 2 / 1 | 2 / 1 |
| 40 | 4 / 2 | 2 / 1 |
| 64 | 4 / 2 | 2 / 1 |

The 32-region extent geometry explains the threshold. Exact isolation can add
one partially occupied extent per lifetime class once low-occupancy identities
exceed 32. The integration regression also packs 40 exact identities from one
coarse class into the theoretical two extents and verifies sentinel integrity,
intermediate first-extent reclaim, region spill, and post-final-slot reassignment.
This is a measured isolation-versus-granularity tradeoff.

## Relationship to prior work

- [LLAMA, ASPLOS 2020](https://colinraffel.com/publications/asplos2020learning.pdf)
  already establishes allocation-context lifetime prediction and lifetime-aware
  2 MiB placement, including error recovery.
- [TEMERAIRE, OSDI 2021](https://www.usenix.org/system/files/osdi21-hunter.pdf)
  already establishes fullness-aware hugepage packing and subrelease in a
  production allocator.
- [Google's warehouse-scale TCMalloc study, ASPLOS 2024](https://people.csail.mit.edu/delimitrou/papers/2024.asplos.memory.pdf)
  already establishes short/long span segregation using allocator-derived
  capacity as a lifetime proxy.

The defensible novelty slice is therefore the following combination. This is an
inference from the cited systems and their published mechanisms:

1. exact Rust `(callsite, type, module)` profile binding with fail-closed
   `Unknown` fallback;
2. compiler metadata continuity through allocation, ownership transfer, semantic
   realloc, Drop, and cross-thread raw release;
3. two-level placement combining lifetime/size/alignment extents with exact
   64 KiB identity regions;
4. an explicit Rust phase boundary that drains retained semantic state and makes
   complete-extent reclamation observable.

Heap residence classification requires ownership, escape, Drop, and profile
evidence; Rust borrow regions supply reference-use facts. The [NLL RFC](https://rust-lang.github.io/rfcs/2094-nll.html),
[rustc MIR dataflow guide](https://rustc-dev-guide.rust-lang.org/mir/dataflow.html),
and [Rust Drop-scope reference](https://doc.rust-lang.org/reference/destructors.html)
provide the semantic foundation and its limits.

## Presentation contract

### Defended statements

- UniAlloc turns a Rust lifetime metadata field into actual payload-page
  placement and complete-extent reclamation.
- Exact profile mismatch and ambiguity preserve the existing allocator path.
- Oracle labels halve HugeTLB demand and steady resident memory in the synthetic
  phase trace.
- Exact identity reduces retained slack by 91.51% at 0.1% error and 67.84% at
  1% error relative to lifetime-only packing.
- The integrated path reduces whole-process L2 dTLB misses by 96.07%.
- Allocation/Drop, moved semantic realloc, delayed release, phase flush, and
  cross-thread release preserve backend provenance in regression tests.

### Open statements

- Real Rust application coverage and classifier accuracy.
- End-to-end application throughput and memory benefit.
- Scalable multithread allocator throughput with the current global spin lock.
- Automatic recovery from high error rates.
- THP/surplus-page behavior and release of reserved persistent-pool RAM to the
  ordinary page allocator.

### Recommended two-slide story

**Slide 1 — Make lifetime hints actionable**

```text
MIR/profile evidence
  -> exact fail-closed site class
  -> semantic allocation/Drop continuity
  -> (lifetime,size,align) 2 MiB extent
  -> exact 64 KiB identity region
  -> phase flush and whole-extent release
```

Show `Unknown → existing allocator` under the pipeline. This communicates the
safety boundary immediately.

**Slide 2 — Mechanism works; classifier defines the ceiling**

- `512 → 256` peak HugeTLB pages.
- `-49.52%` steady effective resident memory.
- `-96.07%` whole-process L2 dTLB misses.
- exact identity slack reduction: `91.51% @ 0.1% error`, `67.84% @ 1%`,
  `0.39% @ 5%`.
- direct dependent-touch timing: inconclusive.
- next gate: trained per-site profiles on real Rust workloads.

A concise defense statement is:

> UniAlloc demonstrates a Rust semantic contract whose compiler transport and
> HugeTLB payload consumer agree on an exact lifetime-bearing identity. Oracle
> labels establish allocator-side placement and reclamation; the error curve
> quantifies the classifier quality required for application benefit. A
> single-binary compiler-to-HugeTLB run is the final integration gate.

## Remaining engineering risks

- One process-global spin lock covers extent search, mapping, and unmapping.
- Negative address lookup can lengthen after sustained tombstone churn while an
  unrelated extent stays live; the table resets when all extents are released.
- Raw internal moved realloc allocates through the raw backend and can lose
  lifetime placement; semantic realloc retains the contract.
- The phase-flush API drains all retained semantic cache and delayed-free state
  on the calling thread.
- Exact 64 KiB regions trade contamination isolation for low-occupancy type
  rounding.
- The classifier/profile generator remains external to this implementation.

The next implementation milestone is a conservative MIR/profile classifier plus
real-workload evaluation. The current result is ready to present as a bounded
compiler-runtime allocator mechanism.
