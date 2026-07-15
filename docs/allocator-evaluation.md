# Allocator evaluation

> **Evidence status:** current source-bound diagnostic; post-measurement
> presentation-analysis amendment. The amendment changes taxonomy and
> aggregation only. It leaves targets, harnesses, variants, observations,
> correctness gates, source identities, and sampling budgets unchanged.

The committee-facing evaluation has two parts. **Microbenchmarks** use Rust's
`std_bench` harnesses. **Macrobenchmarks** use pinned real-world Rust programs.
Both parts report execution cost and peak resident set size (RSS), preserve
matched comparisons, and treat Type Isolation as a UniAlloc variant.

## Presentation figures

| Part | Slide-ready figure | Presentation unit |
|---|---|---|
| Microbenchmarks | [`microbenchmarks.svg`](figures/allocator-evaluation-20260714/microbenchmarks.svg) | Rust `std_bench` leaf, aggregated with equal family weight |
| Macrobenchmarks | [`macrobenchmarks.svg`](figures/allocator-evaluation-20260714/macrobenchmarks.svg) | Real-world harness, aggregated with equal target weight |

The SVG and PNG files contain axes, labels, marks, values, and legends without
a title. The bundle also retains uncapped CSV data, compact derived JSON, and a
hash-bound manifest. Display clipping affects marks only.

## Comparison contract

A cost ratio below `1x` favors the subject variant; a ratio above `1x` favors
the reference. Performance and RSS remain separate metrics.

- A microbenchmark cell is the median of three measured fresh processes. The
  cell ratio uses the matching UniAlloc cell as its reference.
- A macrobenchmark harness forms same-round ratios before taking the median of
  five paired ratios.
- `typed_plain` uses the actual-MIR compiler/runtime route with policy disabled.
- `typeiso_perf` uses the same route with Type Isolation enabled. It is the
  **UniAlloc + Type Isolation** variant.
- Compiler-route cost (`typed_plain / unialloc`), policy increment
  (`typeiso_perf / typed_plain`), and end-to-end cost
  (`typeiso_perf / unialloc`) are distinct comparison families.

## Part I: microbenchmarks

### Complete Rust `std_bench` matrix

The external-allocator cohort contains all 468 canonical leaves in eight
families and seven allocator configurations. Of 3,276 allocator/leaf cells,
3,268 completed. Eight warm-up cells were censored at the 30-second timeout,
leaving 466 leaves with complete seven-allocator comparisons.

| Family | Leaves |
|---|---:|
| `binary_heap` | 6 |
| `btree` | 100 |
| `linked_list` | 9 |
| `slice` | 74 |
| `str` | 129 |
| `string` | 17 |
| `vec` | 118 |
| `vec_deque` | 15 |

Each allocator was a separate Cargo build with exactly one `bench_*` selector.
The repository-default `pthread_dtor` and `rseq` features remained enabled. One
discarded warm-up and three measured fresh processes ran each complete cell,
with one exact leaf and one libtest thread per process. Four deterministic lanes
used CPUs 0, 4, 8, and 12 on NUMA node 0. Build time was excluded.

### Robust equal-family performance headline

The headline admits the 236 complete leaves whose median is at least
100 ns/iter under every allocator. This timer-floor gate retains all eight
families. For each variant:

1. form every eligible leaf's matching-cell median ratio;
2. take the median leaf log-ratio within each family;
3. average the eight family summaries with equal weight;
4. exponentiate the result.

The hierarchy prevents large families from dominating and prevents one extreme
leaf from controlling a family. The conventional geometric mean within each
family, followed by equal family weighting, remains a sensitivity result.

| Variant vs. UniAlloc | Robust equal-family ratio | Conventional equal-family sensitivity |
|---|---:|---:|
| ptmalloc | `0.9826x` | `0.9402x` |
| jemalloc | `0.9864x` | `0.9313x` |
| mimalloc | `0.9901x` | `0.8885x` |
| TCMalloc | `0.9787x` | `0.9099x` |
| snmalloc | `0.9748x` | `0.8725x` |
| Scudo | `1.0207x` | `1.0645x` |

