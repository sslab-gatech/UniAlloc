# Type Isolation Primary-Suite Evaluation

This suite evaluates the current Type Isolation implementation across seven pinned Rust targets and 34 predeclared harnesses. It separates the cost of entering the typed compiler/runtime route from the incremental cost of enabling the Type Isolation policy, and it treats peak RSS according to each harness's work model.

## Frozen evaluation contract

### Targets and source pins

The target set, source pins, harness membership, and three allocator variants were fixed before measurement. Each source checkout retains its full commit identity and lockfile provenance.

| Target | Frozen upstream reference | Full source commit | Harnesses | RSS work model |
| --- | --- | --- | ---: | --- |
| Collections | Rust 1.97.0 | `2d8144b7880597b6e6d3dfd63a9a9efae3f533d3` | 5 | Adaptive iterations |
| Oxipng | v10.1.1 | `628e241e23f368097883807fa6e985ccf7c00357` | 5 | Fixed work |
| redb | v4.1.0 | `6ed1f981ba4deab0b2adbdd7bccb46ec409b2191` | 4 | Fixed work |
| Polars | py-1.42.1 | `0df0c25d4db895ec8cad773bedc4e98400e3135e` | 5 | Fixed work |
| SWC | v1.15.43 | `73f0f386da64fe432975183f9ccf28429b52638a` | 5 | Adaptive iterations |
| RustPython | `main@2026-07-13` | `a9c2c529b14199a7ae7893b9d82c8bebe6b17418` | 5 | Adaptive iterations |
| Actix Web | web-v4.14.0 | `696b1fed9c5b0147c37c70e0808cae5f63a5a4a0` | 5 | Adaptive iterations |

The source-specific compiler contracts are Rust nightly `nightly-2026-06-11` for Collections, Rust 1.85.1 for Oxipng, Rust 1.89 for redb, upstream nightly `nightly-2026-04-01` for Polars, and Rust 1.95.0 for RustPython. SWC and Actix Web retain their pinned upstream lockfiles and campaign toolchain evidence.

### Variants and comparison families

Every harness uses the same three variants:

| Variant | Compiler/runtime route | Type Isolation policy |
| --- | --- | --- |
| `unialloc` | UniAlloc baseline route | Disabled |
| `typed_plain` | Actual-MIR typed route | Disabled (`policy_flags=0`) |
| `typeiso_perf` | Same actual-MIR typed route | Enabled (`policy_flags=1`) |

The analysis reports three lower-is-better cost ratios for both execution cost and process peak RSS:

- **Compiler route:** `typed_plain / unialloc`
- **Policy increment:** `typeiso_perf / typed_plain`
- **End to end:** `typeiso_perf / unialloc`

A ratio above 1 means that the subject has higher cost. The compiler-route attribution gate classifies a harness as route-equivalent when the median paired execution-cost ratio lies in the closed interval `[0.85, 1.15]`. A gate miss retains the measurements and limits attribution for that harness.

### Sampling and aggregation

Each target-harness-variant combination has exactly one discarded warmup at round 0 and five measured rounds numbered 1 through 5. Variant order rotates within the paired rounds. The complete design therefore contains 102 warmup processes and 510 measured processes before accounting for benchmark-internal iterations.

For each harness, the reported summary is the median of the five complete same-round ratios. A target aggregate is the equal-weight geometric mean of its eligible harness medians, and the suite aggregate is the equal-weight geometric mean of eligible target aggregates. Run count, wall time, operation count, harness count, and allocator column count receive no additional weight. Native execution sources remain explicit: process wall time, benchmark-owned operation time, Rust libtest estimates, or Criterion median time per iteration.

Data eligibility requires a successful workload-native correctness oracle, successful builds, verified allocator activation, verified actual-MIR provenance for the typed variants, disabled performance statistics, retained source-audit evidence, one digest-validated warmup per variant, and five complete paired rounds. `typed_plain` and `typeiso_perf` must share the same derived source and differ only in the policy flag.

## Allocator implementation provenance

Every target campaign materializes the allocator and MIR pass from Git revision `f5d0c19c1cc5b56fdac3282d69333dd8c85d4cf2`. Its canonical implementation digest is:

```text
cab1e580c08e2b16308bae75501716049ba428b040fb04bf305269c9ba9eaf01
```

