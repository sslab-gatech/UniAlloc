# Allocator MIR integration and backend validation notes

This note records the current engineering status for the allocator changes that
are validated by real `rustc_driver` MIR probes and focused allocator tests.  It
is intentionally short: detailed evidence lives in the evaluation audit JSON
files named below.

## Opt-in `separate_sc` backend

Feature flag:

```toml
separate_sc_backend = []
```

When enabled, `unialloc/src/sc/mod.rs` exports the adapter in
`unialloc/src/sc/backend.rs`.  The adapter wraps `separate_sc::SCAllocator` so
zone and thread-cache code can use the same batch API shape as the default
`efficient_sc` backend:

- `allocate_batch_v2(align) -> (ptr, count, stride)`
- `deallocate_batch(ptr)`
- retained-empty slab accounting/trimming for global footprint control

The adapter returns a contiguous bump-style batch when strict alignment permits;
otherwise it returns a linked batch while preserving the requested alignment.
This keeps the implementation compatible with existing cache code while making
`separate_sc` usable as a real, selectable backend rather than a dead code path.

Validation:

```sh
cargo +$(cat rust-toolchain) check -p unialloc --features separate_sc_backend --lib --bins
cargo +$(cat rust-toolchain) test -p unialloc sc:: --lib --features separate_sc_backend -- --nocapture
cargo +$(cat rust-toolchain) test -p unialloc zone:: --lib --features separate_sc_backend -- --nocapture
```

## Direct allocator MIR realloc evidence

The direct allocator MIR probe exercises real `std::alloc::{alloc, alloc_zeroed,
realloc, dealloc}` and explicit `GlobalAlloc` calls.  The probe source contains
no manual UniAlloc metadata ABI calls and disables runtime auto metadata, so
passing typed stats require the `rustc_driver` MIR rewrite provider.

The important realloc detail is split runtime provenance:

1. The rewritten realloc callsite records the **new allocation** layout under the
   realloc callsite metadata.
2. The old object is released under the recovered old allocation-site metadata,
   because recovery-backed realloc consumes the side-table record for the old
   pointer before installing metadata for the new object.

Therefore the evaluator must not require both old deallocation and new
allocation observations to appear in the same runtime stats row.  It now gates
realloc as follows:

- the realloc callsite must have a real allocation layout observation;
- if numeric alignment is available, the realloc allocation alignment must match
  the old-layout alignment operand;
- the old-layout deallocation must be proven either in the same row or in a
  same `(type_id, module_id)` recovery-backed row that has both allocation and
  deallocation layout evidence for the old object;
- numeric old size/alignment operands remain fail-closed when they mismatch.

This keeps validation real: the gate still reads raw optimized-MIR rewrite rows
and runtime semantic-type stats rows, not summary counters alone.

Focused validation:

```sh
python3 evaluation/scripts/evaluate.py collect-rustc-driver-direct-allocator-mir-probe \
  --features stats,type_isolation \
  --timeout 120 \
  --no-update-results \
  --output-dir evaluation/raw/bounded-direct-allocator-metadata-validation-smoke-v2
```

Current key result:

- `runtime_probe_validated=true`
- `runtime_layout_provenance_validated=true`
- `runtime_layout_provenance_blockers=[]`
- `recovery_identity_matches=72`
- `recovery_identity_mismatches=0`

### Canonicalized MIR place/ref/tuple layout provenance

`tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs` now
canonicalizes narrow optimized-MIR `Place` spelling differences before looking
up compiler-tracked `Layout` provenance.  The same layout can appear as a local,
`copy`/`move` operand, dereferenced reference temp, or tuple-field projection
after code such as `Layout::extend(...).expect(...); let r = &pair; alloc(r.0)`.
Without canonicalization, the direct allocator pass could solve the original
layout but lose the identity at `(*ref).0`, fall back to a callsite-only type id,
and then report runtime recovery-identity mismatches between allocation and
deallocation.

The direct MIR runtime probe now contains a real ref-projected composite layout
case:

```rust
let ref_projected_pair = Layout::new::<u8>()
    .extend(Layout::new::<[u64; 2]>())
    .expect("valid ref-projected composite layout");
let ref_projected_pair_ref = &ref_projected_pair;
let ref_projected_layout = ref_projected_pair_ref.0.pad_to_align();
let ref_projected_ptr = alloc(ref_projected_layout);
dealloc(ref_projected_ptr, ref_projected_layout);
```

Focused validation:

```sh
RUSTC_BOOTSTRAP=1 rustc +$(cat rust-toolchain) --edition=2021 --test \
  tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs \
  -o /tmp/unialloc-rustc-mir-rewrite-dry-run-tests-20260708a
DYLD_LIBRARY_PATH="$(rustc +$(cat rust-toolchain) --print sysroot)/lib" \
  /tmp/unialloc-rustc-mir-rewrite-dry-run-tests-20260708a --quiet

python3 evaluation/scripts/evaluate.py collect-rustc-driver-direct-allocator-mir-probe \
  --features stats,type_isolation \
  --run-id rustc-mir-place-canonicalization-recovery-backed-20260708b \
  --timeout 420 \
  --no-update-results
```

Current key result:

- pass helper tests: `3/3`
- direct MIR probe: `runtime_probe_validated=true`
- `runtime_layout_provenance_validated=true`
- `runtime_layout_provenance_blockers=[]`
- ref-projected alloc/dealloc rows both solved as
  `std::alloc::Layout::extend(std::alloc::Layout::new::<u8>, std::alloc::Layout::new::<[u64; 2]>)`
- `direct_heap_object_solved_candidate_count=36`
- `recovery_identity_matches=76`
- `recovery_identity_mismatches=0`

### Direct-local no-recovery metadata ABI gate

The direct-local Layout ABI is a deliberate no-recovery path only when the
compiler rewrites a paired `std::alloc::Layout` allocation and its exact
reallocation/deallocation metadata.  Local deallocation then uses the
compiler-supplied metadata directly, so `recovery_identity_matches=0` is
expected only when the audit independently proves applied local rewrites,
row-level layout provenance, no recovery-backed row requiring validation, zero
pairing/contract gaps, and `recovery_identity_mismatches=0`.

Commit `26051b9` tightens the adjacent semantic owner/Drop path.  A local scope
now requires one unprojected owner local, the exact constructor destination, a
single acyclic non-cleanup normal path, and the exact `Drop`, with no intervening
borrow or reference, raw pointer, copy, move, call argument, projection,
overwrite, branch, loop, or early exit.  One unsafe or duplicate candidate makes
the complete same-type candidate group recovery-backed.  Raw
`SizeAlign`/`exchange_malloc` remains recovery-backed because optimized MIR does
not expose a sound pointer-to-owner link; a same-type Drop elsewhere is not
accepted as ownership proof.

The actual two-crate `RUSTC_WRAPPER` regression includes a hidden `&mut owner`
passed into a dependency that calls `mem::replace`.  The fail-first behavior was
typed allocation/deallocation `1/1`, fallback allocation/deallocation `1/1`,
and `raw_dealloc_no_metadata=1`.  After the ownership repair, that hidden-alias
lane reports typed `1/2`, fallback `1/0`, and raw-without-metadata `0`; the
positive aggregate reports typed `4/4`, fallback `0/0`, and raw `0`.  Only the
exact zero-alias owner uses the local scope ABI.  This is a bounded conservative
ownership check, not general Rust escape analysis.

Focused validation shape:

```sh
python3 tools/unialloc-rustc-pass/test_mir_direct_local_ownership_pairing.py

python3 evaluation/scripts/evaluate.py collect-rustc-driver-direct-allocator-mir-probe \
  --features stats,type_isolation \
  --direct-local-size-align-with-semantic-drop \
  --run-id rustc-mir-place-canonicalization-direct-local-gate-20260708a \
  --timeout 420 \
  --no-update-results
```

The earlier pure-Layout direct-local probe reported typed allocations and
deallocations `84/84`, fallback allocations `0`, recovery matches `0`, recovery
mismatches `0`, and validated row-level layout provenance.  That result predates
`26051b9`; it remains historical evidence for its exact Layout ABI snapshot and
must not be promoted to current ownership-hardening or universal coverage
proof.

## Semantic scope and cross-thread validation

The companion probes validate that compiler-inserted semantic scopes carry real
heap-object metadata through ordinary allocations, including Drop cleanup and
cross-thread recovery hints.  They also gate recovery identity mismatches rather
than only typed/fallback rates.

Current focused evidence:

