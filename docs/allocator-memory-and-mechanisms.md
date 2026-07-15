# Allocator Memory and Mechanisms

This document is the canonical committee-facing guide to UniAlloc's memory-retention controls, Type Isolation memory behavior, and lifetime-guided large-page mechanisms. It combines the current evidence into one claim ledger and keeps historical prototypes and diagnostics in explicitly bounded roles. Full sample tables remain in the linked machine artifacts and detailed records.

## Evidence hierarchy

| Evidence family | Current role | Primary artifact |
| --- | --- | --- |
| Type Isolation primary suite | Current application-level memory claim | [`benchmark-results/type-isolation-primary-v1.json`](../benchmark-results/type-isolation-primary-v1.json) |
| Lifetime-aware THP cross-allocator campaign | Current synthetic causal and backing claim | [`docs/evidence/cross-allocator-large-page-final-20260714/summary.json`](evidence/cross-allocator-large-page-final-20260714/summary.json) |
| Allocator feature and THP matrix | Current-host configuration diagnostic | [`benchmark-results/allocator-feature-thp-evaluation-20260714.json`](../benchmark-results/allocator-feature-thp-evaluation-20260714.json) |
| Lifetime THP and runtime collapse | Supporting synthetic mechanism evidence | [`docs/evidence/lifetime-thp-final-20260714/summary.json`](evidence/lifetime-thp-final-20260714/summary.json) |
| Production-path HugeTLB arena | Supporting compiler-to-allocator mechanism evidence | [`docs/evidence/lifetime-hugepage-production-final-20260714/production-summary.json`](evidence/lifetime-hugepage-production-final-20260714/production-summary.json) |
| Confidence and death-epoch experiments | Supporting classification and reclaim evidence | [`docs/evidence/lifetime-confidence-epoch-final-20260714/`](evidence/lifetime-confidence-epoch-final-20260714/) |
| ripgrep/fd/Oxipng allocator matrix | Historical allocator-policy diagnostic | [`benchmark-results/realworld-rust-allocator-20260714.json`](../benchmark-results/realworld-rust-allocator-20260714.json) |
| Bump-only HugeTLB feasibility probe | Historical phase-1 mechanism record | [`docs/evidence/lifetime-hugepage-oracle-chain-final-20260714/summary.json`](evidence/lifetime-hugepage-oracle-chain-final-20260714/summary.json) |

The current committee presentation uses [`docs/allocator-evaluation.md`](allocator-evaluation.md) for allocator performance and Type Isolation policy cost. The large-page presentation uses the matched causal pairs and backing measurements in this document. Historical rows remain useful for mechanism diagnosis and receive no weight in current aggregates.

## Memory metrics and accounting

The evidence uses several memory metrics with distinct scopes:

| Metric | Meaning | Claim scope |
| --- | --- | --- |
| GNU Time `%M` / `ru_maxrss` | Maximum process RSS in KiB | End-to-end process peak for fixed-work paired runs |
| `/proc/<pid>/smaps_rollup` RSS/PSS/anonymous fields | Sampled mapping totals | Mapping and anonymous-memory attribution |
| `AnonHugePages` | Anonymous THP physically backing the process | Actual THP backing |
| `HugetlbPages` | Explicit HugeTLB pages mapped by the process | Actual explicit-pool backing |
| `VmRSS + HugetlbPages` | Effective resident memory used by large-page experiments | Combined ordinary and explicit-HugeTLB process residency |
| Allocator retained/slack counters | Retained extent capacity and unused capacity inside retained extents | Allocator-structure and fragmentation mechanism |
| Requested live bytes | Application payload demand | Required denominator for allocator-efficiency claims |

Peak RSS includes application state, code, stacks, allocator metadata, caches, and resident mappings. It records one high-water mark. Steady-state residency, time at peak, proportional sharing, cooldown retention, and requested live bytes require separate measurements.

Linux reports explicit HugeTLB pages separately from `VmRSS` and GNU Time peak RSS on this host. A footprint analysis of an explicit-HugeTLB arm must add `HugetlbPages` or use the experiment's effective-resident metric. `MADV_HUGEPAGE` proves eligibility; per-process `AnonHugePages` proves physical THP backing. `MADV_COLLAPSE` proves a point-in-time collapse request; subsequent procfs evidence closes the steady-backing claim.

Fixed-work RSS and adaptive-process RSS remain separate:

- Oxipng, redb, and Polars execute fixed work in the primary suite. Their 14 harnesses support equal-work peak-RSS aggregation.
- Collections, SWC, RustPython, and Actix Web use workload-native adaptive iteration counts. Their 20 harnesses retain process-volume RSS diagnostics and stay outside the fixed-work RSS geometric mean.
- The Actix `async_service_direct` audit found an iteration-count/RSS fit of `R² = 0.999955`, directly supporting the process-volume interpretation. See [`actix-async-service-direct-adaptive-rss-audit.json`](evidence/type-isolation-primary-suite-20260714/actix-async-service-direct-adaptive-rss-audit.json).

## Memory-retention architecture

### Thread caches

Hosted builds use a thread-local hot cache. `fixed_heap` builds use one process-wide `ThreadCache` protected by `GLOBAL_TCACHE_LOCK`, because that configuration has no hosted TLS contract. The fixed-heap cache is race-safe and serialized.

