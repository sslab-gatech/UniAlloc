# Lifetime-Guided THP Allocation: Evidence Package

## Defended claims

This package supports six bounded claims:

1. Bounded empty-extent retention removes repeated mapping work from measured
   lifetime-routed paths.
2. An exact Rust ownership prior can identify a resident allocation site before
   its objects are freed, and runtime pressure survival can validate that site
   while the process remains active.
3. Physical THP backing can improve a matched lifetime-routed workload when
   backing is proven in every measured pair.
4. Broad compiler Long admission can fail badly on real software, so exact site
   authentication and runtime validation remain part of admission.
5. Heterogeneous filling can reduce extent count and RSS, while an O(1) lookup
   remains necessary for competitive allocation speed.
6. A minute-scale dependency-internal workload exposes substantial Long and
   Short cohorts, while its density gate can still reject physical THP.

The current evidence supports a presentation claim about the mechanism, exact
classification, and a matched physical-backing benefit. It does not support a
general end-to-end speedup over the default allocator or a generalized database
performance claim.

## Decision pipeline

```text
Rust ownership prior
    -> exact (callsite, type_id, module_id, size, align) join
    -> live-object pressure-survival validation
    -> runtime-confirmed Long-byte density gate
    -> physical THP request and per-process backing proof
```

The allocator follows the full-or-empty objective of a hugepage-aware filler:
dense Long extents preserve useful backing, draining cohorts expose complete
extents for release, and sparse candidates remain ordinary-backed. Rust
ownership information supplies a cold-start placement prior. Runtime outcomes
and occupancy provide admission authority.

The pressure clock labels an object Long after it survives 8 MiB of subsequent
eligible requested allocation traffic. The 8 MiB value measures age, not object
size. This is a pressure-relative residency label; access hotness remains a
separate signal. Local `Drop` proves eventual release; owner-live cleanup or
unwind forces compiler abstention.

## Evidence at a glance

| Experiment | Main result | Evidence boundary |
| --- | --- | --- |
| Syn fixed work | mappings 3,869 -> 13 in both ordinary and THP arms | mapping-churn mechanism; one timing sample excluded |
| Tantivy resident index | runtime byte precision 99.826%, recall 98.350% | diagnostic ground truth; broad static admission failed |
| DataFusion resident tables | generated-driver prior site matched 1/1; 2,048/2,048 target outcomes Long | exact classification mechanism inside a real query process; three timing pairs are preliminary |
| DataFusion natural dependency run | 9 dependency crates, 2,304 compiler sites, 307 runtime sites, 37.94M routed allocations | minute-scale ground truth; selective arm realized zero physical THP |
| SWC N=20 matched backing | THP pipeline time improved 4.076%, 95% CI [3.567%, 4.375%] | claim-grade physical-backing contrast; RSS increased 65.477% |
| SWC mixed prototype N=8 | extents 11 -> 3; THP peak RSS improved 34.453%; mixed THP beat mixed ordinary by 1.045% | quick dirty-tree screen exposed an 8.732% scan penalty against legacy THP |
| SWC mixed cache-first N=8 | extents 11 -> 3; THP peak RSS improved 34.185%; mixed THP beat mixed ordinary by 1.485% | quick dirty-tree rescue screen; mixed-versus-legacy estimate -0.058%, CI spans zero |
| Oxipng 10.1.1 | 5 Long hints, 4 routed allocations, 1 extent, 0 mixed reuse | credible compiler-coverage null; image buffers were outside hinted sites |

### Multi-target claim matrix

Each target answers a distinct evidence question. The original DataFusion
screen isolates one generated-driver site and physical backing. The natural
DataFusion run instruments nine dependency crates and supplies a large mixed
Long/Short population. Tantivy bounds broad-prior precision. SWC supplies the
matched physical-backing result and packing result. Oxipng shows that a real
application can miss the intended size range even when its few hinted objects
receive physical THP.