```sh
python3 evaluation/scripts/evaluate.py collect-rustc-driver-mir-semantic-scope-probe \
  --features stats,type_isolation --timeout 120 --no-update-results \
  --output-dir evaluation/raw/bounded-semantic-scope-metadata-validation-smoke-v2

python3 evaluation/scripts/evaluate.py collect-rustc-driver-mir-cross-thread-hint-probe \
  --features stats,type_isolation --timeout 120 --no-update-results \
  --output-dir evaluation/raw/bounded-cross-thread-metadata-validation-smoke-v4
```

Current key results:

- semantic scope: `runtime_probe_validated=true`, `typed_allocations=161`,
  `typed_deallocations=161`, `recovery_identity_mismatches=0`.
- cross-thread hint: `runtime_probe_validated=true`, `recovery_identity_matches=3`,
  `recovery_identity_mismatches=0`, `semantic_scope_drop_unsolved_candidate_count=0`.

Latest-nightly compatibility was revalidated on 2026-07-09 with
`rustc 1.98.0-nightly (485ec3fbc 2026-06-10)`.  The current `rustc_driver`
MIR pass normalizes rustc crate disambiguators such as `std[hash]::...`,
recognizes current `Vec`/owned-buffer receiver-mutating impl paths, and treats
current `std::thread::functions::spawn` / scoped-spawn impl paths as
cross-thread escape sites.  Focused real-probe results:

- semantic scope:
  `runtime_probe_validated=true`, `semantic_scope_rewrite_applied_count=116`,
  `type_mapping_record_count=144`, `typed_allocations=161`,
  `typed_deallocations=161`, `fallback_allocations=0`;
- direct allocator:
  `runtime_probe_validated=true`, `direct_allocator_rewrite_applied_count=36`,
  `direct_heap_object_solved_rewrite_applied_count=36`,
  `typed_allocations=76`, `typed_deallocations=76`,
  `runtime_layout_provenance_validated=true`;
- cross-thread hint:
  `runtime_probe_validated=true`, `cross_thread_recovery_hint_count=4`,
  `hinted_semantic_scope_rewrite_applied_count=4`,
  `arc_cross_thread_type_mapping_record_count=1`,
  `recovery_identity_mismatches=0`.

At `c426a2f`, deterministic replay recomputes the companion from the validated raw target rewrite and runtime type rows rather than accepting the embedded summary: it requires distinct nonzero Arc and Vec identities in one module, exactly one fully contract-matching multi-owner closure skip, exactly `1/1` allocation/deallocation for each identity, and zero recovery mismatches.  This is bounded Arc+Vec worker-drop recovery-pairing evidence, not complete escape analysis, universal container coverage, or performance evidence; the durable replay summary is under `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/cross-thread-multi-owner-pairing-c426a2f-20260712/`.

These are bounded functionality probes, not the slow paper performance matrix.
They are intended to catch real compiler/runtime integration regressions quickly
before spending time on larger benchmark runs.

### Current actual-rustc identity-pairing replay

