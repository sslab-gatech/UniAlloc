# Lifetime-guided Hugepage Placement: Rapid Feasibility Study

## Decision

**Oracle-hint adversarial mechanism: GO for the next production-arena experiment. Compiler-derived policy and real-workload benefit: open.**

With exact, manual lifetime hints, segregating long-lived and ephemeral objects across 2 MiB arenas cut retained memory from 4 GiB to 2 GiB, eliminated the probe's 50% stranded-space ratio, and returned 1,024 complete HugeTLB pages to the pool. Routing long-lived objects to HugeTLB pages reduced dependent random-access latency by 20.87% relative to an equally packed ordinary-page arena. No HugeTLB request fell back, and the 4,096-page persistent pool returned to its starting free-page count after every process.

The result establishes the value of the runtime placement mechanism under oracle hints. UniAlloc still needs a trustworthy per-allocation classifier and a production size-class arena before this can become an allocator claim.

## What UniAlloc has today

| Layer | Current behavior | Consequence |
|---|---|---|
| Metadata ABI | `AllocationMetadata` carries `lifetime_hint: u16` through allocation, deallocation, and reallocation. | A placement policy can consume the field without changing the ABI layout. |
| Rust compiler pass | `--unialloc-lifetime-hint` / `UNIALLOC_LOWERING_LIFETIME_HINT` supplies one value for the complete compiler invocation. | Per-object lifetime classes are currently absent. |
| Runtime | `lifetime_hint` participates in exact semantic/type-cache identity. | Different hints isolate reuse identities; physical pages remain shared. |
| Hugepage support | `MAP_HUGETLB | MAP_HUGE_2MB` exists in the PAL. `FLAG_HUGEPAGE_METADATA` and the `hugepage` feature back allocator metadata. | The host and PAL can supply real hugepages; application payload placement needs a new arena path. |
| Payload backend | Semantic cache misses end at `RustAllocator::alloc_raw(Layout)`, which receives no metadata. | Lifetime routing must occur before the shared raw allocator/cache path, with symmetric deallocation provenance. |