Many closures mainly measure library work and perform no allocation in the
timed region. These whole-closure ratios therefore describe allocator-linked
binaries; they do not establish allocator fast-path parity. The figure keeps
all eligible leaf points visible, and the CSV retains every positive leaf.

### Collections Type Isolation cohort

Collections contributes five selected `std_bench` harnesses from the current
Type Isolation suite, using Rust 1.97.0 at
`2d8144b7880597b6e6d3dfd63a9a9efae3f533d3`. Its compiler route and leaf set
differ from the complete allocator cohort, so the figure separates it and never
pools the values.

| Variant vs. UniAlloc | Equal-harness execution ratio | Process-observed RSS ratio |
|---|---:|---:|
| Typed control | `1.4374x` | `1.0592x` diagnostic |
| Type Isolation | `1.4288x` | `1.0592x` diagnostic |

The direct policy increment across these five harnesses is `0.9930x` execution
cost. Two of five harnesses meet the compiler-route equivalence gate.

### RSS interpretation

GNU `time` recorded peak RSS for every fresh `std_bench` process. Rust libtest
adaptively chooses iteration counts, and the campaign did not retain equal
operation counts. Microbenchmark RSS therefore describes observed process
volume. Hollow marks identify this diagnostic metric. A claim-grade micro RSS
campaign must pin equal work and a common sampling checkpoint.

The tracked full-result JSON does not contain per-cell RSS. The two-tier
exporter authenticates the retained raw `records.jsonl` by SHA-256 and persists
every uncapped derived RSS ratio in the presentation CSV and JSON bundle.

### Extrema, censoring, and the large-allocation cliff

The stable lowest robust ratio for ptmalloc, jemalloc, mimalloc, TCMalloc, and
snmalloc is `vec::bench_flat_map_collect`: `0.1353x` to `0.1374x`. The closure
creates a 2,000,000-byte `Vec<u8>` on every iteration. UniAlloc's bounded hosted
warm-run slot accepts at most 512 KiB/128 pages, so this request follows the
cold hosted path. A focused profile counted 305 2 MiB `mmap` calls and 302
matching `munmap` calls across 302 reported iterations. Repeated mapping and
page faults identify a concrete large-run retention policy cliff.

The largest robust high ratios are `3.4030x` for ptmalloc on
`slice::sort_large_ascending` and `18.2140x` for Scudo on
`btree::map::clone_fat_val_100_and_clear`. These are descriptive three-sample
extrema; the uncapped CSV preserves exact medians and raw observations.

| Censored leaf | Variants | Phase | Timeout |
|---|---|---|---:|
| `btree::map::iter_1m` | all seven | warm-up | 30 s |
| `linked_list::bench_push_back` | Scudo | warm-up | 30 s |

A censored warm-up produced no timing estimate and skipped its three measured
processes. Scudo also reported `Can't populate more pages for size class 64.`
and `Can't populate more pages for size class 96.` for the linked-list leaf.
The campaign produced 13,080 terminal records and ended as
`complete_with_timeout_censoring`.

### Microbenchmark provenance

- Source commit: `071c4f06426ea06a66419e4ffd208a3cb7c2bb7f`
- Measured `unialloc` Git tree object ID:
  `a96b4143548598a65dfd9d85e3ed6e04f8922875`
- Toolchain: `nightly-2026-06-11`, rustc 1.98.0-nightly, LLVM 22.1.6
- Host: dual-socket AMD EPYC 9354, Linux 6.8.0-111-generic
- Measurement interval: 2026-07-14 23:37:17 UTC to 2026-07-15 00:58:23 UTC
- Raw summary SHA-256:
  `9b224be377e1719031bdd907ee26fbed71b916fef6cc6b00b3f0fcde0d96cca9`
- Raw records SHA-256:
  `e5499bca4d7ebbf722ceeeff881bf905e7e2a2879f2c921fc481f3380a4e286f`
