# Compiler-directed lifetime placement: five primary Rust targets

## Verdict

Five distinct real Rust projects produced the required directional result: the
compiler identified an exact 6–16 KiB Long allocation site, runtime placement
formed physically backed THP cohorts, fixed work and digests matched, and both
AB/BA orders improved operation latency.

Tantivy-SSTable provides an additional compiler-coverage and performance result:
both orders improved, with a smaller median effect and a material RSS cost. It is
reported separately from the five primary targets.

All percentages below use the consistent latency-reduction definition
`(baseline_time - hint_time) / baseline_time`. Each row is one AB and one BA
screen from fresh processes with zero benchmark warm-up iterations.

| Project and production path | Exact Long object | Baseline | AB / BA latency reduction | Median | Median peak RSS change | Physical evidence |
| --- | ---: | --- | ---: | ---: | ---: | --- |
| Apache DataFusion 54.0.0 `ApproxDistinct` | 16 KiB | allocator default | +5.64% / +5.17% | **+5.41%** | +4.26% | 917,504 routes/arm, 8/8 collapse, 16 MiB AHP |
| Roaring 0.11.3 serialized intersection | 8 KiB | allocator default | +2.01% / +1.64% | **+1.83%** | +0.08% | 2,048 routes/arm, 8/8 collapse, 16 MiB AHP |
| Lume BM25 index load / `MiniRoaring` | 8 KiB | allocator default | +15.19% / +9.06% | **+12.13%** | +0.27% | 2,048 routes/arm, 8/8 collapse, 16 MiB AHP |
| Convex 0.13.0 `HolidayBitmap` | 6,000 B | same hinted arena, ordinary pages | +3.23% / +6.50% | **+4.86%** | -0.03% | 2,731 routes/arm, 8/8 collapse, 16 MiB AHP |
| Bedrock-RS `GreedyArray::from_disk` | 8 KiB | allocator default | +13.47% / +16.54% | **+15.00%** | +0.87% | 1,024 routes/arm, 4/4 collapse, 8 MiB AHP |

Bedrock's source artifact also reports a throughput-equivalent median speedup
of 17.69%; the table uses its 15.00% median latency reduction so every row has
the same metric definition.

## Additional directional target: Tantivy-SSTable

The compiler initially could not join Tantivy's `DeltaWriter::new` allocation
because the 8,000-byte `Vec<u8>` capacity appeared in MIR as a checked
`BLOCK_LEN * 2` expression. A fail-closed constant evaluator now follows only
dominating, single-assignment integer Add/Mul chains and rejects parameters,
reassignments, loop values, recursive definitions, unsupported operations, and
overflow.

The clean upstream `tantivy-sstable 0.7.0` streaming encoder then routed 2,048
objects per hint arm, collapsed 8/8 extents, and retained 16 MiB AHP. Fixed work
was 2,048 dictionaries × 60,000 sorted terms with a streaming digest writer.
AB/BA build latency improved **0.62%** and **1.54%**, median **1.08%**. Peak RSS
increased **8.21%**, approximately 8.2 MiB. This row is useful compiler-coverage
and directional performance evidence; the five-target table remains the primary
generality claim.

## Naive runtime profile versus compiler hint

The same DataFusion binary compared policy 11 cold online profiling with policy
9 direct compiler-hint placement. Compiler hints reduced query latency by
**8.73%** and **5.72%** across the two orders, median **7.23%**, with a median
**4.05%** peak-RSS cost.

The online profiler learned 192 sites and classified 16,551 allocations Long,
yet every conservative query-window sample remained at 0 KiB AHP. The compiler
arm retained 16 MiB AHP in every query sample. Online classification also
processed 3,742,201 eligible allocations after arena locking, a 4.079x lower-
bound lock-traffic ratio relative to policy 9's direct routes.

## Causal controls

Three controls separate placement quality from general THP availability:

- DataFusion compiler-directed THP beat same-binary global no-hint THP in both
  orders, median **+2.06%** latency and **-0.81%** peak RSS. Every arm had
  physical THP; global placement reached 4–6 MiB AHP and compiler placement
  retained 16 MiB.
- Lume used identical compiler hints and identical eight extents in policy 10
  ordinary placement and policy 9 THP placement. THP improved both orders by
  **1.28%** and **1.88%** (median **1.58%**) and reduced peak RSS in both orders
  (median **0.54%**).
- Convex uses the same hinted ordinary-versus-THP comparison and improves both
  orders by a median **4.86%**.

## Target-selection boundary

The positive pattern is consistent across the five primary projects:

1. The compiler proves an exact medium-size Long allocation and emits a complete
   runtime join identity.
2. The workload creates at least 8–16 MiB of simultaneously live hinted bytes,
   filling several 2 MiB extents densely.
3. The measured phase repeatedly accesses the full resident working set with
   random or cross-object probes, creating meaningful TLB pressure.
4. The resident phase is long enough to amortize synchronous collapse.

The negative controls sharpen this boundary. Sled formed 4 MiB AHP inside an
approximately 848 MiB process and measured -0.27%. Keyhog filled one 2 MiB page
but its lockstep same-offset probes exposed cache-set aliasing and measured
-9.13%. Image/TGA formed 8 MiB AHP but consumed palettes sequentially and
measured -2.77%. Parquet reliably routed and collapsed its 4 KiB decoder site,
while opposite orders measured +7.97% and -0.25%, so it remains mechanism
evidence with an order-noise warning.

## Claim boundary

These are directional discovery results from one AB and one BA order on one
host. They have matched correctness and physical-backing evidence. Confidence
intervals remain future work. They support the qualifier claim that
compiler-derived lifetime hints can produce real performance gains on multiple
Rust programs when live-byte density and reuse shape pass the stated gates.
Publication-grade effect sizes require isolated repetitions and confidence
intervals.

## Files

- `aggregate.json`: machine-readable five-primary-target results, the additional
  Tantivy result, runtime-profile comparison, causal controls, provenance
  hashes, and claim boundaries.
- `negative-controls.json`: mechanism-positive negatives and compiler coverage
  blockers.
- `*-result.json`: immutable raw result records copied from the ignored
  experiment workspace, including per-arm correctness, runtime, RSS, and THP
  evidence.
- `verification.txt`: local validation commands and results.
- `SHA256SUMS`: integrity manifest for this evidence directory.
