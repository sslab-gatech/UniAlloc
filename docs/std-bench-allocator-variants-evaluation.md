# Rust `std_bench` allocator-feature variants

## Result

The seven Cargo allocator selectors were measured as seven separate variants.
Each benchmark cell is the median of three measured fresh processes after one
discarded warm-up. Lower ratios mean less time than UniAlloc on the same
benchmark.

![Allocator-feature microbenchmark medians](figures/std-bench-allocator-variants-20260714/allocator-variant-microbench.svg)

| Variant | Cargo feature | Geometric mean of per-leaf median ratios vs. UniAlloc | Observed time difference |
|---|---|---:|---:|
| UniAlloc | `bench_ourself` | 1.000 | baseline |
| ptmalloc | `bench_ptmalloc` | 0.812 | -18.8% |
| jemalloc | `bench_jemalloc` | 0.816 | -18.4% |
| mimalloc | `bench_mimalloc` | 0.772 | -22.8% |
| TCMalloc | `bench_tcmalloc` | 0.764 | -23.6% |
| snmalloc | `bench_snmalloc` | 0.793 | -20.7% |
| Scudo | `bench_scudo` | 0.897 | -10.3% |

The aggregate is a geometric mean of eight per-benchmark ratios. Raw
nanoseconds from different benchmark leaves are never averaged together.
`binary_heap::bench_from_vec` is the largest differentiator: UniAlloc's median
was 414.4 us/iter, while the other variants measured 109.2--109.8 us/iter.
Most other leaves cluster more closely. Scudo measured 1.51x UniAlloc on
`btree::set::clone_10k_and_remove_half` and 1.14x on
`vec_deque::bench_grow_1025`.

These results are current-toolchain diagnostics. The three retained samples
support medians, MAD, and min/max ranges. They do not support confidence
intervals or paper-reproduction claims. UniAlloc's three
`binary_heap::bench_from_vec` observations were 345.3, 414.4, and 426.5
us/iter, so that influential cell should be repeated at larger `n` before a
strong claim. The shared host's one-minute load varied from 5.24 to 8.63 during
measurement.

## Matrix

- Source: clean detached `dev` commit
  `aa5ee466fd5de31d56f43187536193454b530c65`.
- Toolchain: `rustc 1.98.0-nightly (485ec3fbc 2026-06-10)` from the repository's
  `nightly-2026-06-11` pin.
- Benchmark inventory: the same byte-identical 468-name `std_bench` list for
  every variant.
- Selected allocation-heavy leaves:
  - `binary_heap::bench_from_vec`
  - `btree::map::from_iter_rand_10_000`
  - `btree::set::clone_10k_and_remove_half`
  - `slice::random_inserts`
  - `string::bench_push_char_one_byte`
  - `vec::bench_from_elem_1000`
  - `vec::bench_extend_1000_1000`
  - `vec_deque::bench_grow_1025`
- Schedule: 56 discarded warm-up processes plus 168 measured processes, for
  224/224 valid fresh-process executions.
- Placement: CPU 20, NUMA node 0; allocator order rotates by round and leaf.
- Runtime environment: allocator defaults, stats disabled, `GLIBC_TUNABLES`
  absent, and glibc's default rseq policy retained.
- Timing: build time excluded; each process runs one exact leaf through the
  built libtest benchmark binary.

All allocator binaries include the repository's default `pthread_dtor` and
`rseq` features. The variant dimension is the single `bench_*` selector. The
recorded Cargo resolution proves that exactly one allocator selector was active
in each binary.

## Scudo identity

Scudo used the packaged LLVM 18 standalone runtime:

```text
/usr/lib/llvm-18/lib/clang/18/lib/linux/libclang_rt.scudo_standalone-x86_64.so
SHA256 e7859566ba641ac49674e5c51c6edf605624786a4a4a748b31b73ec0135c9d6f
package libclang-rt-18-dev:amd64 1:18.1.3-1ubuntu1
```

The runtime passed the repository's dpkg-content and behavioral Scudo probe.
All 32 Scudo warm-up/measured processes emitted exactly one
`unialloc: verified Scudo runtime identity` marker; the inventory process did
the same. The Scudo row therefore measures the authenticated Scudo provider.
The `bench_ptmalloc` row is the explicit `System`/glibc variant.

## Artifacts

- Machine-readable result:
  [`benchmark-results/std-bench-allocator-variants-20260714.json`](../benchmark-results/std-bench-allocator-variants-20260714.json)
- Long-form three-sample data:
  [`std-bench-allocator-variants.csv`](figures/std-bench-allocator-variants-20260714/std-bench-allocator-variants.csv)
- Figures:
  [`SVG`](figures/std-bench-allocator-variants-20260714/allocator-variant-microbench.svg),
  [`PNG`](figures/std-bench-allocator-variants-20260714/allocator-variant-microbench.png)
- Artifact hashes:
  [`artifact-manifest.json`](figures/std-bench-allocator-variants-20260714/artifact-manifest.json)
- Ignored raw evidence:
  `evaluation/raw/std-bench-allocator-variants-20260714/` contains build logs,
  binary hashes, dependency-feature closures, benchmark inventories, per-run
  stdout/stderr/GNU-time records, provenance, and the complete `records.jsonl`.

## Reproduce

Run the campaign from a clean detached worktree so unrelated developer changes
cannot enter the build:

```bash
git worktree add --detach /tmp/unialloc-std-bench-variants aa5ee466fd5d

uv run python evaluation/scripts/run_std_bench_allocator_variants.py \
  --source-root /tmp/unialloc-std-bench-variants \
  --output-dir "$PWD/evaluation/raw/std-bench-allocator-variants-20260714" \
  --measured-rounds 3 \
  --cpu 20 --numa-node 0 \
  --tcmalloc-lib-dir "$UNIALLOC_TCMALLOC_LIB_DIR" \
  --scudo-runtime-library \
    /usr/lib/llvm-18/lib/clang/18/lib/linux/libclang_rt.scudo_standalone-x86_64.so

uv run evaluation/scripts/plot_std_bench_allocator_variants.py
```

The runner fails closed on a dirty source worktree, missing benchmark leaf,
different benchmark inventory, duplicate allocator selector, invalid timing
record, unpinned TCMalloc runtime, unauthenticated Scudo artifact, or absent
Scudo identity marker.