The three-lane bundle at
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/type-isolation-actual-rustc-2e3c0e2-a7b5f75-20260712/`
binds the executed Rust source to `2e3c0e2` and the final validators to
`a7b5f75`.  The three application collectors each ran once
(`collector_runs=3`, `collector_reruns=0`); validator-only repairs were then
accepted by deterministic replay of the preserved, hashed audit/log/runtime
artifacts rather than by rerunning the binaries.

- **Same-class `Layout` shrink.** Actual direct alloc/realloc/dealloc rewrite
  rows show the allocation establishing a nonzero compiler identity while
  realloc and dealloc use strict neutral delegation: zero type/module/flags and
  hints with the recovery-delegated basis.  Runtime maps all three callsites
  back to the allocation identity.  For the bounded `63 -> 57`, align-64
  lifecycle, the pointer remains reused and aligned, the payload is preserved,
  realloc observes typed allocation/deallocation `1/1`, final deallocation is
  typed once, and fallback/raw-realloc, recovery mismatch, and corrupt-slot
  counts are all zero.
- **Partial-coverage non-interference.** The supported seed and recovery helpers
  each have exactly one actual `Vec::with_capacity` allocation scope and zero
  target helper Drop/deallocation rows, so allocation-side recovery is the
  explicit pairing boundary.  The ambiguous raw Clone performs exactly one raw
  alloc/dealloc pair, cannot consume the protected buffer, and a later supported
  `Vec<ProducerPayload>` recovers that exact protected address.
- **Generic helper boundary.** Four calls through the generic helper produce
  four typed deallocations and four typed-cache insertions with zero fallback
  deallocations (`4/0/4`).  The current `optimized_mir` provider exposes no
  separate generic-helper audit row, so the evidence records that observability
  boundary and does not claim a separately observed monomorphized symbol or
  invent a generic-Drop skip claim.

This bundle is functional actual-rustc evidence only: it is not a benchmark,
does not support a percentage, and does not establish universal compiler or
container coverage.

### Actual-rustc `VecDeque` same-layout type isolation

Commit `5eb25f5` adds an ordinary-Rust `VecDeque` lifecycle probe rather than a
manual metadata-ABI fixture.  The actual compiler pass assigns distinct
nonzero identities to two same-layout element types.  In both hosted and
`fixed_heap` one-shot runs, four objects of the first type and four of the
second type occupy disjoint address sets; after release, four new objects of
the first type recover exactly its original set.  The bounded run reports 12
typed allocations, 12 typed deallocations, four cache hits, and zero
fallback/raw allocation, deallocation, or reallocation events, recovery
identity mismatches, corrupt slots, or dropped events.

This proves one compiler-derived `VecDeque` identity-to-reuse lifecycle for two
same-layout Rust types.  It is not universal container coverage, an exploit
proof, or performance evidence.

### Boxed-slice to `Vec` ownership-identity transfer

At `3d08399`,
`python3 tools/unialloc-rustc-pass/test_mir_box_slice_into_vec_rebind.py`
compiles and runs ordinary Rust source through the actual rustc pass.  Exact
DefId and structural type matching rewrites the `Box<[T], A>`-to-`Vec<T, A>`
ownership transfer to `__unialloc_semantic_box_slice_into_vec`, preserving the
allocation pointer and payload while rebinding only the live recovery `type_id`
from the Box owner to a distinct compiler-derived Vec owner identity; module,
flags, hints, and the allocation callsite remain bound to the original
allocation.  The probe observes wrong-type non-reuse followed by exact
same-Vec-type reuse, typed allocation/deallocation `3/3`, and raw fallback,
recovery mismatch, and corrupt-slot counts all `0`.  Separately, the runtime
regression `box_slice_into_vec_rebind_rejects_memory_tagged_record_without_mutation`
verifies that a memory-tagged record fails closed and retains its old identity
without mutation.

Commit `9c04871` extends that actual-rustc path through an ordinary Rust helper
that accepts `Result<Option<Box<[u8]>>, u8>`, unwraps it through `?` and
`Option::expect`, moves the box, and calls `into_vec`.  The audit observes
exactly two ownership-transfer candidates and applies both (`2/2`: direct and
wrapped).  The wrapped runtime oracle preserves the allocation pointer and
payload, rejects wrong-type reuse, permits exact same-Vec-type reuse, and leaves
recovery mismatches at `0`.

These are bounded functional ownership-transfer probes.  They do not establish
universal compiler/container coverage, performance, or a paper percentage.

### Cross-thread boxed-slice to `Vec` ownership transfer

Commit `718aab9` adds the cross-thread composition regression.
`tools/unialloc-rustc-pass/test_mir_cross_thread_box_slice_into_vec_rebind.py`
compiles and runs an ordinary Rust program that allocates a `Box<[u8]>` on the
main thread, moves it to a distinct worker, and calls `into_vec` there.  The
compiler lane uses the explicit cross-thread recovery placement policy; it is
not an automatic escape-inference test.  The actual-rustc audit finds exactly
one transfer candidate and applies it (`1/1`).
Runtime transfer attempted/applied/rejected is `1/1/0`: pointer and payload are
preserved across the thread boundary, a wrong Box identity cannot reuse the
released Vec-owned storage, and the same Vec identity reuses it exactly.
Fallback allocation/deallocation, raw alloc/dealloc/realloc-without-metadata,
recovery mismatch, corrupt-slot, and dropped-event counts are all `0`.

This is bounded cross-thread functional and safety evidence only.  It does not
establish universal thread/container coverage, performance, or a paper
percentage.

### Cross-thread memory-tagged `String` to `Vec<u8>` transfer

Commit `2b33401` closes the previously untested composition of compiler actual
rewrite, type isolation, software memory tagging, cross-thread recovery, and
pointer-preserving ownership transfer.  The ordinary Rust probe allocates a
`String` on the main thread, moves it to a worker, and calls
`String::into_bytes` there.  The pass applies exactly one transfer rewrite; the
String and Vec allocation scopes carry policy flags `129` and explicit
cross-thread placement `32768`.

On current `nightly-2026-06-11` and legacy `nightly-2022-07-01`, one functional
run each reports transfer attempted/applied/rejected `1/1/0`.  Pointer,
capacity, and payload are preserved; the old String identity cannot reuse the
released address, the exact Vec identity can, and a second Vec reuse proves the
tag record was retired rather than left stale.  Fallback, raw-without-metadata,
recovery-mismatch, corrupt-slot, and dropped-event counters are all `0`.
Separately, the hosted and fixed-heap runtime regression each pass the same
tagged cross-thread cache-routing boundary.

The placement is an explicit compiler policy, not automatic escape analysis.
This is a purpose-built ordinary Rust actual-rewrite probe, not external-app,
benchmark, performance, universal thread/container, or paper-percentage
evidence.

### `String` to `Vec<u8>` ownership-identity transfer

The same commit `718aab9` adds the previously missing compiler/runtime transfer
and its fail-closed safety regression.
`tools/unialloc-rustc-pass/test_mir_string_into_bytes_rebind.py` compiles and
runs ordinary Rust `String::with_capacity -> String::into_bytes -> Vec<u8>`
source.  Exact non-generic `String::into_bytes` structural matching finds one
candidate and applies it (`1/1`), using distinct compiler-derived identities
for String (`11507945832468554002`) and Vec (`13513741751600386252`).  Runtime
transfer attempted/applied/rejected is `1/1/0`: pointer, capacity, and payload
are preserved; a wrong String identity cannot reuse the transferred storage;
and the same Vec identity reuses it exactly.  Fallback allocation/deallocation,
raw alloc/dealloc/realloc-without-metadata, recovery mismatch, corrupt-slot,
and dropped-event counts are all `0`.

The dedicated integration regression
`unialloc/tests/string_into_bytes_rebind.rs` separately exercises the accepted
path plus three fail-closed controls.  A wrong expected source identity, a
memory-tagged source record, and a missing source record are each rejected
without mutating a trusted source record, fabricating a missing record, or
introducing a recovery mismatch; the aggregate transfer result is one applied
and three rejected (`1/3`).  The same regression passes once on the hosted
backend and once on `fixed_heap` (`1/1` each).

This is bounded functional and safety evidence only.  It does not establish
universal string/container coverage, performance, or a paper percentage.

### `Vec` to boxed-slice shrink-aware ownership-identity transfer

Commit `99762a9` adds the reverse actual-rustc ownership-transfer path for
ordinary `Vec<T, A>::into_boxed_slice` source.  Exact DefId and structural
`Vec<T, A> -> Box<[T], A>` matching rewrites both the exact-capacity and
spare-capacity callsites to `__unialloc_semantic_vec_into_boxed_slice`; the
audit reports exactly two candidates and applies both (`2/2`).  The single-run
runtime oracle observes the following bounded behavior:

- when `capacity == len`, the allocation pointer and payload remain exact and
  the conversion emits no allocation/deallocation/cache lifecycle event;
- when spare capacity forces shrink, the pointer moves but the payload remains
  exact, the target Box identity is published at the replacement pointer, and
  the old Vec storage remains recoverable only under its old exact identity;
- across the two transfers, attempted/applied/rejected is `2/2/0`: a wrong Vec
  identity cannot reuse the Box storage, the same Box identity can reuse it,
  and the old spare Vec identity can reuse the released old storage; and
- fallback allocation/deallocation, raw alloc/dealloc/realloc-without-metadata,
  recovery mismatch, and corrupt-slot counts are all `0`.

The independent review first found two fail-closed defects in the writer
version: a rejected wrong-identity or memory-tagged shrink could lose the
authenticated source policy, and a missing source record under an outer scope
could falsely attribute the replacement through outer or auto metadata.  The
accepted repair runs rejected authenticated shrink under the exact source
metadata (preserving its policy and memory tag), while missing records mask the
unrelated outer scope and suppress auto selection.  The suppression is RAII
guarded, so unwind restores the guard depth and a finite compiler metadata
stream is not consumed.  The three focused runtime regressions pass `3/3` in
both hosted and `fixed_heap` configurations: wrong/tagged exact and moved
rejections retain source policy/tag, while missing + outer + auto remains on
the conventional raw path without fabricated typed attribution.

This is bounded functional and safety evidence only.  It does not establish
universal container/compiler coverage, performance, or a paper percentage.

### Ownership-consuming `Vec`/`String` conversions rebind the live identity

Commit `32c3c5b` closes two P0 ownership-pairing gaps in ordinary Rust
conversions.  The actual-rustc `Vec<T, A> -> IntoIter<T, A>` probe applies its
only transfer candidate (`1/1`) and observes runtime attempted/applied/rejected
`1/1/0`: pointer and payload are preserved, a new `Vec` identity cannot consume
the transferred storage, the exact `IntoIter` identity can recover it, and an
implicit Drop introduces no recovery mismatch.  The runtime rejection matrix
also fails closed for wrong, zero, same, missing, duplicate, and memory-tagged
records without mutating an authenticated source record.

The same commit rewrites both exact-capacity and spare-capacity
`String -> Box<str>` candidates (`2/2`), with runtime transfer `2/2/0`.  The
exact case preserves pointer and payload; the shrink case may move while
publishing the target identity and retaining the old String identity only for
released old storage.  Wrong-String reuse is blocked, exact-`Box<str>` reuse is
observed, and the moved case lets the old String identity recover its old
storage.  Across the compiler/runtime probes, fallback/raw allocation,
deallocation, and reallocation, recovery mismatch, corruption, and dropped
events remain zero.  The dedicated String regression passes on both hosted and
`fixed_heap` backends and covers exact/moved positives plus wrong, tagged, and
missing-record rejection.

These are bounded current-rustc functional and safety probes.  They do not
establish every ownership-consuming standard-library conversion, stable rustc
compatibility, or performance.

### Compiler-driven `Vec` realloc identity and type-isolation probe

Commits `7096fc6`, `f0fe4d1`, and `37ea7cd` strengthen the focused `Vec<T>`
lifecycle probe.  Its primary lifecycle uses ordinary Rust source and no manual
metadata or allocator ABI calls.  The creator thread allocates a
`Vec<ProducerPayload>` with capacity 1 and transfers it before growth.  A
distinct worker verifies the original pointer, capacity, and payload; grows the
vector to at least capacity 8 through `reserve_exact`; fills and drops it on the
worker; then allocates a same-layout `Vec<ConsumerPayload>` followed by another
`Vec<ProducerPayload>`.

The validator requires real `rustc_driver` evidence and runtime behavior:

- actual semantic-scope and Drop rewrites for the producer and consumer `Vec`
  object types, with no unsolved candidate in this probe;
- creator-thread allocation, transfer to a distinct worker before growth, and
  worker-side typed realloc with the same nonzero compiler-derived type id;
- one typed replacement allocation and at most one typed deallocation for
  growth, with raw-realloc and recorded-old-metadata fallback both zero;
- exactly one typed worker Drop deallocation with raw-deallocation fallback
  zero;
- distinct compiler-derived type ids for the same-layout producer and consumer
  vectors;
- wrong-type non-reuse by `Vec<ConsumerPayload>`;
- exact same-type recovery by the next `Vec<ProducerPayload>` allocation;
- `recovery_identity_mismatches=0` and `side_cache_corrupt_slots=0`.

A separate standard `alloc`/`dealloc` positive control runs after the lifecycle
snapshots.  The validator requires exactly two function-bound direct rewrite
rows with applied/resolved UniAlloc metadata-ABI symbols and a shared nonzero
identity.  This proves actual direct allocator-call replacement without adding
events to the primary `Vec` counters; aggregate summary flags alone are not
accepted.

Current implementation evidence was collected once for the hosted allocator
and once for `fixed_heap` at clean commit `37ea7cd`.  Both audit files reported two
direct applied replacements,
`semantic_scope_rewrite_applied_count=11`,
`semantic_scope_drop_rewrite_applied_count=4`,
`semantic_scope_unsolved_candidate_count=0`,
`semantic_scope_drop_unsolved_candidate_count=0`, and
`cross_thread_recovery_hint_count=22`.  Both runtime summaries observed
`initial_capacity=1`, `final_capacity=8`, creator allocation followed by worker
growth, one typed growth allocation/deallocation, one typed worker Drop
deallocation, zero raw realloc/dealloc fallback, wrong-type reuse blocked,
same-type producer buffer recovery, zero recovery mismatches, and zero corrupt
side-cache slots.  The allocation and growth type ids matched.

Two observations are explicitly not gates: whether the capacity growth moved the
physical pointer is recorded but not required, and a zero recovery-match count
would be acceptable only for lifecycles whose allocation-side recovery proves
the deterministic same-type address recovery.  This probe is functional safety
evidence only; it makes no timing claim, no paper-performance claim, and no
universal claim about every `Vec` or collection path.  Durable summaries are in
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/typeisolation-final-safety-37ea7cd-20260712/`.