- TCMalloc runtime SHA-256:
  `3e9994675a51a1a7f893e02236b36e3150a37b5e8ea740e0cc78f6650f7b1841`
- Scudo runtime: LLVM 18 standalone allocator at
  `/usr/lib/llvm-18/lib/clang/18/lib/linux/libclang_rt.scudo_standalone-x86_64.so`,
  SHA-256 `e7859566ba641ac49674e5c51c6edf605624786a4a4a748b31b73ec0135c9d6f`
- `GLIBC_TUNABLES` was absent; glibc's default rseq policy remained active.

The host was shared and cells contain three measurements, so this complete
matrix remains diagnostic grade. Runtime identities for TCMalloc and Scudo,
benchmark inventories, binaries, process outputs, timeout records, and focused
syscall evidence remain under the ignored raw evidence root.

The recorded runner and plot commands were:

```bash
RAW_ROOT="$PWD/evaluation/raw/std-bench-allocator-full-20260714"
git worktree add --detach /tmp/unialloc-full-std-bench-run \
  071c4f06426ea06a66419e4ffd208a3cb7c2bb7f
cd /tmp/unialloc-full-std-bench-run

numactl --physcpubind=0-15 --membind=0 taskset -c 0-15 \
  uv run python evaluation/scripts/run_std_bench_allocator_full.py \
  --source-root "$PWD" --output-dir "$RAW_ROOT" \
  --jobs 4 --cpus 0,4,8,12 --numa-node 0 --timeout-seconds 30 \
  --tcmalloc-lib-dir "$UNIALLOC_TCMALLOC_LIB_DIR" \
  --scudo-runtime-library \
    /usr/lib/llvm-18/lib/clang/18/lib/linux/libclang_rt.scudo_standalone-x86_64.so

uv run evaluation/scripts/plot_std_bench_allocator_full.py \
  --summary "$RAW_ROOT/summary.json" \
  --records "$RAW_ROOT/records.jsonl"
```

## Part II: macrobenchmarks

### Frozen target set

Collections belongs to the microbenchmark part. The macro cohort contains 29
predeclared harnesses across six current real-world Rust programs.

| Target | Frozen upstream identity | Harnesses | RSS work model |
|---|---|---:|---|
| Oxipng | v10.1.1, `628e241e23f368097883807fa6e985ccf7c00357` | 5 | Fixed work |
| redb | v4.1.0, `6ed1f981ba4deab0b2adbdd7bccb46ec409b2191` | 4 | Fixed work |
| Polars | py-1.42.1, `0df0c25d4db895ec8cad773bedc4e98400e3135e` | 5 | Fixed work |
| SWC | v1.15.43, `73f0f386da64fe432975183f9ccf28429b52638a` | 5 | Adaptive iterations |
| RustPython | main at 2026-07-13, `a9c2c529b14199a7ae7893b9d82c8bebe6b17418` | 5 | Adaptive iterations |
| Actix Web | web-v4.14.0, `696b1fed9c5b0147c37c70e0808cae5f63a5a4a0` | 5 | Adaptive iterations |

Every harness has one digest-validated warm-up and five measured paired rounds.
Variant order rotates within a round. Workload-native correctness, successful
builds, allocator activation, actual-MIR provenance, statistics-disabled
execution, source audits, and complete paired rounds are mandatory eligibility
gates.

For each comparison family and metric, the analysis takes a harness paired
median, forms a geometric mean within each target, and gives every target equal
weight in the suite geometric mean. Harness count and internal iteration count
receive no extra suite weight.

The paper artifact supplies useful target classes and normalized plot data, but
omits enough source, lockfile, input, command, and memory-collection identity to
recreate several historical rows directly. Historical SWC v1.2.51 lacks a
usable lockfile, RustPython v0.1.2 has no matching upstream tag, Polars v0.13.0
contains a floating dependency, Rsedis lacks a revision and load command, and
the TechEmpower table lacks a FrameworkBenchmarks commit and workload contract.
The current-version suite therefore freezes maintained sources independently
and keeps historical reproduction as a separate evidence track.

