# Compiler-Derived Heap Lifetime Inference

## Status

This document records the design, preregistered evaluation method, and completed
five-pair controlled-oracle campaign for marker-free compiler heap-lifetime
inference. The machine-readable evidence is
[`summary.json`](evidence/compiler-heap-lifetime-oracle-20260715/summary.json).

Current result status: **bounded mechanism oracle verified; physical THP gate
failed closed because all five THP samples had zero resident
`AnonHugePages`.**

## Research question

The experiment asks whether Rust ownership evidence can direct heap placement
without application epoch markers or allocator-side lifetime training. The
compiler exports a bounded set of ownership facts through the existing
semantic allocation ABI:

| Compiler fact | ABI hint | Runtime meaning |
|---|---:|---|
| Exact local or move-chain Drop | `0` | Unknown: the owner eventually reaches Drop, while pressure-relative duration remains unclassified |
| Bounded process-long mechanism oracle | `0xA102` | The controlled owner reaches `mem::forget` or `Box::leak` |
| Unproven | `0` | Preserve the existing allocator path |

`0xA101` remains a compatibility identity for the local-Drop analysis fact,
and the runtime classifies it as Unknown. The current compiler exports exact
Drop with hint/confidence `0`; eventual release alone supplies no
pressure-relative Short claim. The bounded process-long mechanism oracle
`0xA102` carries confidence `100` in its controlled fixture scope.
Ambiguous aliases, nonlinear control flow, missing terminals, unsupported
owners, and reference-counted owners retain hint `0` and an auditable
`automatic_heap_*_unknown` basis.

The compiler interface is:

```text
--unialloc-auto-heap-lifetime-inference
UNIALLOC_AUTO_HEAP_LIFETIME_INFERENCE=1
```

When both automatic classifiers are enabled, every concrete heap-inference
decision retains precedence, including Unknown. The epoch classifier runs only
when heap inference produced no decision, so an owner-live cleanup or unwind
edge cannot be upgraded into a placement hint by a narrower phase-boundary
proof.

No semantic epoch API is called by the fixture.

## Runtime policies and causal arms

The harness builds one source fixture with inference disabled and one with
inference enabled. The inferred binary supplies both placement treatments so
the THP comparison preserves compiler output and executable identity.

| Arm | Binary | Runtime policy | Physical intent |
|---|---|---|---|
| `default` | inference disabled | `Disabled` (`0`) | Existing allocator |
| `runtime-adaptive` | inference disabled | `AdaptiveRuntimeHugepage` (`5`) | Runtime-only survival learning and adaptive backing |
| `hints-ordinary` | inference enabled | `CompilerInferredOrdinary` (`7`) | Bounded-oracle admission and packing on ordinary pages |
| `hints-thp` | inference enabled | `CompilerInferredHugepage` (`6`) | The same bounded-oracle admission and packing with density-gated THP promotion |

Policies `6` and `7` form the matched physical-backing ablation. Exact-Drop and
other unproven allocations bypass before the arena lock in this controlled
policy. Bounded process-long oracle allocations enter the compiler-directed
extent path directly, without an adaptive site table, allocation trailer,
training sample, or runtime epoch marker.

The `runtime-adaptive` arm supplies the general runtime-only comparator. It
receives the inference-disabled binary and must learn from allocator-observed
survival. The fixture's leaked and forgotten objects remain live through
process exit, so their labels are right-censored at measurement time. This arm
therefore exposes runtime cold-start and missing-deallocation feedback, while
the bounded process-long mechanism oracle can route the same controlled cohort
immediately.

## Marker-free fixture

`evaluation/fixtures/compiler_heap_lifetime_workload.rs` contains five explicit
ownership patterns:

1. a direct local page-sized `Box` followed by exact Drop;
2. an ownership-preserving three-local move chain followed by exact Drop;
3. a page-sized `Box` followed by `mem::forget`;
4. initialized page-sized `Box` allocation followed by `Box::leak`, with the
   leaked pointer retained for the complete process lifetime;
5. a page-sized `Box` with branched Drop/forget terminals that must remain
   Unknown.

The retained boxes are linked in a deterministic pointer cycle. Configurable
dependent pointer-touch passes traverse that cycle after allocation, keeping
the long-lived cohort live and measuring address-translation-sensitive access
separately from allocation cost. `--thp-settle-ms` inserts an equal post-link,
pre-touch wait in every arm when a host campaign needs time for asynchronous
THP collapse. The wait contributes to outer/GNU wall time and stays outside the
reported inner allocation, link, touch, and workload phase times.

