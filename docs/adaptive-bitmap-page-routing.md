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
2. One sampled failed `try_lock` on the global free-list radix lock publishes a
   contention generation. The probe runs once per eight radix accesses, and
   every thread that observes the generation starts a lease.

Each activation leases 65,536 page-run allocations to the hosted bitmap. Every
bitmap-routed request of at least 16 pages refreshes that lease. Large coalescing
demand therefore stays on one ownership path, while a later quiet 8-page phase
with stable routing generations returns to the exact free list after at most one
short lease. A plain 64-K lease without refresh repeatedly crossed backend
boundaries in the guarded workload; large-run refresh preserves its bitmap-rate
result without retaining a fixed 1-M tail.

The 16-page bridge-request gate separates the measured exact 8-page reuse case
from the 32-page coalescing demand. Contention remains sufficient for 8-page
runs, since the four-thread 8-page workload strongly favours bitmap arenas.
Sparse bridge generations are consumed without accumulating across unrelated
allocation observations.

The allocation hot path reads one shared routing generation. The separate
contention generation is loaded only after that routing generation changes, and
an active lease performs no global generation load. A newly initialized thread
snapshots historical generations, so old process-wide activity does not seed a
fresh lease.

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

# Route-decision-only cost and exact -> coalesce -> exact transition shape.
cargo run --locked --release -p unialloc \
  --example hosted_page_run_backend_bench \
  --features adaptive_bitmap_page_allocator,stats -- route
cargo run --locked --release -p unialloc \
  --example hosted_page_run_backend_bench \
  --features adaptive_bitmap_page_allocator,stats -- phase
```

The diagnostic build recorded these route decisions on the benchmark host:

| Isolated workload | Free-list allocations | Bitmap allocations | Route evidence |
|---|---:|---:|---|
| Same 8-page reuse | 1,402,000 | 0 | no bridge or contention activation |
| Fragmented exact 8-page reuse | 365,568 | 0 | bridge bursts consumed by the 16-page gate |
| Guarded coalesce/refill | 1,024 | 799,744 | one bridge activation on 32-page demand |
| Four-thread 8-page contention | 100,084 | 1,299,916 | median of seven processes; 92.85% bitmap routed |

The backend three-way timing comparisons below use builds with `stats` disabled;
counter atomics are diagnostic instrumentation. The route-decision-only median
was 1.036 ns/op with `stats` enabled; this includes the benchmark's `black_box`
boundary.

The full route-diagnostics capture contains 350 raw benchmark-output records,
35 normalized process records, and five aggregate records in
`benchmark-results/adaptive-page-run-route-stats.jsonl` (SHA-256
`d7d6727e0271c98766c7c0ceb268de90945341ec130628b344b18e07717b41f5`).

## Backend A/B motivation

At `22558d7`, the two backends were measured in five counterbalanced process
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

The optimized policy was compared with both backends using five rotated,
counterbalanced process triples and isolated workload processes. Timing builds
used default features and omitted `stats`. The measured source commit was
`9ed7cac`, based on dev `8d369c3`.

| Workload | Free list | Bitmap | Adaptive | Adaptive result |
|---|---:|---:|---:|---|
| Same 8-page reuse | 20.392 ns/op | 25.311 ns/op | 21.610 ns/op | +5.97% vs free list; 14.62% faster than bitmap |
| Fragmented exact reuse | 36.686 ns/op | 46.034 ns/op | 38.149 ns/op | +3.99% vs free list; 17.13% faster than bitmap |
| Guarded coalesce/refill | 69.948 ns/op | 42.471 ns/op | 43.701 ns/op | 37.52% faster than free list; within 2.90% of bitmap |
| Four-thread contention | 100.935 ns/op | 34.919 ns/op | 21.445 ns/op | 78.75% faster than free list; 38.59% faster than bitmap |

Adaptive beat the free list in all five coalescing and contention triples. The
free list beat adaptive in all five exact-reuse triples. Adaptive beat the pure
bitmap in every exact and contention triple; pure bitmap won all five
coalescing triples by 1.24-3.14%. Relative to the pre-optimization A/B, the
exact-route premiums fell from 8.53% to 5.97% and from 5.82% to 3.99%, a roughly
30% reduction in each premium. Coalescing remains within 2.90% of bitmap and
contention remains the fastest measured route.

All 540 captured benchmark-output records (420 timing trials, 60 backend
headers, and 60 in-process summaries), 60 normalized process medians, four
aggregate records, binary hashes, commands, ordering, and affinity metadata are stored in
`benchmark-results/hosted-page-run-adaptive-threeway-ab.jsonl` (SHA-256
`193a9a1e8442204fc343c71af09554c41f5970984ae39e08439256ee72f09edd`).

## Phase-transition A/B

The `phase` selector measures exact reuse, runs guarded coalescing with ten
warmup cycles followed by two timed cycles, and then records twelve
100-K-allocation exact buckets. Five counterbalanced process pairs compared the
original fixed 1-M lease with the 64-K lease plus large-run refresh, with
`stats` enabled for route counts.

| Phase measure | Fixed 1-M lease | 64-K + large-run refresh |
|---|---:|---:|
| Bitmap allocations after the trigger | 1,048,576 | 78,399 |
| Late exact buckets 04-11 | 25.882 ns/op | 22.150 ns/op |
| Late exact premium over each pre-trigger baseline | 20.01% | 1.30% |
| Warmed coalescing-phase median | 691.661 ns/op | 698.034 ns/op |

Large-run refresh reduced retained bitmap allocations by 92.52% and late exact
cost by 14.42%, while the warmed coalescing phase moved by 0.92%. The full 160
raw phase records, ten normalized processes, aggregates, commands, and binary
hashes are stored in
`benchmark-results/adaptive-page-run-phase-lease-ab.jsonl` (SHA-256
`5e9546a42ca3364ab8a994e75a1ab03c802ad8e7c26524d0b194a3eb6b6e665f`).

## Memory-overhead A/B

`hosted_page_run_memory_probe` separates process memory from bitmap-owned
mappings. It explicitly touches every live payload page, reads Linux
`smaps_rollup`, and records quiescent hosted-bitmap snapshots. Each result below
is the median of five counterbalanced, CPU-8-pinned processes built from
`53ef6fa` with `stats` enabled. Timing claims continue to use the stats-free
builds above.

```bash
cargo run --locked --release -p unialloc \
  --example hosted_page_run_memory_probe --features stats -- retention