| Target | Compiler-site scope | Runtime ground truth | Physical backing | Time result | RSS result | Claim grade |
| --- | --- | --- | --- | --- | --- | --- |
| DataFusion 54.0.0, 512 queries | 1/1 exact site in generated driver; dependency-internal sites pending | 15 runtime sites; 50.58 MB Long, 0 Short; target 2,048 Long / 0 Short | 3/3 pairs: ordinary 0 KiB, THP 51,200 KiB | +1.221% median query time, 2/3 pairs; preliminary | THP peak RSS +7.266% | mechanism contrast only; performance and presentation flags false |
| DataFusion 54.0.0, natural Q4218 | 9 dependency crates; 2,304 sites, 649 priors, 8 executed exact prior matches | 307 sites; 67.43 MB Long, 7.977 GB Short; 37.94M allocations | selective pair: 8 candidate mappings, 0 advice, 0 collapse, 0 KiB THP | observed +6.879% time; excluded because backing was absent | observed +0.247%; excluded | dependency ground truth is presentation-ready; comparative claims false |
| Tantivy 0.26.1 resident index | 499 rewrites across Tantivy dependencies; 140 candidates, 10 executed exact matches | 104 sites; 9.22 MB Long and 5.171 GB confirmed Short; runtime byte precision 99.826%, recall 98.350% | selective smoke: 0 advice, 0 KiB in 17/17 samples | excluded because backing was absent | absolute peaks only; matched delta unavailable | ground-truth and fail-closed admission diagnostic |
| SWC N=20 matched backing | 313 Long hints; 59 executed exact sites | Stage-A Long precision 96.174%, recall 100% | 20/20 pairs: ordinary median 0 KiB, THP 22,528 KiB | pipeline +4.076%, 95% CI [3.567%, 4.375%], 20/20 | `VmHWM` +65.477% | presentation-eligible matched backing mechanism |
| SWC mixed cache-first N=8 | classifier evidence carried from the shared SWC prior stage; accuracy unmeasured here | ground-truth matrix absent from this artifact | 8/8 samples: mixed ordinary 0 KiB, mixed THP 6,144 KiB | within mixed layout +1.485%, CI [0.797%, 2.211%]; mixed-vs-legacy -0.058%, CI spans zero | mixed-vs-legacy THP -34.185%; within-layout CI spans zero | dirty-tree quick diagnostic; all claim flags false |
| Oxipng 10.1.1 | 40 candidates; 5 Long hints at 8 or 16 bytes; zero hints at 4--16 KiB | no classifier ground truth; 4 routed allocations per arm | both THP arms 2,048 KiB; 1 extent and 0 mixed reuse | mixed-vs-legacy THP observed -0.419% saving | observed -0.391% saving | one-pair compiler-coverage null; performance/RSS claims false |

The natural DataFusion ground-truth process ran 65.107 seconds and closed the
dependency-internal duration gap. Its CPU-affined adaptive processes completed
the same 4,218 queries in 28--30 seconds. The selective arm reached zero advice,
zero collapse, and zero sampled `AnonHugePages`, so the observed timing remains
outside a THP claim. One same-binary default comparison observed lifetime-aware
ordinary routing 2.680% slower with 0.079% higher peak RSS; one sample per arm
on a shared host keeps that comparison directional.

The machine-readable source of this table is
`multi-target-evidence-matrix.json`. It records the exact raw and compact
artifact SHA-256 values and preserves each experiment's claim flags.

## Bounded empty-extent retention

The process-local LRU retains fully empty, eligible lifetime extents and
reactivates an exactly compatible mapping. Compatibility covers slot geometry
and requested/actual backing state. Reconfiguration, reset, and an explicit
trim API release retained mappings.

The LRU holds at most 16 extents, for a 32 MiB virtual-mapping budget. Counters
expose current and peak retained mappings and bytes, insertions, reuse hits,
evictions, and trims.