### Expansion queue

The next targets should add allocation patterns absent from the six-program
macro core while retaining short, fixed, reproducible harnesses.

| Priority | Frozen target | Representative workload | Added coverage |
|---|---|---|---|
| 1 | Tantivy 0.26.1, `d8f4c0b703120ed98f06297724dc1522df6019b9` | HDFS/GitHub/Wiki indexing | Multi-threaded posting lists, segments, and strong RSS signal |
| 2 | Nushell 0.114.1, `0df4ca222cc713e79b6b1684ad8ccaec584ce4ac` | Parser, records, JSON, tables, parallel evaluation | AST, structured-value, string, and pipeline churn |
| 3 | Typst v0.15.0, `3ae52774b48987fc78a72ff483068cacc28e46c2` | Prose/table, math, and image-heavy documents | Parser, layout, font cache, and PDF serialization lifetimes |
| RSS | rust-analyzer 2026-07-13, `ffcdbbd9066218089aaf0d428d5bf51cadcbcf48` | Analysis statistics, cache priming, completion | Long-lived incremental queries and interning |
| Overlay | Ruff 0.15.21, `3e1f63645109e69f9bccc7be354c5ee4672fb8f3` | Parser, linter, formatter | Short-lived AST and string churn; Linux benches require allocator overlay |

Wasmtime remains an appendix candidate because JIT mappings, linear memories,
and its pooling allocator require PSS and `Private_Dirty` alongside global-
allocator peak RSS.

### Target results

| Target | Route pass | Compiler route | Policy increment | End to end | Policy RSS | RSS interpretation |
|---|---:|---:|---:|---:|---:|---|
| Oxipng | 5/5 | `1.0354x` | `1.0058x` | `1.0374x` | `0.9998x` | Fixed work |
| redb | 3/4 | `1.1156x` | `1.0058x` | `1.1232x` | `1.0033x` | Fixed work |
| Polars | 1/5 | `1.2940x` | `1.0001x` | `1.2975x` | `0.9987x` | Fixed work |
| SWC | 0/5 | `23.84x` | `1.0060x` | `23.93x` | `1.0147x` | Adaptive diagnostic |
| RustPython | 1/5 | `2.585x` | `1.0048x` | `2.595x` | `1.0000x` | Adaptive diagnostic |
| Actix Web | 1/5 | `3.093x` | `1.0100x` | `3.121x` | `1.0108x` | Adaptive diagnostic |

| Suite comparison | Execution cost | Route-equivalent execution cost | Fixed-work peak RSS |
|---|---:|---:|---:|
| Compiler route | `2.5652x` (29 harnesses) | `1.0435x` (11 harnesses, 5 targets) | `1.0329x` (14 harnesses, 3 targets) |
| Policy increment | **`1.0054x` (+0.54%)** | **`0.9968x` (-0.32%)** | **`1.0006x` (+0.057%)** |
| End to end | `2.5770x` (29 harnesses) | `1.0421x` (11 harnesses, 5 targets) | `1.0034x` (14 harnesses, 3 targets) |

The policy increment is the principal Type Isolation result. The typed compiler
route dominates end-to-end cost where route equivalence fails, especially SWC,
RustPython parsing, and Actix Web microbenchmarks. The lead macro figure shows
policy increment only. Compiler-route and end-to-end observations remain in the
CSV, JSON, and table above.

### RSS amendment and audit boundary

The fixed-work RSS core contains Oxipng, redb, and Polars: 14 harnesses across
three targets. SWC, RustPython, and Actix Web retain workload-native adaptive
iteration counts. Their hollow RSS observations describe process volume and are
excluded from the suite RSS diamond.