cargo run --locked --release -p unialloc \
  --example hosted_page_run_memory_probe \
  --features hosted_bitmap_page_allocator,stats -- retention
cargo run --locked --release -p unialloc \
  --example hosted_page_run_memory_probe \
  --features adaptive_bitmap_page_allocator,stats -- coalesce
```

The 32-MiB exact-retention workload stayed on the free-list route under the
adaptive policy:

| Backend | Touched-live RSS delta | All-freed RSS delta | Bitmap mappings after free |
|---|---:|---:|---:|
| Free list | 32.031 MiB | 0.297 MiB | 0 |
| Hosted bitmap | 32.676 MiB | 2.449 MiB | 2.352 MiB |
| Adaptive | 32.094 MiB | 0.391 MiB | 0 |

The hosted bitmap's live metadata was 0.586 MiB for 64 active 512-KiB arenas,
or 1.83% of their 32-MiB payload mapping. After all frees, four warm arenas
retained 2 MiB of payload; 16 KiB of live trees, 256 KiB of descriptor mappings,
and 88 KiB of owner-directory mappings brought the bitmap-owned total to
2.352 MiB. Adaptive created no bitmap arena for quiet exact reuse.

The guarded coalescing workload first touched 32 MiB of small runs, retained one
guard per logical arena, and then touched 24 MiB of 32-page runs. The requested
live payload at the coalesced checkpoint was 26 MiB.

| Backend | Peak RSS delta | All-freed RSS delta | Bitmap mappings at coalesced live | Bitmap mappings after free |
|---|---:|---:|---:|---:|
| Free list | 32.027 MiB | 6.254 MiB | 0 | 0 |
| Hosted bitmap | 32.691 MiB | 2.461 MiB | 32.586 MiB | 2.352 MiB |
| Adaptive | 32.211 MiB | 8.391 MiB | 24.445 MiB | 2.273 MiB |

Adaptive kept the 2-MiB guards on the free list and placed the 24-MiB large-run
phase in 48 full bitmap arenas. Pure bitmap kept guards and large runs together
in 64 partially occupied arenas, leaving 6 MiB of mapped arena slack. Peak RSS
therefore stayed close across the three backends. After all frees, adaptive
retained both the free-list residue and 2.273 MiB of bitmap mappings, producing
a 2.14-MiB RSS premium over the free list in this phase-transition shape.

One cold 8-page bitmap allocation maps a 512-KiB minimum arena plus 32 KiB of
tree, descriptor, and owner metadata: 544 KiB of virtual mappings for 32 KiB of
live payload. Demand paging held the measured touched RSS delta to 152 KiB. The
single warm arena retained the same mappings after free.

The capture contains 225 raw checkpoint records, 45 process summaries, and 45
aggregates in `benchmark-results/hosted-page-run-memory-overhead-ab.jsonl`
(SHA-256
`9c78b35e5cbae7efffd2d199b582a63bf467b273707940aa3f0d7596fe4d2bbb`).

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

- The bridge threshold, 16-page demand gate, 64-K lease, refresh rule, and
  eight-access contention sampling interval are empirically tuned against five
  microbenchmarks on one Linux/x86-64 host.
- A stable-generation phase that changes to quiet exact reuse can retain bitmap
  routing for at most the remaining short lease. A generation published during
  that lease is observed afterward and can start one additional lease.
- Sampling can miss contention bursts shorter than eight radix accesses; it
  preserves exclusion and changes only the routing hint.
- Mixed live owners require owner-directory probes for free-list deallocations.
- The adaptive coalescing path can retain both free-list state and up to four
  warm bitmap arenas. The measured 8-page arena shape retained 2 MiB of bitmap
  payload; the configured four-arena, 2-MiB-per-arena bound permits an 8-MiB
  payload high-water for larger warm arenas.
- Descriptor mappings and owner-directory nodes follow peak arena/address
  coverage and are reused without a current reclamation path.
- The policy reacts to radix-lock contention shared by page-run metadata and
  higher allocator layers; the signal intentionally represents observed global
  metadata pressure.