### Syn mechanism result

The before/after runs used the same input SHA-256
`15275fa30b90d4e9281846801a1bfce46e805b99ce371fb76bb65728721b4b94`,
the same output digest `910697faf3e95f3d`, and the same 79,972 routed
allocations per arm.

| Arm | Mappings before | Mappings after | Reduction | Reuse hits |
| --- | ---: | ---: | ---: | ---: |
| Ordinary | 3,869 | 13 | 3,856 (99.664%) | 3,856 |
| THP | 3,869 | 13 | 3,856 (99.664%) | 3,856 |

Every avoided mapping has a corresponding retained-empty reuse hit. The after
run recorded zero evictions and a peak of one retained empty extent. This
establishes the mapping-churn mechanism.

The THP arm reported 26,624 KiB of `AnonHugePages` against 14,300,344 live slot
bytes. Density admission and fuller packing address the remaining physical
slack.

## Ground-truth-first classification

### Tantivy: broad-prior counterexample

The resident workload uses Tantivy 0.26.1 at commit
`d8f4c0b703120ed98f06297724dc1522df6019b9`. One process retains its in-memory
index, writer, reader, and searcher while indexing 442,368 documents, executing
216 commits and query phases, and evaluating 27,648 queries. Correctness passed
with zero descendants.

The force-track run observed 9,220,049 Long requested bytes and 5,171,077,058
confirmed-Short requested bytes across 104 exact runtime sites. The runtime
predictor achieved:

| Weighting | Precision | Recall | Accuracy |
| --- | ---: | ---: | ---: |
| Objects | 98.560% | 89.765% | 99.922% |
| Requested bytes | 99.826% | 98.350% | 99.997% |

Only 10 of 140 applied compiler-prior sites executed with an exact runtime
match. The broad Long prior produced 2,936 Long and 693,690 Short outcomes,
equivalent to 0.421% object precision and 0.0207% requested-byte precision.

A selective adaptive smoke correctly stopped before timing: no candidate
reached the 75% runtime-confirmed Long-byte threshold, THP advice remained zero,
and all 17 procfs samples reported zero `AnonHugePages`. This is a fail-closed
density diagnosis.

### Tantivy: exact borrowed-`Vec` coverage gap and bounded repair

The same force-track process exposes one narrower compiler opportunity. The
exact `Vec::reserve` call in `store/compression_lz4_block.rs:36` receives a
direct `&mut Vec<u8, Global>` MIR argument. Its seven runtime KEY5 subcohorts
span 15,290--15,310 requested bytes and contain 432/432 Long outcomes,
6,610,902 requested bytes, zero Short outcomes, and zero censored outcomes.
The old compiler emitted
`automatic_rust_lifetime_prior_owner_live_call_unknown`, so every subcohort
carried static prior zero.

Requested size cannot recover this signal. In the same run, the 16,384-byte
`Box<[TScoreCombiner; 4096]>` site in `query/union/buffered_union.rs:97`
produced 235,646/235,646 decisive Short outcomes; two additional outcomes were
censored.

The implemented repair authenticates only the exact inherent Global-`Vec`
DefIds for `reserve`, `reserve_exact`, `try_reserve`, and
`try_reserve_exact`. The receiver must be a formal mutable MIR argument or its
compiler-generated one-step reborrow. Local aliases, parameter aliases,
projected receivers, raw-pointer receivers, custom allocators, and other
methods abstain. The compiler emits observation tag `0xA103` with KEY3 identity;
the allocator supplies actual size and alignment, splits learning into KEY5
subcohorts, and keeps ordinary placement until eight decisive online outcomes
confirm Long. The existing 75% runtime-confirmed Long-byte density gate remains
authoritative for physical THP promotion.

