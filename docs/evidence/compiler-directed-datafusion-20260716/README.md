# Compiler-directed lifetime placement: DataFusion 54.0.0

## Defensible result

On the pinned Apache DataFusion 54.0.0 `ApproxDistinct` path, the compiler emitted `ReturnLong` for exact 16 KiB `NumericHLLAccumulator` allocations. Policy 9 used those hints to construct full-density 2 MiB THP extents and reuse them by exact geometry, allocation identity, and callsite.

Against allocator-default policy 0, two opposite-order pinned-core fixed-work pairs improved query time by **5.644%** and **5.174%** (median **5.409%**). Median peak-RSS cost was **4.259%**. Both arms used the same binary and identical work/result digests.

Against a same-binary global-THP control that ignored every compiler hint and applied `MADV_HUGEPAGE` to every private anonymous mapping of at least 2 MiB, the compiler-directed mechanism was faster in both opposite-order pairs: **0.627%** and **3.501%** (median **2.064%**). It also reduced peak RSS by **1.107%** and **0.509%** (median **0.808%**). Every query-window sample in all four arms had physical THP backing; global no-hint arms reached 4–6 MiB `AnonHugePages`, while compiler-directed arms held 16 MiB.

Across the two THP arms:

- all **210/210** query-window samples reported **16 MiB `AnonHugePages`**;
- all **16/16** collapse attempts succeeded, with zero errors;
- each arm routed exactly **917,504** compiler-hinted allocations and recorded **7,160** promoted-extent reuse hits;
- runtime lifetime learning, live trailers, runtime validation, site rows, and diagnostic telemetry were zero.

A separate 128-group matched-placement pair reduced peak RSS by **1.491%**, anonymous memory by **2.424%**, and PSS by **1.511%**, with a **2.175%** time cost.

## Measurement boundary

These are directional macro results from two opposite-order pairs, with no confidence interval. `performance_claim_eligible` remains false. The global no-hint control and compiler-directed arm deliberately use different placement mechanisms and THP coverage: global mmap advice versus selective full-density packing, synchronous collapse, and exact-key reuse. The comparison supports the complete compiler-directed selective-THP mechanism over naive global THP; a same-arena size-only policy is still required to isolate hint selection alone. Placement still uses an arena lock; the zero-overhead statement applies to runtime lifetime classification, trailers, and validation. A clean end-to-end rebuild remains required because the captured allocator compatibility snapshot used a hashed runtime overlay.

## Files

- `manifest.json`: complete claims, provenance, hashes, matched ablations, and boundaries.
- `default-vs-thp-two-pairs.json`: compact primary AB/BA synthesis.
- `global-nohint-thp-vs-hint-thp.json`: same-binary global-THP/no-hint versus compiler-directed-THP AB/BA result.
- `global-thp-mmap-shim.c`: auditable preload control that globally requests THP without compiler hints.
- `verification.txt`: tests and artifact-integrity checks.
