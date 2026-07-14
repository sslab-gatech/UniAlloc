# Conservative Automatic Lifetime Classification

## Status

UniAlloc now has an opt-in, one-pass rustc MIR classifier that turns a bounded
ownership-and-phase proof into allocator lifetime metadata. The implementation
passes compiler/runtime mechanism testing. The first three-application smoke
found zero classified real sites under the strict proof, so receiver-owner and
move-chain analysis are required before application-level THP performance
claims. Comprehensive runtime accuracy and THP performance remain the next
evaluation stage.

The classifier is intentionally selective. Its first obligation is to avoid
placing an ambiguous short-lived object into the long-lived THP arena. Every
unproven site retains `LifetimeHint::Unknown` and follows the existing allocator
path.

## Operational lifetime definition

The runtime defines lifetime relative to an explicit application phase:

- `Ephemeral`: allocation and deallocation occur in the same logical epoch.
- `LongLived`: the allocation remains live across at least one successful call
  to `lifetime_hugepage_advance_epoch()`.
- `Unknown`: the compiler cannot prove either class under the phase contract.

Applications must call the epoch API at synchronized request, transaction,
iteration, or batch barriers. The phase boundary is process-wide. Concurrent
unsynchronized epoch changes are outside the static proof contract and must be
excluded from claim-grade evaluation.

## Compiler algorithm

### Owner identity

The analysis starts from an existing solved semantic allocation candidate. Its
per-owner key is:

```text
(mir_function, semantic_object_type, destination Place)
```

The emitted runtime/profile identity remains the exact
`(callsite, type_id, module_id)` tuple. Allocation and Drop callsites differ, so
the classifier propagates one owner decision to both metadata scopes.

The first classifier version accepts only scopes whose direct MIR destination
is the same supported heap owner named by `semantic_object_type`. Receiver-
owned calls such as `drain`, `reserve`, and `push` frequently return `()`, an
`Option`, or a temporary guard/iterator. The returned value's Drop cannot prove
the receiver allocation's lifetime, so these scopes explicitly use
`automatic_unsupported_site_unknown` until a future analysis carries the exact
receiver owner Place.

### Exact local path proof

The classifier accepts a temporal proof only when all of these conditions hold:

1. the scope is a direct destination-owning constructor or clone;
2. the destination is one unprojected MIR local;
3. the allocation writes that exact destination once;
4. read-only inspection and shared borrows may use the exact owner local;
5. mutable/raw borrows, projected-value uses, moves, copies, overwrites, and
   ownership escapes are absent;
6. the normal path is unique and acyclic;
7. that path reaches the exact destination-matched Drop;
8. a same-epoch `Ephemeral` result requires payload Drop glue that cannot run a
   user destructor; generic owner payloads whose instantiated type needs Drop
   conservatively abstain;
9. cleanup, branch, loop, missing-Drop, and duplicate-owner ambiguity abstain.

This temporal proof is per owner site. Local/no-recovery ABI authorization is a
separate, stricter contract: the pass must also solve a matching single-owner
semantic Drop lowering for the exact destination Place. Nested multi-owner
shapes such as `Box<Vec<u8>>` therefore retain the recovery-backed ABI. Keeping
the analyses separate allows a proven local owner to classify under recovery
while a sibling owner of the same Rust type escapes.

### Phase classification

The unique path is scanned from successful allocation to exact Drop:

```text
no call, no epoch boundary, exact Drop
    + no potentially effectful payload Drop glue
    -> Ephemeral, confidence 100

exact DefId lifetime_hugepage_advance_epoch, exact Drop
    -> LongLived, confidence 100

intervening unclassified call, effectful Drop glue, or opaque terminator
    -> Unknown (the operation may hide an epoch advance)

cleanup edge capable of dropping before the visible boundary
    -> Unknown (the site is path-dependent)
```

The phase API is marked `#[inline(never)]` so optimized MIR retains the marker.
The matcher requires the defining crate `unialloc` and an exact supported Def
path. A same-named application function cannot create LongLived evidence.

### Abstention reasons

Each supported allocation row records one auditable basis:

- `automatic_exact_local_drop_before_phase_boundary`;
- `automatic_exact_local_drop_after_phase_boundary`;
- `automatic_alias_or_escape_unknown`;
- `automatic_missing_or_cleanup_drop_unknown`;
- `automatic_nonlinear_control_flow_unknown`;
- `automatic_intervening_call_may_advance_epoch_unknown`;
- `automatic_cleanup_before_boundary_unknown`;
- `automatic_effectful_drop_glue_unknown`;
- `automatic_ambiguous_owner_site_unknown`;
- `automatic_unsupported_site_unknown`.