Compiler fixtures cover all four exact methods, the positive receiver shape,
the negative receiver shapes, backedges, opaque calls, and panic-abort plus
balanced panic-unwind scopes. Runtime and evaluator tests cover the 4--32 KiB
layout gate, ordinary-until-confirmed routing, and KEY3-to-multiple-KEY5 joins.
These are component-level mechanism tests. The original Tantivy run motivates
the rule; it predates the repair and supplies no post-repair coverage, backing,
timing, or RSS result.

### DataFusion: exact resident-site success

The resident target uses DataFusion 54.0.0 at commit
`45d943dfb8699dc9cb9ef2320e955b73e3e6c03b`. One process builds two in-memory
tables and executes 512 fixed queries. The target site allocates 2,048 exact
24,576-byte, alignment-8 `Vec<u64>` buffers, representing 50,331,648 requested
bytes. Correctness and output digests matched across arms.

The generated Rust harness driver owns this `Vec::with_capacity` site and feeds
the resulting resident Arrow batches into DataFusion. The compiler proved the
driver's return-owned allocation and exported its exact requested layout. The
numeric five-field join matched one of one applied prior sites and all 2,048
target allocations. Runtime ground truth reported 2,048 Long, zero Short, and
zero censored target outcomes. This proves an exact Rust compiler/runtime join
inside a real DataFusion query process; it does not claim discovery of a
DataFusion-library-internal site.

The live-survivor path registered 78 samples, performed 76 due observations,
and promoted nine sites before their resident owners were freed. The adaptive
THP arm recorded 25 successful collapse operations and 51,200 KiB peak
`AnonHugePages`; all 28 sampled backing points were positive.

Three fresh-process matched pairs give a preliminary direction:

- median query-time improvement: 1.221%;
- median total-operation improvement: 1.140%;
- query outcomes: two faster pairs and one slower pair;
- median peak-RSS increase: 7.266%;
- ordinary backing: 0 KiB `AnonHugePages` in every pair;
- selective THP backing: 51,200 KiB in every pair.

The representative selective-THP process finished with seven retained empty
extents, 14,680,064 mapped bytes, and `all_mappings_released=false`. This is the
bounded retention state behind part of the RSS cost, rather than an unreported
released-at-exit state.

The exact site result and physical-backing mechanism are presentation-ready.
The three-pair timing estimate remains preliminary and awaits a preregistered
larger campaign after hot-path work.

This DataFusion cohort is Long-dominated. The representative static matrix
reached 94.166% requested-byte precision and 100% recall with 99.484% classified
byte coverage. Its absence of a Short-stable cohort leaves specificity
unmeasured; the Tantivy counterexample supplies the broad-prior boundary.

### DataFusion: natural dependency-internal result

The natural fixed-work process asks DataFusion to materialize 786,432 joined
rows into a resident Arrow `MemTable`, then executes 4,218 complete group
aggregations. The retained logical payload is 44,040,192 bytes. The force-track
ground-truth query phase lasted 65.107 seconds and preserved identical result
digests.

The compiler instrumented nine pinned DataFusion and Arrow dependency crates
while excluding the generated driver. It exported 2,304 allocation sites, 649
transported lifetime priors, and 742 complete exact runtime keys. The
process-wide allocator observation recorded:

- 37,943,834 routed allocations and 8,044,407,009 requested bytes;
- 307 runtime sites and 100 requested sizes;
- 67,432,538 Long requested bytes across 147 sites;
- 7,976,878,915 Short requested bytes across 182 sites;
- 6,896,270,389 confirmed-Short requested bytes across 145 sites.

The exact `(callsite,type_id,module_id,size,align)` join matched eight executed
dependency priors covering 143,955 allocations: 134,947 Long, 8,948 Short, and
60 censored outcomes. The mixed outcomes support the design in which the Rust
prior selects a packing lane and runtime survival controls THP admission.