### Cross-thread type-changing realloc quarantine invariant

Commit `2eea36f` adds a P0 allocator regression for the lower-level lifecycle
that the compiler-driven `Vec` probe depends on. A producer allocation is
reallocated under a distinct consumer identity, then handed to another thread
while delayed-free quarantine is active. The test requires the old recovery
record to be consumed exactly once, the replacement allocation to publish the
new identity, and the payload to survive the replacement. While quarantined,
the old buffer must be invisible to the new type; after quarantine release, it
may return only to the old type's cache. Final cleanup must leave no stale
recovery record.

Hosted and `fixed_heap` test suites both pass this invariant with no recovery
identity mismatch. This is a bounded allocator-mechanism regression for
cross-thread realloc, delayed free, and cache routing. It does not establish
that arbitrary forged metadata is safe or that every application realloc path
has compiler coverage.

### Memory-tagged cross-thread mismatch quarantines only once

Commit `13807b3` adds a combined regression for process-visible recovery,
memory-tag side-table state, a foreign-thread wrong Drop identity, delayed-free
quarantine, and duplicate deallocation.  The recovered allocation identity may
quarantine the address exactly once.  A second typed free must fail-stop before
it can enqueue the address again, poison the wrong type cache, or create a
second owner; the one accepted free remains owned by the allocation identity.

This closes one concrete cross-thread metadata-mismatch and duplicate-free
path.  It does not establish hardware memory tagging, arbitrary forged metadata
handling, universal application coverage, or performance impact.

### Realloc policy-key separation across physical cache domains

Commits `48cdfcf`, `e5b992d`, and `ae923c6` add a lower-level regression for a
cross-thread moved realloc whose old allocation requests the hugepage policy
and whose replacement requests the ordinary metadata-segregated policy.  The
test requires the old recovery record to be consumed exactly once, the new
identity to be published, the payload to survive, and each pointer to be
removed from its cache exactly once before raw cleanup.

On the hosted allocator, direct side-cache snapshots additionally prove that
the old pointer entered the physical hugepage cache and the replacement entered
the ordinary inline cache, with the expected old and new metadata keys.  The
`fixed_heap` backend intentionally maps both policies to the ordinary physical
domain, so that configuration proves policy-key and metadata-identity
separation only; it does not claim distinct physical hugepage storage.  Hosted
and `fixed_heap` focused tests each pass once.  This is a bounded allocator
mechanism regression, not compiler coverage or performance evidence.

### Cross-thread realloc-to-zero retires the old identity

Commits `909a7ad` and `68749b9` add a regression for an allocation whose
metadata is published for cross-thread recovery and whose foreign-thread
realloc requests size zero under a different same-layout type.  The operation
must return the aligned zero-size sentinel, consume the old global recovery
record exactly once, publish no replacement record, and leave no stale
slow-path state.  The distinct type must not observe the retired storage; the
old type must recover it exactly once with its payload intact, after which one
owner performs one raw cleanup.

Hosted and `fixed_heap` focused tests each pass once with zero recovery-identity
mismatch and zero corrupt cache slots.  This test intentionally does not make a
PAC claim: the lifecycle neither enables pointer authentication nor records PAC
failure counters.  It proves the ordinary TLS identity-retirement/cache-routing
path only, not a hugepage physical-domain or universal realloc guarantee.

### Memory-tagged realloc-to-zero rejects a second owner

Commit `9ca5a10` extends the zero-size boundary to allocations protected by the
memory-tag side table.  Both the thread-local record path and the
process-visible cross-thread-recovery path must return the aligned zero-size
sentinel, retire the old tag, and consume the old recovery identity exactly
once.  A second deallocation of the retired address must then fail before it
can add another cache entry or reach the raw allocator; the type-isolation
side-cache snapshot is required to remain unchanged by that rejected attempt.

The two focused cases pass on hosted and `fixed_heap` configurations.  This is
a bounded fail-stop double-free regression for `FLAG_MEMORY_TAGGING`; it does
not claim hardware memory-tag enforcement, arbitrary stale-pointer detection,
or performance impact.

### Cross-thread overflow realloc preserves the live allocation

Commit `422c91f` adds the failure-side counterpart.  A creator publishes a
cross-thread recovery identity and payload; a foreign worker then requests a
distinct-type realloc with `usize::MAX`, which the test first proves cannot form
a valid `Layout`.  The failed realloc must return null before changing recovery,
validation, cache, delayed-free, or payload state.  The exact old global record
must remain live until a subsequent normal deallocation consumes it once; the
requested type must not observe the storage, while the old type can recover it
exactly once for raw cleanup.

Hosted and `fixed_heap` focused tests each pass once with no recovery mismatch,
cache corruption, stale slow path, or duplicate ownership.  Statistics are
disabled and PAC is not requested, so this test makes no PAC or performance
claim.  It is a bounded invalid-layout failure invariant, not a proof for every
allocator failure source.

### Size-negotiated semantic snapshot ABIs

Commit `6fd22fb` adds hosted and `fixed_heap` regressions for the checked
semantic-stats, fallback-attribution, and metadata-validation snapshot ABIs.
The tests reject null and undersized output buffers without writing, accept
exact-size and larger buffers, and verify the returned counters and fields.
This protects the versioned C-facing inspection boundary used by platform and
compiler probes; it does not by itself prove an external platform runtime.

### `Layout` fallback provenance remains fail-closed

Commits `f8612de` and `b5b70ed` add a clean-head real-`rustc_driver` probe for a
`Layout` value selected through `Result`.  The positive control uses
`Layout::new::<[u64; 4]>().align_to(64).expect(...)`; the actual allocator-call
rewrite preserves its compiler-derived identity and runtime allocation/
deallocation shape (`32` bytes, alignment `64`).  The negative control makes
`align_to(3)` fail and uses `unwrap_or_else` to synthesize a different
`Layout::new::<[u8; 37]>()`.  The compiler audit must classify that selected
layout as unknown heap-object type with a direct-callsite fallback identity,
not inherit the `[u64; 4]` identity.

The clean one-shot run at `b5b70ed` observed distinct nonzero identities,
two typed allocations, one typed deallocation, zero recovery mismatches, and
zero corrupt side-cache slots.  The validator also requires the positive
deallocation size and alignment to be exactly `32` and `64`; a fail-first unit
test rejects a forged alignment.  Evidence is preserved under
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/typeisolation-policy-layout-b5b70ed-20260712/`.
This proves only the two bounded `Result` shapes above.  It is not universal
`Layout`-transformer coverage and contains no benchmark or paper claim.

### Plain `Clone` candidate classification boundary

The MIR pass now classifies exact plain `Clone::clone` return values before
lowering them as semantic heap-object scopes.  The fail-closed behavior is
intentional:

- a single supported owner, such as `Option<Vec<u8>>` or a wrapper that contains
  exactly one supported owner, may be lowered to that owner identity;
- plain Clone results with no supported heap owner are skipped as non-heap object
  candidates, which does **not** assert that the Clone implementation performs
  no temporary allocation;
- ambiguous results, such as `Result<Vec<u8>, String>`, remain unsolved instead
  of choosing one owner;
- raw-pointer wrappers remain unsolved;
- const-generic Clone results remain unresolved when type or const parameters are
  still present.

Commit `52a7002` fixes a current-rustc ICE by preventing
`TypingEnv::fully_monomorphized()` Copy queries on Clone destination types that
still contain type or const parameters.  The const-generic fixture now exercises
`ConstGenericClone<N>` and validates fail-closed classification rather than
crashing the pass.  This strengthens compiler coverage accounting; it does not
turn skipped, ambiguous, raw, or const-generic Clone candidates into protected
runtime allocation events.

Commit `374d455` adds a compiler-driven fallback regression for an ordinary
`Result<Vec<ProducerPayload>, String>::clone` call.  The real rustc audit must
contain exactly one ambiguous fail-closed row and no applied or planned scope
for that call.  Hosted and `fixed_heap` runs both observed zero typed Clone
allocations, one raw fallback allocation and deallocation, correct cloned
contents, distinct buffers, zero recovery-identity mismatches, and zero corrupt
side-cache slots.  This proves safe conventional execution for this bounded
ambiguous path; it does not turn fallback traffic into type-isolation coverage.

### Nested semantic-scope unwind restoration

Commits `97c7aae`, `1aa4481`, and `d394f19` add a real `rustc_driver` regression
for nested compiler-inserted scopes. The inner `Vec::extend_from_slice` path
panics during Clone, so its MIR cleanup edge must pop only the inner semantic
scope and restore the still-active outer `Box` scope at depth 1. Returning from
the outer call must then restore depth 0, and the compiler-attributed `Box`
allocation and Drop must retain one matching nonzero type identity.

The hosted and `fixed_heap` one-shot summaries both validate the `1 -> 0`
restoration sequence, one typed allocation/deallocation pair, zero fallback,
zero recovery mismatch, and zero corrupt side-cache slots. Their MIR audits
contain 8 scope rewrites, 8 Drop rewrites, 5 unwind-pop insertions, and no
unsolved candidates. The subprocess explicitly overrides the workspace's
development `panic = "abort"` policy with `CARGO_PROFILE_DEV_PANIC=unwind` so
the probe exercises cleanup rather than terminating. These summaries and
audits are preserved under
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/type-isolation-real-rust-88c35fd-374d455-20260712/nested-unwind/`.

