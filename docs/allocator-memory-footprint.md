# Allocator memory-footprint controls

This document explains the allocator mechanisms that bound retained memory and
fragmentation.  Hosted builds use a thread-local cache on the ordinary hot path;
`fixed_heap` builds intentionally use one process-wide cache protected by a
`spin::Mutex` because they cannot rely on hosted TLS.  It is intentionally a
design and usage note; long per-run validation logs belong in
`.omx/ultragoal/artifacts/` and `evaluation/raw/`.

## Design goals

- Keep the common hosted path thread-local and nonblocking where practical.
- Keep `fixed_heap` cache mutation race-safe through one guarded process-wide
  cache, accepting serialization in that configuration.
- Bound retained bytes in caches, slabs, and semantic side tables.
- Prefer small hot sets over unbounded warm caches.
- Repair or expose accounting drift instead of hiding it behind silent defaults.
- Make diagnostics cheap enough to use before running a slow benchmark matrix.

## 1. Thread-cache retention

File: `unialloc/src/cache/thread_cache.rs`

In hosted, non-`fixed_heap` builds, the thread cache keeps recently freed objects
thread-local to avoid allocator-wide contention.  Its risk is retained memory: a
thread can free many objects and then stop allocating that size class.  UniAlloc
therefore treats the cache as a bounded hot set rather than a large per-class
reservoir.

`fixed_heap` is deliberately different.  Allocation and deallocation enter
`with_fixed_tcache_mut`, which guards one process-wide `ThreadCache` with
`GLOBAL_TCACHE_LOCK`.  This avoids aliased mutable access and intrusive-list
corruption when TLS is unavailable, but it means the fixed-heap cache path is
serialized rather than thread-local/nonblocking.  See
`unialloc/src/cache/mod.rs` and the `fixed_tcache` module in
`unialloc/src/cache/thread_cache.rs`.

Policy summary:

```text
soft flush threshold  = min(256 KiB / object_size, 4096 objects)
post-flush target     = min( 64 KiB / object_size, 1024 objects)
hard flush threshold  = soft threshold * THREAD_CACHE_BLOCKING_FLUSH_MULTIPLIER
```

A soft flush first tries to return the cold suffix to the slab allocator with a
nonblocking slab-lock attempt.  If the slab is busy, the suffix is restored into
the cache list and retries use a geometric schedule (`1, 2, 4, 8, ...` overflow
lengths) so a contended class does not detach and relink the same suffix on every
free.  That list belongs to the current thread in hosted builds and to the
guarded process-wide cache in `fixed_heap`.  Past the hard cap, UniAlloc may use
the blocking return path so a busy slab cannot make a cache retain memory
indefinitely.

### Thread-cache diagnostics

All builds expose:

- `thread_cache_footprint_snapshot() -> ThreadCacheFootprintSnapshot`

With the `stats` feature, UniAlloc also exposes:

- `thread_cache_flush_stats_snapshot() -> ThreadCacheFlushStatsSnapshot`
- `thread_cache_flush_stats_reset()`

`ThreadCacheFootprintSnapshot` recomputes the selected cache's local-list and
bump-batch footprint, then reports it beside the maintained hot-path counters.
For hosted builds that is the current thread's cache; for `fixed_heap` it is the
guarded process-wide cache.
Use it to distinguish:

- no initialized cache vs. an initialized empty cache,
- one large retained class vs. broad cross-class pressure,
- real retained bytes vs. counter drift,
- soft/target/hard budget state.

The snapshot is read-only.  It does not add per-object metadata or extra atomics
to the allocation/free fast path.

## 2. Empty slab and page-run retention

Files include:

- `unialloc/src/sc/efficient_sc.rs`
- `unialloc/src/sc/separate_sc.rs`
- `unialloc/src/zone.rs`
- `unialloc/src/page/efficient_page.rs`
- `unialloc/src/freelist/bump.rs`

Slab and page-run caches are useful for bursty workloads, but they must be sized
in OS pages rather than only in object counts.  UniAlloc keeps a small warm set
for ordinary size classes and releases oversized or fully cold runs back to the
lower layer sooner.

Key rules:

- Empty slab retention is capped by both object-page count and OS-page span.
- Oversized aligned page-runs return unused slack instead of pinning neighboring
  free space.
- Strict-alignment misses keep only a small hot prefix and return colder surplus
  objects.
- Active-class and retained-class bitsets guide trimming so cleanup work is
  proportional to classes with visible state, not every possible class.

These rules reduce external fragmentation while keeping common same-class reuse
fast.

## 3. Semantic/type-isolation side caches

File: `unialloc/src/alloc_api/type_isolation.rs`

The semantic caches separate objects by compiler- or runtime-supplied type
identity.  They are bounded by retained bytes as well as entry counts:

- plain type cache slots track retained bytes and reject oversized/cold growth;
- plain inline and linked entries retain the exact allocator-visible identity
  fields (type, module, policy flags, lifetime, and placement) instead of
  trusting the 64-bit lookup key alone.  This prevents a cache-key collision
  from crossing those boundaries, at a measured 64-bit TLS footprint increase
  of 1,040 bytes per thread; callsite is intentionally excluded because cache
  reuse is allocation-site agnostic;
- metadata-segregated buckets keep per-bucket retained-byte counters;
- hosted metadata-segregated buckets retain four cold entries per bucket after
  the two inline hot entries; excess frees bypass the cache and return to the
  ordinary allocator rather than inflating every thread's TLS image;
- hosted memory-tag and same-thread recovery fast tiers keep 128 records
  (currently 8 KiB each on 64-bit targets) and preserve correctness through
  their existing overflow/global spill paths; `fixed_heap` keeps the prior
  larger capacities because hosted overflow mappings are unavailable there;
- delayed-free quarantine has its own retained-byte budget;
- hugepage metadata side-cache mappings are released when empty;
- inline side-cache entries serve the common one-object reuse case without
  materializing a full bucket table.
- ordinary and hugepage metadata keep separate one-entry inline hot slots, so a
  hot object in one domain does not force the other domain to allocate or scan a
  bucket table.
- retained semantic-cache pointers also enter the allocation-free,
  process-visible ownership registry added by `a93cf97`: eight shards with
  1,024 primary slots per shard on hosted targets and 128 per shard under
  `fixed_heap`, with bounded probe limits 64 and 32 respectively. Each primary
  slot has an exact entry epoch. An equal-count exact generation-history tier
  uses eight-way buckets so unrelated primary-hash colliders cannot invalidate
  one another. On 64-bit targets the table payload is approximately 273.1 KiB
  hosted and 34.2 KiB under `fixed_heap`, before lock padding. Duplicate
  publication fail-stops; a full primary probe window can replace a terminal
  `Released` entry, while tombstones preserve lookup and later safe slot reuse.
  The history tier is a recent-generation window: a ninth distinct address in
  one history bucket displaces the oldest missing record and returns that
  address to epoch-zero raw-only semantics.

Empty semantic side-table records use an all-zero field representation. Their
zero alignment is intentionally not a valid `Layout`: pointer/key/active
liveness is checked before layout reconstruction. This keeps inactive tables in
demand-zero storage instead of embedding initialized records in every binary and
thread image.

Commit `d87d5e0` introduced these hosted footprint reductions. Independent
review then exposed a saturation bug: a full matching bucket could spill the
same semantic identity into a neighboring bucket. Commit `25d316c` makes
matching entry-depth, per-bucket-byte, and aggregate-byte saturation bypass the
cache instead, while preserving neighbor probing for distinct colliding
identities. Hosted and `fixed_heap` type-isolation suites pass, including a
deterministic 512 KiB aggregate-cap regression that proves rejection does not
replace or shift any cached owner.

That process-visible type-cache ownership registry has a deterministic
probe-window regression at `45c5e8c`. It fills one shard/probe window with
`PROBE_LIMIT` synthetic aligned pointer keys, verifies that the next key returns
`Full`, removes an interior owner, then proves that lookup and duplicate
detection traverse the tombstone before the slot is reused. All keys are only
hashed, stored, and compared; they are never dereferenced or deallocated. The
test finishes with zero registered owners and passes in hosted and `fixed_heap`
configurations. This bounds registry-pressure and deletion-chain correctness;
it is not a universal address-collision or concurrent linearizability claim.
The registry only prevents duplicate ownership while a pointer is retained by
these caches; it is not universal stale-pointer or UAF detection and supplies
no performance claim.