The ordinary retention policy is:

```text
soft flush threshold = min(256 KiB / object_size, 4096 objects)
post-flush target    = min( 64 KiB / object_size, 1024 objects)
hard threshold       = soft threshold * THREAD_CACHE_BLOCKING_FLUSH_MULTIPLIER
```

A soft flush attempts a nonblocking return of the cold suffix to the slab allocator. A busy slab restores the suffix and uses a geometric retry schedule. The hard threshold permits a blocking return, bounding retention under sustained lock contention.

`thread_cache_footprint_snapshot()` recomputes local-list and bump-batch footprint without adding per-object metadata to the hot path. Stats builds also expose `thread_cache_flush_stats_snapshot()` and `thread_cache_flush_stats_reset()`.

### Slabs and hosted page runs

UniAlloc combines object-count and OS-page budgets:

- empty slabs retain a small warm set, approximately eight OS pages per class and 32 OS pages globally in the evaluated implementation;
- large empty spans return immediately;
- aligned page runs return unused slack;
- strict-alignment misses retain a small hot prefix and release colder surplus;
- active-class and retained-class bitsets constrain trimming work to classes with visible state;
- released hosted mappings use `munmap`.

`hosted_bitmap_page_run_snapshot()` reports live and warm arenas, allocations, mapped payload, and metadata mappings. `hosted_page_run_memory_probe` combines those counters with explicit page touches, `smaps_rollup`, process faults, thread-cache state, zone retention, and route counts. The retained five-process capture is `benchmark-results/hosted-page-run-memory-overhead-ab.jsonl`.

### Semantic and Type Isolation caches

Type Isolation authorizes reuse through complete allocator-visible identity:

```text
(type_id, module_id, policy flags, lifetime_hint, placement_hint,
 size, alignment, structural authentication)
```

The hash selects a candidate location. Exact field equality authorizes reuse. Callsite remains provenance and compiler-coverage evidence; allocation-site identity does not restrict allocator reuse after the exact semantic identity matches.

The hosted implementation bounds semantic retention at several levels:

- semantic cache admission accepts objects through 64 KiB;
- a hosted plain exact-layout slot can hold 1,024 linked owners within 128 KiB;
- all hosted plain linked owners are capped at 4,096 objects and 512 KiB per thread;
- segregated buckets keep two inline hot entries and four cold entries, with 64 KiB per bucket and 512 KiB aggregate retained bytes per thread;
- delayed-free quarantine is capped at 256 KiB of allocator-rounded storage per thread;
- saturation bypasses the semantic cache and returns the object to the ordinary allocator;
- inactive side-table records remain zero-filled and demand-zero;
- ordinary and hugepage metadata use separate inline hot slots and release empty hugepage metadata mappings.

A bounded process-wide exact-type L2 depot handles producer/consumer handoff after the TLS L1. The evaluated configuration has eight shards, 512 payload slots per shard, a 64 KiB payload limit per shard, and four probes in each 32-key directory. Its process-wide payload budget is 512 KiB. This design recovered useful cross-thread reuse in the fd diagnostic and avoided the full-directory miss cost observed in the larger-probe experiment.

The process-visible ownership registry prevents duplicate cache ownership while a pointer is retained. Hosted builds use eight shards with 1,024 primary slots per shard and a probe limit of 64; `fixed_heap` uses 128 slots per shard and a probe limit of 32. Exact entry epochs and an equal-count generation-history tier bound recent-generation authorization. Full, ambiguous, or displaced-history paths fall back conservatively. The registry is a bounded ownership mechanism for allocator caches; stale-pointer detection and universal concurrent linearizability remain outside its contract.

Relevant diagnostics are:

- `metadata_segregation_side_cache_snapshot()`;
- `hugepage_metadata_side_cache_snapshot()`;
- `hugepage_metadata_side_cache_backing_snapshot()`;
- Type Isolation admission, depot, registry-full, and corruption counters under `stats`.

### Fragmentation controls

The allocator uses compact fixed-size bitmaps, exact-size freelist pruning, alignment-aware page reuse, stack/local seen-bitmaps for validation, and fallible metadata growth. Lifetime arenas add explicit retained-capacity accounting:

```text
reusable unassigned region bytes
  = free 64 KiB identity regions in retained extents
assigned-region slack bytes
  = assigned region capacity - live rounded slots
retained slack bytes
  = reusable unassigned bytes + assigned-region slack
```

These counters distinguish reusable empty regions from internal slack in a live exact-identity region.

## Lifetime-guided page placement

### Compiler and runtime contract

`AllocationMetadata` carries `type_id`, `module_id`, policy flags, `lifetime_hint`, `placement_hint`, and callsite provenance. The profile lookup uses exact `(callsite, type_id, module_id)` keys and returns `Ephemeral`, `LongLived`, or `Unknown`. Low confidence, missing keys, stale guards, duplicate ambiguity, malformed rows, and unsupported layouts resolve to `Unknown` and follow the existing allocator path.

MIR rewriting carries the selected metadata through allocation, ownership transfer, semantic realloc, Drop, delayed release, and foreign-thread release. Address provenance selects the correct backing during deallocation after semantic-scope exit.