The canonical scope contains 131 files and 4,426,670 bytes. It includes Rust and TOML files under `unialloc/` and `alloc_macros/`, plus `tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs`. Paths containing a `target` component and other extensions, including `Cargo.lock`, are excluded. For each lexicographically sorted snapshot-relative POSIX path, SHA-256 consumes the UTF-8 path, a NUL byte, the exact Git blob, and a final NUL byte.

The independent Git-blob recomputation and frozen snapshot recomputation agree. The tracked canonical inventory is `docs/evidence/type-isolation-primary-suite-20260714/canonical-implementation-snapshot-f5d0c19.json`. Its nested component audit records the Collections and Oxipng capture campaign; the central assembler separately validates the same implementation identity in all seven target results.

## Host placement and shared-machine boundary

Measurements run on the same 128-logical-CPU AMD EPYC 9354 host with two NUMA nodes. The campaign lanes use disjoint physical-core ranges:

| Campaign lane | CPU affinity | NUMA contract |
| --- | --- | --- |
| Collections and Oxipng | `0-15` | Physical CPUs and memory bound to node 0 |
| redb and Actix Web | `16-31` | Exact inherited CPU affinity; all selected CPUs belong to node 0 |
| Polars, SWC, and RustPython | `32-63` | Physical CPUs and memory bound to node 1 |

All primary measurements acquire `evaluation/raw/type-isolation-primary-v1/primary-measurement.lock`, so suite measurement processes are serialized across campaign lanes. The Polars, SWC, and RustPython lane also waits for build-process quiescence on CPUs `32-63`. redb and Actix Web record their exact inherited affinity; their lane has no separate memory-binding requirement.

This is a shared remote host. CPU affinity, NUMA placement, variant rotation, and the cross-campaign lock reduce controlled interference. External system activity outside the reserved CPU sets can still affect wall-time and process-RSS observations, so the retained raw environment and paired-round design define the evidence boundary.

## RSS interpretation amendment

The original result-blind campaign manifest is preserved at `docs/evidence/type-isolation-primary-suite-20260714/pre-amendment-suite-manifest-96fa640.json` with SHA-256:

```text
96fa64009550d52f89549dd2124e3b1a402a42b7f1fd1750888600dd67bb8a6f
```

The active analysis contract, `evaluation/config/type_isolation_primary_suite.json`, has SHA-256:

```text
ba386a779e45e0884e02cf8825c69985d1b5cf68d57366af78e7f77cfa8f6ccb
```

Amendment `rss-work-interpretation-20260714` changes RSS interpretation, aggregate eligibility, and presentation markers. Target membership, source pins, harnesses, variants, benchmark definitions, and the one-warmup plus five-round sampling budget remain unchanged.

The fixed-work RSS core contains Oxipng, redb, and Polars: three targets and 14 harnesses. These harnesses execute the same predefined work in every variant, so their peak-RSS ratios support equal-work comparisons and geometric-mean aggregation.

Collections, SWC, RustPython, and Actix Web use workload-native adaptive iteration counts: four targets and 20 harnesses. Their peak RSS remains visible as a process-volume diagnostic and is excluded from fixed-work RSS geometric means. The figure marks these rows with `‡`. Execution-cost ratios remain eligible under their execution contracts.

### Actix adaptive-RSS audit boundary

The `async_service_direct` audit retains the Criterion sample records and all warmup and measured process observations at `docs/evidence/type-isolation-primary-suite-20260714/actix-async-service-direct-adaptive-rss-audit.json`. Criterion changes the measured iteration count in response to variant speed. The retained iteration-count/RSS fit has `R² = 0.999955`, establishing that its peak RSS primarily tracks process volume for this microbenchmark. Its RSS supports a process-observed diagnostic, while its execution timing supports an exact-microbenchmark observation subject to the compiler-route attribution classification.

## Tracked artifacts and reproduction

Reproduce the tracked suite result from the seven validated target bundles with:

```bash
mkdir -p evaluation/raw/.tmp
TMPDIR="$PWD/evaluation/raw/.tmp" \
  uv run python evaluation/scripts/assemble_type_isolation_primary_results.py \
    --suite evaluation/config/type_isolation_primary_suite.json \
    --targets-dir evaluation/raw/type-isolation-primary-v1/targets \
    --output benchmark-results/type-isolation-primary-v1.json
```

The assembler validates exact membership, pins, implementation identity, compact build provenance, warmup attestations, gates, and measured-round completeness. The resulting JSON is self-contained for figure generation and contains repository-relative provenance paths.

