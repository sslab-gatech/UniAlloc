# Segment-tree bitmap page allocator

## Scope

The `bitmap_page_allocator` feature selects an experimental page-run backend for
the contiguous `fixed_heap` configuration. The default page-run backend retains
its intrusive free lists. The hosted counterpart is described in
[`hosted-bitmap-page-allocator.md`](hosted-bitmap-page-allocator.md).

```bash
cargo run --release -p unialloc \
  --example page_run_backend_bench \
  --no-default-features --features fixed_heap

cargo run --release -p unialloc \
  --example page_run_backend_bench \
  --no-default-features --features bitmap_page_allocator

# Run the direct segment-tree correctness and invariant suite.
cargo test -p unialloc --lib bitmap_alloc::tests

# Exercise the SegmentPageAllocator directly, without page-heap or arena cost.
cargo run --release -p unialloc \
  --example segment_tree_page_allocator_bench -- all
```

## Data structure

Each leaf stores one 64-page occupancy word. Each internal node stores:

- longest free prefix;
- longest free suffix;
- longest free run anywhere below the node.

Allocation scans up to eight nearby leaf words using scalar bit operations. On
a collision it jumps beyond the highest occupied bit in the candidate window,
skipping every start that must still overlap that bit. Segment-tree summaries
handle fragmented or cross-word runs. Freeing a range clears its leaf bits and
recomputes the ancestor summaries. Two adjacent free ranges therefore become
one longer `max_free` run during the normal update; there is no linked-list node
removal or explicit neighbor splice.

`RunNode` is 24 bytes. A full power-of-two tree uses two nodes per 64 managed
pages, giving approximately 0.75 bytes of tree metadata per page.

## Correctness coverage

The unit tests cover:

- adjacent frees becoming one larger allocatable run;
- aligned fragmented runs crossing leaf and tree-child boundaries;
- partial tail leaves remaining unavailable;
- cross-leaf partial-free rejection;
- search-cursor rewind across the eight-leaf fast-scan boundary;
- earliest-run selection and over-page alignment after conflict skipping;
- double-free and foreign-range rejection;
- 10,000 deterministic randomized allocate/free operations checked against a
  naive occupancy bitmap, including recursive prefix/suffix/max-free invariant
  validation every 127 operations.

The feature is wired into `BuddySystemAllocator`, so object-page backing and
large fixed-heap allocations exercise the same tree through the existing
`GlobalBackend` boundary.

## Direct segment-tree benchmark

`segment_tree_page_allocator_bench` owns a 4,096-page aligned range and calls
`SegmentPageAllocator` directly. Setup, fragmentation, and warmup occur before
timing. Each operation count includes the individual allocate/free updates.
Five CPU-8 process runs, each containing seven trials, produced:

| Isolated mode | Exercised path | Median of five process medians |
|---|---|---:|
| `leaf` | leaf-local 8-page allocate/free | 36.007 ns/op |
| `cross` | aligned 12-page hole spanning pages 60-71 | 187.106 ns/op |
| `long` | 130-page root-free allocate/free | 76.330 ns/op |
| `update` | two 64-page frees merging into a 128-page allocation | 47.967 ns/op |

The `cross` mode forces the summary-tree fallback because neither leaf contains
the complete run. The `update` mode checks the core bitmap property: clearing
adjacent bits and pulling ancestor summaries makes the 128-page run immediately
allocatable, with no explicit neighbor-splice pass.

All 180 benchmark-output records, 20 process medians, commands, affinity,
binary hash, and four aggregates are stored in
`benchmark-results/segment-tree-direct-benchmark.jsonl` (SHA-256
`e14393b6a1c6974784a94086c452fd8d3cefe762b6f06425cde96bf218205911`).

## Performance result

### Initial backend comparison

Measured on 2026-07-13 on an AMD EPYC 9354 host, Linux 6.8, 4 KiB pages, release
profile with fat LTO. Each number is the median of seven trials. Raw samples are
stored in `benchmark-results/page-run-*.jsonl`.

| Workload | Intrusive free list | Segment bitmap | Result |
|---|---:|---:|---:|
| Same 8-page run allocate/free | 24.386 ns/op | 65.129 ns/op | free list 2.67x faster |
| Fragmented exact 8-page reuse | 25.559 ns/op | 94.779 ns/op | free list 3.71x faster |
| Shuffled 8-page frees, refill as 32-page runs | 278.662 ns/op | 92.619 ns/op | segment bitmap 3.01x faster |
| Four-thread same-run contention | 269.963 ns/op | 344.477 ns/op | free list 1.28x faster |

The conflict-skip search was compared against the exact `502911d` segment
bitmap in five counterbalanced process pairs, with seven trials per process:

| Workload | Linear candidate scan | Conflict skip | Change |
|---|---:|---:|---:|
| Same 8-page run allocate/free | 55.827 ns/op | 54.373 ns/op | 2.60% faster |
| Fragmented exact 8-page reuse | 85.028 ns/op | 69.702 ns/op | 18.02% faster |
| Shuffled frees, refill as 32-page runs | 83.507 ns/op | 69.099 ns/op | 17.25% faster |
| Four-thread same-run contention | 108.181 ns/op | 107.803 ns/op | 0.35% faster |

The five per-process medians used to derive each table cell are stored in
`benchmark-results/bitmap-hint-conflict-skip-ab-summary.jsonl`.

The result supports a workload-selective integration. The initial comparison
keeps the intrusive list ahead on exact-size reuse; the current A/B shows
conflict skipping improving the bitmap exact-size path by 2.60%. Fragmented
coalescing exercises the bitmap tree's strongest path and removes most
radix-marker/list-splice work. The feature remains opt-in while workload policy
is being selected.

## Current boundaries

- A single `spin::Mutex` serializes page-run tree mutations.
- Fixed-heap growth returns `false` before changing allocator state because the
  tree metadata is sized during initialization.
- Hosted mmap windows have a separate opt-in multi-arena integration.
- The leaf scan uses scalar `u64` operations plus highest-conflict-bit skipping.
  SIMD remains a candidate for long multiword scans after a workload shows that
  the segment fallback dominates.

The next integration step is an adaptive policy that keeps exact-size hot reuse
on the free list and routes coalescing-heavy fixed arenas to the segment bitmap,
or a sharded bitmap tree that reduces the single-lock cost.