Continuous confidence remains in the compiler/profile layer. The allocator receives the thresholded discrete lifetime class, keeping reuse identity and ABI behavior explicit.

### Placement geometry and backends

Lifetime policy and physical page backend are orthogonal:

| Policy | Ephemeral | Long-lived | Unknown |
| --- | --- | --- | --- |
| `Disabled` | Existing allocator | Existing allocator | Existing allocator |
| `SegregatedOrdinary` | Ordinary arena | Separate ordinary arena | Existing allocator |
| `LongLivedHugepage` | Ordinary arena | Large-page arena | Existing allocator |
| `SegregatedHugepage` | Large-page arena | Separate large-page arena | Existing allocator |
| `EpochCohortHugepage` | Ordinary arena | Survival candidate | Existing allocator |

`LifetimePageBackend` selects `TransparentHugepage` or `ExplicitHugeTLB`. Existing `lifetime_hugepage_configure(policy)` calls retain the explicit-HugeTLB behavior; `lifetime_hugepage_configure_with_backend(policy, backend)` makes the physical backend explicit.

The allocator has two geometry levels:

1. A 2 MiB extent groups objects by lifetime, size class, alignment class, and optional epoch cohort.
2. A 64 KiB identity region serves one exact live allocator identity. One extent contains 32 regions.

An empty region can accept a new exact identity. An empty extent is unmapped immediately. The 32-region geometry creates a measured granularity boundary: 40 or 64 low-occupancy exact types require four peak extents, while 32 or fewer fit in two for the evaluated two-lifetime trace.

### Eager selective THP

The current deployment candidate is `LongLivedHugepage + TransparentHugepage`:

```text
LongLived -> 2 MiB-aligned anonymous VMA -> MADV_HUGEPAGE
Ephemeral -> aligned ordinary VMA -> MADV_NOHUGEPAGE
Unknown   -> existing allocator
```

Anonymous THP uses ordinary memory and kernel-managed promotion. It requires no application-reserved persistent HugeTLB pool. The formal causal control preserves the same lifetime layout and trace, sets `PR_SET_THP_DISABLE=1`, confirms `PR_GET_THP_DISABLE=1`, and requires `AnonHugePages=0`.

### Runtime survival validation

`EpochCohortHugepage` starts accepted-long candidates with `MADV_NOHUGEPAGE`. A candidate that survives an explicit epoch boundary and retains at least 50% occupancy receives `MADV_HUGEPAGE` followed by `MADV_COLLAPSE`. Short objects released before the boundary remain on ordinary pages. Sparse survivors increment the low-occupancy-skip counter.

The 50% threshold is a feasibility parameter. The current synchronous collapse executes under the arena lock and has high lifecycle and transient-memory cost. The production direction is an asynchronous promotion queue with occupancy, memory, collapse-latency, and THP-split budgets.

### Explicit HugeTLB

The explicit backend uses 2 MiB `MAP_HUGETLB` mappings and a configured persistent pool. `munmap` restores pages to that pool. Returning reserved RAM to the ordinary Linux page allocator requires a separate pool shrink or surplus-page policy. Claim-bearing runs require zero fallback and record pool restoration.

## Current Type Isolation memory result

The primary suite contains seven targets and 34 harnesses. It uses one warmup and five paired measured rounds for UniAlloc, the typed route with policy off, and Type Isolation with policy on. The implementation is fixed at:

```text
Git revision:      f5d0c19c1cc5b56fdac3282d69333dd8c85d4cf2
Canonical SHA-256: cab1e580c08e2b16308bae75501716049ba428b040fb04bf305269c9ba9eaf01
Canonical scope:  131 files, 4,426,670 bytes
```

The fixed-work RSS result is:

> Across 14 workloads in Oxipng, redb, and Polars, Type Isolation relative to the same typed compiler/runtime route with policy disabled has an across-target geometric mean peak-RSS ratio of **`1.000570x` (`+0.057%`)**.

This is the current committee-facing Type Isolation memory claim. The all-process adaptive RSS rows remain visible diagnostics. Compiler-route equivalence passes for 13 of 34 workloads and fails for 21, so compiler-route, policy-increment, and end-to-end effects remain separate. The current result status is `complete_with_attribution_limits`.

Provenance is dual-recorded:

- original result-blind manifest SHA-256: `96fa64009550d52f89549dd2124e3b1a402a42b7f1fd1750888600dd67bb8a6f`;
- active analysis manifest SHA-256: `ba386a779e45e0884e02cf8825c69985d1b5cf68d57366af78e7f77cfa8f6ccb`;
- the RSS amendment changes interpretation and aggregate eligibility while preserving target membership, pins, harnesses, variants, definitions, and sampling.

Detailed methods and current micro/macro results are in
[`docs/allocator-evaluation.md`](allocator-evaluation.md). Every target-local
pin, harness row, round, and provenance field remains in
[`benchmark-results/type-isolation-primary-v1.json`](../benchmark-results/type-isolation-primary-v1.json)
and the [`primary-suite evidence index`](evidence/type-isolation-primary-suite-20260714/README.md).

## Allocator and THP configuration diagnostic

