# UniAlloc raw release-path ROI benchmark

This artifact evaluates the compile-time raw `GlobalAlloc` dispatch in commit
`2060313812b2369fc0c82761d10606c1d7a20a6d` against its direct development-line
baseline `0d117f6f5d887bdfab432db20f2ccfa8f596f6f8`.

## Result

The source-audited allocator-path subset contains 37 repeated leaves. Its median
speedup is **+1.30%**, its geometric-mean speedup is **+3.40%**, and its observed
range is **-1.10% to +19.22%**. Every leaf in this subset remains within 2% of the
baseline or improves by more than 2%.

The strongest repeated small-allocation results are:

| Benchmark | Baseline median | Candidate median | Speedup |
|---|---:|---:|---:|
| `string::bench_with_capacity` | 14.39 ns | 12.07 ns | +19.22% |
| `string::bench_from_str` | 15.40 ns | 13.34 ns | +15.44% |
| `linked_list::bench_collect_into` | 951.81 ns | 846.56 ns | +12.43% |
| `vec::bench_with_capacity_1000` | 14.12 ns | 13.00 ns | +8.62% |
| `linked_list::bench_push_front_pop_front` | 13.11 ns | 12.22 ns | +7.28% |

Speedup uses `100 * (baseline / candidate - 1)`. Positive values mean the
candidate is faster.

## Regression coverage

- A 22-leaf targeted panel uses five measured fresh processes per binary and
  leaf, one warmup, alternating A/B order, CPU 22, and NUMA node 0.
- A 57-leaf stratified panel uses four measured fresh processes under the same
  pinning and order protocol.
- A five-leaf confirmatory panel repeats full-sweep extremes with five fresh
  processes.
- The complete sweep executes all 468 leaves once for each binary. Both runs
  completed successfully. The baseline took 988.35 seconds; the candidate took
  958.67 seconds.
- Floor-filtered full-sweep family medians range from -0.51% to +0.28%. This
  keeps every family centered near parity while the repeated allocator-path
  subset records the intended gains.

The complete single-process sweep exposes order and linked-layout sensitivity.
Its raw range is -41.17% to +1345.74%; fresh-process confirmation moves the
apparent `vec::bench_chain_collect` extreme to +0.85%. Repeated panels carry the
performance claims.

Five repeated leaves carry explicit `[layout]` annotations. Disassembly shows
that `vec_deque::bench_mut_iter_1000` has an allocation-free timed loop whose
fetch-block alignment changes between binaries. `binary_heap::bench_from_vec`
includes allocation plus a dominant heapify loop; its baseline distribution is
bimodal and its linked loop alignment changes. The two large ordered-sort leaves
move strongly in opposite directions under the same allocator change. These
leaves remain visible as regression diagnostics and stay outside the
unconfounded allocator-path aggregate.

## Artifacts

- `allocator-roi-regression.svg`: repeated medians and paired ranges, family
  medians, the full 468-leaf heatmap, and binary/input provenance.
- `../../../benchmark-results/unialloc-raw-release-roi-20260715.json`: all
  repeated samples, full-sweep observations, classifications, aggregates, and
  SHA-256 provenance.
- `../../../benchmark-results/unialloc-raw-release-roi-rebased-validation-20260715.json`:
  final-rebase source and runtime-section identity validation.
- `../../../evaluation/scripts/render_unialloc_raw_release_roi.py`: deterministic
  standard-library renderer with input completeness and binary-identity gates.

The final rebased binaries preserve the measured normal-path sections byte for
byte. The shared section hashes are:

- baseline `.text`: `3221fa6510d3285d09d5519b7ba3b93b1e52a378f200800b81912d1f3b5bf927`
- candidate `.text`: `64117e3c759e6976afcd3d599bb4f43f0b8733fe04e86576aaaca18addd330c5`
- baseline `.rodata`: `a06c2b785462f6bb6b8fc4f63ad9f9db2d8700addd2284f21953f2c78c8b04a6`
- candidate `.rodata`: `5c683db4ee20cdcc67abc90e71215e143dccb9d7a161623e50a15a650b491ed9`

The whole-ELF hashes change with build IDs and source-location records. The
`.data.rel.ro` audit finds 54 baseline bytes and 4 candidate bytes changed; each
record increments its encoded source line by three after the helper refactor.
Executable code, primary immutable/mutable data, section sizes, and BSS sizes
remain exact, which preserves the completed timed-path evidence.