These reasons distinguish classifier error from compiler-analysis coverage.

## Input precedence and safety

The compiler uses this fixed precedence:

```text
exact external profile
    > invocation-wide manual hint
    > automatic classifier
    > Unknown
```

An external profile remains fail-closed. Missing, stale, malformed, duplicate,
guard-mismatched, and below-threshold profile entries stay Unknown; automatic
analysis does not fill profile holes. This preserves reproducibility for
profile-controlled experiments.

Automatic decisions are paired by construction. The presence of any external
profile disables every compiler-selected local/no-recovery ABI for the
compilation unit, including direct `Layout` allocation/deallocation rewrites
and semantic allocation/Drop scopes. Recovery records can therefore restore
the exact runtime identity when a partial or mismatched profile gives paired
operations different hints. The audit summarizer checks both allocation-first
and Drop-first nonzero selection mismatches. Compile-time validation of complete
pairs could later recover the local ABI as a performance optimization.

## Allocator use

The runtime treats lifetime and physical backing as separate policy inputs:

- LongLived objects are packed into lifetime-isolated, 2 MiB-aligned extents
  eligible for selective THP backing.
- Ephemeral objects use ordinary-page extents so short frees do not create holes
  inside long-lived THPs.
- Unknown objects bypass the lifetime arena and retain the existing allocator
  behavior.

Several objects share an extent. The design does not reserve one 2 MiB page per
object. Exact type/module/flags/lifetime/placement identity remains enforced at
the allocator's region boundary.

## Configuration

Direct driver invocation:

```sh
/path/to/unialloc-rustc-mir-rewrite-dry-run \
  --unialloc-auto-lifetime-classifier \
  --unialloc-actual-semantic-scope-rewrite \
  --unialloc-rewrite-audit-out /tmp/audit.json \
  -- <rustc arguments>
```

Cargo wrapper environment:

```sh
export RUSTC_WRAPPER=/path/to/unialloc-rustc-mir-rewrite-dry-run
export UNIALLOC_AUTO_LIFETIME_CLASSIFIER=1
export UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE=1
export UNIALLOC_REWRITE_AUDIT_DIR=/tmp/unialloc-audits
```

The automatic mode requests the hint-capable semantic scope ABI even when no
manual hint or external profile is configured.

## Audit metrics

The compiler audit reports two distinct count families:

1. metadata-row counts, which include allocation and Drop scopes;
2. unique semantic allocation-site counts, which form the classifier coverage
   denominator.

The site-level fields distinguish semantic candidates, classifier eligibility,
and unsupported receiver scopes:

```text
automatic_lifetime_candidate_allocation_site_count
automatic_lifetime_eligible_allocation_site_count
automatic_lifetime_ephemeral_allocation_site_count
automatic_lifetime_long_lived_allocation_site_count
automatic_lifetime_abstained_allocation_site_count
automatic_lifetime_unsupported_allocation_site_count
automatic_lifetime_unknown_allocation_site_count
automatic_lifetime_unknown_allocation_site_reasons
```

Use `evaluation/scripts/summarize_automatic_lifetime_classifier.py` to
deduplicate exact sites across audit files and verify classified
allocation/Drop pairing. Its `classification_coverage` denominator contains
automatic-eligible direct owner sites. Its `end_to_end_classification_coverage`
denominator contains every semantic allocation candidate, including unsupported
receiver scopes.

## Verification completed for the implementation

The focused process fixture covers:

- exact same-phase Drop;
- exact cross-phase Drop;
- return/escape;
- same-named fake boundary;
- a wrapper-hidden real boundary;
- a cleanup path before the boundary;
- a pre-Drop assertion cleanup edge on an otherwise same-epoch path;
- opaque inline assembly between allocation and Drop;
- a payload destructor that advances the epoch;
- another local's destructor that advances the epoch before the owner Drop;
- a nested multi-owner Drop that cannot use the local semantic ABI;
- a receiver-owned `drain` scope whose temporary destination must remain
  unsupported;
- CLI/environment parity;
- external profile and manual-hint precedence;
- profile misses remaining Unknown;
- partial external profiles forcing recovery-backed direct `Layout` and
  semantic allocation/deallocation scopes;
- allocation/Drop decision equality.

The actual MIR rewrite probe additionally executes hint-capable recovery and
local/no-recovery semantic scope ABIs. It verifies a mixed same-type function in
which one exact local owner classifies while the escaping sibling abstains.

## Real-application integration smoke (2026-07-14)