The 2026-07-14 feature matrix separates Cargo closure from active runtime mechanism. Its Oxipng source is v4.0.3 at `dea23211ae6259007e068c59ab16929798d00d96`; this source differs from the current primary-suite Oxipng pin.

The 15-cell Oxipng matrix contains system, mimalloc THP on/off, TCMalloc, and these UniAlloc closures:

| UniAlloc cell | Cargo defaults | Effective closure | Evidence role |
| --- | --- | --- | --- |
| No optional features | Off | none | Allocator baseline |
| `rseq` | Off | `rseq` | Build-selected module |
| `pthread_dtor` | Off | `pthread_dtor` | Build-selected closure |
| `hugepage` | Off | `hugepage` | Runtime-confirmed explicit HugeTLB mappings |
| `separate_sc_backend` | Off | `separate_sc_backend` | Compile-time slab backend |
| `type_isolation` | Off | `type_isolation` | Runtime closure on an untyped application |
| `metadata_segregation` | Off | `metadata_segregation + separate_sc_backend + type_isolation` | Backend closure on an untyped application |
| `pac` | Off | `pac + type_isolation` | x86-64 software-fallback closure |
| Default | On | `pthread_dtor + rseq` | Repository default |
| `typed_plain` | On | default + `type_isolation` | Actual-MIR route, policy off |
| `typeiso_perf` | On | default + `type_isolation` | Actual-MIR route, policy on |

All 75 measured Oxipng processes passed output and evidence gates. The three-cell Rsedis matrix used fresh servers and passed all 18 fresh-server correctness and activation gates.

Key current-host observations are:

- Disabling mimalloc THP on the same Oxipng binary changed the median paired wall-time ratio by `+2.26%` and peak RSS by `-23.61%`.
- On the Rsedis service diagnostic, disabling mimalloc THP changed throughput by `+0.22%`, peak RSS by `-85.08%`, and post-load `AnonHugePages` by `-36,864 KiB`.
- gperftools TCMalloc 2.18.1 issued no `MADV_HUGEPAGE`, recorded `AnonHugePages=0`, and used no memfs HugeTLB path. Its precise label is **TCMalloc default, effectively THP-inactive under the measured host's `madvise` policy**.
- UniAlloc's `hugepage` Cargo feature produced four successful 2 MiB `MAP_HUGETLB` mappings. The process sampler observed 8,192 KiB of `HugetlbPages`, which GNU Time peak RSS omitted.
- The actual-MIR Type Isolation policy/control pair measured `+1.73%` wall time and approximately zero paired peak-RSS change in this older Oxipng diagnostic. The primary suite supplies the current policy claim.

The mimalloc identity is Rust wrapper `mimalloc 0.1.25`, `libmimalloc-sys 0.1.49`, and embedded mimalloc core 3.3.2. The TCMalloc identity is gperftools 2.18.1 `libtcmalloc.so.4.6.5`, SHA-256 `3e9994675a51a1a7f893e02236b36e3150a37b5e8ea740e0cc78f6650f7b1841`.

`MIMALLOC_ALLOW_THP=0` invokes a process-wide THP disable in the measured mimalloc core. Both mimalloc THP arms set `MIMALLOC_ALLOW_LARGE_OS_PAGES=0` and `MIMALLOC_RESERVE_HUGE_OS_PAGES=0`, isolating the transparent-page option.

The current `rseq` feature row proves build selection. Repository audit found no allocator callsite outside the rseq module, and glibc's rseq registration remained at its default. An allocator-owned registration path must consume libc-managed rseq state or disable glibc registration before registering another per-thread area. The `pthread_dtor` row also remains build-selection evidence until runtime callsites and counts establish activation. The x86-64 PAC row exercises software fallback; hardware PAC cost requires an ARM host with the corresponding mechanism.

## Lifetime-aware THP result

### Causal decomposition

The formal cross-allocator campaign uses a 512 MiB peak requested payload, a 50/50 lifetime mix, one discarded warmup block, 20 measured randomized paired blocks, and 20,000 fixed-seed bootstrap resamples. CPU 15 and NUMA node 0 are fixed. Every claim-bearing arm passes actual-backing and integrity gates.

| Comparison | Dependent touch | Allocate/fault/free lifecycle | Steady resident | Peak resident |
| --- | ---: | ---: | ---: | ---: |
| Lifetime layout, THP forced off vs default | `+7.28%` `[+5.65%, +8.80%]` | `-22.70%` `[-23.36%, -22.06%]` | `-8.35%` `[-8.85%, -7.84%]` | `-0.261%` |
| Selective THP vs same layout forced off | **`+10.37%` `[+6.72%, +14.05%]`** | **`+31.99%` `[+31.78%, +32.18%]`** | `+0.283%` cost | `+0.334%` cost `[+0.309%, +0.358%]` |
| Full feature vs default | **`+16.90%` `[+13.50%, +20.38%]`** | **`+16.55%` `[+15.98%, +17.09%]`** | **`-8.09%` `[-8.59%, -7.61%]`** | `+0.072%` cost |

Lifetime layout supplies the steady-residency reduction and adds lifecycle cost. Selective THP recovers the lifecycle path and improves dependent-touch latency. The same-layout on/off pair isolates physical THP eligibility and actual backing.

