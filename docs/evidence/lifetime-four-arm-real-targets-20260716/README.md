# Compiler-directed lifetime hints: four-arm target evidence

## Result

Compiler hints are the fastest arm in all five directional target screens. The measured operation gains range from **+1.59% to +15.65%**, with a five-target median of **+7.07%**. Broad THP placement produced physical huge pages in every target while slowing four of five targets and increasing peak RSS in all five; this control separates the lifetime signal from THP alone.

Peak RSS improves on Roaring and Convex, stays within 0.5% of default on Lume and Bedrock, and costs 4.54% on DataFusion. The defensible primary claim is **performance from compiler-directed lifetime placement**; the memory result is workload-dependent.

## Four-arm contract

1. **Default** — UniAlloc policy 0, zero lifetime routing, zero explicit huge-page advice.
2. **THP first-fit** — the same policy-0/default placement plus mmap-wide `MADV_HUGEPAGE` eligibility for private anonymous mappings at least 2 MiB; zero lifetime signal and zero lifetime routes. “First-fit” is the requested chart shorthand for allocator-default placement; a separate internal fit-algorithm ablation remains outside this experiment.
3. **Runtime profile** — policy 11 with compiler hints masked to Unknown; cold online observation, classification, and adaptive THP placement.
4. **Compiler hint (ours)** — policy 9 with a compiler-injected semantic-scope Long hint around the allocation scope and lifetime-aware 2 MiB cohorts; zero online lifetime learning.

## Measured effects

Positive operation values mean faster. Positive RSS values mean lower peak RSS. Default is 0.00% in both panels.

| Target | THP first-fit operation | Runtime operation | Ours operation | THP first-fit RSS saving | Runtime RSS saving | Ours RSS saving |
|---|---:|---:|---:|---:|---:|---:|
| DataFusion | +0.36% | -3.34% | **+2.68%** | -4.17% | -0.41% | -4.54% |
| Lume | -4.33% | -2.52% | **+9.71%** | -18.78% | -5.64% | -0.45% |
| Roaring | -2.74% | -0.98% | **+1.59%** | -16.55% | -8.55% | **+0.11%** |
| Convex | -2.64% | +2.69% | **+7.07%** | -15.00% | +4.74% | **+15.98%** |
| Bedrock | -1.68% | -3.88% | **+15.65%** | -39.39% | -0.39% | -0.12% |

Physical `AnonHugePages` medians (MiB):

| Target | Default | THP first-fit | Runtime profile | Ours |
|---|---:|---:|---:|---:|
| DataFusion | 0 | 5 | 0 | **16** |
| Lume | 0 | 4 | 8 | **16** |
| Roaring | 0 | 4 | 10 | **16** |
| Convex | 0 | 4 | 10 | **16** |
| Bedrock | 0 | 5 | 0 | **8** |

## Mechanism evidence

- The compiler-hint arm obtains 8–16 MiB of physical huge-page backing in all five targets and wins the operation screen in all five.
- The broad-THP arm obtains physical huge pages in all five targets, yet its operation and RSS outcomes are consistently weaker. THP availability alone does not explain the compiler-hint result.
- The runtime profiler reaches useful Long placement on Lume, Roaring, and Convex. DataFusion ends its measured query windows with 0 MiB AHP; Bedrock samples 8 of 1,024 objects, records only censored outcomes, and produces zero Long routes.
- Compiler hints route the Long cohort immediately, avoiding cold observation and classification work. Runtime placement remains a useful fallback when compiler identity is unavailable.

## Measurement boundary

- One forward sequence and one reverse sequence per target; each arm runs in a fresh process.
- Zero warm-up iterations and fixed work with matching correctness digests across all arms.
- One binary per target across all four arms. Policy selection occurs at runtime.
- Each plotted value is the median of the forward and reverse effects, normalized against the default from the same sequence.
- These are macro/fixed-work screens. Criterion is not used.
- Directional evidence has no confidence interval. Independent repetitions remain required for a publication-grade estimate.
- DataFusion is an engine workload; Lume, Roaring, Convex, and Bedrock are fixed-work target or target-derived harnesses that preserve the identified upstream allocation site and operation.
- The current compiler audits report applied semantic-scope push/pop rewrites together with `actual_allocator_call_replacement=false`, `claim_grade=false`, and `complete_for_claim=false`. This package limits its claim to the measured compiler-hint path; a claim-grade direct allocator-call rewrite remains outside this evidence.

## Lock-free reference scope

The branch `exp/lifetime-short-prelock-bypass-20260716` was audited read-only. The compiler-hint placement mechanism and the naive runtime-profile arm remain unchanged. No lock-free code was integrated into this evidence. `lockfree-readonly-scope.json` records the scope and source-integrity hashes.

## Artifacts

- Canonical normalized data: `aggregate.json`
- Plot-ready CSV: `chart-data.csv`
- Raw per-target result summaries: `{datafusion,lume,roaring,convex,bedrock}-result.json`
- DataFusion/Lume raw-run manifest: `datafusion-lume-final-summary.json`
- Compact compiler rewrite boundary with raw-audit hashes: `compiler-audit-boundary.json`
- Figure: `../../figures/lifetime-four-arm-real-targets-20260716/four-arm-performance-memory.{svg,png}`
- Integrity manifest: `SHA256SUMS`
