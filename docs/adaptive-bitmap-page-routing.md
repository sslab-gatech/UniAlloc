# Adaptive hosted bitmap page routing

## Scope

The opt-in `adaptive_bitmap_page_allocator` feature applies workload-sensitive
routing at the regular hosted `BuddySystemAllocator` boundary. Object-page
backing, large allocations, and over-page-aligned requests all use the same
policy. The feature includes `hosted_bitmap_page_allocator` and remains
exclusive with `fixed_heap`.

```bash
cargo test --locked -p unialloc --lib --no-default-features \
  --features adaptive_bitmap_page_allocator

cargo run --locked --release -p unialloc \
  --example hosted_page_run_backend_bench \
  --features adaptive_bitmap_page_allocator -- same
```

## Routing policy

The regular free list remains the cold and exact-reuse route. Two measured
signals start a thread-local bitmap lease:

1. A free-list deallocation that joins both neighbours publishes a bridge
   generation. Four or more bridges observed as one burst route the next request
   when that request spans at least 16 pages.
2. A failed `try_lock` on the global free-list radix lock publishes a contention
   generation. Every thread that observes the generation starts a lease.

Each activation leases 1,048,576 page-run allocations to the hosted bitmap.
The long lease keeps a coalescing phase on one ownership path. A 64-K allocation
lease repeatedly crossed backend boundaries in the guarded coalescing workload;
the resulting mixed-owner probes and arena churn raised the median from roughly
45 ns/op to more than 700 ns/op. The 1-M lease covered the measured phase and
restored a stable bitmap-rate result.

The 16-page bridge-request gate separates the measured exact 8-page reuse case
from the 32-page coalescing demand. Contention remains sufficient for 8-page
runs, since the four-thread 8-page workload strongly favours bitmap arenas.
Sparse bridge generations are consumed without accumulating across unrelated
allocation observations.

Direct Rust TLS stores the lease state on supported hosted targets. arm64e uses
the free-list route while its direct-TLS support remains conservative.

## Ownership and deallocation

The two hosted backends allocate disjoint OS mappings. Adaptive bitmap
allocations use `allocate_pooled_layout`, which succeeds only when an arena owner
is published in the hosted owner directory. Arena-incompatible requests and
mapping failures return to the wrapper and fall back to the free list.

Every deallocation performs one atomic ownership dispatch:

- `live_allocations == 0` selects the free list without an owner-directory walk;
- an owner miss selects the free list;
- an active owner and validated range selects the bitmap arena;
- an owner hit followed by an inactive/range/bitmap error fails closed and
  retains the allocation.

This protocol keeps route changes independent of pointer lifetime. Cross-thread
frees use the same owner decision. The hosted bitmap's full-backend mode retains
its direct `PageHeap` fallback; only the adaptive wrapper uses the provenance-
strict pooled entry point.

## Route diagnostics

The `stats` feature exposes `adaptive_page_run_stats_snapshot()` with allocation,
fallback, ownership, bridge, contention, and lease-activation counters. Each
benchmark workload has an isolated process selector so one workload cannot seed
the next workload's TLS lease.

```bash
RUSTFLAGS='-C target-cpu=native' \
  CARGO_TARGET_DIR=target/bench-hosted-adaptive-stats \
  cargo run --locked --release -p unialloc \
    --example hosted_page_run_backend_bench \
    --features adaptive_bitmap_page_allocator,stats -- fragmented
```

The diagnostic build recorded these route decisions on the benchmark host:

| Isolated workload | Free-list allocations | Bitmap allocations | Route evidence |
|---|---:|---:|---|
| Same 8-page reuse | 1,402,000 | 0 | no bridge or contention activation |
| Fragmented exact 8-page reuse | 365,568 | 0 | bridge bursts consumed by the 16-page gate |
| Guarded coalesce/refill | 1,024 | 799,744 | one bridge activation on 32-page demand |
| Four-thread 8-page contention | 3 | 1,399,997 | two radix-lock contention signals |

Timing comparisons use builds without `stats`; counter atomics are diagnostic
instrumentation.

## Backend A/B motivation

The current `22558d7` backends were measured in five counterbalanced process
pairs, with seven trials per process, fat LTO, `-C target-cpu=native`, CPU 8 for
single-thread work, and CPUs 8-11 for the four-thread work. Each cell is the
median of five process medians.