A source-bound 64-thread diagnostic compares baseline `318b66c` with repaired
current `25d316c` using the same release harness, host, toolchain, and input,
with three interleaved measured runs per binary and no warmup or retry. Median
ready/peak/idle/sampled-max RSS moved from 9.578/66.969/71.188/71.922 MiB to
7.500/64.953/69.141/69.484 MiB. The sampled-max ranges overlap, so this is only
directional regression evidence, not a publication percentage or universal
performance result. Raw records and identities are preserved under
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/footprint-diagnostic-318b66c-25d316c-20260712/`.

The compiler metadata fast path uses the same cache classification policy as the
generic semantic path after the compiler ABI has already proved the typed,
type-isolated, non-layout-derived preconditions.  This avoids duplicated policy
logic while skipping redundant checks in the hot path.

Related diagnostics:

- `metadata_segregation_side_cache_snapshot()`
- `hugepage_metadata_side_cache_snapshot()`
- `hugepage_metadata_side_cache_backing_snapshot()`

Use these snapshots when inspecting C004/C005-style regressions.  They report
occupied buckets/entries, retained bytes, corrupt bucket visibility, and whether
hugepage metadata is backed by a true hugepage mapping or a compact ordinary
fallback mapping.

## 4. Internal and external fragmentation controls

UniAlloc uses several small mechanisms together instead of one large global
policy:

- compact bitmap representations for fixed-size object pages,
- exact-size freelist pruning for stale markerless heads,
- alignment-aware partial-page and page-run reuse,
- stack/local seen-bitmaps for batch validation,
- fallible metadata growth paths that fail closed instead of reserving large
  temporary buffers.

The intent is to avoid both internal waste (serving much larger cached blocks for
small layouts) and external waste (splitting or retaining page runs in ways that
make later aligned requests fail).

## 5. Validation entry points

Targeted checks used for recent footprint/cache work:

```sh
cargo +$(cat rust-toolchain) fmt --all -- --check
cargo +$(cat rust-toolchain) test -p unialloc --lib thread_cache -- --nocapture
cargo +$(cat rust-toolchain) test -p unialloc --lib --no-default-features \
  --features fixed_heap,type_isolation,stats thread_cache -- --nocapture
cargo +$(cat rust-toolchain) test -p unialloc --lib type_isolation -- --nocapture
cargo +$(cat rust-toolchain) test -p unialloc --lib \
  --features metadata_segregation,type_isolation,stats type_isolation -- --nocapture
```

Keep `fixed_heap` isolated with `--no-default-features`; it must not be mixed
with hosted TLS/rseq defaults.  Allocator `bench_*` selector features are
mutually exclusive, and Cargo feature lists are comma-separated.

The 2026-07-09 final audit passed 604 hosted/default library tests, 301
integration tests, 561 constrained fixed-heap tests, 686 tests in the combined
metadata-segregation/type-isolation/stats/hugepage/PAC/force-init configuration,
and the 430-benchmark std-bench surface.  The workspace all-target no-run gate
also passed.  These are implementation gates, not performance-claim evidence.

Real compiler-path validation is under `evaluation/scripts/evaluate.py`, for
example:

```sh
python3 evaluation/scripts/evaluate.py collect-rustc-driver-direct-allocator-mir-probe \
  --toolchain "$(cat rust-toolchain)" \
  --features bench_ourself,type_isolation,stats \
  --direct-local-size-align-with-semantic-drop \
  --no-update-results
```

Short performance smoke should stay narrow unless preparing claim-grade imports.
For example, use one std_bench row to check that a hot-path change is not an
obvious regression before spending time on the full matrix.

## 6. Evidence locations

- Claim status: `evaluation/results/claim_check_current.json`
- Claim blockers: `evaluation/results/overclaim_worklist.json`
- Raw benchmark/probe artifacts: `evaluation/raw/`
- OMX progress and quality evidence: `.omx/ultragoal/ledger.jsonl` and
  `.omx/ultragoal/artifacts/`

Keep this document short and update it only when the allocator mechanism or its
public diagnostic surface changes.