The CPU20/21 diagnostic pair used the same fixed work. Ordinary lifetime-aware
routing took 28.156 seconds and peaked at 116,656 KiB RSS. The selective arm
took 30.092 seconds and peaked at 116,944 KiB RSS. It created eight THP-candidate
extent mappings, then produced zero advice attempts, zero collapse successes,
and zero sampled `AnonHugePages`. The backing gate rejected the comparison, so
the observed 6.879% time increase and 0.247% RSS increase carry no THP effect
claim.

A same-binary single-sample default arm took 27.421 seconds and peaked at
116,564 KiB. Lifetime-aware ordinary routing was therefore observed 2.680%
slower with 0.079% higher RSS. Shared-host, one-sample provenance makes this a
directional overhead diagnosis. It motivates moving sampling and lane choice
out of the common allocation path.

The original 45--60 second wrapper rejected the successful 65-second
force-track process before serializing procfs samples. Its stderr retained the
complete authenticated runtime-site export, and the accepted 45--70 second
recovery retains classification and exact-join evidence only. The adaptive
pair independently supplies conservative query-window procfs samples.

## Matched physical-THP effect

The claim-grade SWC campaign uses one compiler-prior binary, 996,240 routed
allocations per process, 16 complete parse/transform/codegen units, zero
application warmup, and no Criterion boundary. Twenty paired blocks passed
correctness, route identity, and per-pair backing gates.

Relative to matched ordinary backing, physical THP produced:

- pipeline-time improvement: **4.076%**, paired-bootstrap 95% CI
  **[3.567%, 4.375%]**, 20/20 blocks faster;
- operation-time improvement: **4.050%**, 95% CI
  **[3.543%, 4.353%]**, 20/20 blocks faster;
- THP `AnonHugePages`: median 22,528 KiB;
- ordinary `AnonHugePages`: 0 KiB;
- peak `VmHWM` increase: 65.477%.

This contrast isolates the benefit of physical THP backing inside the same
lifetime-routed mechanism. The same static-THP path remained about 6.84% slower
than the default allocator in its five-arm run. Broad routing covered roughly
93% of allocation traffic, all routed alloc/free paths entered the global arena
lock, and the earlier lifecycle produced 256 mappings for a peak of 11 extents.
These costs define the hot-path engineering target.

## Heterogeneous filling opportunity

The default-off mixed filler lets different slot geometries share a 2 MiB
extent at 64 KiB region boundaries while preserving lane, backing, cohort,
identity, and trailer provenance. Its four-arm SWC N=8 diagnostic used 32 fresh
processes with identical 996,240 routed allocations and equal output digests.

The initial prototype reduced legacy extent count from 11 to 3. In the THP arm it
reduced paired peak RSS by 34.453%, 95% CI [32.191%, 36.034%], with physical
backing in every sample. The prototype descriptor-table scan ran on 940,170
allocations, or 94.372% of routed traffic, and caused an 8.732% operation-time
penalty, 95% CI [8.096%, 9.411%].

Inside the same mixed packing layout, physical THP improved operation time by
1.045% over mixed ordinary backing, 95% CI [0.632%, 1.537%], with all eight
pairs faster. The paired peak-RSS interval crossed zero: median saving -0.570%,
95% CI [-1.377%, 4.994%]. This is a useful backing-mechanism contrast inside
the denser layout; its N=8 dirty-tree provenance keeps it outside the formal
performance claim.

A follow-on cache-first prototype adds an exact hot-region cache and an O(1)
free-side ownership prefilter. It preserved three extents and 6,144 KiB of
physical THP backing in every mixed-policy-2 process while changing the hot path:

- exact mixed cache hits: 939,574 allocations;
- cache lookup hit rate: 94.312%;
- available-list searches: 996,240 -> 56,666, a 94.312% reduction;
- descriptor-scan selections: 940,170 -> 1,376, a 99.854% reduction;
- scan attempts: 0.138% of routed allocations;
- THP mixed-versus-legacy paired median saving: -0.058%, 95% CI
  [-0.463%, 0.704%], four of eight pairs faster;