This proves one bounded nested-unwind pairing invariant. It is not a general
proof for every panic source, future rustc MIR shape, or unsupported allocation
path.

### Plain type-cache accounting is O(1) after a bounded rebuild

Commit `1228f71` replaces a healthy cold-cache insertion's repeated full scan
of all 64 plain type-cache slots with a trusted retained-byte counter.  The
first cold insertion after reset performs one bounded rebuild; healthy pushes
and pops then update the counter in O(1).  Observed per-slot corruption or an
accounting repair marks the aggregate untrusted, so the next growth performs
one bounded rebuild before returning to O(1) updates.

The same change repairs the aggregate admission check so the inline entry and
cold slots share one 512 KiB retained-byte cap; an empty inline slot can no
longer bypass remaining aggregate headroom.  Regressions cover push/pop totals,
exact admission at the cap, inline/cold replacement, drain/reset, and
corruption-triggered rebuild behavior.

A single before/after micro-diagnostic used 24 distinct type identities with
the same 64-byte layout, one thread, 50,000 cycles, and 2.4 million typed
allocation/deallocation operations.  With identical 1.2 million cache hits,
1.2 million inserts, and zero bypasses, the observed cost changed from
`72.338 ns/op` at `32c3c5b` to `54.808 ns/op` at `a51960d`
(`-24.233%`).  This is one directional run only: there is no median or range,
and it supports no general-application, paper, publication-grade, or stable
percentage claim.

### Auto-metadata policy changes preserve allocation-time identity

Commit `6603460` makes auto-metadata configuration generation-aware without
discarding live recovery records.  Global, thread-local, and layout-derived
allocations retain the exact metadata selected at allocation time across
disable or reconfiguration.  A long-lived worker lazily restarts a cyclic
compiler-ID stream at the first ID of each coherent generation.  Publication
and control-change overlap is covered by a concurrency regression.

Two negative boundaries prevent retroactive attribution: a pointer allocated
before auto metadata was enabled remains raw on deallocation, and an unrecorded
old pointer in realloc/move remains raw even when the replacement may use the
current policy.  The hosted `stats,type_isolation` suite passes `720/720`; the
fixed-heap suite passes `606/606`.  Thread-local recovery APIs still require
same-thread free/realloc; process-visible global recovery remains the explicit
cross-thread contract.  These are lifecycle correctness results, not
performance measurements.

### Oxipng real-application compiler/runtime evidence boundary

Historical commit `88c35fd` added exact `indexmap::map::IndexMap` and
`indexmap::set::IndexSet` heap-container identities to the compiler pass.  The
match is intentionally narrow: rustc crate disambiguators such as
`indexmap[hash]::set::IndexSet` are normalized, but the final def path must
exactly match a supported container path.  The pass does not infer arbitrary
custom ADTs.  At that source snapshot, `png::PngData`, `headers::Headers`, and
`crossbeam_channel::Sender<_>` Clone results all remained unresolved.

Commit `7cd31be` then classifies exact `alloc`/`std` `Arc` and `Rc` plain-Clone
handles as non-owning without changing constructors, Drop, or other heap-owner
semantics.  A single successful Oxipng v4.0.3 build/run bound to code-bearing
pre-ownership-hardening source `15d892e` consequently applies both concrete
`PngData::clone` sites to
their sole allocating owner, `Vec<u8>`.  `Headers` still fails closed because
it has multiple heap owners.  `Sender` also remains unresolved: its pinned
dependency implementation is non-allocating, but the compiler pass cannot bind
that source version, so a global name-only exception would be unsound.

The pre-ownership-hardening audit reports 843 actual semantic scopes, 278
applied Drop rows,
265 multi-owner Drop rows that fail closed, 2 semantic fail-closed rows, 6
direct rewrites, and 131 complete runtime rows.  The change from 853 to 843
scopes removes standalone `Arc`/`Rc` handle-Clone false positives while making
the two `PngData` sites precise; it is a semantic correction rather than a
coverage regression.  The functional PNG SHA-256 is
`565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`, and the
injected compiler-identity-bound address oracle remains validated.  Evidence
is under
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-arc-vec-15d892e-20260712/`.
This source-bound run predates `26051b9` and cannot be rebound to the hardened
owner analysis.  It proves neither universal compiler coverage nor a
publication-grade performance result.

The post-ownership-hardening Oxipng v4.0.3 one-shot is bound to application
source `af342f7e26dc4a5e132acc18d7f7a450009e6517`.  The wrapper build and the
single functional run both returned zero, and the output SHA-256 exactly
matched
`565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`.
The target-crate static audit keeps four categories distinct: 6 applied direct
rewrites, 843 applied semantic scopes, 278 applied Drop scopes, and 267
fail-closed candidates (265 multi-owner Drop plus 2 semantic).  Across the 1121
applied semantic/Drop rows, 23 satisfy the `_local` zero-alias proof and 1098
use recovery-backed scopes.

The separate runtime recording window reports 1061 typed allocation events out
of 1070 total, 9 fallback allocations, 129 complete type rows, zero dropped
events, and zero corrupt slots.  Its `9915 bp` counter is only a diagnostic
dynamic-event ratio, not static compiler coverage or object coverage.  The
injected address oracle records producer/wrong-type/producer-recovery addresses
`4379656256 / 4379656320 / 4379656256`: the wrong type does not reuse producer
storage, the same type does, oracle mismatches remain `0/0`, and oracle corrupt
slots remain zero.

The whole workload separately reports 67 recovery identity mismatches.  Each
is routed with the authoritative allocation-time recorded identity, so the
allocator fails closed without reporting corruption, but the result is
`recovery_corrected_non_exact`: it does not support an exact whole-application
compiler-pairing claim.  These 67 dynamic corrections are neither the oracle's
zero-delta window nor the 267 static fail-closed MIR candidates.  Validator
commit `3dc1039` repairs the former coupling and revalidates only the preserved
artifacts; the application was not rerun.  Evidence is under
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-typeiso-af342f7-20260712a/posthoc-preserved-run-validation.json`.
This is one-shot functional/diagnostic evidence, not universal coverage,
performance evidence, or a publication-grade percentage.

Current compiler hardening continues after that one-shot without rebinding its
counts.  Commit `04b8108` removes the non-Clone first-owner shortcut: receiver
calls inspect only their first MIR receiver, factory/constructor calls inspect
only their destination, and the existing bounded supported-owner scan of that
selected receiver/destination must yield exactly one owner before a semantic
scope can be applied; multi-owner or unresolved cases fail closed.  Actual-rustc regressions require both
an aggregate `(Vec<u8>, String)` factory and `Vec<String>::resize` to emit one
ambiguous fail-closed row and no applied scope while preserving conventional
runtime results.  The positive direct-local regression, pinned standalone pass
compile, and all 15 embedded pass tests also pass.  That `04b8108` hardening
round did not rerun Oxipng, so the `af342f7` application evidence remained
historical to its exact source.

