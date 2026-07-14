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
```

## Data structure

Each leaf stores one 64-page occupancy word. Each internal node stores:

- longest free prefix;
- longest free suffix;
- longest free run anywhere below the node.

Allocation scans up to eight nearby leaf words using scalar bit operations,
then uses the segment-tree summaries for fragmented or cross-word runs. Freeing
a range clears its leaf bits and recomputes the ancestor summaries. Two adjacent
free ranges therefore become one longer `max_free` run during the normal update;
there is no linked-list node removal or explicit neighbor splice.

`RunNode` is 24 bytes. A full power-of-two tree uses two nodes per 64 managed
pages, giving approximately 0.75 bytes of tree metadata per page.

## Correctness coverage

The unit tests cover:

- adjacent frees becoming one larger allocatable run;
- fragmented runs crossing tree-child boundaries;
- over-page alignment;
- double-free and foreign-range rejection;
- 10,000 deterministic randomized allocate/free operations checked against a
  naive occupancy bitmap.

The feature is wired into `BuddySystemAllocator`, so object-page backing and
large fixed-heap allocations exercise the same tree through the existing
`GlobalBackend` boundary.

## Performance result

Measured on 2026-07-13 on an AMD EPYC 9354 host, Linux 6.8, 4 KiB pages, release
profile with fat LTO. Each number is the median of seven trials. Raw samples are
stored in `benchmark-results/page-run-*.jsonl`.

| Workload | Intrusive free list | Segment bitmap | Result |
|---|---:|---:|---:|
| Same 8-page run allocate/free | 24.386 ns/op | 65.129 ns/op | free list 2.67x faster |
| Fragmented exact 8-page reuse | 25.559 ns/op | 94.779 ns/op | free list 3.71x faster |
| Shuffled 8-page frees, refill as 32-page runs | 278.662 ns/op | 92.619 ns/op | segment bitmap 3.01x faster |
| Four-thread same-run contention | 269.963 ns/op | 344.477 ns/op | free list 1.28x faster |

The result supports a workload-selective integration. Exact-size reuse already
matches the intrusive list's strongest path. Fragmented coalescing exercises
the bitmap tree's strongest path and removes most radix-marker/list-splice work.
The feature remains opt-in while workload policy is being selected.

## Current boundaries

- A single `spin::Mutex` serializes page-run tree mutations.
- Fixed-heap growth returns `false` before changing allocator state because the
  tree metadata is sized during initialization.
- Hosted mmap windows have a separate opt-in multi-arena integration.
- The leaf scan uses scalar `u64` operations. SIMD remains a candidate for long
  multiword scans after a workload shows that the segment fallback dominates.

The next integration step is an adaptive policy that keeps exact-size hot reuse
on the free list and routes coalescing-heavy fixed arenas to the segment bitmap,
or a sharded bitmap tree that reduces the single-lock cost.