### Within-allocator large-page controls

| Matched pair | Touch effect, 95% CI | Actual backing | Maximum effective resident change |
| --- | ---: | ---: | ---: |
| UniAlloc selective THP | **`+10.37%` `[+6.72%, +14.05%]`** | 258 MiB THP | `+0.334%` |
| mimalloc global THP | **`+7.42%` `[+3.75%, +9.73%]`** | 516 MiB THP | `+0.842%` |
| jemalloc allocator THP | `+4.51%` `[-0.70%, +8.32%]` | 516 MiB THP | `+0.789%`; inconclusive |
| gperftools explicit HugeTLB | **`+37.00%` `[+36.02%, +37.91%]`** | 514 MiB HugeTLB | `+0.550%` |
| snmalloc process eligibility | `-1.53%` `[-3.31%, -0.01%]` | 0 MiB | approximately zero |

Each percentage is an allocator's own on/off effect. UniAlloc uses a semantic Rust probe; the third-party allocators share one neutral C probe. The cross-family claim covers incremental effects, actual backing, and deployment mechanism. Absolute endpoint ranking remains within each probe family.

On this 50/50 trace, semantic selection uses 258 MiB of THP while the mimalloc and jemalloc global controls use 516 MiB. This supports the committee statement that selective lifetime placement halves actual THP backing on the evaluated trace while UniAlloc's matched pair records a 10.37% speedup.

Slide-ready assets are:

- [`incremental-effect.svg`](figures/cross-allocator-large-page-20260714/incremental-effect.svg);
- [`actual-backing.svg`](figures/cross-allocator-large-page-20260714/actual-backing.svg);
- [`lifetime-aware-feature-decomposition.svg`](figures/lifetime-aware-thp-design-20260714/lifetime-aware-feature-decomposition.svg);
- [`lifetime-aware-thp-pipeline.svg`](figures/lifetime-aware-thp-design-20260714/lifetime-aware-thp-pipeline.svg).

## Runtime validation and explicit-HugeTLB evidence

### Survival-gated THP

The separate 80-process lifetime-THP experiment uses a 1 GiB initial trace and synthetic 5% false-long plus 5% false-short classification noise. Eager long-only THP provides 514 MiB of actual THP, compared with 1,028 MiB for all-THP. Relative to the same lifetime packing on ordinary pages, eager THP improves dependent touch by `8.45%` with a 95% interval of `[+2.39%, +13.65%]`.

Runtime survival validation changes placement failure from `4.990%` to `1.263%`, a `74.68%` reduction, and records zero false-positive physical placements. The current synchronous collapse adds `57.79%` allocator lifecycle cost and `41.50%` maximum effective resident memory on this trace. This experiment has `claim_grade=false`; its role is mechanism validation and engineering direction.

### Production-path explicit HugeTLB

The compiler-to-HugeTLB integration gate executes a real UniAlloc global allocator with exact profile-derived MIR metadata. It records three exact profile matches, zero misses, three rewrites, one real HugeTLB extent, zero fallback, one matching routed release, one `munmap`, and persistent-pool restoration.

The 180-sample synthetic production-path matrix records:

- peak HugeTLB demand of 512 pages for all-HugeTLB and 256 pages for long-only HugeTLB, a 50% reduction;
- steady effective resident memory of 1,066,076 KiB for policy-off and 538,200 KiB for long-only HugeTLB, a 49.52% reduction;
- a 96.07% whole-process reduction in all L2 dTLB misses for long-only HugeTLB relative to ordinary segregation;
- a dependent-touch improvement of 3.04% with a 95% interval of `[-4.07%, +9.57%]`, yielding an inconclusive direct-latency verdict;
- full release of every arena mapping and identity region.

The ordinary-segregated control reaches the same steady-resident reduction, attributing that memory result to lifetime segregation and complete-extent release. Explicit HugeTLB supplies the translation mechanism.

Exact identity bounds low-rate contamination relative to lifetime-only packing. Retained-slack reductions are `91.51%` at 0.1% symmetric error, `67.84%` at 1%, `0.39%` at 5%, and 0% under a shuffled 50% trace. The curve defines a classifier-quality requirement for this geometry.

### Confidence and death epochs

In the confidence experiment, an 80-point threshold under 5% symmetric error changes classified failure from `4.993%` to `0.581%` at `86.012%` coverage. Counting abstained long objects as ordinary-placement false negatives gives `3.995%` end-to-end failure. Under FP-heavy 5% error, classified failure changes from `3.746%` to `0.430%` at `87.007%` coverage, with `2.874%` end-to-end failure.

The rolling epoch-cohort experiment holds live byte-epochs constant and changes physical cohort reuse. It reduces retained byte-epochs by 50% in the target release window, 33.33% across target plus reset, and 20% across the complete cycle. The current implementation adds 35.224% allocation/deallocation lifecycle cost with a bootstrap 95% interval of `[+34.002%, +36.436%]`.

These results support confidence abstention, runtime truth, and epoch isolation as orchestration inputs. Real-service phase definitions, asynchronous promotion, and scalable concurrency remain the production gates.

## Historical and diagnostic evidence

### Older three-application Type Isolation matrix