| Hosted workload | Free list | Segment bitmap | Preferred route |
|---|---:|---:|---|
| Same 8-page reuse | 20.219 ns/op | 24.246 ns/op | free list by 19.92% |
| Fragmented exact 8-page reuse | 36.324 ns/op | 44.643 ns/op | free list by 22.90% |
| Guarded coalesce/refill | 56.515 ns/op | 42.783 ns/op | bitmap by 24.30% |
| Four-thread 8-page contention | 106.893 ns/op | 21.855 ns/op | bitmap by 4.89x |

Every preferred direction held in all five pairs. Equal 8-page request sizes
produce opposite winners between exact reuse and contention, so request size
alone provides insufficient routing information. The bridge and real-lock-
contention signals encode the measured workload shape.

The hosted and fixed metadata, five-round aggregate comparisons, and capture
validation records are stored in
`benchmark-results/page-run-current-backend-ab-summary.jsonl`. Its first record
also preserves the SHA-256 hashes of both complete raw captures.

## Adaptive three-way A/B

The implemented policy was then compared with both backends using five rotated,
counterbalanced process triples and isolated workload processes. Timing builds
used default features and omitted `stats`. The measured source commit was
`8ac14ff`, rebased on dev `1308d2b`; the later evidence-only amend preserves the
same allocator and benchmark binary.

| Workload | Free list | Bitmap | Adaptive | Adaptive result |
|---|---:|---:|---:|---|
| Same 8-page reuse | 20.418 ns/op | 25.178 ns/op | 22.160 ns/op | +8.53% vs free list; 11.99% faster than bitmap |
| Fragmented exact reuse | 36.705 ns/op | 45.631 ns/op | 38.840 ns/op | +5.82% vs free list; 14.88% faster than bitmap |
| Guarded coalesce/refill | 65.795 ns/op | 42.461 ns/op | 43.693 ns/op | 33.59% faster than free list; within 2.90% of bitmap |
| Four-thread contention | 106.251 ns/op | 34.875 ns/op | 21.487 ns/op | 79.78% faster than free list; 38.39% faster than bitmap |

Adaptive beat the free list in all five coalescing and contention triples. The
free list beat adaptive in all five exact-reuse triples. Adaptive beat the pure
bitmap in every exact and contention triple; coalescing stayed close to pure
bitmap with a 2/5 adaptive win count. The result gives the adaptive feature a
balanced opt-in profile: a 5.8-8.5% exact-path dispatch cost buys near-bitmap
coalescing and a large contention gain.

All 540 captured benchmark-output records (420 timing trials, 60 backend
headers, and 60 in-process summaries), 60 normalized process medians, four
aggregate records, binary hashes, commands, ordering, and affinity metadata are stored in
`benchmark-results/hosted-page-run-adaptive-threeway-ab.jsonl` (SHA-256
`0f9fb2fc5a466c0ada57a064deb94703f91fc60f0c734201a70293c1c948fc91`).

## Fixed-heap boundary

The fixed free list and fixed bitmap currently cover the same caller-provided
address interval. A direct two-backend router would give both allocators
authority over overlapping addresses. Safe fixed-heap adaptation needs one of:

- disjoint ownership chunks with an address-to-backend directory;
- a single authoritative bitmap with exact-run free-list caches and a drain
  protocol;
- an experimental static partition for bounded evaluation.

The hosted implementation establishes the routing and ownership protocol while
fixed-heap chunk ownership remains a separate design task.

The same five-pair method on the fixed heap measured free-list wins of 3.60x
for same-run reuse, 4.31x for fragmented exact reuse, and 1.28x under four-thread
contention; bitmap won guarded coalescing by 3.90x. Address ownership and the
opposite contention result both support a separate fixed-heap design.

## Current boundaries

- The bridge threshold, 16-page demand gate, and 1-M lease are empirically tuned
  against four microbenchmarks on one Linux/x86-64 host.
- A phase that activates bitmap routing and immediately changes to quiet exact
  reuse can retain bitmap routing for the remaining lease.
- Mixed live owners require owner-directory probes for free-list deallocations.
- The policy reacts to radix-lock contention shared by page-run metadata and
  higher allocator layers; the signal intentionally represents observed global
  metadata pressure.
