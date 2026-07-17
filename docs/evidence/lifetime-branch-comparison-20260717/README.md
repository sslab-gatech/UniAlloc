# Compiler-lifetime branch comparison and absorption evidence

## Decision

Keep `lifetime-aware` as the integration branch. The experiment branch's
`escape_long` classifier rule produced four Long predictions on the shared
Oxipng ground truth and all four were false positives. The integration branch
abstained on all twelve executed sites in that snapshot, so neither current
classifier recovered any of its 257 observed Long allocations.

Three changes were absorbed into `lifetime-aware`:

1. `4fb9c67` ports the checked constant-layout evaluator. It accepts target-`usize`
   `Add`/`Mul` chains and rejects mutation exposure, unsupported operations,
   overflow, and excessive dependency depth.
2. `053f16f` synchronizes the evaluator contract with the existing borrowed-Vec
   reserve Long basis.
3. `a5a1c7a` keeps Long runtime transport while removing recovery-backed
   non-Long allocation scopes. Exact local allocation/Drop pairs remain intact.

The experiment branch's `escape_long` rule was deliberately excluded.

## Same-snapshot classification

Both committed pre-integration heads were compiled against the same Oxipng
10.1.1 source and joined to one ABI-v2 runtime snapshot: 293 allocations across
12 executed sites, with 257 Long and 36 Short outcomes.

| Head | Decisive predictions | Correct | False positive | Non-Unknown coverage | Long recall including abstentions |
|---|---:|---:|---:|---:|---:|
| experiment `98e4381` | 4 Long | 0 | 4 | 1.365% | 0% |
| lifetime-aware `5477873` | 0 | 0 | 0 | 0% | 0% |

The offline adapter fails closed on any semantic-callsite drift and only maps
current compiler identities back into the retained ABI-v2 identity domain.
This establishes a same-allocation classifier comparison. It does not establish
fresh executable identity parity.

The synthetic MIR fixture passes 11/11 tests in both branches, with three
correct decisive labels and five abstentions. Those assertions encode the
expected labels and serve as regression evidence rather than independent
accuracy evidence. Retained historical data show strong SWC byte metrics
(96.31% Long precision, 93.16% Long recall, 24.25% decisive-byte coverage) and
large cross-program variation; they are not current-head accuracy measurements.

## Current-head DataFusion screen

The fixed workload is DataFusion 54.0.0 ApproxDistinct, group count 1024,
256 query iterations, two partitions, CPU set 120-123, fresh processes, and
zero warm-up iterations. Policy 9 is hinted THP and policy 10 is the hinted
ordinary control.

| Head | THP speedup over ordinary | Peak-RSS change | Runtime allocation scopes | Gate |
|---|---:|---:|---:|---|
| experiment `98e4381` | +2.158% | +0.985% | 19 | one of two orders failed one collapse |
| lifetime-aware `5477873` | +0.286% | -0.467% | 2,290 | both orders passed |

Absolute THP medians were 22.9673 s and 22.9372 s respectively, a 0.13%
difference. The experiment head's larger paired speedup came from its slower
ordinary control, so the two final THP arms were effectively tied. This is one
direction-balanced smoke with no confidence interval.

## Orthogonal method ablations

The checked-layout matrix had ten cases. Before the port, the experiment head
failed closed in all ten; `lifetime-aware` was safe in seven, emitted stale
exact sizes after mutable/raw mutation in two, and triggered a rustc ICE on an
unsupported shift in one. Commit `4fb9c67` closes all three cases without adding
runtime work.

The final safe selective-transport source retained the same 19 DataFusion Long
sites and reduced applied allocation scopes from 2,290 to 29 (98.73%). Across
one AB/BA THP screen its direct cross-binary effect was -0.095% and -0.338% on
query speed (median -0.217%, effectively flat) and +1.791% and +0.609% peak-RSS
saving (median +1.200%). One forward collapse gate failed; the reverse gate
passed. The 1.20% memory direction is supporting evidence, not a claim-grade
estimate.

A current-source six-pair synthetic scope-dominated probe measured 25.12%
lower operation time. Five million tiny allocations and a sequentially
consistent scope stub make this a mechanism upper bound. DataFusion gives the
real-software boundary: scope elision receives no independent performance
credit. Within each DataFusion binary, policy 9 still beat policy 10 by 1.44%
and 1.74%, supporting Long placement plus THP as the performance mechanism.

## Verification

- 19/19 marker-free lifetime-pass tests passed after the final transport and
  audit changes.
- 61/61 lifetime-prior campaign tests passed after the evaluator-contract fix.
- Direct-local runtime probes closed typed allocation/deallocation at 1/1 and
  recovery-backed fallback allocation/deallocation at 3/3.
- All eight selective-transport DataFusion processes produced the same fixed
  output digest and routed 786,432 hinted allocations.

Large raw builds and per-process samples remain under `/tmp`; this package keeps
compact, hashable summaries. A claim-grade performance or RSS estimate still
requires more AB/BA pairs and a confidence interval.