Render the title-free horizontal comparison figure and its uncapped data export with:

```bash
TMPDIR="$PWD/evaluation/raw/.tmp" \
  uv run evaluation/scripts/plot_type_isolation_primary_suite.py \
    --suite evaluation/config/type_isolation_primary_suite.json \
    --results benchmark-results/type-isolation-primary-v1.json \
    --output-dir docs/figures/type-isolation-primary-suite
```

The output bundle contains SVG and PNG figures, the uncapped paired-ratio CSV, and a manifest with source and asset digests. Display clipping affects only the plotted log2 axes; the CSV and manifest preserve the true ratios.

<!-- PRIMARY-SUITE-RESULTS:START -->
## Results

Central assembly accepted all **7 targets and 34 harnesses**. The tracked result contains **102 digest-validated warmups** and **510 measured processes**. Every correctness, build, allocator-activation, actual-MIR, stats-disabled, and source-audit data gate passes. Compiler-route equivalence passes **13/34** harnesses; the remaining 21 harnesses stay visible with attribution-limit markers.

### Suite-level comparisons

| Comparison | Execution cost, all harnesses | Execution cost, route-equivalent subset | Fixed-work peak RSS | Process-observed peak RSS |
| --- | ---: | ---: | ---: | ---: |
| Compiler route | `2.362x` (34 harnesses) | `1.0432x` (+4.32%) (13 harnesses, 6 targets) | `1.0329x` (+3.29%) (14 harnesses, 3 targets) | `0.9643x` (-3.57%) (34 harnesses) |
| Policy increment | `1.0036x` (+0.36%) (34 harnesses) | `0.9969x` (-0.31%) (13 harnesses, 6 targets) | `1.0006x` (+0.06%) (14 harnesses, 3 targets) | `1.0039x` (+0.39%) (34 harnesses) |
| End to end | `2.369x` (34 harnesses) | `1.0413x` (+4.13%) (13 harnesses, 6 targets) | `1.0034x` (+0.34%) (14 harnesses, 3 targets) | `0.9508x` (-4.92%) (34 harnesses) |

The policy-only comparison is the principal Type Isolation mechanism result: across all 34 matched typed-path harnesses, enabling the policy changes execution cost by **+0.36%**. The route-equivalent subset is **-0.31%**. Across the 14 fixed-work harnesses, policy-only peak RSS changes by **+0.057%**. The compiler/runtime typed route itself is the dominant cost in the route-failing harnesses, especially SWC, RustPython parsing, and Actix microbenchmarks; those end-to-end ratios do not estimate the incremental policy cost.

The process-observed RSS column retains every target. Its adaptive rows describe the memory reached by each fresh benchmark process under workload-native iteration calibration. Only the fixed-work column supports equal-work RSS aggregation.

### Target aggregates

| Target | Route pass | Compiler route time | Policy time | End-to-end time | Policy RSS | End-to-end RSS | RSS interpretation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Collections | 2/5 | `1.4374x` (+43.74%) | `0.9930x` (-0.70%) | `1.4288x` (+42.88%) | `1.0000x` (+0.00%) `‡` | `1.0592x` (+5.92%) `‡` | Adaptive process volume `‡` |
| Oxipng | 5/5 | `1.0354x` (+3.54%) | `1.0058x` (+0.58%) | `1.0374x` (+3.74%) | `0.9998x` (-0.02%) | `1.0015x` (+0.15%) | Fixed work |
| redb | 3/4 | `1.1156x` (+11.56%) | `1.0058x` (+0.58%) | `1.1232x` (+12.32%) | `1.0033x` (+0.33%) | `1.0033x` (+0.33%) | Fixed work |
| Polars | 1/5 | `1.2940x` (+29.40%) | `1.0001x` (+0.01%) | `1.2975x` (+29.75%) | `0.9987x` (-0.13%) | `1.0055x` (+0.55%) | Fixed work |
| SWC | 0/5 | `23.84x` | `1.0060x` (+0.60%) | `23.93x` | `1.0147x` (+1.47%) `‡` | `1.0237x` (+2.37%) `‡` | Adaptive process volume `‡` |
| RustPython | 1/5 | `2.585x` | `1.0048x` (+0.48%) | `2.595x` | `1.0000x` (+0.00%) `‡` | `0.9788x` (-2.12%) `‡` | Adaptive process volume `‡` |
| Actix Web | 1/5 | `3.093x` | `1.0100x` (+1.00%) | `3.121x` | `1.0108x` (+1.08%) `‡` | `0.6553x` (-34.47%) `‡` | Adaptive process volume `‡` |

