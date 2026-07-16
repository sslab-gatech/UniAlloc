# Lifetime-guided THP allocation

## Current claim boundary

The production-facing experiment is default-off and uses a four-stage decision:

```text
Rust ownership prior
    -> exact compiler/runtime site join
    -> runtime survival validation
    -> confirmed-byte density gate
    -> physical THP request
```

The compiler prior is useful for candidate selection, cold-start packing, and
site identity. Runtime evidence controls the defensible adaptive THP admission.
A separate compiler-only policy remains a mechanism arm for isolating page
backing effects.

The allocator objective matches the high-level HugePageFiller/HPAA objective:

- keep a backed 2 MiB extent densely occupied by objects that remain live;
- group draining objects so a complete extent becomes empty and releasable;
- avoid THP backing for sparse or rapidly draining extents;
- avoid repeated map, fault, zero, advise, and unmap cycles.

Lifetime information adds a Rust-specific signal for pursuing that objective. It
does not replace occupancy, release, or hot-path engineering.

## What Long and Short mean

The runtime clock measures later allocation pressure:

```text
pressure = cumulative eligible requested allocation bytes
age      = pressure at observation or free - pressure at allocation

Short    = age < 2 MiB
Censored = 2 MiB <= age < 8 MiB
Long     = age >= 8 MiB
```

The 8 MiB threshold refers to **subsequent allocation pressure**, not object
size. A 64-byte object can be Long, while a 24 KiB object can be Short. These
thresholds are preregistered feasibility parameters tied to the 2 MiB target
extent. They are workload-policy choices rather than universal Rust lifetime
definitions. The label measures pressure-relative residency; access hotness is
a separate signal.

Rust semantic lifetime and allocator performance lifetime remain separate:

- a final `Drop` proves eventual release;
- an owner return or consuming escape is a useful Long prior;
- survival through later allocation pressure is runtime Long evidence;
- cleanup and unwind can change the terminal path and force abstention.

## Compiler analysis

The marker-free pass runs after borrow checking and before normal MIR
optimization. For each supported semantic allocation, it exports ownership and
control-flow facts:

- exact destination owner and owner move chain;
- return, consuming escape, projected store, and local Drop sinks;
- opaque calls while the owner is live;
- loop backedges, `yield`/`await`, and receiver-owned allocation;
- normal and cleanup successors and cleanup Drops;
- exact requested layout when rustc can prove it.

The advisory rules are:

| MIR fact | Prior | Confidence | Boundary |
| --- | --- | ---: | --- |
| owner reaches return | Long | 70 | no cleanup, backedge, or yield ambiguity |
| owner reaches consuming escape | Long | 70 | same boundary |
| all-path local release | Short | 85 | exact Drop plus no opaque call, store, loop, yield, or owner-live cleanup |
| bounded receiver-local release | Short | 85 | exact receiver owner and the same path restrictions |
| owner-live cleanup/unwind | Unknown | 0 | all paths remain unproven |
| opaque call before release | Unknown | 0 | call may allocate, retain, or unwind |
| exact `Drop` fact alone | Unknown | 0 | eventual release has no pressure bound |

Exact `mem::forget` and `Box::leak` recognition is retained only as a bounded
process-long smoke oracle. It is excluded from real-program Long-classification
claims.

### Exact site authentication

The runtime observation key is:

```text
(callsite, type_id, module_id, requested_size, align)
```

The compiler now evaluates exact constant `Vec::with_capacity` layouts. For the
DataFusion resident target, a generated driver `Vec<u64>` with capacity 3,072
exports size 24,576, alignment 8, and a compiler-derived TypeId matching the
monomorphized runtime helper. The driver feeds resident Arrow batches into the
real DataFusion query process; this site is outside the DataFusion library.

A generic definition that still has no concrete TypeId retains a zero sentinel
and can resolve only through an authenticated, unique runtime TypeId. A
concrete positive compiler TypeId uses the stronger exact five-field key.

This observation/export identity is separate from the predictor key. Exporting
requested size never changes `AdaptiveSiteKey`, so dynamic-size callsites do not
fragment the runtime learner into one predictor per byte size.

## Runtime validation

The adaptive learner keeps 4,096 bounded sites. Its predictor key retains the
existing allocation-policy identity, including callsite, semantic type/module,
flags, placement, payload geometry, and alignment. Static hints initialize
small vote priors; runtime outcomes control state changes.

Each site may register up to eight live-survivor samples. A global next-due
pressure gate avoids scanning until some sample can cross the Long threshold.
When an exact slot remains live for 8 MiB of later pressure:

1. the site receives one Long observation before `Drop`;
2. a trailer flag prevents a second Long vote when the object is later freed;
3. the current allocation keeps its original placement;
4. only future allocations receive confirmed-Long placement and density credit.

This closes the shutdown-resident blind spot of a deallocation-only classifier.
A process can learn a resident site while its important owners are still live.

Cold-to-stable classification requires eight decisive observations. State
transitions use hysteresis, four post-transition samples, and periodic vote
decay. Censored outcomes add no vote. Static-prior conflicts neutralize the
prior while runtime learning continues.

## Placement and THP backing

Every routed 2 MiB extent contains 32 fixed 64 KiB identity regions. A region
binds exact semantic identity and slot geometry while live. Unknown and
unsupported allocations retain the base allocator path.

For the adaptive policy:

- Cold and sampled Short allocations use ordinary candidate backing;
- confirmed Long allocations prefer the fullest compatible candidate extent;
- static Long evidence may help candidate packing;
- only runtime-confirmed Long live slot bytes count toward physical promotion;
- promotion requires at least 75% of one 2 MiB extent;
- the allocator then issues `MADV_HUGEPAGE` and attempts collapse outside the
  arena lock;
