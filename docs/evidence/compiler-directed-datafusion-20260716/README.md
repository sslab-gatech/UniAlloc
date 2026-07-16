# Compiler-directed lifetime placement: DataFusion 54.0.0

## Defensible result

On the pinned Apache DataFusion 54.0.0 `ApproxDistinct` path, the compiler emitted `ReturnLong` for exact 16 KiB `NumericHLLAccumulator` allocations. Policy 9 used those hints to construct full-density 2 MiB THP extents and reuse them by exact geometry, allocation identity, and callsite.

Against allocator-default policy 0, two opposite-order pinned-core fixed-work pairs improved query time by **5.644%** and **5.174%** (median **5.409%**). Median peak-RSS cost was **4.259%**. Both arms used the same binary and identical work/result digests.

Across the two THP arms:

- all **210/210** query-window samples reported **16 MiB `AnonHugePages`**;
- all **16/16** collapse attempts succeeded, with zero errors;
- each arm routed exactly **917,504** compiler-hinted allocations and recorded **7,160** promoted-extent reuse hits;
- runtime lifetime learning, live trailers, runtime validation, site rows, and diagnostic telemetry were zero.

A separate 128-group matched-placement pair reduced peak RSS by **1.491%**, anonymous memory by **2.424%**, and PSS by **1.511%**, with a **2.175%** time cost.

## Measurement boundary

These are directional macro results from two opposite-order pairs, with no confidence interval. `performance_claim_eligible` remains false. Placement still uses an arena lock; the zero-overhead statement applies to runtime lifetime classification, trailers, and validation. A clean end-to-end rebuild remains required because the captured allocator compatibility snapshot used a hashed runtime overlay.

## Files

- `manifest.json`: complete claims, provenance, hashes, matched ablations, and boundaries.
- `default-vs-thp-two-pairs.json`: compact primary AB/BA synthesis.
- `verification.txt`: tests and artifact-integrity checks.