The first two patterns must contribute two `exact_drop_path=true` analysis
facts with hint/confidence `0`. Their conservative lifetime-hint basis can
remain `automatic_heap_*_unknown`. The process-long oracle patterns must appear
with these exact bases:

```text
automatic_heap_exact_mem_forget
automatic_heap_exact_box_leak
```

The ambiguous pattern must contribute at least one
`automatic_heap_*_unknown` allocation site. The harness fails closed when any
oracle basis is absent, fewer than two exact-Drop facts are exported, an oracle
hint differs from `0xA102`, oracle confidence differs from `100`, or the
inference-disabled build emits a nonzero oracle hint.

The controlled fixture uses `Box::new` because it remains a solved semantic
allocation callsite in optimized downstream MIR. Optimized
`Vec::with_capacity` currently exposes an alloc-crate `RawVec` allocation whose
downstream owner type is unresolved by this pass; Vec constructors are a
current compiler-coverage boundary rather than evidence in this experiment.

## Measurement protocol

The default campaign uses randomized complete blocks. Each block runs all four
arms once in a seeded random order. Warmups are recorded and excluded from all
summaries. Every measured process receives the same fixture iteration count and
workload seed.

The harness captures:

- outer monotonic wall time;
- GNU Time elapsed, user, system, maximum RSS, and fault counters;
- separate workload allocation, link, dependent-touch, and combined inner
  elapsed times;
- compiler audit site coverage and basis counts;
- runtime routing, bypass, mapping, and density-promotion telemetry;
- bounded process-long oracle requested bytes, allocated slot bytes, and
  observed deallocations;
- `/proc/self/smaps_rollup` RSS, PSS, private dirty memory, `AnonHugePages`, and
  HugeTLB residency before and after the dependent-touch phase while leaked
  objects remain live;
- stdout, stderr, binary, fixture, pass-source, and audit SHA-256 digests;
- a deterministic workload checksum for every process.

Correctness requires one checksum across every arm and repetition. Mapping
failures must remain zero. The ordinary control must also record zero
`MADV_NOHUGEPAGE` advice failures. Every measured randomized block receives its
own physical-backing verdict. A block enters a THP causal comparison only when
all of the following hold for its paired samples:

1. the `hints-thp` arm records kernel acceptance of compiler-directed
   `MADV_HUGEPAGE` advice;
2. `AnonHugePages` is nonzero in the `hints-thp` process;
3. the matched `hints-ordinary` control records zero `AnonHugePages`;
4. the compiler and runtime contract gates pass.

Advice acceptance records mechanism execution. Only nonzero smaps
`AnonHugePages` establishes actual THP backing.
Both inferred arms must record bounded process-long oracle routes and zero
deallocations because the fixture retains every leaked or forgotten allocation
through measurement.

The summary lists included and excluded block IDs with per-block evidence. The
campaign-level THP gate passes only when every measured block qualifies. A
partial sequence such as nonzero backing followed by zero backing remains
incomplete; maxima and counter totals cannot convert it into claim-ready
evidence. THP performance and RSS effects use qualifying paired blocks only,
and `--require-thp` fails the campaign when any measured pair is excluded.

The harness reads host THP configuration and never changes global sysfs state.
`--require-thp` converts unavailable or unrealized backing into a failed run.

## Statistical contract

The following paired effects are reported:

1. `runtime-adaptive` relative to `default`: runtime-only adaptive effect;
2. `hints-ordinary` relative to `default`: compiler-directed layout effect;
3. `hints-thp` relative to `runtime-adaptive`: bounded-oracle compiler method
   relative to the general runtime comparator;
4. `hints-thp` relative to `hints-ordinary`: physical THP increment;
5. `hints-thp` relative to `default`: complete feature effect.

For each comparison, the unit of pairing is one randomized block. The harness
reports the median paired reciprocal effect
`(control / treatment - 1) * 100`; seeded paired bootstrap resampling supplies
the 95% interval. Positive values mean the treatment reduced a
lower-is-better metric. Presentation tables additionally report the
conventional reduction between arm medians,
`(control_median - treatment_median) / control_median * 100`. These two
statistics carry explicit labels because they have different scales. Outer
wall time, allocation time, dependent-touch time, combined inner workload
time, and maximum RSS are kept separate. The compiler-layout arm identifies
routing and packing cost; the matched ordinary-to-THP arm identifies the
physical-backing increment for the same exact hints and geometry.

