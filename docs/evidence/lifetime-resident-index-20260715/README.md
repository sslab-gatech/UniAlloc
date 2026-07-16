# Lifetime-Guided THP Allocation: Evidence Package

## Defended claims

This package supports five bounded claims:

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
| SWC N=20 matched backing | THP pipeline time improved 4.076%, 95% CI [3.567%, 4.375%] | claim-grade physical-backing contrast; RSS increased 65.477% |
| SWC mixed filler N=8 | extents 11 -> 3; THP peak RSS improved 34.453% | quick dirty-tree mechanism screen; scan caused an 8.732% time penalty |

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

Mixed filling reduced legacy extent count from 11 to 3. In the THP arm it
reduced paired peak RSS by 34.453%, 95% CI [32.191%, 36.034%], with physical
backing in every sample. The current descriptor-table scan ran on 940,170
allocations, or 94.372% of routed traffic, and caused an 8.732% operation-time
penalty, 95% CI [8.096%, 9.411%].

The mixed filler remains a memory mechanism. An O(1) validated
`(lane, backing, cohort, bucket)` hint must replace the scan before integration
or combined performance claims.

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
- **Learning before shutdown:** bounded live-survivor sampling promoted nine
  sites while owners remained live, enabling 25 physically backed THPs.
- **Backing benefit:** matched physical THP improved SWC pipeline time by
  4.076%, 95% CI [3.567%, 4.375%], with all 20 blocks faster.
- **Memory and boundary:** that SWC contrast increased `VmHWM` by 65.477%; the
  preliminary DataFusion contrast increased peak RSS by 7.266%.
- **Precision boundary:** broad static Long admission reached 0.0207% byte
  precision on Tantivy, while the exact DataFusion site matched 1/1 and produced
  only Long target outcomes.
- **Packing opportunity:** heterogeneous filling reduced SWC extents 11 -> 3
  and THP peak RSS by 34.453%; its current scan cost sets the O(1) refill-lane
  implementation requirement.

## Evidence inventory

Tracked compact evidence:

- `syn-retention-summary.json`, `syn-retention-before.json`, and
  `syn-retention-after.json` -- mapping-retention invariants and counters;
- `tantivy-ground-truth-summary.json` -- resident fixed work, exact runtime
  outcomes, broad-prior matrix, and classifier metrics;
- `datafusion-resident-summary.json` -- exact compiler/runtime join,
  live-survivor mechanism, per-pair backing, preliminary timing, and provenance;
- `swc-thp-matched-summary.json` -- claim-grade paired physical-backing result;
- `mixed-filler-swc-quick-summary.json` -- quick mixed-filler memory and scan
  diagnosis.

Large raw artifacts remain gitignored under `evaluation/raw/`. Compact
summaries record source identities and SHA-256 digests; summaries add source
paths and byte counts where those fields are available.

## Remaining risks and next proof

1. Replace the mixed descriptor scan with an O(1) validated lane/bucket hint.
2. Serve common lifetime allocations from a TLS/per-CPU batch cache and enter
   the global filler only on refill/overflow.
3. Keep adaptive observations sampled and apply learned state only to future
   refills.
4. Repeat the DataFusion matched campaign after those changes and report query
   time, peak RSS, `AnonHugePages`, mappings, live bytes, and packing slack.
5. Evaluate a Short-rich resident target alongside DataFusion so static
   specificity and Long recall are measured in one fixed-work process.