The final quick diagnostic rebuilt and executed the pinned ripgrep, fd, and
Oxipng workloads with the actual semantic rewrite and automatic classifier
enabled. Output hashes and workload checks passed. This run intentionally used
one sample, statistics-enabled binaries, and unmodified applications with no
epoch markers, so its timing and RSS values are performance-ineligible.

| Application | Semantic candidates | Automatic-eligible direct owners | Unsupported receiver scopes | Classified | Dominant abstention |
|---|---:|---:|---:|---:|---:|
| ripgrep | 335 | 186 | 149 | 0 | alias/move/escape: 177 |
| fd | 219 | 168 | 51 | 0 | alias/move/escape: 157 |
| Oxipng | 113 | 32 | 81 | 0 | alias/move/escape: 31 |
| **Total** | **667** | **386** | **281** | **0** | **alias/move/escape: 365** |

The runtime Type Isolation path remained active and reported dynamic semantic
allocation coverage of 23.67% for ripgrep, 73.01% for fd, and 93.18% for
Oxipng. Those percentages measure compiler/runtime metadata reach; they are
separate from lifetime-classifier coverage. Recovery diagnostics recorded one
identity correction in fd and six in Oxipng; the recovery-backed runtime
preserved execution, while this smoke supplies no exact whole-application
identity claim.

This smoke is a mechanism/integration pass and a classifier-coverage no-go. The
exact proof prevents a false positive discovered during the run: a receiver-
owned `drain_bytes` call returned a temporary `DrainBytes`, and the temporary's
Drop initially looked like the `Vec` owner's Drop. The direct-owner gate now
marks that scope unsupported. Allocation/Drop pairing has no mismatch among
classified sites; the real applications produced no classified pair to check.

The highest-return compiler work before claim-grade application performance is:

1. recover the exact receiver owner Place through reference temporaries;
2. track ownership-preserving move chains between constructor temporaries and
   user locals;
3. separate safe read-only/mutating receiver methods from ownership-transfer
   calls;
4. compute an interprocedural may-advance-epoch summary;
5. instrument synchronized epoch barriers and compare static predictions with
   runtime allocation/death epochs.

Raw reproducibility artifacts are stored in
`docs/evidence/automatic-lifetime-classifier-smoke-20260714/`.

## Real-software evaluation plan

### Primary applications

Use the pinned offline-ready sources already managed by
`evaluation/scripts/realworld_type_isolation_matrix.py`:

| Application | Pinned revision | Role |
|---|---|---|
| ripgrep | `4649aa9700619f94cf9c66876e9549d83420e16c` | low semantic coverage and abstention stress |
| fd | `b19136871310b01500b4f09eadd7387b8476be47` | allocation-volume and filesystem traversal stress |
| Oxipng | `dea23211ae6259007e068c59ab16929798d00d96` | high semantic coverage and algorithmic lifetime mix |

Add RRedis/Rsedis at `e50029606295cb9e0980c04a09f5bf888afa309a`
as the service/concurrency holdout after its runner gains lifetime and THP
instrumentation.

### Required variants

Run matched builds and inputs for:

1. default allocator;
2. UniAlloc with lifetime classifier disabled;
3. UniAlloc automatic lifetime classification with ordinary-page segregation;
4. the same classified layout with process THP forced off;
5. automatic lifetime classification with selective long-lived THP;
6. all-Unknown and oracle-profile controls in the diagnostic campaign.

This separates compiler metadata overhead, lifetime segregation, arena layout,
and physical THP backing.

### Runtime truth

Instrument direct synchronized epoch calls at workload-specific barriers:

- ripgrep: completed search batch;
- fd: completed traversal batch;
- Oxipng: completed image/batch optimization;
- RRedis: request or transaction windows after warmup.

Record allocation epoch and death epoch for every routed object. Report
object- and byte-weighted TP/TN/FP/FN, coverage, selective risk, long precision,
long recall, balanced accuracy, confidence calibration, and runtime-validation
exclusions. Keep application folds disjoint; random event splitting leaks the
same callsite between train and test.

### Performance and memory

For performance builds, remove diagnostic collection and retain identical
compiler decisions. Collect paired wall time or throughput, CPU time, tail
latency where applicable, peak and steady RSS, `AnonHugePages`, THP-backed
extent coverage, page faults, allocator retained/stranded bytes, and complete
teardown. Verify output hashes for every application.

## Claim boundary

The current implementation establishes a conservative compiler mechanism and
exact allocation/Drop hint continuity. Uninstrumented applications expose
static coverage only because they contain no semantic epoch boundaries.
Real-application accuracy and selective-THP benefit require the campaign above.
