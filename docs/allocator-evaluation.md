# Allocator evaluation

This document defines the current committee-facing evaluation for the frozen
UniAlloc revision `ce8af7b89a5cba9a9b3f57d9b02bb0c8cb5c3503`. It replaces
earlier mixed-cohort summaries with two populations, two comparison groups,
and two metrics:

- **Microbenchmarks:** the complete 468-case Rust `std_bench` inventory.
- **Macrobenchmarks:** 34 workloads from seven pinned real-world Rust targets.
- **Allocator baselines:** external allocators compared with default UniAlloc.
- **Feature ablation:** `typed_plain` and `typeiso_perf` compared with the
  appropriate UniAlloc route.
- **Metrics:** execution cost and peak resident set size (RSS) are reported in
  separate figures.

Microbenchmarks and macrobenchmarks remain separate statistical populations.
The presentation places them in adjacent panels for compact comparison and
applies an independent estimator to each population.

## Frozen evidence identity

| Item | Identity |
|---|---|
| Frozen source | `/home/hanqing/alloc/UniAlloc-current-eval-ce8` |
| Implementation revision | `ce8af7b89a5cba9a9b3f57d9b02bb0c8cb5c3503` |
| Implementation SHA-256 | `9deea74eaa2580ec1a0a57b001edfe9fa714428eaf0c211ee836bb0ddb16f178` |
| Suite manifest | `evaluation/config/type_isolation_primary_suite_v6_ce8af7b.json` |
| Suite manifest SHA-256 | `688b65afa2bf61f20c44192faf96afd6f4205e386ca2aea502dc101b7cd4484d` |

An input may contribute to the current figures only when its source revision,
implementation digest, suite digest, runner protocol, variant contract, and
workload contract match this frozen identity. The exporter fails closed on a
missing or mismatched binding.

## Comparison and aggregation contract

A cost ratio below `1x` favors the subject; a ratio above `1x` favors the
reference. RSS uses the same subject/reference orientation.

### Allocator baselines

The reference is default UniAlloc. The current external subjects are
`jemalloc`, `mimalloc`, `mimalloc_no_thp`, and modern Google TCMalloc.
`mimalloc_no_thp` uses the same mimalloc build contract with transparent huge
pages disabled. Google TCMalloc uses the pinned modern HPAA artifact described
in [google-tcmalloc-baseline.md](google-tcmalloc-baseline.md).

### Type Isolation ablation

| Comparison family | Subject | Reference | Interpretation |
|---|---|---|---|
| Compiler route | `typed_plain` | `unialloc` | Cost of the actual MIR compiler/runtime route with the isolation policy disabled |
| Isolation policy | `typeiso_perf` | `typed_plain` | Incremental cost of enabling the Type Isolation policy on the same route |
| End to end | `typeiso_perf` | `unialloc` | Deployment-visible cost of UniAlloc with Type Isolation enabled |

`typeiso_perf` is the **UniAlloc + Type Isolation** variant. `typed_plain` is a
control used for attribution rather than an external allocator baseline.

### Hierarchical estimators

The estimators follow established normalized-benchmark practice: medians
reduce run-level sensitivity and geometric means aggregate ratios without
changing when numerator and denominator units are rescaled.

For microbenchmarks:

1. take the median of three fresh measured processes for each variant and
   benchmark case;
2. divide the subject median by the matching reference median;
3. take the median log-ratio within each Rust benchmark family and transform
   it back to ratio space; and
4. take an unweighted geometric mean across the eight family summaries.

The headline set uses the same all-variant complete cases and requires a
minimum median of 100 ns/iteration under every selected variant. This prevents
one large family or timer-floor case from controlling the result.

For macrobenchmarks:

1. form same-round subject/reference ratios for each workload;
2. take the median of the three measured round ratios;
3. take an unweighted geometric mean across workload medians within a target;
   and
4. take an unweighted geometric mean across eligible target summaries.

References:

- [SPEC CPU 2017 Run and Reporting Rules](https://www.spec.org/cpu2017/Docs/runrules.html)
- [Fleming and Wallace, *How not to lie with statistics*](https://cgi.cse.unsw.edu.au/~cs9242/11/papers/Fleming_Wallace_86.pdf)
- [Cargo targets and the libtest harness](https://doc.rust-lang.org/cargo/reference/cargo-targets.html)

## Microbenchmark protocol

The Micro population is Rust's complete canonical `std_bench` inventory:

| Family | Cases |
|---|---:|
| `binary_heap` | 6 |
| `btree` | 100 |
| `linked_list` | 9 |
| `slice` | 74 |
| `str` | 129 |
| `string` | 17 |
| `vec` | 118 |
| `vec_deque` | 15 |
| **Total** | **468** |

Every cell uses one discarded warm-up followed by three fresh measured
processes. Each process selects one exact benchmark case and one libtest
thread. Build time is excluded. Timed processes are pinned to CPUs 0--31 on
NUMA node 0, which uses all 32 physical cores on that node. Independent builds
may use up to 64 jobs; the two timed Micro populations run sequentially to
preserve machine-level isolation.

The current raw roots are:

- Feature ablation:
  `evaluation/raw/std-bench-feature-ce8af7b-v8-final`
- Allocator baselines:
  `evaluation/raw/std-bench-allocator-baselines-ce8af7b-v1`

The feature matrix has 5,616 possible process slots
(`468 cases x 3 variants x (1 warm-up + 3 measured)`). It records 5,605
terminal processes: 467 cases are complete across all three variants and 238
also pass the 100 ns/iteration robustness gate. The allocator matrix has 9,360
possible process slots (`468 cases x 5 variants x (1 warm-up + 3 measured)`).
It records 9,345 terminal processes: 467 cases are complete across all five
variants, 237 pass the robustness gate, and five cells are timeout-censored.
Timeout-censored cases retain terminal records and are excluded symmetrically
from all selected variants.

### Current Micro performance result

| Comparison | Execution-cost ratio |
|---|---:|
| Google TCMalloc / UniAlloc | `1.0149x` |
| jemalloc / UniAlloc | `1.0026x` |
| mimalloc / UniAlloc | `1.0005x` |
| mimalloc with THP disabled / UniAlloc | `0.9976x` |
| Compiler route | `1.0070x` |
| Isolation policy | `0.9971x` |
| Type Isolation end to end | `1.0035x` |

The Type Isolation end-to-end Micro result is a `0.35%` execution-cost
increase. The matched compiler route accounts for `0.70%`; the incremental
isolation policy is `0.29%` faster within this population. These are
hierarchical aggregates over the robustness-gated common-complete cases.

### Micro RSS boundary

GNU `time` records peak RSS for each fresh process. The libtest benchmark
harness chooses iteration counts adaptively, so process RSS can reflect
different operation counts across variants. Micro RSS is therefore a
**diagnostic** metric. A claim-grade Micro RSS experiment requires equal work
and a common sampling checkpoint.

The Micro RSS geometric means are diagnostic. Google TCMalloc reports
`3.0198x` the default UniAlloc process peak RSS in this adaptive libtest
population. jemalloc, both mimalloc configurations, and all three Type
Isolation comparison families round to `1.0000x` at the process-observed
resolution. Historical Micro values remain archival evidence under their
original implementation identities.

## Macrobenchmark protocol

The Macro population contains seven maintained, immutable target pins and 34
workloads:

| Target | Pinned source | Workloads | RSS classification |
|---|---|---:|---|
| Collections | Rust `1.97.0`, `2d8144b7880597b6e6d3dfd63a9a9efae3f533d3` | 5 | Process-observed diagnostic |
| Oxipng | `v10.1.1`, `628e241e23f368097883807fa6e985ccf7c00357` | 5 | Fixed work |
| redb | `v4.1.0`, `6ed1f981ba4deab0b2adbdd7bccb46ec409b2191` | 4 | Fixed work |
| Polars | `py-1.42.1`, `0df0c25d4db895ec8cad773bedc4e98400e3135e` | 5 | Fixed work |
| SWC | `v1.15.43`, `73f0f386da64fe432975183f9ccf28429b52638a` | 5 | Process-observed diagnostic |
| RustPython | `main@2026-07-13`, `a9c2c529b14199a7ae7893b9d82c8bebe6b17418` | 5 | Process-observed diagnostic |
| Actix Web | `web-v4.14.0`, `696b1fed9c5b0147c37c70e0808cae5f63a5a4a0` | 5 | Process-observed diagnostic |

Every workload uses one discarded warm-up and three measured rounds. The
Type Isolation matrix contains all 408 raw records
(`34 workloads x 3 variants x 4 rounds`). The allocator-baseline selection
contains all 1,088 planned pairwise cells: each of four external subjects has
its own matched UniAlloc anchor, and every pair contains four rounds.

### Current Type Isolation result

The assembled result is
`benchmark-results/type-isolation-primary-v6-ce8af7b.json` (SHA-256
`04fb361c42b28f945b26130a63a81546d3e5613bb100e3bf3f50115cc0880de6`).
Its status is `complete_with_attribution_limits`.

| Comparison | Execution cost, all workloads | Execution cost, route-equivalent workloads | Fixed-work peak RSS |
|---|---:|---:|---:|
| Compiler route | `1.0358x` | `1.0319x` | `0.9970x` |
| Isolation policy | `1.2993x` | `1.3005x` | `1.0436x` |
| End to end | `1.3509x` | `1.3476x` | `1.0405x` |

The compiler-route equivalence gate accepts ratios in `[0.85x, 1.15x]`.
Thirty-three of 34 workloads pass. RustPython `parse_mandelbrot` reports
`1.1934x`, so attribution claims retain this explicit limit. The SWC target's
end-to-end execution-cost geometric mean is `2.8737x`; it remains visible in
the target-level data and figures.

Fixed-work RSS aggregates cover Oxipng, redb, and Polars: 14 workloads across
three targets. Peak RSS for Collections, SWC, RustPython, and Actix Web is
retained as process-observed diagnostic data and excluded from the headline
fixed-work aggregate.

### Current allocator-baseline evidence

The exact selected view is rooted at:

```text
evaluation/raw/type-isolation-primary-v6-ce8af7b/
  campaigns/macro-allocator-baselines-v4/
  selections/69037d76c4c0c99c16eb6dc216707a763ed3a5d66872185a363ab5abcf8e30b6/
```

The selection contains all 1,088 planned cells. Its exact-path cell index has
SHA-256
`fb919c79c78362ab36c96667440da91772569c0c69d55d568dcd424ce54bfc8b`;
the derived summary has SHA-256
`8ff1bd4ace14fe971dc5e64cd783dcf120ca620efbc11c9188a3c6646f14729b`.
The materializer records `raw_cells_mutated: false`.

The current Macro allocator aggregates are:

| Allocator subject | Execution-cost ratio | Peak RSS ratio |
|---|---:|---:|
| Google TCMalloc | `0.9881x` | `1.3595x` |
| jemalloc | `0.9521x` | `1.0661x` |
| mimalloc | `0.8125x` | `1.5327x` |
| mimalloc with THP disabled | `0.8975x` | `1.0497x` |

## Incremental evidence storage

Measurements are stored as immutable cells; summaries and presentation files
are replaceable views. This separation supports adding or removing a variant
without rerunning compatible cells.

1. **Raw cell:** one target, workload, cohort, variant, phase, and round. The
   record includes its command, environment, input, binary, source, and
   protocol bindings. Its content digest is immutable.
2. **Build record:** allocator and compiler-route build evidence is stored
   separately from timed measurements, keeping each raw timing record
   immutable during build reuse.
3. **Selection plan:** an immutable manifest enumerates the exact relative raw
   paths admitted for one comparison. Variant and target changes create a new
   plan while preserving raw data.
4. **Derived view:** `cells.jsonl`, summaries, normalized CSV, and figures are
   regenerated from the selected paths. `latest-selection.json` is a small
   mutable pointer to the chosen immutable plan.
5. **Compatibility boundary:** a source, implementation, suite, runner,
   workload, input, build, or environment change creates a new campaign epoch.
   Invalidated epochs are retained with an invalidation manifest and excluded
   from publication.

Adding a compatible variant therefore appends only its missing build and raw
cells plus any required matched reference anchor. Removing a variant changes
only the selection and derived outputs. Protocol or implementation changes
deliberately require fresh evidence.

## Presentation bundle

The current publication is
`docs/figures/allocator-evaluation-20260716`. The strict exporter writes four
title-free figures, each as SVG, PDF, and 300-dpi PNG:

- `allocator-baselines-performance`
- `allocator-baselines-peak-rss`
- `unialloc-features-performance`
- `unialloc-features-peak-rss`

Each figure uses separate Micro and Macro panels, large slide-readable labels
and legends, and high-contrast variant colors. The bundle also contains
`normalized-observations.csv`, `presentation-data.json`, and
`artifact-manifest.json`. The manifest binds every output to the exact input
digests; plots use display clipping only, while CSV and JSON retain uncapped
ratios.

The final bundle contains 15 files. `artifact-manifest.json` has SHA-256
`83282752e13ddd3742dee6e450ea3c9371cdb438e0859525c0e9b55973ba12be`; `presentation-data.json` has SHA-256
`64052d08b97ffaf5089884f897cafc74dd7a3d7cad891470f696efc76c4cafec`; and `normalized-observations.csv` has SHA-256
`79b42ebb337803de1755f7551469cf01ae4ffa914d8ad24a92c86e57bfcc55df`.

## Reproduction

Run the exporter only after both Micro campaigns report complete terminal
accounting:

```bash
uv run evaluation/scripts/plot_current_allocator_evaluation.py \
  --suite evaluation/config/type_isolation_primary_suite_v6_ce8af7b.json \
  --micro-feature evaluation/raw/std-bench-feature-ce8af7b-v8-final \
  --micro-baseline evaluation/raw/std-bench-allocator-baselines-ce8af7b-v1 \
  --macro-feature benchmark-results/type-isolation-primary-v6-ce8af7b.json \
  --macro-baseline evaluation/raw/type-isolation-primary-v6-ce8af7b/campaigns/macro-allocator-baselines-v4 \
  --output docs/figures/allocator-evaluation-20260716
```

The exporter validates raw-record identity, content digests, terminal
accounting, selected variants, complete paired rounds, suite bindings, source
pins, and aggregation results before atomically replacing the output
directory.

## Scope boundaries

- Micro RSS is diagnostic because adaptive libtest iteration counts do not
  establish equal work.
- RustPython `parse_mandelbrot` is outside the predeclared compiler-route
  equivalence interval, so the current result retains an attribution limit.
- SWC has a `2.8737x` target-level end-to-end Type Isolation execution-cost
  ratio and remains a visible optimization priority.
- Lifetime-aware allocation is outside this frozen evaluation. Its evidence
  belongs to a separate feature campaign and is not mixed into these figures.