Commit `2ff8770` closes the corresponding runtime layout/auth boundary.
Recovery lookup now distinguishes missing, mismatched, and exact records across
the TLS and process-visible tables.  A live pointer presented with a valid but
wrong layout/auth fails before stats, cache publication, delayed-free routing,
copying, or raw deallocation.  Deallocation preserves the exact record for a
correct retry; conservative/recovery-backed FFI single/split realloc ABI
wrappers return null while preserving the original record and payload.
Missing-record fallback and local exact ABI
paths retain their prior contracts.  Focused dealloc/realloc regressions,
adjacent recovery tests, independent review, and the repository pre-commit
suite (652 UniAlloc tests plus 430 std-bench tests) pass.  These are
current-source safety results, not performance or exploit-success evidence.

Commit `0026dfe` applies the same `Missing / Mismatched / Exact` distinction to
active recovery-required `GlobalAlloc` scopes.  The fail-first regression
showed a pre-existing raw pointer being attributed to the active scope and sent
to delayed free (`occupied_slots=1`, expected `0`).  Missing metadata now uses
the unknown/raw fallback, releases the raw allocation, and accounts the fallback
exactly once; a moved raw realloc preserves its prefix, gives the replacement a
new recovery identity, and accounts the old raw release once.  Exact metadata
uses the recorded identity, while mismatched metadata fails closed without
consuming or releasing the record so an exact retry remains possible.  Focused
hosted and fixed-heap filters each pass `2/2`.  This is P0 functional
correctness evidence, not performance evidence.

### Current hidden/consumed-owner hardening and Oxipng replay

Commit `427583bc69b7fcba09f1c7d6b464b8bd7a9c68b7` closes two additional P0
compiler-attribution holes.  The current-rustc path now traverses concrete
custom-ADT fields instead of treating their generic arguments as the complete
owner graph, so a hidden `String` beside a visible `Vec` cannot be mislabeled as
the `Vec` identity.  For factory and receiver calls, the selected destination
or receiver remains the proposed attribution owner, but supported owners in
consumed by-value arguments are merged as a safety check.  A conflicting or
unresolved owner graph is audit-only and fail-closed; duplicate same owners are
deduplicated and remain eligible for lowering.

Minimized actual-`RUSTC_WRAPPER` programs provide fail-first evidence.  Before
the fix, the hidden custom ADT and the conflicting by-value factory/receiver
cases each produced a recovery-identity mismatch delta of `1`.  After the fix,
those calls receive no semantic scope, preserve conventional program results,
and report mismatch `0`; same-owner factory and receiver controls remain
actually rewritten and also report mismatch `0`.  The pinned standalone pass
tests pass `15/15`, and the same pre-commit run passes 652 UniAlloc and 430
std-bench tests.  These are bounded actual-rustc regressions, not a claim about
all custom ADTs or all ownership transfer.

The corresponding then-current-source real-application check builds and runs an
instrumented Oxipng v4.0.3 copy once with pinned
`nightly-2022-07-01`.  Both source-binding snapshots record
`scoped_status=""`, scoped fingerprint
`5f36c0a7f1bad4284071cd3a8f6d50bb7a894282e5f76726e6f2b095d5bc49e8`, and
pass-source SHA-256
`aedef38625f6096e3f5875b35f3d89f839709ac3277d7c79e4df6756da8a1373`.
The offline build and functional invocation both return zero; the output hash
matches `565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`.
The target-crate audit counts 6 direct rewrites, 383 applied semantic scopes,
320 applied Drop scopes, and 581 fail-closed candidates (462 semantic and 119
Drop, including 117 multi-owner Drop rows).  The runtime contains 64 type rows,
zero corrupt slots, and no dropped type events.

The separately labeled bounded address oracle passes: wrong-type storage is
not reused, the same type recovers its address, the oracle mismatch count is
zero, and corruption remains zero.  The whole workload nevertheless records 13
recovery-identity corrections and is therefore
`recovery_corrected_non_exact`, not exact whole-application compiler pairing.
Those 13 current-run corrections cannot be mapped to or presented as repairs
of the historical `af342f7` run's 67 corrections; the old events cannot be
attributed or rebound across source revisions.  The artifact is
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-typeiso-current-427583b-20260712a/oxipng-realapp-repro-summary.json`.
It is a one-shot instrumented functional/diagnostic check, not an unmodified
application, timing benchmark, performance claim, whole-program coverage
result, or publication-grade percentage.

Commit `572bfab` subsequently restores one precisely bounded coverage class
without weakening the owner-graph safety gate.  The six capacity-only `Vec`
receiver methods may select the direct outer `Vec` identity only after an exact
receiver-ADT path check; `resize`, `extend`, `push`, `clone_from`, `Drop`, and
factory calls retain their existing full-graph or consumed-owner checks.  In a
current-rustc same-layout regression, `Vec<String>` and `Vec<Vec<u8>>` receive
distinct nonzero compiler/runtime identities, the wrong type cannot reuse the
first buffer, the same type recovers it exactly with one cache hit, and
recovery mismatch/corruption remain `0/0`; `Vec<String>::resize` remains
fail-closed.  The `427583b` Oxipng artifact predates this change and remains
exact only for its recorded source snapshot.

### Current-content Oxipng boxed-slice ownership-transfer run

The first pinned old-nightly build of the current pass exposed a
`GenericArg::as_type` compatibility break.  Commit `9bb9f8d` adds only the
required cfg adapter and received an independent `APPROVE`.  The pinned
old-nightly pass compile, the current actual-rustc Box ownership-transfer probe,
and the embedded pass tests (`16/16`) then pass.

One instrumented Oxipng v4.0.3 functional build/run was collected at HEAD
`576df61` while that byte-stable adapter was still uncommitted; it was committed
byte-identically as `9bb9f8d`.  At `9bb9f8d`, all 60 scoped collection-input
hashes matched.  After code-bearing commit `f8f0d90`, 58/60 match: the only
changes are the
post-run summarizer and its test, while the compiler pass and allocator/runtime
inputs remain byte-identical to the collection.  This is a scoped source
binding and does not claim that the full working tree was clean.  Build and run
both return zero, and the output SHA-256 exactly matches the known-good value.
Target-crate actual-rustc evidence contains 6 direct, 383 semantic-scope, and
320 Drop rewrites, plus 6 actual `Box<[u8]>`-to-`Vec<u8>` ownership-transfer
rewrites in real functions; 575 candidates remain explicitly fail-closed.

The runtime records 904 typed allocations out of 1070 allocation events, 64
type rows, zero dropped events, and zero corrupt slots.  Its injected oracle
passes wrong-type non-reuse and same-type reuse.  The whole run nevertheless has
13 recovery corrections, so pairing remains `recovery_corrected_non_exact`, not
exact whole-application pairing or natural-application isolation coverage.  The
enriched artifact is
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-typeiso-current-576df61-20260712c-enriched/`;
`oxipng-realapp-repro-summary.json` has SHA-256
`cd14bf7bdcbff8fe99d5fc6d97888ce2065050c527b423baa63b856857bdd43b`.
The enrichment did not rerun Oxipng: commit `f8f0d90` deterministically replayed
the two preserved raw audit files and records exactly 6 ownership-transfer
candidates, 6 applied rows, and the 6 selected rows by matching the exact
lowering kind and applied status.  That count is independent of the separate 6
direct allocator rewrites.  The original artifact remains unchanged with
summary SHA-256
`054b0e6a9fe7d8827d8304bb7929bb187151542541f9cfa83bb8b8ba8718ee4f`.
This was one functional run with no timing loop: it supports no performance or
paper percentage and does not establish universal compiler or application
coverage.

### Current-source Oxipng ownership-transfer execution

Commits `6d955c0` and `38b8b59` close the concrete optimized `vec!` path that
materializes boxed-array storage before converting it to `Vec`.  The first
change preserves the exact immediate `Box<[T; N]>` allocation owner across the
array-to-slice unsize; the second recognizes the optimized storage markers that
remain at the actual `into_vec` callsite.  The earlier artifact
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-ownership-runtime-6d955c0-20260712c-failed-after-first-repair/`
is retained unchanged as failure history: its functional process returned zero,
but the dynamic transfer delta was attempted/applied/rejected `1/0/1`, so it is
not promoted to ownership-transfer PASS evidence.

A subsequent single instrumented Oxipng v4.0.3 functional build/run is bound at
both start and end to source HEAD
`38b8b59c9748691d07b0ac9c0ad7c6adf94396cf`, pass-source SHA-256
`c2c83bec49001c0b40d045a32daaed10d4094afb7eea2415685670a756fe6d10`,
and 60-file scoped fingerprint
`8609ff8ff261f27998779613eefb739ddfcc0c682ac1a76ddefaf9dadbd2eb29`.
The target-crate audit contains exactly 6 ownership-transfer candidates and 6
applied rewrites.  The workload recording window then observes one attempted,
one applied, and zero rejected transfers (`1/1/0`).  The executed site is
`png::PngData::output`; its recovered old allocation owner is
`Box<[u8; 8]>`, with basis `exact_immediate_box_array_unsize`, before the
pointer-preserving rebind to `Vec<u8>`.  Build and functional run return zero,
and the output SHA-256 is
`565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`.

The successful artifact is
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-ownership-runtime-38b8b59-20260712d-success/`;
its `oxipng-realapp-repro-summary.json` has SHA-256
`292c8ff8479887f4bfa90fc58b48ce60326e27f89a9f26baf7ac50cce1a0e113`.
This is one bounded diagnostic functional run.  It is not a timing benchmark,
whole-program ownership/isolation coverage, universal compiler coverage, a
paper percentage, or publication-grade performance evidence.