- THP peak-RSS saving: 34.185%, 95% CI [32.561%, 37.152%], eight of eight
  pairs lower;
- within the cache-first mixed layout, physical THP operation-time saving over
  ordinary backing: 1.485%, 95% CI [0.797%, 2.211%], eight of eight pairs
  faster; the paired peak-RSS interval crossed zero at -0.339%, 95% CI
  [-0.640%, 2.611%]; every mixed-ordinary sample recorded zero anonymous THP
  and every mixed-THP sample recorded 6,144 KiB;
- mixed ordinary versus legacy ordinary remained 0.486% slower, 95% CI
  [0.019%, 0.784%] slower, and used 7.693% more peak RSS, 95% CI
  [5.468%, 9.288%] more.

The earlier 8.732% scan-bound THP penalty is absent from the current point
estimate; the current mixed-versus-legacy THP estimate is -0.058% saving with
a confidence interval spanning zero. No equivalence margin was preregistered.
The matched within-layout contrast retains a positive physical-backing effect.
The N=8 dirty-working-tree provenance keeps performance and presentation claim
eligibility false. A clean committed rerun is the next gate. Routed deallocation
still records one arena slow-path lock per object, so the larger TLS/per-CPU
refill-lane work remains valuable.

### Oxipng: compiler-coverage null

The Oxipng 10.1.1 four-arm quick diagnostic used four fresh CPU24-pinned
processes, zero warmup, no Criterion, and identical optimized PNG output. The
compiler audited 40 allocation candidates and emitted five Long hints. Four
hints describe 8-byte CLI parser boxes; one describes a 16-byte deflate box.
No hinted site falls in the 4--16 KiB image-buffer range.

Each arm routed only four allocations into one extent. Mixed geometry reuse was
zero. Both THP arms obtained 2,048 KiB of physical backing, yet the mixed filler
had no population to combine. The one-pair mixed-versus-legacy THP observations
were -0.419% performance saving and -0.391% RSS saving, equivalent to 0.419%
slower and 0.391% larger. These values form a credible null tied to compiler
coverage and size admission. Dirty working-tree and N=1 provenance exclude
performance and RSS claims.

## Modern TCMalloc relationship

The relevant baseline is Google TCMalloc/Temeraire HPAA at compatibility pin
`12f255231938d30493186b0a037feedd70f5a1c1`, built with Bazel 8.4.2 and
authenticated fail-closed through its mapped DSO and HPAA stats. Historical
gperftools results are labeled `gperftools-legacy`.

TCMalloc keeps common allocation work in size-class per-CPU caches, moves
batches through transfer and central caches, and reaches the page heap and
HugePageFiller on refill misses. UniAlloc's next architecture step applies the
same separation to a Rust-specific low-cardinality lifetime lane:

```text
hot allocation/free:
    size class + lifetime lane -> TLS/per-CPU run pop/push

refill/overflow:
    batch 64 KiB regions -> central lane -> extent filler and THP decision

sampled learning:
    local pressure countdown -> rare exact-site observation slow path
```

This moves predictor scans, extent selection, mapping, and THP syscalls out of
the common allocation lock domain. The compiler prior becomes a refill-time
lane selector; exact runtime survival continues to correct its admission.

## Presentation-ready wording

- **Rust-specific mechanism:** an exact compiler ownership prior authenticated
  one generated-driver resident allocation site inside a DataFusion query
  process, covering 2,048/2,048 Long target outcomes and 50.33 MB of requested
  traffic.
- **Dependency-internal scale:** nine DataFusion/Arrow crates produced 2,304
  compiler sites; eight applied priors matched 143,955 runtime outcomes inside
  a 37.94-million-allocation workload.
- **Mixed real-program opportunity:** the natural DataFusion run observed
  67.43 MB Long and 7.977 GB Short traffic across 307 runtime sites.
- **Learning before shutdown:** bounded live-survivor sampling promoted nine
  sites while owners remained live, enabling 25 physically backed THPs.
