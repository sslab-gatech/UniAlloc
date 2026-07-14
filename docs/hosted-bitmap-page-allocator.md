# Hosted segment-bitmap page allocator

## Scope

The `hosted_bitmap_page_allocator` feature applies the segment-tree bitmap to
the regular hosted `GlobalBackend` path. Object-page backing, large allocations,
and ordinary over-page-aligned requests therefore reach the same backend through
`BuddySystemAllocator`.

```bash
cargo test -p unialloc --lib --features hosted_bitmap_page_allocator \
  hosted_bitmap_alloc::tests -- --test-threads=1

cargo run --release -p unialloc \
  --example hosted_page_run_backend_bench \
  --features hosted_bitmap_page_allocator
```

The feature is opt-in and exclusive with `fixed_heap`. The existing
`bitmap_page_allocator` feature remains the fixed-heap integration.

## Multi-arena design

A hosted process grows through independent OS mappings, so each mapping owns one
`SegmentPageAllocator`:

- arena sizes start at 512 KiB, grow by powers of two, and stop at 64 MiB;
- larger requests go directly to `PageHeap`;
- each arena publishes an atomic `largest_free_run` summary, allowing allocation
  to reject undersized arenas before taking their lock;
- a six-level, 9-bit radix directory maps virtual pages to stable arena
  descriptors, giving deallocation a bounded owner lookup;
- descriptors form an append-only registry and are reused after payload and tree
  mappings are retired;
- four small empty arenas stay warm, and lock contention can create up to four
  arena shards; this bounds retained warm payload at 8 MiB;
- freeing at least 256 KiB inside a live arena issues `MADV_DONTNEED` on Unix.

Each arena uses the same 64-page leaf words and prefix/suffix/max-free internal
summaries as the fixed-heap backend. Clearing adjacent bits updates their common
ancestors, so the resulting larger free run is immediately visible through the
root summary. The allocator performs no neighbor lookup or list splice.

Allocator metadata uses direct OS mappings. This keeps arena creation and owner
directory growth outside `GlobalBackend` and avoids allocator recursion.

## Correctness coverage

Eleven focused tests cover:

- adjacent cross-leaf frees recombining into one exact larger allocation;
- over-page alignment and full-arena deallocation;
- capacity-directed selection across multiple arenas;
- warm-arena retention, descriptor reuse, and direct allocation above 64 MiB;
- deterministic contention-shard creation and the four-arena shard cap;
- owner-radix indexing above the native pointer width;
- 10,000 randomized multi-arena operations with alignment, non-overlap, and
  first/last-byte checks;
- 20,000 four-thread allocation/free cycles with live-address uniqueness.

## Performance result

Measured on 2026-07-14 on an AMD EPYC 9354 host, Linux 6.8, 4 KiB pages, release
profile with fat LTO and `-C target-cpu=native`. Single-thread workloads were
pinned to core 2; the four-thread workload was restricted to four distinct
physical cores 2-5. Each number is the median of seven trials. Raw samples are stored in
`benchmark-results/hosted-page-run-*.jsonl`.

| Workload | Hosted free list | Segment bitmap | Effect |
|---|---:|---:|---:|
| Same 8-page run allocate/free | 38.924 ns/op | 43.394 ns/op | bitmap +11.48% |
| Fragmented exact 8-page reuse | 53.149 ns/op | 75.252 ns/op | bitmap +41.59% |
| Shuffled frees, refill as 32-page runs | 73.102 ns/op | 74.977 ns/op | bitmap +2.56% |
| Four-thread same-run contention | 163.486 ns/op | 85.210 ns/op | bitmap 1.92x faster |

Reproduction uses separate target directories so both binaries keep their own
feature set:

```bash
RUSTFLAGS='-C target-cpu=native' \
  CARGO_TARGET_DIR=target/bench-hosted-freelist \
  cargo build --release -p unialloc \
    --example hosted_page_run_backend_bench

RUSTFLAGS='-C target-cpu=native' \
  CARGO_TARGET_DIR=target/bench-hosted-bitmap \
  cargo build --release -p unialloc \
    --example hosted_page_run_backend_bench \
    --features hosted_bitmap_page_allocator

{
  taskset -c 2 target/bench-hosted-freelist/release/examples/hosted_page_run_backend_bench single
  taskset -c 2-5 target/bench-hosted-freelist/release/examples/hosted_page_run_backend_bench contended
} > benchmark-results/hosted-page-run-freelist.jsonl

{
  taskset -c 2 target/bench-hosted-bitmap/release/examples/hosted_page_run_backend_bench single
  taskset -c 2-5 target/bench-hosted-bitmap/release/examples/hosted_page_run_backend_bench contended
} > benchmark-results/hosted-page-run-segment-bitmap.jsonl
```

The measured policy is strongest for concurrent page-run traffic. Automatic
coalescing reaches near parity in the small 512 KiB arenas used here, while
exact-size hot reuse remains the free list's strongest case. The hosted bitmap
therefore stays an explicit workload-selective feature.

## Current boundaries

- Arena mutations use one spin mutex per mapping; four contention shards reduce
  shared-lock pressure.
- Radix nodes and stable arena descriptors retain their peak metadata mappings
  for process lifetime. Payload and segment-tree mappings follow the warm-arena
  retirement policy.
- The bounded hot scan uses scalar `u64` operations across at most eight leaf
  words. Tree descent handles longer and cross-word searches. SIMD remains a
  follow-on experiment after profiling identifies leaf-word scanning as the
  dominant cost.
- The performance result covers this Linux/x86-64 host and the four listed
  micro-workloads.