## Reproduction

```bash
uv run python evaluation/scripts/compiler_heap_lifetime_experiment.py \
  --run-id compiler-heap-lifetime-<date> \
  --repeats 9 \
  --warmups 1 \
  --iterations 4096 \
  --touch-passes 64 \
  --thp-settle-ms 0 \
  --bootstrap-resamples 10000
```

Add `--cpu <cpu> --numa-node <node>` for a pinned host campaign and
`--require-thp` for a claim-bearing physical-backing run.

## External comparator gate

This four-arm harness contains internal causal controls only. The fail-closed
helper `evaluation/scripts/google_tcmalloc_bazel_probe.py` pins
`github.com/google/tcmalloc` commit
`12f255231938d30493186b0a037feedd70f5a1c1` (Bazel Central Registry release
`0.0.0-20250927-12f2552`). It builds target
`//:cross_allocator_large_page_workload_google_tcmalloc` with the official
link-time malloc target `@com_google_tcmalloc//tcmalloc:tcmalloc`, and records
the HPAA source hashes, symbol proof, revision, and build options. This is the
tested 2025 compatibility pin, with Bazel 8.4.2 required exactly. It is
deliberately separate from upstream-current tracking; current upstream commit
`5cbf010` has unresolved Bzlmod dependency conflicts with the locally tested
Bazel 8/9 toolchains. The neutral helper build is **VERIFIED**: the linked
binary contains the expected Temeraire/HPAA and strong `malloc` symbols, and
the matched system-control binary uses the same source and compile options.
This verification establishes the comparator identity and build contract.

Repository real-application runners use the stronger authenticated DSO
contract documented in [`google-tcmalloc-baseline.md`](google-tcmalloc-baseline.md).
The validated artifact is uniquely named `libunialloc_google_tcmalloc.so`, has
SHA-256 `5f99dcf644a7e1e138439fed5d42216c5313ad28e2557f936a250643e0f42081`,
and must pass revision, HPAA stats, active malloc-provider, mapped-library, and
per-target identity-marker checks. An unavailable or mismatched modern arm is
reported **BLOCKED**.
gperftools 2.18.1 is labeled `gperftools-legacy`, remains a historical control,
and never serves as a fallback or source of a modern TCMalloc/Temeraire claim.
The preserved
[`Google TCMalloc provenance`](evidence/cross-allocator-modern-tcmalloc-20260715/google-tcmalloc-provenance.json)
and
[`Bazel action graph`](evidence/cross-allocator-modern-tcmalloc-20260715/google-tcmalloc-bazel-actions.textproto)
make this identity auditable.

The completed five-pair
[`cross-allocator summary`](evidence/cross-allocator-modern-tcmalloc-20260715/summary.json)
records the modern Google TCMalloc identity as ready. Its Temeraire/HPAA arm
realized physical hugepage backing in 0/5 samples, so both the
hugepage-mechanism and HPAA performance-causality gates remain false. Within
matched allocator-specific THP toggles, the measured effects were:

| Allocator toggle | Paired touch reduction | Effective-resident increase | Physical backing |
|---|---:|---:|---:|
| UniAlloc selective lifetime THP | 33.8% (95% CI 32.9% to 34.7%) | 1.50% (95% CI 1.44% to 1.56%) | 66 MiB median THP |
| mimalloc THP | 38.4% (95% CI 38.1% to 38.7%) | 5.29% (95% CI 5.287% to 5.293%) | 132 MiB median THP |
| jemalloc THP | 37.3% (95% CI 37.0% to 37.6%) | 2.88% (95% CI 2.85% to 2.90%) | 126 MiB median THP |
| snmalloc OS eligibility | -0.37% (95% CI -0.71% to -0.08%) | 0.001% (95% CI -0.018% to 0.020%) | 0 MiB |

These are within-allocator placement-potential results from a semantic probe.
Real-program automatic-classifier gain remains unmeasured, and cross-family raw
timing ranks remain outside the evidence contract. Presentation-ready figures:

- incremental toggle effects
  ([SVG](figures/cross-allocator-modern-tcmalloc-20260715/incremental-effect.svg),
  [PNG](figures/cross-allocator-modern-tcmalloc-20260715/incremental-effect.png));