- each measured process must independently prove `AnonHugePages` backing.

The matched adaptive ordinary arm uses identical learning, trailers, geometry,
and routing while forcing `MADV_NOHUGEPAGE`. It isolates THP backing from the
rest of the lifetime-aware mechanism.

Transparent Huge Pages are the primary backend because they preserve ordinary
anonymous allocation and allow per-extent eligibility. Explicit HugeTLB remains
an extra mechanism arm with reserved-pool and coarse-residency tradeoffs.

## Empty-extent retention

Lifetime-routed workloads repeatedly drain the same geometry set. Immediate
`munmap` converted this into repeated map/fault/zero/advice work. The current
allocator retains a bounded LRU of fully empty compatible mappings:

- capacity: 16 extents;
- maximum mapped retention: 32 MiB;
- exact geometry and backing-state validation on reuse;
- explicit trim, reset, and reconfiguration release;
- counters for insertion, reuse, eviction, trim, and current/peak bytes.

In the syn fixed-work mechanism screen, extent mappings fell from 3,869 to 13
and 3,856 retained-empty reuse hits matched the avoided mappings exactly. This
is a mapping-churn claim. Peak physical memory still depends on density and
reclaim policy.

An unconditional `MADV_DONTNEED` on every empty transition would preserve the
VMA while forcing refault and zero-fill on reuse. A later two-budget policy may
retain 2--4 warm MRU extents and discard older ordinary pages under pressure.

## Heterogeneous region filling

The default-off mixed filler allows different slot geometries to share one 2
MiB extent at 64 KiB region boundaries while preserving lifetime class,
backing, cohort, exact identity, and trailer provenance.

The initial SWC N=8 diagnostic established the memory opportunity and its first
hot-path defect:

- legacy layout: 11 extents;
- mixed layout: 3 extents;
- THP peak RSS paired saving: 34.45%;
- physical THP backing passed in every policy-2 sample.

That prototype scanned the descriptor table after a legacy bucket miss. It
selected that path for 940,170 of 996,240 routed allocations and made the THP
mixed arm 8.73% slower. The compact evidence is in
`docs/evidence/lifetime-resident-index-20260715/mixed-filler-swc-quick-summary.json`.

The matched backing contrast within the same mixed layout still favored THP:
operation time improved 1.045%, 95% CI [0.632%, 1.537%], with eight of eight
pairs faster. Its paired peak-RSS interval crossed zero. This separates a
positive physical-backing effect from the larger cross-layout descriptor-scan
cost, while the quick dirty-tree boundary keeps the result diagnostic.

A cache-first follow-on uses an exact hot-region key and an O(1) free-side
ownership prefilter. In a second four-arm N=8 screen it:

- served 939,574 allocations from the mixed hot-region cache;
- reduced scan selections from 940,170 to 1,376, or 99.854%;
- kept the three-extent layout and 6,144 KiB physical THP backing;
- retained a 34.185% paired peak-RSS saving, 95% CI [32.561%, 37.152%];
- produced a -0.058% THP paired median operation saving, 95% CI
  [-0.463%, 0.704%].

This recovers operation-time parity in the quick screen. The result comes from
a dirty working-tree snapshot with eight pairs, so it remains mechanism evidence
until the implementation is committed and the campaign reruns from a clean
revision. Routed deallocation still takes one arena slow-path lock per object.
The compact evidence is in
`docs/evidence/lifetime-resident-index-20260715/mixed-filler-fastpath-swc-quick-summary.json`.

## TCMalloc relationship and hot-path plan

Modern Google TCMalloc translates allocation size into a low-cardinality size
class, serves common operations through per-CPU caches, moves batches through
transfer/central caches, and reaches the page heap and HugePageFiller on refill
misses. Its HPAA is the relevant modern comparison; gperftools is retained only
as `gperftools-legacy`.

UniAlloc currently applies the same high-level full-or-empty objective with a
Rust lifetime lane. Its routed allocation and free paths still enter one global
arena lock. The next architecture step is:

```text
hot allocation/free:
    size class + low-cardinality lifetime/placement lane
    -> TLS or per-CPU run pop/push

refill/overflow:
    batch 64 KiB regions
    -> central lifetime lane
    -> extent filler and THP density decision

sampled learning:
    cheap local pressure countdown
    -> rare exact-site observation slow path
```

The compiler hint becomes a refill-time lane selector. Predictor updates,
pressure scans, extent selection, and syscalls leave the common allocation
lock domain. This is the route to a TCMalloc-like hot path while preserving the
Rust-specific lifetime contribution.

## Evaluation contract

A presentation result requires all of the following:

1. pinned source commit, allocator revision, toolchain, and evaluator hashes;
2. fixed work in fresh processes, with zero application warmup and no Criterion
   boundary around the real operation;
3. equal correctness/output digests and routed allocation counts;
4. exact target-site routing and compiler/runtime join evidence;
5. every measured ordinary operation-window sample with zero anonymous THP and
   every measured THP operation-window sample with positive anonymous THP,
   checked per process and per pair;
6. time, peak RSS, `AnonHugePages`, mappings, live bytes, and packing slack
   reported together;
7. modern Google TCMalloc at compatibility pin
   `12f255231938d30493186b0a037feedd70f5a1c1`, Bazel 8.4.2, with HPAA identity
   and mapped library proven fail-closed.

A failed backing gate produces a density/admission diagnosis and no timing
claim. A quick N=8 diagnostic identifies direction and bottlenecks; a stable
presentation estimate requires a larger preregistered matched campaign.