### Harness medians

Each cell is the median of five same-round ratios. `†` marks a compiler route outside `[0.85, 1.15]`; `‡` marks adaptive-iteration RSS. The full five-round observations remain in the tracked JSON and figure CSV.

| Target | Harness | Route time | Policy time | End-to-end time | Policy RSS | End-to-end RSS |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Collections | `binary_heap_push` ‡ | `1.0125x` (+1.25%) | `0.9937x` (-0.63%) | `1.0055x` (+0.55%) | `1.0000x` (+0.00%) | `1.0000x` (+0.00%) |
| Collections | `btree_set_clone_remove` †‡ | `3.258x` | `0.9844x` (-1.56%) | `3.234x` | `1.0000x` (+0.00%) | `1.3333x` (+33.33%) |
| Collections | `slice_random_inserts` ‡ | `1.0712x` (+7.12%) | `1.0016x` (+0.16%) | `1.0700x` (+7.00%) | `1.0000x` (+0.00%) | `1.0000x` (+0.00%) |
| Collections | `vec_in_place_collect_droppable` †‡ | `1.3952x` (+39.52%) | `0.9935x` (-0.65%) | `1.3858x` (+38.58%) | `1.0000x` (+0.00%) | `1.0000x` (+0.00%) |
| Collections | `vec_deque_grow_1025` †‡ | `1.2444x` (+24.44%) | `0.9919x` (-0.81%) | `1.2350x` (+23.50%) | `1.0000x` (+0.00%) | `1.0000x` (+0.00%) |
| Oxipng | `cli_issue_141_t1`  | `1.0176x` (+1.76%) | `1.0298x` (+2.98%) | `1.0442x` (+4.42%) | `0.9879x` (-1.21%) | `1.0096x` (+0.96%) |
| Oxipng | `cli_issue_141_t4`  | `1.0169x` (+1.69%) | `1.0255x` (+2.55%) | `1.0420x` (+4.20%) | `1.0112x` (+1.12%) | `0.9979x` (-0.21%) |
| Oxipng | `filters_8bit_filter_0`  | `1.0880x` (+8.80%) | `0.9801x` (-1.99%) | `1.0558x` (+5.58%) | `1.0000x` (+0.00%) | `1.0000x` (+0.00%) |
| Oxipng | `filters_entropy`  | `1.0508x` (+5.08%) | `0.9969x` (-0.31%) | `1.0465x` (+4.65%) | `1.0000x` (+0.00%) | `1.0000x` (+0.00%) |
| Oxipng | `reductions_rgba_to_palette_8`  | `1.0057x` (+0.57%) | `0.9975x` (-0.25%) | `0.9994x` (-0.06%) | `1.0000x` (+0.00%) | `1.0000x` (+0.00%) |
| redb | `bulk_small`  | `1.0549x` (+5.49%) | `1.0057x` (+0.57%) | `1.0689x` (+6.89%) | `1.0132x` (+1.32%) | `1.0132x` (+1.32%) |
| redb | `transaction_churn` † | `1.3983x` (+39.83%) | `1.0087x` (+0.87%) | `1.4105x` (+41.05%) | `1.0000x` (+0.00%) | `1.0000x` (+0.00%) |
| redb | `delete_reinsert`  | `1.0587x` (+5.87%) | `1.0096x` (+0.96%) | `1.0633x` (+6.33%) | `1.0000x` (+0.00%) | `1.0000x` (+0.00%) |
| redb | `large_values`  | `0.9918x` (-0.82%) | `0.9992x` (-0.08%) | `0.9928x` (-0.72%) | `1.0000x` (+0.00%) | `1.0000x` (+0.00%) |
| Polars | `groupby_low_cardinality` † | `1.4308x` (+43.08%) | `0.9982x` (-0.18%) | `1.4282x` (+42.82%) | `0.9933x` (-0.67%) | `1.0068x` (+0.68%) |
| Polars | `groupby_high_cardinality` † | `1.4001x` (+40.01%) | `0.9979x` (-0.21%) | `1.3971x` (+39.71%) | `1.0000x` (+0.00%) | `1.0068x` (+0.68%) |
| Polars | `groupby_multikey` † | `1.2588x` (+25.88%) | `1.0011x` (+0.11%) | `1.2662x` (+26.62%) | `0.9965x` (-0.35%) | `1.0013x` (+0.13%) |
| Polars | `filter_retain` † | `1.4237x` (+42.37%) | `1.0016x` (+0.16%) | `1.4281x` (+42.81%) | `1.0000x` (+0.00%) | `1.0068x` (+0.68%) |
| Polars | `csv_scan`  | `1.0106x` (+1.06%) | `1.0018x` (+0.18%) | `1.0193x` (+1.93%) | `1.0035x` (+0.35%) | `1.0059x` (+0.59%) |
| SWC | `large_parser` †‡ | `56.52x` | `1.0070x` (+0.70%) | `57.02x` | `1.0203x` (+2.03%) | `0.9887x` (-1.13%) |
| SWC | `large_fixer` †‡ | `81.69x` | `1.0215x` (+2.15%) | `82.72x` | `1.0022x` (+0.22%) | `1.0151x` (+1.51%) |
| SWC | `large_codegen_es2020` †‡ | `2.891x` | `0.9878x` (-1.22%) | `2.857x` | `1.0182x` (+1.82%) | `1.0939x` (+9.39%) |
| SWC | `large_all_es2020` †‡ | `27.24x` | `1.0056x` (+0.56%) | `27.33x` | `1.0080x` (+0.80%) | `1.0077x` (+0.77%) |
| SWC | `large_all_es5` †‡ | `21.17x` | `1.0086x` (+0.86%) | `21.30x` | `1.0248x` (+2.48%) | `1.0162x` (+1.62%) |
| RustPython | `parse_mandelbrot` †‡ | `4.579x` | `1.0123x` (+1.23%) | `4.617x` | `1.0190x` (+1.90%) | `1.0249x` (+2.49%) |
| RustPython | `execute_mandelbrot` ‡ | `1.0626x` (+6.26%) | `0.9631x` (-3.69%) | `1.0260x` (+2.60%) | `0.9897x` (-1.03%) | `1.0076x` (+0.76%) |
| RustPython | `execute_nbody` †‡ | `1.4291x` (+42.91%) | `1.0356x` (+3.56%) | `1.4740x` (+47.40%) | `0.9946x` (-0.54%) | `1.0108x` (+1.08%) |
| RustPython | `execute_fannkuch` †‡ | `4.615x` | `1.0095x` (+0.95%) | `4.675x` | `1.0036x` (+0.36%) | `0.9324x` (-6.76%) |
| RustPython | `pystone_30000` †‡ | `3.599x` | `1.0049x` (+0.49%) | `3.602x` | `0.9936x` (-0.64%) | `0.9232x` (-7.68%) |
| Actix Web | `async_service_direct` †‡ | `53.91x` | `0.9859x` (-1.41%) | `52.95x` | `1.0159x` (+1.59%) | `0.107x` |
| Actix Web | `async_web_service_direct` †‡ | `3.113x` | `1.0211x` (+2.11%) | `3.160x` | `1.0837x` (+8.37%) | `1.0714x` (+7.14%) |
| Actix Web | `get_body_async_burst` †‡ | `1.2080x` (+20.80%) | `1.0495x` (+4.95%) | `1.2641x` (+26.41%) | `0.9583x` (-4.17%) | `1.0000x` (+0.00%) |
| Actix Web | `compression_gzip` ‡ | `1.0756x` (+7.56%) | `1.0092x` (+0.92%) | `1.0883x` (+8.83%) | `1.0000x` (+0.00%) | `1.0000x` (+0.00%) |
| Actix Web | `router_actix` †‡ | `1.2987x` (+29.87%) | `0.9856x` (-1.44%) | `1.2857x` (+28.57%) | `1.0000x` (+0.00%) | `1.0588x` (+5.88%) |

### Figure and machine-readable evidence

- Slide-ready SVG: `docs/figures/type-isolation-primary-suite/type-isolation-primary-suite.svg`
- Slide-ready PNG: `docs/figures/type-isolation-primary-suite/type-isolation-primary-suite.png`
- Uncapped plotted observations: `docs/figures/type-isolation-primary-suite/type-isolation-primary-suite-data.csv`
- Self-contained result: `benchmark-results/type-isolation-primary-v1.json`
- Figure/provenance manifest: `docs/figures/type-isolation-primary-suite/type-isolation-primary-suite-manifest.json`
<!-- PRIMARY-SUITE-RESULTS:END -->