- **Backing benefit:** matched physical THP improved SWC pipeline time by
  4.076%, 95% CI [3.567%, 4.375%], with all 20 blocks faster.
- **Memory and boundary:** that SWC contrast increased `VmHWM` by 65.477%; the
  preliminary DataFusion contrast increased peak RSS by 7.266%.
- **Precision boundary:** broad static Long admission reached 0.0207% byte
  precision on Tantivy, while the exact DataFusion site matched 1/1 and produced
  only Long target outcomes.
- **Compiler coverage repair:** an exact Tantivy `Vec::reserve` site produced
  432/432 Long outcomes across seven 15,290--15,310-byte KEY5 subcohorts, while
  a 16,384-byte Box-array site produced 235,646/235,646 decisive Short outcomes.
  The implemented rule uses receiver provenance to request online observation;
  size never selects lifetime.
- **Admission boundary:** natural DataFusion produced eight THP-candidate
  mappings and zero physical THP; Oxipng routed four tiny objects while its
  4--16 KiB image buffers had no compiler hint.
- **Packing opportunity:** heterogeneous filling reduced SWC extents 11 -> 3.
  The cache-first rescue preserved a 34.185% THP peak-RSS saving and reduced
  scan selections by 99.854%, with a quick N=8 THP paired median saving of
  -0.058% against the legacy layout. Within the mixed layout, physical THP was
  1.485% faster than ordinary backing, 95% CI [0.797%, 2.211%]. These are
  mechanism results pending a clean committed rerun.

## Evidence inventory

Tracked compact evidence:

- `syn-retention-summary.json`, `syn-retention-before.json`, and
  `syn-retention-after.json` -- mapping-retention invariants and counters;
- `tantivy-ground-truth-summary.json` -- resident fixed work, exact runtime
  outcomes, broad-prior matrix, and classifier metrics;
- `tantivy-borrowed-vec-gap-summary.json` -- exact missed Long site,
  same-size Short counterexample, narrow compiler matcher, runtime admission,
  and the fixture-versus-real-program claim boundary;
- `datafusion-resident-summary.json` -- exact compiler/runtime join,
  live-survivor mechanism, per-pair backing, preliminary timing, and provenance;
- `datafusion-natural-minute-summary.json` -- minute-scale dependency compiler
  coverage, process-wide Long/Short ground truth, exact applied-prior join, and
  fail-closed no-backing/default diagnostics;
- `oxipng-fastpath-null-summary.json` -- tiny-site compiler coverage, physical
  backing, and the one-pair mixed-filler null boundary;
- `swc-thp-matched-summary.json` -- claim-grade paired physical-backing result;
- `mixed-filler-swc-quick-summary.json` -- quick mixed-filler memory and scan
  diagnosis;
- `mixed-filler-fastpath-swc-quick-summary.json` -- cache-first scan rescue,
  retained memory result, all four planned paired contrasts, per-arm backing
  proof, and remaining lock boundary.
- `multi-target-evidence-matrix.json` -- target-by-target compiler coverage,
  runtime ground truth, physical backing, time, RSS, claim flags, and exact
  source hashes.

Large raw artifacts remain gitignored under `evaluation/raw/`. Compact
summaries record source identities and SHA-256 digests; summaries add source
paths and byte counts where those fields are available.

## Remaining risks and next proof

1. Commit the cache-first mixed implementation and rerun the four-arm campaign
   from a clean allocator revision.
2. Serve common lifetime allocations from a TLS/per-CPU batch cache and enter
   the global filler only on refill/overflow.
3. Keep adaptive observations sampled and apply learned state only to future
   refills.
4. Repeat the natural DataFusion pair after those changes and report query time,
   peak RSS, `AnonHugePages`, mappings, live bytes, and packing slack.
5. Extend compiler coverage toward medium resident buffers, then require exact
   runtime Long-byte density before THP admission.