The result-blind pre-amendment manifest remains at
[`pre-amendment-suite-manifest-96fa640.json`](evidence/type-isolation-primary-suite-20260714/pre-amendment-suite-manifest-96fa640.json),
SHA-256 `96fa64009550d52f89549dd2124e3b1a402a42b7f1fd1750888600dd67bb8a6f`.
The active analysis contract is
[`type_isolation_primary_suite.json`](../evaluation/config/type_isolation_primary_suite.json),
SHA-256 `ba386a779e45e0884e02cf8825c69985d1b5cf68d57366af78e7f77cfa8f6ccb`.
The amendment changes RSS interpretation, aggregate eligibility, and markers;
it changes no measured membership or sampling.

The Actix `async_service_direct` audit retains exact Criterion iterations and
RSS samples. The iteration-count/RSS fit has `R² = 0.999955`, which establishes
process volume as the dominant explanation for that adaptive RSS row.

### Implementation, placement, and lock boundary

All seven source campaigns, including Collections, materialize UniAlloc and the
MIR pass from Git revision `f5d0c19c1cc5b56fdac3282d69333dd8c85d4cf2`.
The canonical 131-file, 4,426,670-byte implementation digest is:

```text
cab1e580c08e2b16308bae75501716049ba428b040fb04bf305269c9ba9eaf01
```

Independent Git-blob and frozen-snapshot recomputations agree. The inventory is
[`canonical-implementation-snapshot-f5d0c19.json`](evidence/type-isolation-primary-suite-20260714/canonical-implementation-snapshot-f5d0c19.json).

All campaigns ran on the same 128-logical-CPU AMD EPYC 9354 host. Collections
and Oxipng bound CPUs and memory to CPUs 0-15 and NUMA node 0. redb and Actix
Web inherited exact CPU affinity on CPUs 16-31, whose selected CPUs belong to
node 0; their contract did not add a separate memory-binding requirement.
Polars, SWC, and RustPython bound CPUs and memory to CPUs 32-63 and node 1.
Primary runs use a shared campaign lock. The lock prevents overlap between
these campaigns; it does not eliminate unrelated host activity.

## Historical allocator comparison cohort

An older diagnostic campaign measured ripgrep, fd, and Oxipng with System,
jemalloc, mimalloc, TCMalloc, UniAlloc, typed control, and Type Isolation. It
uses implementation SHA-256
`d4ade5d634e4b7b03ca83611a22b596f5bac0498428c25058b5b9317f9ed29cf`
and a different target cohort, so it is excluded from the current macro
aggregate and figures.

| Subject vs. reference | Wall-time geomean | Peak-RSS geomean |
|---|---:|---:|
| UniAlloc vs. mimalloc | `1.0652x` | `0.4538x` |
| UniAlloc vs. TCMalloc | `1.0544x` | `0.5790x` |
| Type Isolation vs. typed control | `1.0181x` | `1.0003x` |

The observations showed UniAlloc using 54.62% less peak RSS than mimalloc and
42.10% less than TCMalloc across the three workloads, alongside 6.52% and 5.44%
higher wall-time geomeans. Those RSS differences are allocator-policy
sensitivities, including mimalloc THP and arena behavior; the canonical memory
appendix records the control experiments.

| App | Captured result SHA-256 | Workload input SHA-256 | Output-equivalence SHA-256 |
|---|---|---|---|
| ripgrep | `62e59c2147ffb959c3adffb700e9de95c4263c105fa735647731452558a9a38c` | `4a059105d877d838d7166cfd2d5fd7780487fe9020599bef8bf384b78ba3ca9b` | `d9bd5723d23e35e03e773f87a8be1f77715118df0bbe65a0e36d48d2eeea0f47` |
| fd | `4fb23c3698fa43fcdc03c05528a50f6a9fe850b06863d13410a9439e856af04d` | `835f96aaa48205f8c6decd7683c594f8fbabc1d00bb0a673cf3adb03fd125c8d` | `c8e3f5f5753d414b6d409d296860f77898980f3ca80d482241fbdbd73df25d03` |
| Oxipng | `7835b3f576906ecab96476816a321eca9e641d2ebedffe704c09fef72a88978d` | `909418f09bfb42612575aa3d12c7d7b57546d361b382a9ebd8d3c83531948334` | `581b84d2185c6c10f9f3eec2cdbc75671136910428c630d347c43e7ec41f6743` |