Rust borrow lifetimes also differ from object residence time. The Rust NLL RFC calls the interval before a value is freed its **value scope**; reference lifetime describes where a reference may be used. A compiler policy should therefore use exact drop/escape evidence or profile data rather than treating borrow regions as heap-object lifetime labels. See the [Rust NLL RFC](https://rust-lang.github.io/rfcs/2094-nll.html).

## Research basis

- [LLAMA, ASPLOS 2020](https://colinraffel.com/publications/asplos2020learning.pdf) organizes 2 MiB pages by predicted lifetime class, dynamically recovers from under-prediction, and reports up to 78% less fragmentation. Its key mechanism is keeping rare long-lived objects from pinning pages filled during a short-lived peak.
- [TEMERAIRE, OSDI 2021](https://www.usenix.org/system/files/osdi21-hunter.pdf) packs allocations using per-hugepage fullness and longest-free-range state so dense pages preserve hugepage coverage and sparse pages remain reclaimable.
- [Google's warehouse-scale TCMalloc study, ASPLOS 2024](https://people.csail.mit.edu/delimitrou/papers/2024.asplos.memory.pdf) separates short- and long-lived spans into dedicated hugepage sets. It reports 1.02% fleet throughput, 0.82% memory, and 8.1% dTLB-miss improvements for this component.
- [Linux HugeTLB documentation](https://docs.kernel.org/admin-guide/mm/hugetlbpage.html) defines `nr_hugepages` as a persistent pool. `munmap` returns pages to that pool; releasing RAM to the ordinary page allocator requires shrinking the persistent pool or using surplus/THP semantics.

These systems support a small first hypothesis: **a trustworthy binary lifetime class can keep sparse long-lived objects from pinning short-lived hugepages and can reduce the active translation set.**

## Prototype

The worktree branch `exp/lifetime-hugepage-orchestration-20260714` adds an isolated research harness rather than changing the production allocator path.

Prototype hint vocabulary:

- `0`: unknown, conservatively unclassified;
- `1`: ephemeral;
- `2`: long-lived;
- every other existing/custom `u16` value remains unclassified through exact matching.

The probe uses actual 2 MiB mappings and 4 KiB fixed slots. It alternates long-lived and ephemeral allocations, frees the ephemeral cohort, reclaims empty arenas, then follows a randomized dependent pointer cycle through the surviving long-lived cohort. The dependent chain exposes translation latency without memory-level parallelism hiding page-walk cost.

Five policies separate the causal effects:

1. `ordinary-mixed`: ordinary pages, interleaved lifetimes;
2. `ordinary-segregated`: ordinary pages, separate lifetime arenas;
3. `huge-mixed`: HugeTLB pages, interleaved lifetimes;
4. `huge-segregated`: HugeTLB pages, separate lifetime arenas;
5. `tiered`: long-lived objects on HugeTLB pages, ephemeral objects on ordinary pages.

The prototype arena is fixed-size and bump-only. It tests one phase transition and whole-extent reclamation. It does not model repeated free-slot reuse, size-class diversity, cross-thread caches, or classifier error.

## Host and run configuration

- CPU: 2-socket AMD EPYC 9354, 128 logical CPUs;
- NUMA: process pinned to CPU 0 and memory node 0;
- HugeTLB: 4,096 × 2 MiB persistent pages = 8 GiB;
- node 0 pool: 2,722 pages; node 1 pool: 1,374 pages;
- workload: 2,048 extents × 2 MiB = 4 GiB peak virtual/payload capacity;
- slots: 4 KiB; 1,048,576 objects, half long-lived and half ephemeral;
- scan: 4 warmup passes, 32 measured dependent-chain passes;
- repetition: five fresh processes per policy in deterministic randomized order;
- ordinary mappings: `MADV_NOHUGEPAGE` to exclude THP promotion;
- huge mappings: `--require-hugetlb`, so any fallback fails the run.

Reproduction:

```bash
uv run python evaluation/scripts/lifetime_hugepage_experiment.py \
  --run-id lifetime-hugepage-oracle-chain-final-20260714 \
  --extents 2048 --slot-bytes 4096 \
  --warmup-passes 4 --passes 32 --repeats 5 \
  --numa-node 0 --cpu 0 --seed 20260714
```

Tracked selected evidence:

- `docs/evidence/lifetime-hugepage-oracle-chain-final-20260714/manifest.json`
- `docs/evidence/lifetime-hugepage-oracle-chain-final-20260714/samples.jsonl`
- `docs/evidence/lifetime-hugepage-oracle-chain-final-20260714/summary.json`
- `docs/evidence/lifetime-hugepage-oracle-chain-final-20260714/perf/`

The runner also writes working copies below Git-ignored `evaluation/raw/` and `evaluation/results/`. The tracked manifest labels this evidence non-claim-grade and records artifact digests, host configuration, commands, and the manual-oracle boundary.

## Results

Median of five runs:

| Policy | Peak HugeTLB pages | Whole extents reclaimed after ephemeral frees | Retained payload capacity | Stranded capacity | Fragmentation | Long-set ns/touch |
|---|---:|---:|---:|---:|---:|---:|
| `ordinary-mixed` | 0 | 0 | 4 GiB | 2 GiB | 50% | 157.539 |
| `ordinary-segregated` | 0 | 1,024 ordinary extents | 2 GiB | 0 | 0% | 138.581 |
| `huge-mixed` | 2,048 | 0 | 4 GiB | 2 GiB | 50% | 112.348 |
| `huge-segregated` | 2,048 | 1,024 HugeTLB pages | 2 GiB | 0 | 0% | 112.164 |
| `tiered` | 1,024 | 1,024 ordinary extents | 2 GiB | 0 | 0% | 109.662 |

Mechanism effects:

- `huge-mixed → huge-segregated`: lifetime grouping returned 1,024 complete HugeTLB pages (2 GiB) and reduced retained capacity by 50%.
- `huge-mixed → tiered`: tiering reduced both peak and retained HugeTLB demand by 50%.
- `ordinary-segregated → tiered`: holding packing constant, the ratio of medians improved by 20.87%; paired improvement averaged 20.46% with a 95% t-interval of `[19.23%, 21.68%]`.
- `ordinary-mixed → tiered`: the combined packing and page-size policy improved by a paired mean of 30.21%, with a 95% t-interval of `[28.74%, 31.68%]`.
- `huge-mixed → tiered`: paired improvement averaged 1.54%, with a 95% t-interval of `[-0.05%, 3.14%]`. This delta is inconclusive.

All hugepage policies recorded zero fallbacks. Every process restored `HugePages_Free` to its starting value.

### PMU mechanism check

`sudo perf stat -r 3` compared `ordinary-segregated` with `tiered` using the same pointer-chain workload. The event set ran multiplexed at 62%, with perf scaling both sides consistently.

```bash
sudo -n perf stat -x ';' -r 3 \
  -e cycles,instructions,page-faults,\
ls_l1_d_tlb_miss.all,ls_l1_d_tlb_miss.all_l2_miss,\
ls_l1_d_tlb_miss.tlb_reload_2m_l2_hit,\
ls_l1_d_tlb_miss.tlb_reload_2m_l2_miss,\
ls_l1_d_tlb_miss.tlb_reload_4k_l2_hit,\
ls_l1_d_tlb_miss.tlb_reload_4k_l2_miss \
  -- numactl --physcpubind=0 --membind=0 \
  target/release/examples/lifetime_hugepage_probe \
  --policy tiered --extents 2048 --slot-bytes 4096 \
  --warmup-passes 4 --passes 32 --require-hugetlb
```

| Whole-process counter | `ordinary-segregated` | `tiered` | Reduction |
|---|---:|---:|---:|
| cycles | 17.917 B | 12.659 B | 29.35% |
| L1 dTLB reloads that missed L2 TLB | 22.456 M | 1.570 M | 93.01% |
| 4 KiB reloads that missed L2 TLB | 21.781 M | 1.153 M | 94.71% |
| page faults | 1.053 M | 0.530 M | 49.70% |

The timed scan is the primary performance result because it excludes setup and reclaim. The whole-process PMU counters confirm the expected TLB mechanism and include mmap, prefault, warmup, scan, and teardown work.

### Setup and reclamation costs

| Policy | Median allocation ns/object | Median ephemeral free+reclaim ns/object |
|---|---:|---:|
| `ordinary-mixed` | 1,547.68 | 1.09 |
| `ordinary-segregated` | 1,551.54 | 359.10 |
| `huge-mixed` | 315.26 | 1.11 |
| `huge-segregated` | 315.08 | 13.18 |
| `tiered` | 968.97 | 318.45 |

These numbers include mapping and first-touch costs. The bump-only research arena makes them mechanism diagnostics rather than production allocator fast-path estimates.

## Feasibility judgment

### Proven in this worktree

- The configured host supplies real 2 MiB HugeTLB mappings with zero fallback at a 4 GiB peak.
- Existing UniAlloc metadata can express exact lifetime classes without an ABI change.
- Lifetime segregation enables complete 2 MiB page reclamation in the adversarial mixed-lifetime trace.
- Long-lived HugeTLB placement materially improves dependent random access and sharply reduces TLB reload misses.
- Conservative unknown handling preserves a default/fallback class for future integration.

### Remaining gates

- **Per-object hint production:** the current pass copies one invocation-wide hint. Exact MIR drop/escape proofs can conservatively identify some ephemeral allocations; general long-lived classification needs allocation-context profiling or application annotations.
- **Production arena:** implement `(lifetime_class, size_class)` payload arenas, page fill state, reuse, empty-page release, and symmetric pointer provenance. Unknown objects continue through the current allocator.
- **Misprediction robustness:** inject 0.1%, 1%, and 5% long-as-ephemeral errors and add deadline/promotion behavior following LLAMA's recovery design.
- **Real workloads:** run compiler-instrumented collection/server workloads and report lifetime coverage, allocator throughput, RSS/PSS, actual HugeTLB bytes, fragmentation after phase changes, and PMU counters.
- **Pool semantics:** current `munmap` increases `HugePages_Free` inside the reserved 8 GiB pool. Ordinary RAM becomes available after deliberate pool shrinking or through a THP/surplus design.

The fastest next implementation is an opt-in two-class payload arena for one or two common size classes, with `Unknown → existing allocator`, followed by profile-derived `(type_id, callsite, size_class) → lifetime class` hints. The runtime mechanism has enough signal to justify that next experiment.