The historical ripgrep/fd/Oxipng matrix used implementation-bundle SHA-256 `7e98e63ce2fbeccc361ea57bd26773ccdb02664b83d772f0475161c980c55929`. Type Isolation relative to the same typed policy-off route changed peak RSS by 0%, 0%, and `+0.036%`, respectively. The Type Isolation rows were 37.449% to 67.444% lower than default mimalloc in those three pinned workloads.

An exploratory sensitivity run with `MIMALLOC_PURGE_DELAY=0 MIMALLOC_ALLOW_THP=0` brought mimalloc to parity on ripgrep and fd and within 0.62% of Type Isolation on Oxipng. This establishes allocator purge/THP policy as the main explanation for the large default gap. The current primary suite supplies the policy-memory claim; the older matrix supplies allocator-policy diagnosis.

The tracked historical result preserves every build, allocator identity, source commit, command, measurement, and direct comparison. Its SHA-256 is listed in the machine-artifact table below.

### Bump-only HugeTLB feasibility prototype

The phase-1 manual-oracle prototype used a 4 GiB bump-only trace. Lifetime segregation reduced retained capacity from 4 GiB to 2 GiB, returned 1,024 complete HugeTLB pages, and improved dependent random access by 20.87% relative to equally packed ordinary pages. Its fixed-size, single-phase, manual-oracle geometry is historical. The production arena, exact profile, misclassification sweep, and current THP backend supersede it for current claims.

The durable phase-1 evidence contains the exact five-policy samples, manifest, PMU records, and host configuration under [`docs/evidence/lifetime-hugepage-oracle-chain-final-20260714/`](evidence/lifetime-hugepage-oracle-chain-final-20260714/).

## Prior-work boundary