- actual physical backing
  ([SVG](figures/cross-allocator-modern-tcmalloc-20260715/actual-backing.svg),
  [PNG](figures/cross-allocator-modern-tcmalloc-20260715/actual-backing.png));
- endpoint measurements with cross-family raw timing ranking withheld
  ([SVG](figures/cross-allocator-modern-tcmalloc-20260715/endpoint-frontier.svg),
  [PNG](figures/cross-allocator-modern-tcmalloc-20260715/endpoint-frontier.png)).

Focused non-THP unit validation:

```bash
uv run python evaluation/scripts/test_compiler_heap_lifetime_experiment.py
```

The output directory contains:

```text
manifest.json
samples.jsonl
summary.json
compiler-audits/{baseline,inferred}/
compiler-logs/{baseline,inferred}/
runs/<block>/<arm>/{stdout.bin,stderr.bin,gnu-time.txt}
```

## Controlled-oracle results

The campaign used five measured randomized pairs. The inferred compiler audit
contained nine candidate sites: two bounded `0xA102` mechanism-oracle sites and
seven Unknown sites. Two of the Unknown sites exported exact-Drop eventual
release facts. The bounded-oracle routing coverage was 22.2%. Workload
checksums matched across every arm, and all arms recorded zero mapping
failures.

### Compiler coverage

| Metric | Result |
|---|---:|
| Audited allocation sites | 9 |
| Exact-Drop Unknown analysis-fact sites | 2 |
| Bounded process-long mechanism-oracle sites | 2 |
| Unknown fallback sites | 7 |
| Oracle-routed site coverage | 22.2% |

### Performance and memory

The conventional endpoint comparison gives the most direct slide-ready
interpretation:

| Metric | Default median | Hints-ordinary median | Conventional reduction |
|---|---:|---:|---:|
| Outer wall time | 104.355 ms | 94.245 ms | 9.7% |
| Allocation time | 42.080 ms | 33.454 ms | 20.5% |
| Dependent-touch time | 3.163 ms | 1.388 ms | 56.1% |
| Combined inner workload | 45.283 ms | 34.880 ms | 23.0% |
| Maximum RSS | 46,080 KiB | 29,696 KiB | 35.6% |

The experiment runner's paired statistic uses the reciprocal-effect formula
defined above:

| Comparison/statistic | Wall time | Allocation | Touch | Inner workload | Maximum RSS |
|---|---:|---:|---:|---:|---:|
| Runtime adaptive vs default, paired reciprocal effect | -0.9% | -2.1% | -0.2% | -1.9% | 0.0% |
| Hints ordinary vs default, paired reciprocal effect | 10.7% | 26.2% | 127.4% | 28.7% | 55.2% |
| Compiler hugepage vs runtime adaptive | unavailable: 0 backed pairs | unavailable | unavailable | unavailable | unavailable |
| THP increment vs matched hints | unavailable: 0 backed pairs | unavailable | unavailable | unavailable | unavailable |
| Full feature vs default | unavailable: 0 backed pairs | unavailable | unavailable | unavailable | unavailable |

### Physical backing

| Metric | Result |
|---|---:|
| Ordinary-control maximum `AnonHugePages` | 0 KiB |
| Ordinary-control `MADV_NOHUGEPAGE` failures | 0 |
| THP-arm pre-touch maximum `AnonHugePages` | 0 KiB |
| THP-arm maximum `AnonHugePages` | 0 KiB |
| `MADV_HUGEPAGE` acceptances | 80 total |
| Claim-ready THP gate | false; 0/5 backed pairs |

## Claim boundary

The fixture establishes compiler analysis export for four bounded ownership
patterns, runtime transport for the two process-long oracle patterns, and one
conservative fallback. Exact Drop remains Unknown: it establishes an eventual
scoped owner release path and carries no Short-duration claim. `mem::forget`
and `Box::leak` provide a controlled process-long mechanism oracle and do not
solve real-program Long classification. General Rust borrow lifetimes,
reference-counted last-owner behavior, unsafe ownership recovery, FFI, and
application-wide coverage remain outside this experiment. A broader paper
claim requires held-out real software, byte-weighted compiler coverage,
multithreaded allocator cost, and long-running THP split/reclaim evidence.
The measured ordinary-page improvements demonstrate bounded-oracle routing and
placement potential. Automatic-classifier benefit in real software remains a
held-out evaluation target.