### Presentation-bound current-head Oxipng validation

The current presentation bundle is bound to clean scoped source HEAD
`a51960d92a7c72deabaf25fc23e985c3b26c09a5` with scoped fingerprint
`f23eda54db834f2a81475ea84f28d70a55a54fb5502c379d7b36dc355a6e1d8f`.
Commit `a51960d` strengthens the Oxipng acceptance gate: a static ownership
transfer candidate is insufficient unless at least one transfer was actually
applied at runtime.

One pinned instrumented Oxipng v4.0.3 build/run passes at that source.  The
output SHA-256 exactly matches
`565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`.
The target-crate audit keeps its denominators separate: 6 direct rewrites, 383
semantic-scope rewrites, 320 Drop rewrites, 12 ownership-transfer candidates
with 12 applied static rewrites, and 575 explicit fail-closed candidates.  The
runtime transfer delta is attempted/applied/rejected `3/1/2`.  The injected
same-layout address oracle observes wrong-type non-reuse and exact same-type
reuse with oracle mismatch/corruption `0/0`; the whole workload separately
records 13 correctly recovered identity mismatches and therefore remains
`recovery_corrected_non_exact`, not exact whole-application pairing.

At the same source, the full repository test run passes 660 UniAlloc unit tests
and 430 std-bench tests.  The authoritative summary is
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-type-isolation-a51960d-20260712T202813Z/summary.json`
with SHA-256
`bc6f32805e58cb021223dde2e01a91887cbe36e653f5ff73257823349a12e685`.
This is one instrumented real-application functional run plus bounded
regressions.  It is not natural-application universal isolation, whole-program
coverage, a security proof, or performance evidence.  Full paper performance
reproduction remains intentionally deferred under the G001-to-G002 objective
change.

### Post-bundle type-isolation hardening

Three focused changes after the `a51960d` presentation bundle close concrete
safety and compiler-attribution gaps without rebinding the saved application
run.  Commit `ed188ca` adds a hosted and fixed-heap regression that transfers a
String allocation to a Vec identity, drops it on another thread, and requires
payload preservation, wrong-old-type non-reuse, exact-new-type reuse, and zero
recovery mismatch.  Commit `4cd0d7f` restricts exact `Vec::with_capacity`
destination attribution to the outer Vec backing allocation, including nested
`Vec<Vec<u8>>`; current and legacy actual-rustc probes pass while element-
affecting hazards remain fail closed.

Commit `997e840` separates the two roles of exact `Result<T, E>` destinations.
Only `Ok(T)` may supply a returned allocation identity; `Err(E)` remains a
by-value safety hazard and cannot create a semantic scope on its own.  The
actual-rustc A/B/C regression proves that an Err-only owner emits no outer
scope, `Result<Vec<u8>, u8>` is actually rewritten with the Vec owner, and a
conflicting Vec/Box error-owner result remains audit-only ambiguous.  The probe
passes both the current and `nightly-2022-07-01` toolchains; clean-tree Clone
and Layout provenance probes, the independent review, and the 660+430
pre-commit suite also pass.

Commit `681398e` narrows one additional compiler false negative without
widening general ownership inference.  Only the by-value hazard scan treats
the exact `core::slice::Iter` and `core::slice::IterMut` DefPaths as borrowed
nonowners.  An ordinary Rust
`input.iter().copied().collect::<Vec<u8>>()` call therefore receives an actual
Vec semantic-scope rewrite on both the current and
`nightly-2022-07-01` toolchains.  Its runtime control preserves the payload,
rejects reuse under the wrong String identity, permits exact Vec-identity
reuse, records transfer attempted/applied/rejected `2/2/0`, and records zero
fallback, raw, recovery-mismatch, or corruption events.  A custom raw-pointer
iterator remains unresolved, while a Zip Drop containing `IterMut` and
`IntoIter` remains two unresolved rows with zero applied rows.  General, Drop,
Clone, and ownership-transfer scans were not widened.  Independent review and
the 660+430 pre-commit suite pass.

Commit `9240fc6` closes the remaining pointer-preserving memory-tagged
ownership-transfer gap for the compiler/runtime ABI.  Before the fix, an
ordinary Rust `String::into_bytes` probe with policy flags `129` received an
actual MIR rewrite but recorded transfer attempted/applied/rejected `1/0/1`:
the old String identity could reuse the address and the exact Vec identity
could not.  The runtime now rebinds the authenticated recovery record and the
matching software memory-tag record together, changing only `type_id`; a failed
recovery commit rolls the tag back.  Cold transfer validation exhaustively
classifies TLS and process-global fast/overflow tag storage as zero, one, or
multiple matching records.  Duplicate, cross-domain duplicate, layout,
metadata, and authenticator inconsistencies therefore reject without mutation.

Current and `nightly-2022-07-01` actual-rustc probes now record `1/1/0`, preserve
payload, pointer, and capacity, prevent wrong-String reuse, permit exact-Vec
reuse, and retire the tag record; fallback, raw, recovery-mismatch, and corrupt
counts are zero.  Hosted and fixed-heap integration tests plus local/global,
forced-rollback, and duplicate-record regressions pass.  Independent review
found no remaining findings, and the pre-commit suite passes 662 UniAlloc tests
and 430 std-bench tests.  This is bounded evidence for the pointer-preserving
`String -> Vec<u8>` compiler/runtime contract.  It does not establish every
ownership-transfer helper, linearization under illegal concurrent ownership,
current-head Oxipng coverage, or performance.

Oxipng was not rerun after these focused fixes.  Therefore the 13 corrected
identity mismatches in the `a51960d` bundle remain historical observations;
they cannot be claimed eliminated or rebound to `9240fc6`.  These additions
strengthen bounded safety and actual-rewrite evidence, not whole-application
exact pairing, universal compiler coverage, or performance claims.

For historical comparison, a source-bound Oxipng v4.0.3 smoke at validator
commit `e466831` validated the
actual rewrite path without running a benchmark loop.  The pinned application
built once and ran once with the expected output SHA-256.  Its target-crate MIR
audit reported 6 direct allocator rewrites, 848 semantic-scope rewrites, 535
Drop rewrites, 4 semantic unresolved candidates, and 0 Drop unresolved
candidates; all 4 unresolved candidates have exact row-level fail-closed
evidence.  The runtime recording window captured all 140 type-class rows with
0 dropped events and 0 corrupt slots.  It reported 1058 typed allocations out
of 1067 total allocation events, 1016 typed deallocations, 9 fallback
allocations, and 1 fallback deallocation.

The validator matched 5 compiler identities to runtime lifecycle rows.  In one
same-module, same-layout case, `Vec<(InFile, OutFile)>` and
`Box<std::io::error::Custom>` both used size 139 and alignment 1, yet retained
distinct compiler-derived type ids and distinct runtime type-class rows; each
row recorded one allocation and one deallocation.  This proves a bounded
actual-MIR-identity-to-runtime-class lifecycle in one instrumented real Rust
application.  It does not by itself prove address-level non-reuse,
whole-program coverage, unmodified-application deployment, or performance.
The durable summary (SHA-256
`b53452cdab3421f0f41717acb0cc109720444d115546b8f5f6ea81a8acf6da55`) and
target-crate audits are in
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-runtime-typeclass-e466831-20260712/`.

### Aggregate Drop fails closed and the real-app address oracle is explicit

Commit `532435a` removes the unsafe first-owner shortcut for aggregate Drop.
When a supported struct, enum, closure, or coroutine exposes more than one heap
owner, one active Drop scope cannot represent every deallocation.  The pass now
emits an audit-only multi-owner row and leaves allocation-side recovery
authoritative.  Actual-MIR regressions cover generic struct and enum values with
both `Vec<u8>` and `String`; the security-probe runner passes 18/18 tests.