[LLAMA (ASPLOS 2020)](https://colinraffel.com/publications/asplos2020learning.pdf) establishes allocation-context lifetime prediction, multiple lifetime classes, 2 MiB placement, and dynamic misprediction recovery. [TEMERAIRE (OSDI 2021)](https://www.usenix.org/system/files/osdi21-hunter.pdf) establishes fullness-aware hugepage packing, tail donation, and subrelease. The [warehouse-scale TCMalloc study (ASPLOS 2024)](https://people.csail.mit.edu/delimitrou/papers/2024.asplos.memory.pdf) establishes allocator-derived short/long span separation and fleet-scale evaluation.

UniAlloc's defensible mechanism contribution is the combination of exact Rust profile identity, MIR metadata continuity through ownership and release, fail-closed `Unknown` fallback, exact-identity regions within lifetime extents, selective anonymous THP, and runtime survival as a physical-promotion input. Direct percentage comparisons with prior systems remain outside the claim because their allocators, workloads, denominators, and deployment environments differ.

## Claim ledger

### Defended current statements

- Type Isolation adds `+0.057%` to the across-target geometric mean peak RSS across 14 fixed-work workloads relative to the same typed compiler/runtime route with policy disabled.
- The full lifetime-aware feature improves dependent touch by 16.90%, improves allocate/fault/free lifecycle by 16.55%, and reduces steady effective resident memory by 8.09% on the formal synthetic trace.
- Selective THP adds a 10.37% dependent-touch improvement relative to the same lifetime layout with THP forced off, with a 95% interval fully above zero.
- Semantic selection uses 258 MiB of THP on the 50/50 trace, half the 516 MiB used by the measured global mimalloc and jemalloc THP controls.
- Exact Rust profile identity reaches a real HugeTLB allocation and matching release through actual MIR rewriting and the production allocator path.
- Runtime survival and confidence reduce synthetic placement error, and epoch isolation reduces retained byte-epochs under a controlled rolling trace.

### Current boundaries

- Type Isolation RSS is an end-to-end process peak. A live-byte denominator and time series remain future evidence.
- The primary suite covers pinned targets, inputs, one host, and five paired rounds. General allocator memory superiority remains open.
- Large-page results use single-host synthetic traces with fixed sizes, lifetime mixes, phase boundaries, and NUMA placement.
- Cross-family large-page percentages are within-allocator effects. Raw Rust/C endpoint rankings have separate probe semantics.
- Held-out lifetime classification, real-service throughput and P99 latency, multithreading, cross-NUMA behavior, long-run THP split/reclaim, and size-class sweeps remain open.
- Explicit HugeTLB relies on a reserved pool. Anonymous THP retains ordinary-memory deployment semantics.
- Synchronous epoch promotion has measured lifecycle and transient-memory costs. Asynchronous bounded promotion is the next implementation step.

## Reproduction and evidence index

### Type Isolation primary suite

```bash
TMPDIR="$PWD/evaluation/raw/.tmp" \
  uv run python evaluation/scripts/assemble_type_isolation_primary_results.py \
    --suite evaluation/config/type_isolation_primary_suite.json \
    --targets-dir evaluation/raw/type-isolation-primary-v1/targets \
    --output benchmark-results/type-isolation-primary-v1.json

TMPDIR="$PWD/evaluation/raw/.tmp" \
  uv run evaluation/scripts/plot_type_isolation_primary_suite.py

uv run evaluation/scripts/plot_two_tier_allocator_evaluation.py
```

The [`primary-suite evidence README`](evidence/type-isolation-primary-suite-20260714/README.md)
indexes source pins, implementation hashes, warmup attestations, and tracked
outputs. Exact target-local collection commands remain in the ignored raw
target bundles; the tracked assembler command above reconstructs the canonical
result from those validated bundles.

### Allocator feature and THP diagnostic

```bash
uv run evaluation/scripts/plot_allocator_feature_thp_slides.py
```

The tracked result embeds all measured rows, runtime preflights, source and binary hashes, feature closures, and host controls. The original host commands were:

```bash
python3 evaluation/scripts/realworld_type_isolation_matrix.py \
  --apps oxipng \
  --variants system,mimalloc,mimalloc_no_thp,gperftools_legacy,unialloc_no_optional,unialloc_rseq,unialloc_pthread_dtor,unialloc_hugepage,unialloc_separate_sc,unialloc_type_isolation,unialloc_metadata_segregation,unialloc_pac,unialloc,typed_plain,typeiso_perf \
  --quick --oxipng-threads 4 --warmups 1 --repetitions 5 \
  --toolchain nightly-2026-06-11 --cpu-list 20-23 --numa-node 0 \
  --jobs 32 \
  --gperftools-legacy-library /home/hanqing/.local/state/unialloc/runs/g001-minimal-235549b2-20260711T155042Z/evidence/pilot-controller/repairs/tcmalloc-full-runtime-v1-20260711T1717Z-attempt2/lib/libtcmalloc.so.4.6.5 \
  --discard-run-output \
  --raw-dir evaluation/raw/rust-alloc-paper-feature-thp-20260714/oxipng

python3 evaluation/scripts/rsedis_thp_matrix.py \
  --mimalloc-rsedis-binary evaluation/raw/rust-alloc-paper-rsedis-current-20260714/binaries/mimalloc/rsedis \
  --system-rsedis-binary evaluation/raw/rust-alloc-paper-rsedis-current-20260714/binaries/system_ptmalloc/rsedis \
  --tcmalloc-cell gperftools_legacy \
  --gperftools-legacy-library /home/hanqing/.local/state/unialloc/runs/g001-minimal-235549b2-20260711T155042Z/evidence/pilot-controller/repairs/tcmalloc-full-runtime-v1-20260711T1717Z-attempt2/lib/libtcmalloc.so.4.6.5 \
  --raw-dir evaluation/raw/rust-alloc-paper-feature-thp-20260714/rsedis \
  --warmups 1 --repetitions 5 --requests 100000 --clients 50 \
  --data-size 64 --server-cpus 64-67 --client-cpus 68-71 --numa-node 0
```

### Cross-allocator large-page campaign

```bash
PYTHONHASHSEED=0 python3 \
  evaluation/scripts/cross_allocator_large_page_experiment.py \
  --output-dir docs/evidence/cross-allocator-large-page-final-20260714 \
  --repeats 20 --warmup-blocks 1 --bootstrap-resamples 20000 \
  --objects 131072 --slot-bytes 4096 --passes 32 --warmup-passes 2 \
  --waves 2 --settle-ms 25 --types-per-truth 8 \
  --false-long-rate 0.05 --false-short-rate 0.05 \
  --cpu 15 --numa-node 0 \
  --tcmalloc-memfs-prefix /dev/hugepages/unialloc-eval/tcmalloc
```

Exact argv, library identities, host snapshots, validation output, and sample hashes are in [`runner-manifest.json`](evidence/cross-allocator-large-page-final-20260714/runner-manifest.json) and [`manifest.json`](evidence/cross-allocator-large-page-final-20260714/manifest.json).

### Lifetime THP and runtime validation

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

The confidence/death-epoch evidence is in [`docs/evidence/lifetime-confidence-epoch-final-20260714/`](evidence/lifetime-confidence-epoch-final-20260714/). The summary files preserve all formal parameters, paired traces, and integrity checks.

### Production-path HugeTLB

```bash
uv run python evaluation/scripts/lifetime_hugepage_allocator_experiment.py \
  --run-id lifetime-hugepage-production-final-20260714-0408 \
  --objects 262144 --slot-bytes 4096 --types-per-truth 1 \
  --long-fraction 0.5 --error-rates 0.001,0.01,0.05 \
  --include-shuffled --include-lifetime-only-baseline \
  --ephemeral-waves 2 --warmup-passes 4 --passes 64 --repeats 15 \
  --numa-node 0 --cpu 0 --seed 20260714

uv run python -m unittest -v \
  tools/unialloc-rustc-pass/test_mir_lifetime_hugepage_integration.py
```

The manifest records source commit `079711f81238d45a5e9dd8673e067cb335d5e39e`, the exact command, host configuration, and artifact hashes.

## Machine-artifact digests

These SHA-256 values identify the artifacts used for this consolidation:

| Artifact | SHA-256 |
| --- | --- |
| `benchmark-results/type-isolation-primary-v1.json` | `3e252a0f8ed1f6ca18c5b63f30acd48dcf5ed0280f29e8ecb1327b659333e8a5` |
| `benchmark-results/allocator-feature-thp-evaluation-20260714.json` | `293dbfd0e1b6966b4bb2e1884fd498994299e065fc5c240a337b18e43d9fd490` |
| `benchmark-results/realworld-rust-allocator-20260714.json` | `7598e2b06143b7bd3a73d8c599402fd3c8dd6bdddb60706d46f987b3b4fd2741` |
| `docs/evidence/cross-allocator-large-page-final-20260714/summary.json` | `8161635ad9cb402a906934e5780c70cf02ed93de02a10c9e1bd26fac5c3823e4` |
| `docs/evidence/lifetime-thp-final-20260714/summary.json` | `0fafe8b09b3be9354743bba6c8872a3fdcd70e0d1717d4cd96050399c78ac7ff` |
| `docs/evidence/lifetime-hugepage-production-final-20260714/production-summary.json` | `6a2818590c04af650ab37208ae2e2ffbf03d47d6418e59a919ed9613b85048ce` |
| `docs/evidence/lifetime-confidence-epoch-final-20260714/main-summary.json` | `4ffbf62b39d7f97340c64855cf7638f11f9b1fb87a6034a6b98f1a998a22bc07` |
| `docs/evidence/lifetime-confidence-epoch-final-20260714/rolling-summary.json` | `9af92d9dcf2f18c2e5d9721d3a1eaefa523b640c007f8b33790e7e4f4ce2cd2d` |
| `docs/evidence/lifetime-hugepage-oracle-chain-final-20260714/summary.json` | `ed35159be6879b34845dfe4c403a14698d8005edb724024d299b564e94667999` |
| `docs/figures/allocator-feature-thp-20260714/slide-data-manifest.json` | `a2e143a678c17027940b08eab4ebd8b6e49d089e3201c893407bd8571d35dcff` |
| `docs/figures/cross-allocator-large-page-20260714/manifest.json` | `d986c6fd51a8982a695c487ebdbb6bfddbf5c9417e5f7b3a4b7e73eb1c611b72` |

## Durable evidence and figure index

- Type Isolation: [`primary result`](../benchmark-results/type-isolation-primary-v1.json), [`implementation snapshot`](evidence/type-isolation-primary-suite-20260714/canonical-implementation-snapshot-f5d0c19.json), [`Actix adaptive-RSS audit`](evidence/type-isolation-primary-suite-20260714/actix-async-service-direct-adaptive-rss-audit.json), and [`figure manifest`](figures/type-isolation-primary-suite/type-isolation-primary-suite-manifest.json).
- Feature/THP diagnostic: [`tracked result`](../benchmark-results/allocator-feature-thp-evaluation-20260714.json), [`figure data manifest`](figures/allocator-feature-thp-20260714/slide-data-manifest.json), and [`figure bundle`](figures/allocator-feature-thp-20260714/).
- Cross-allocator large pages: [`summary`](evidence/cross-allocator-large-page-final-20260714/summary.json), [`samples`](evidence/cross-allocator-large-page-final-20260714/samples.jsonl), [`runner manifest`](evidence/cross-allocator-large-page-final-20260714/runner-manifest.json), [`verification`](evidence/cross-allocator-large-page-final-20260714/verification.txt), and [`figure manifest`](figures/cross-allocator-large-page-20260714/manifest.json).
- Lifetime THP: [`summary`](evidence/lifetime-thp-final-20260714/summary.json), [`samples`](evidence/lifetime-thp-final-20260714/samples.jsonl), [`runner manifest`](evidence/lifetime-thp-final-20260714/runner-manifest.json), and [`verification`](evidence/lifetime-thp-final-20260714/verification.txt).
- Production HugeTLB: [`manifest`](evidence/lifetime-hugepage-production-final-20260714/manifest.json), [`production summary`](evidence/lifetime-hugepage-production-final-20260714/production-summary.json), [`production samples`](evidence/lifetime-hugepage-production-final-20260714/production-samples.jsonl), [`type-diversity summary`](evidence/lifetime-hugepage-production-final-20260714/type-diversity-summary.json), and [`verification`](evidence/lifetime-hugepage-production-final-20260714/verification.txt).
- Confidence and death epoch: [`manifest`](evidence/lifetime-confidence-epoch-final-20260714/manifest.json), [`main summary`](evidence/lifetime-confidence-epoch-final-20260714/main-summary.json), [`rolling summary`](evidence/lifetime-confidence-epoch-final-20260714/rolling-summary.json), [`samples`](evidence/lifetime-confidence-epoch-final-20260714/main-samples.jsonl), and [`verification`](evidence/lifetime-confidence-epoch-final-20260714/verification.txt).
- Historical records: [`three-application allocator result`](../benchmark-results/realworld-rust-allocator-20260714.json), [`manual-oracle HugeTLB summary`](evidence/lifetime-hugepage-oracle-chain-final-20260714/summary.json), [`manual-oracle samples`](evidence/lifetime-hugepage-oracle-chain-final-20260714/samples.jsonl), and [`manual-oracle manifest`](evidence/lifetime-hugepage-oracle-chain-final-20260714/manifest.json).

The recommended committee sequence is: bounded Type Isolation RSS overhead; exact semantic identity and retention controls; lifetime layout plus selective THP causal decomposition; actual backing relative to global controls; runtime validation and its measured cost; real-workload and long-running evaluation boundaries.