The command family is preserved by
[`realworld_type_isolation_matrix.py`](../evaluation/scripts/realworld_type_isolation_matrix.py):

```bash
python3 evaluation/scripts/realworld_type_isolation_matrix.py \
  --apps APP \
  --variants system,jemalloc,mimalloc,tcmalloc,unialloc,typed_plain,typeiso_perf \
  --warmups 1 --repetitions 7 --toolchain nightly-2026-06-11 \
  --cpu-list 20 --numa-node 0 --jobs 32 \
  --tcmalloc-library "$TCMALLOC_LIBRARY" --discard-run-output \
  --raw-dir "evaluation/raw/realworld-rust-allocator-20260714/APP"
```

ripgrep and fd used `--full` with path repetitions 32 and 12 respectively;
Oxipng used `--quick`. Exact commands and every row remain in
[`realworld-rust-allocator-20260714.json`](../benchmark-results/realworld-rust-allocator-20260714.json).

## Historical G001 boundary

The paper-reproduction goal G001 was superseded on 2026-07-12 after 20 accepted
source-bound records. It remained incomplete: the formal claim check had zero
passing claims, seven missing claims, and 249 missing requirements. Its final
content-scoped source digest was
`222726711f3c31515d6073a060af45fc6d910a063af4db759fd877ee3989c08e`.
The digest covered claim-affecting source, evaluation logic, and workload
configuration. It excluded documentation, generated evidence, build output,
external checkouts, and test-only evaluator scripts.
Historical partial matrices, fixture transcripts, and smoke runs remain
diagnostic evidence and do not support a paper performance claim. Current
claims use the artifacts and explicit boundaries in this document.

## Artifact index

- Full microbenchmark result:
  [`std-bench-allocator-full-20260714.json`](../benchmark-results/std-bench-allocator-full-20260714.json)
- Full microbenchmark detailed CSV and legacy audit figures:
  [`std-bench-allocator-full-20260714/`](figures/std-bench-allocator-full-20260714/)
- Current Type Isolation result:
  [`type-isolation-primary-v1.json`](../benchmark-results/type-isolation-primary-v1.json)
- Current Type Isolation evidence and provenance:
  [`type-isolation-primary-suite-20260714/`](evidence/type-isolation-primary-suite-20260714/)
- Two-tier presentation data and manifest:
  [`allocator-evaluation-20260714/`](figures/allocator-evaluation-20260714/)
- Historical three-application result:
  [`realworld-rust-allocator-20260714.json`](../benchmark-results/realworld-rust-allocator-20260714.json)
- Historical paper-target audit and live diagnostic data:
  [`rust-alloc-paper-evaluation-20260714.json`](../benchmark-results/rust-alloc-paper-evaluation-20260714.json)
- Memory and large-page mechanisms:
  [`allocator-memory-and-mechanisms.md`](allocator-memory-and-mechanisms.md)
- Evaluation toolchain policy:
  [`evaluation-toolchains.md`](evaluation-toolchains.md)

## Reproduction

Reassemble the current Type Isolation result:

```bash
mkdir -p evaluation/raw/.tmp
TMPDIR="$PWD/evaluation/raw/.tmp" \
  uv run python evaluation/scripts/assemble_type_isolation_primary_results.py \
    --suite evaluation/config/type_isolation_primary_suite.json \
    --targets-dir evaluation/raw/type-isolation-primary-v1/targets \
    --output benchmark-results/type-isolation-primary-v1.json
```

Regenerate the canonical figures and data:

```bash
uv run evaluation/scripts/plot_two_tier_allocator_evaluation.py
```

The exporter validates source schemas and hashes, the authenticated full
`std_bench` raw-record identity, target classification, comparison eligibility,
and aggregation shape before atomically replacing the bundle. Its PEP 723
metadata pins Matplotlib 3.11.0 for deterministic rendering.
