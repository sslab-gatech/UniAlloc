# Compiler-lifetime branch comparison and absorption evidence

## Decision

Keep `lifetime-aware` as the integration branch. The experiment branch's
general Rust-prior `escape_long` rule produced four Long predictions on the
shared Oxipng ground truth and all four were false positives. All four were
8-byte allocations; the production compiler-directed selector's 4 KiB lower
size gate would have abstained before placement. This snapshot therefore
demonstrates weak precision for the unrestricted prior, while fresh held-out
medium-size truth remains necessary to judge the placement rule itself. The
integration branch abstained on all twelve executed sites in that snapshot, so
neither current classifier recovered any of its 257 observed Long allocations.

Four changes were absorbed into `lifetime-aware`:

1. `4fb9c67` ports the checked constant-layout evaluator. It accepts target-`usize`
   `Add`/`Mul` chains and rejects mutation exposure, unsupported operations,
   overflow, and excessive dependency depth.
2. `053f16f` synchronizes the evaluator contract with the existing borrowed-Vec
   reserve Long basis.
3. `a5a1c7a` keeps Long runtime transport while removing recovery-backed
   non-Long allocation scopes. Exact local allocation/Drop pairs remain intact.
4. `24f75c5` admits synchronous collapse only for a full homogeneous compiler
   identity and callsite, and pins the mapping with a low-footprint
   `collapse_pending` state while the arena lock is released.

The experiment branch's `escape_long` rule was deliberately excluded.

Fresh A/B screens on DataFusion, Lume, Roaring, Convex, and Bedrock also reject
a wholesale merge of the experiment runtime. Lume and Roaring were tied,
DataFusion's final THP arms were tied, and `lifetime-aware` was 12.08% faster on
Bedrock under policy 9. Convex exposed one useful safety distinction but its
policy-9 timing was too noisy for a performance conclusion. The retained
absorption is therefore limited to narrow runtime correctness guards. The
experiment branch's classifier and transport policy remain excluded as a unit.

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

## Fresh current-head multi-target A/B

Lume, Roaring, Convex, and Bedrock compare experiment `98e4381` with the
post-integration `lifetime-aware` head `cbc6877`. Every cell is a fresh process,
uses fixed work, has zero warm-up iterations, and is paired in A/B and B/A
order. The table reports B relative to A under policy 9.

| Target | B speed | B peak-RSS saving | Physical gate | Decision |
|---|---:|---:|---|---|
| DataFusion | +0.13% absolute THP median | effectively tied | B passed both orders; A passed one | tied |
| Lume | -0.102% | -0.158% | both 16 MiB AHP, 8/8 collapse | tied |
| Roaring | +0.042% | +0.260% | both 16 MiB AHP, 8/8 collapse | tied |
| Bedrock | +12.08% | +0.078% | both 8 MiB AHP, 4/4 collapse | keep B |
| Convex | direction-conflicted | effectively tied | A 0 AHP; B 16 MiB AHP | performance inconclusive |

Bedrock's policy-10 comparison also favored B by 10.58%, so most of B's 12.08%
policy-9 advantage is a whole-branch/compiler-runtime difference rather than
incremental THP credit. Within each Bedrock branch, policy 9 beat policy 10 by
22.25% for A and 23.54% for B. Both compilers selected the same unique 8192-byte
Long site and both policy-9 arms achieved identical backing.

The experiment's aggressive transport elision retained one Bedrock runtime
scope while B retained forty; the experiment still lost. Lume and Roaring were
also tied despite the same transport contrast. These screens provide no real-
software performance reason to absorb broader scope elision.

Convex uses a 6000-byte request rounded to a 6528-byte slot. A requires exactly
2 MiB of live slot bytes and suppresses collapse even though a full 32-region
cohort occupies 2,088,960 bytes (99.61% density). B collapses eight extents and
observes 16 MiB AHP. The exact-100% byte-density gate is therefore excluded;
density remains an explicit policy threshold rather than an equality test.

The durable compact artifacts are `current-head-lume-roaring.json`,
`current-head-bedrock.json`, and `current-head-convex-noisy.json`. These are
directional screens with one A/B and one B/A pair and no confidence interval.

## Runtime absorption ablation

The runtime guard was rebuilt from one fixed source path, runner path, Cargo
target, RUSTFLAGS set, and compiler-pass binary. Each variant was clean-built
and compared with the rebuilt `cbc6877` baseline in fresh-process Bedrock
policy-9 A/B and B/A order.

| Variant | Median speed vs baseline | Binary-size delta | Decision |
|---|---:|---:|---|
| homogeneous identity/callsite gate only | +0.046% | -64 B | keep |
| generation `CollapseTicket`, cold/noinline | -17.199% | +34,856 B | reject |
| reuse `ThpAdvisedUnverified` as pending state | +0.246% | small | reject: state aliases advice-only promotion |
| separate `collapse_pending: bool` plus gate | +0.220% | +1,312 B | keep |

All sixteen measured processes produced the same fixed digest, routed 1024
allocations, observed 8192 KiB AHP, completed 4/4 collapses, and reported zero
collapse errors. The accepted variant's two directions were -1.419% and
+1.859%, establishing performance parity at this screening resolution; peak
RSS was also flat at -0.016% median saving.

The generation ticket's regression remained after `cold` and `inline(never)`
annotations, so `24f75c5` uses one explicit boolean pending state instead. The
pending bit distinguishes synchronous collapse from advice-only
`ThpAdvisedUnverified`; the advice-only regression test confirms the latter
keeps `collapse_pending=false` and releases its mapping after deallocation.
The exact-100%-density equality remains excluded. A separate Convex mechanism
cell still reached 16 MiB AHP and 8/8 collapses for 6000-byte requests rounded
to 6528-byte slots.

`runtime-absorption-ablation.json` contains the matched build records, source
and patch hashes, directional cells, strict gates, and advice-only regression
proof. It remains a one-pair-per-direction screen without a confidence
interval.

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
- The final collapse guard passed 870/870 lifetime-feature library tests and
  33/33 lifetime allocator integration tests.
- Clippy completed successfully with the repository's existing warnings; the
  final changed files introduced no new diagnostic class.

Large raw builds and per-process samples remain under `/tmp`; this package keeps
compact, hashable summaries. A claim-grade performance or RSS estimate still
requires more AB/BA pairs and a confidence interval.
