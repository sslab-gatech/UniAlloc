# Lifetime-guided payload arenas

## Scope

The `lifetime_hugepage` experiment turns `AllocationMetadata::lifetime_hint`
into an opt-in payload-placement decision.  It keeps the default allocator
unchanged and adds three hosted policies:

| Policy | Ephemeral objects | Long-lived objects | Unknown objects |
|---|---|---|---|
| `Disabled` | existing allocator | existing allocator | existing allocator |
| `SegregatedOrdinary` | ordinary 2 MiB extent | separate ordinary 2 MiB extent | existing allocator |
| `LongLivedHugepage` | ordinary 2 MiB extent | 2 MiB HugeTLB extent | existing allocator |
| `SegregatedHugepage` | 2 MiB HugeTLB extent | separate 2 MiB HugeTLB extent | existing allocator |

Every routed extent belongs to one coarse
`(lifetime class, size class, alignment class)` bucket. Each 2 MiB extent is
split into 32 fixed 64 KiB identity regions. A region belongs to one exact
semantic identity `(type, module, flags, lifetime, placement)` while live;
different identities can share an extent and never share a region.

## Integration boundary

1. Semantic allocation resolves exact metadata and runs guard-page precedence.
2. A known lifetime class asks the feature-gated arena for a slot before the
   semantic object cache or raw allocator miss.
3. Arena success publishes one raw-allocation lifecycle witness and continues
   through the existing recovery, tag, initialization, and statistics finish
   path.
4. Arena-origin provenance is address based.  Every raw terminal release,
   including a Drop without active metadata and a moved reallocation, checks
   the arena before dispatching to the ordinary backend.
5. Arena-owned semantic frees bypass the per-object TLS type cache.  Retaining
   even a small number of cache entries could pin complete 2 MiB extents.
6. `lifetime_hugepage_phase_flush_current_thread` turns a known phase boundary
   into terminal release of that thread's semantic caches and quarantine.

## Arena structure

- Process-global spin-locked state; it supports cross-thread frees without
  allocating allocator metadata recursively.
- Fixed descriptor and address-index tables cover at most the configured 8 GiB
  experimental pool.
- One 2 MiB aligned mapping per extent.
- Existing UniAlloc size classes plus alignment-aware slot strides.
- Bump allocation for never-used slots and an intrusive free list stored in
  released slots.
- One available-extent list per coarse bucket. Lookup prefers an existing
  matching identity region, then an unassigned region, and promotes the chosen
  extent to the list head.
- Exact identities co-pack at 64 KiB granularity. An empty region can be
  reassigned only after every slot previously owned by its identity is freed.
- Immediate checked `munmap` when an extent becomes empty.  A failed unmap
  leaves the descriptor published and reusable.
- An atomic zero-active-extents check keeps raw traffic out of the arena lock
  when no routed mapping exists.

## Required invariants

1. `Unknown` and unsupported layouts always use the existing allocator.
2. A pointer has exactly one backend owner for its complete lifetime.
3. Region assignment and reuse bind exact semantic identity; pointer release
   validates the extent base, region, slot boundary, class, and allocated slot
   range.
4. Policy changes succeed only with zero live arena objects and zero retained
   mappings.
5. HugeTLB fallback is observable.  Claim-grade runs require zero fallback.
6. Reallocation may stay in place only when the new layout fits the original
   arena slot; class changes move through the normal semantic transaction.
7. Every empty extent either unmaps successfully or remains indexed and
   reusable.

## Prototype boundaries

- One process-global lock serializes routed allocation, release, and lookup.
  The current evidence covers the placement mechanism and single-thread probe;
  scalable multithread throughput remains an evaluation item.
- A raw-backend reallocation that moves through the internal raw API can lose
  lifetime placement. Semantic reallocation preserves placement metadata.
- `lifetime_hugepage_phase_flush_current_thread` drains all retained semantic
  cache and delayed-free entries on the calling thread, including entries that
  originate in other semantic paths. Same-thread reentry is ignored by a TLS
  guard.
- The experiment's `exact` synthetic mode uses distinct Rust-like type IDs for
  the long and ephemeral cohorts. `lifetime-only` removes that type boundary;
  it measures the value of exact identity when program types correlate with
  lifetime cohorts.

## Evaluation gates

- Regression: exact-hint routing, Unknown fallback, cross-scope Drop,
  same-class reuse, class-changing realloc, checked teardown, and fixed-heap
  feature compatibility.
- Mechanism: ordinary segregation versus long-lived HugeTLB placement with
  equal `(lifetime,size)` packing.
- Robustness: 0.1%, 1%, and 5% label corruption, type-diversity sweeps, and
  phase-shift traces.
- Cost: allocation/deallocation throughput, pointer-chain latency, retained
  bytes, live slot bytes, reusable unassigned-region capacity, assigned-region
  slack, complete extents returned, RSS/PSS, HugeTLB fallback, and
  dTLB/page-walk PMU counters. `retained_slack = retained - live slots` includes
  both immediately reusable free regions and slack inside identity-bound
  regions; reports keep those components separate.
- Presentation: distinguish manual-oracle, profile-predicted, and MIR-proven
  hint sources.  A production-performance claim requires predicted hints on
  real workloads; the integrated arena alone supports a mechanism claim.