This correctness change intentionally reduces applied Drop coverage in the
subsequent pinned Oxipng run: 278 Drop rows were applied, while 265 multi-owner
Drop rows failed closed.  The same run retained 853 actual semantic-scope rows,
6 direct rewrites, 4 semantic fail-closed rows, 131 complete runtime type rows,
and 2 naturally matched compiler/runtime lifecycle rows.  No natural
same-layout pair remained after the safer Drop attribution, so natural-pair
presence is diagnostic rather than an acceptance gate.

The instrumented application therefore labels its address-effect check as an
injected functional oracle.  Two concrete, same-layout Rust `Vec` element types
receive distinct identities from actual MIR audit rows.  The producer identity
(`15719177160194310719`) allocated twice and recorded one cache hit; the
wrong-type identity (`11520851239810895908`) allocated once.  The observed
addresses were producer `4349034560`, wrong type `4349034624`, and producer
recovery `4349034560`: the wrong class did not cross-reuse the address and the
original class recovered it.  Two executed producer Drop identities and one
wrong-type Drop identity had exact runtime rows; recovery mismatch, corrupt
slot, and dropped-event counters were all zero.  The PNG output hash also
matched the pinned reference.

Two actual build/run attempts completed successfully, but the collector first
rejected unexecuted unwind-cleanup Drop rows and then rejected the now-optional
absence of a natural same-layout pair.  Those two concrete validator defects
were repaired; no third build/run was performed.  Commit `6aae903` passed 22/22
unit tests, including forged-oracle rejection, and replayed the preserved
`9c74b95` raw evidence offline.  The replay artifact SHA-256 is
`debb31038ab3912bdd07260de7facd394f8152ba086a4054e9b098b68b3aab0a` at
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-address-oracle-9c74b95-20260712/`.

This supports one injected, compiler-identity-bound same-layout address
sequence inside a pinned real Rust application.  It is not natural Oxipng
address coverage, whole-program or address universality, a general security
proof, or performance evidence.

### Supported plain Clone is paired with an ambiguous fail-closed control

Commit `dd30004` extends the ordinary-Rust Clone probe with a supported
`Option<Vec<ProducerPayload>>::clone` positive path beside the existing
ambiguous `Result<Vec<ProducerPayload>, String>::clone` negative path.  The
source does not call UniAlloc metadata or allocator ABIs manually.  Its actual
optimized-MIR audit contains exactly one applied semantic-scope row for the
Option Clone, with compiler type id `11653960357981974603` and module id
`13835860698770440193`.  A same-layout `Vec<ConsumerPayload>` row has the same
module but distinct type id `17450045950661180065`.

The runtime evidence binds those compiler identities to reuse behavior.  The
Option Clone records typed allocation/deallocation/cache-hit/cache-insert
`1/1/1/1`, zero fallback/raw allocation, deallocation, or reallocation, and
recovers the protected Producer address while not reusing the Consumer address.
The ambiguous Result Clone remains exactly one compiler fail-closed row and one
raw allocation/deallocation pair, and it cannot consume the protected Producer
entry.  Recovery mismatch and corrupt side-cache counts are both zero.  The
clean source-bound artifact is under
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/type-isolation-option-clone-dd30004-20260712/`.
This proves one supported Clone callsite and one ambiguous control, not universal
Clone or whole-application coverage, and it contains no performance evidence.

### Cargo target allowlisting does not rewrite the path dependency

Commit `1956350` upgrades the POSIX wrapper regression to a real two-crate Cargo
fixture.  Both the selected binary and a tiny path dependency compile and run;
the exact output is `target=7 dependency=11 sum=18`.  The dependency invocation
passes through the delegating compiler shim, whereas the allowlisted target is
compiled inside the rustc-driver wrapper.  The test requires exactly one target
audit and one pass log, an actual semantic-scope rewrite in the selected
function, and no dependency MIR row.  The exact focused unittest passes `1/1`.
This is bounded target/dependency non-interference evidence for that wrapper
path, not direct allocator-call coverage, arbitrary dependency-graph coverage,
runtime type-isolation evidence, or performance evidence.

### Windows FLS teardown has bounded Wine runtime evidence

The Windows work is split across three source-bound changes.  Commit `f5c4935`
provides symmetric PAL `FlsSetValue`/`FlsGetValue` access; `df5f449` makes the
production `GlobalTcache` owner fiber-local; `f238f10` handles registration and
save failures plus destructor ownership.  A current-owner callback performs a
full semantic drain.  When `DeleteFiber(B)` invokes B's callback while A is
current, the callback first narrow-drains OS-thread-shared allocator-retained
semantic caches, preserving live recovery/tag records, active scopes, and the
compiler cursor, then reclaims B.  If temporary binding or clearing fails, the
code leaks safely rather than risking use-after-free or double-free.

The host retained-only regression passes `1/1`, the host thread-cache filter
passes `69/69`, the Windows GNU cross-target type-check/cross-build passes, and
a Windows test executable links through the configured Zig wrapper.  Using the
Wine 10 runner introduced by `0bd84c1`, a fresh source-bound capture at HEAD
`38b8b59c9748691d07b0ac9c0ad7c6adf94396cf` runs the exact
A-current/B-delete, A-null/B-populated, and current-owner thread-exit lifecycle
tests; all three pass (`3/3`).  The freshly linked test executable has SHA-256
`7d4e060d7fc08a32134776f30e45550a8a4561373e15e5bcf077844115c8379b`.
The durable transcripts, source-input hashes, Wine 10.0 image identity, runner
and Dockerfile hashes, and executable identity are recorded in
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/windows-wine10-fls-38b8b59-20260712b/summary.json`
(SHA-256
`d44dbe74b7a5fd30776185ee5be4e7f7254eed2e3be5b5ec9bf08f086835a5a2`).
The earlier Wine 8 attempt was blocked by its missing `bcryptprimitives.dll`;
that is a runner dependency gap, not an allocator functional failure.  This
supports a bounded Wine-based Windows functional path, not native-Windows
universality or performance.

## PAC metadata-auth probes use allocator object addresses

The PAC metadata-auth evidence path now distinguishes three things that should
not be conflated:

1. whether the allocator reused a real typed side-cache object;
2. whether the raw PAC context-binding probe was run against that allocator
   object address, rather than an unrelated representative stack address;
3. whether the allocator actually used hardware PAC or the fail-closed software
   integrity fallback.

`std_bench` records `pac_probe_uses_allocated_object=true` only after the probe
has allocated, freed into the typed side cache, and reused an object through the
real `SemanticAlloc` path.  If allocation fails, the bench harness may still emit
a target diagnostic, but that diagnostic is explicitly marked as not object-bound
and cannot silently stand in for allocator PAC evidence.

The direct probe also reports `backend_observation_consistent=true`: hardware
PAC counters must agree with an active context-binding probe, while software
fallback counters must agree with an inactive/no-op PAC target.  This keeps the
fallback useful for correctness testing without allowing it to satisfy the paper
hardware-PAC claim.

Focused validation:

```sh
python3 evaluation/scripts/evaluate.py collect-pac-metadata-direct-probe \
  --run-id pac-metadata-direct-object-probe-20260708f --timeout 120

CARGO_INCREMENTAL=0 cargo bench -p unialloc --bench std_bench \
  --features bench_ourself,stats,pac \
  zzz_semantic_auto_metadata_report -- --exact --nocapture
```

Current key result on this host:

- direct allocator probe: `probe_validated=true`, `allocator_validated=true`,
  `pac_probe_uses_allocated_object=true`, `backend_observation_consistent=true`;
- the default `aarch64-apple-darwin` Rust target uses the safe software
  fallback (`hardware_pac_validated=false`, `software_fallback_validated=true`);
- cleanup: the direct probe removed its temporary cargo target directory after
  collecting the audit.

Commit `0d00c8d` adds a separate no-std consumer contract so the real allocator
probe can target `arm64e-apple-darwin` without pulling UniAlloc's std-only
dev-dependencies:

```sh
cargo +nightly-2026-06-11 run \
  -Z build-std=core,alloc,panic_abort \
  --manifest-path tools/pac-nostd-contract/Cargo.toml \
  --target arm64e-apple-darwin \
  --features pac,stats
```

That current-source arm64e run reports `allocator_validated=true`,
`hardware_pac_validated=true`, and `context_binding_active=true`; wrong-layout
and wrong-metadata authentication are rejected, typed-cache insert/hit counts
are `2/1`, and PAC sign/verification/failure counts are `3/2/0`.  This closes
the local allocator-runtime PAC functionality lane while preserving the safe
fallback on non-arm64e targets.  It does not measure PAC cost, so the optional
C006 performance percentage remains deferred.
