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

Commit `c02baa6` adds the narrower outer-owner capacity proof for exact
std-owned `VecDeque::with_capacity` and `reserve_exact`.  Current and pinned
actual-`RUSTC_WRAPPER` runs report typed allocation/deallocation `4/4`,
wrong-type non-reuse, exact-type reuse, and zero fallback, raw
allocation/reallocation/deallocation, recovery-mismatch, or corrupt-slot
events.  `push_back`, a custom same-name helper, and multi-owner `Drop` remain
fail closed.  This is diagnostic functional evidence for those exact paths,
not universal coverage or performance evidence.

### Actual-rustc multi-crate module isolation

Commit `0704852` closes a compiler/runtime boundary that the allocator already
treated as security-relevant: external target crates no longer all receive the
same compiler module id.  The driver hashes normalized crate name with rustc's
Cargo metadata disambiguator; no-metadata direct-rustc invocations use the
canonical primary input, with full argv as the last-resort invocation identity.
The legacy in-tree `unialloc` id is preserved for probe ABI compatibility.

`python3 tools/unialloc-rustc-pass/test_mir_multicrate_module_isolation.py`
builds two packages whose libraries deliberately have the same rustc crate
name, identical source, identical semantic object type, and the same nonzero
compiler type id (`13297006753675728434`).  One current-toolchain run observes
two different module ids, rejects cross-module address reuse, reuses the
original address under the original module, and reports zero recovery mismatch
and corrupt slots.  The same regression separately compiles two same-name
direct-rustc inputs without `-C metadata` and requires distinct nonzero module
ids.  Both current and `nightly-2022-07-01` pass binaries compile, and the full
feature test run passes 738 allocator unit tests plus 432 std-bench functional
tests.  Independent review returned `APPROVE`.

This is a bounded actual-rewrite/cache-routing safety result.  The 64-bit hash
still inherits the documented trusted-metadata and collision boundary; this is
not universal crate coverage, cryptographic identity, or performance evidence.

### Lifetime-hint cache isolation

Commit `eadfa9c` adds a direct metadata-lifecycle regression for the policy
identity carried by `lifetime_hint`.  With the type id, module id, flags, and
placement held constant, an object released under lifetime `0x11` must not be
reused by lifetime `0x22`; returning to lifetime `0x11` must recover the
original address.  Hosted and `fixed_heap` configurations each pass the same
one-shot test with zero fallback allocations/deallocations, recovery identity
mismatches, and corrupt side-cache slots.

This test proves that the current allocator cache key keeps two explicitly
supplied lifetime classes separate on the covered paths.  It does not establish
compiler-derived lifetime coverage, universal policy isolation, or performance.

### Placement-hint cache isolation

Commit `a379f23` applies the same end-to-end cache-routing check to
`placement_hint`, which previously had only key-level unit coverage.  With the
type id, module id, flags, lifetime, and layout held constant, placement `0x21`
must not share a released entry with placement `0x22`; returning to `0x21` must
recover the original address.  Hosted and `fixed_heap` configurations each pass
the focused test with zero fallback allocation/deallocation, recovery mismatch,
and corrupt side-cache slots.

This is a manual-metadata allocator lifecycle regression for two covered
placement classes.  It is not automatic compiler placement inference,
universal policy isolation, or performance evidence.

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

### `Box<str>` to `String` pointer-preserving ownership transfer

Commit `ded36de` closes the symmetric current-rustc ownership-pairing gap for
ordinary `Box<str, Global>::into_string` source.  Exact allocation-crate DefId
matching plus a structural `Box<str, Global> -> String` proof reports one
candidate and applies it (`1/1`) through
`__unialloc_semantic_boxed_str_into_string`.  The single-run runtime oracle
reports attempted/applied/rejected `1/1/0`: the pointer, payload, length, and
capacity remain exact; the compiler-derived Box and String identities are
distinct and nonzero; the old Box identity cannot reuse the transferred
storage; and the exact String identity reuses it.  Fallback/raw allocation,
deallocation, and realloc-without-metadata, recovery mismatch, corrupt-slot,
and dropped-event counts are all `0`.

`unialloc/tests/boxed_str_into_string_rebind.rs` independently passes once on
the hosted backend and once on `fixed_heap`.  It covers the accepted path,
wrong-old-identity rejection, missing recovery state, and an authenticated
memory-tagged path without fabricating or mutating an untrusted identity.  The
adjacent current-rustc `String -> Box<str>` and `String -> Vec<u8>` probes also
remain green; embedded current-pass tests pass `16/16`, and the
`nightly-2022-07-01` pass still compiles.  Independent verification approved
the implementation and claim boundary, and the pre-commit suite passed 663
UniAlloc plus 430 std-bench functional tests.

This is an ordinary-Rust actual-rewrite and bounded isolation-effect probe, not
an external-application run, universal conversion coverage, performance, or a
paper percentage.  The preserved Oxipng run was not rerun or rebound.

### `CString` to `Vec<u8>` pointer-preserving ownership transfer

Commit `5408c04` adds exact current-rustc handling for ordinary
`CString::into_bytes_with_nul`.  Exact allocation-crate DefId matching and a
structural `CString -> Vec<u8, Global>` proof report one candidate and apply it
(`1/1`) through `__unialloc_semantic_cstring_into_bytes_with_nul`.  The
single-run runtime oracle reports attempted/applied/rejected `1/1/0`: 258 bytes,
the allocation pointer, payload, and capacity are preserved; the compiler-derived
CString and Vec identities are distinct and nonzero; the old CString identity
cannot reuse the transferred storage; and the exact Vec identity reuses it.
Fallback/raw allocation, deallocation, and realloc-without-metadata, recovery
mismatch, corrupt-slot, and dropped-event counts are all `0`.

The direct runtime regression passes once on hosted and once on `fixed_heap`
with `stats,type_isolation`.  It covers accepted, wrong-old-identity, missing
record, and authenticated memory-tagged cases.  Independent review approved the
exact fail-closed proof and runtime contract.  This is a bounded ordinary-Rust
actual rewrite and isolation-effect probe only; it does not establish other
CString APIs, custom allocators, external-application coverage, or performance.

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

### Untagged current-thread duplicate quarantine fails closed

Commit `05d18be` closes a caller-metadata bypass in the thread-local
delayed-free quarantine. Once a pointer is quarantined, a second deallocation
now checks pointer ownership before raw/compiler fast paths, recovery-record
consumption, stats, cache mutation, or raw free. The adversarial regression
zeros the occupancy hint and omits `FLAG_DELAYED_FREE` on the second call; the
authoritative retained-byte state still forces a slot scan, the duplicate
fail-stops, quarantine accounting is unchanged, and no type cache is poisoned.

Focused hosted and `fixed_heap` tests pass, as do 11 adjacent delayed-free
tests and the full pre-commit suite (663 UniAlloc tests plus 430 std-bench
tests). Independent review approved the repaired ordering and claim boundary.
This proves current-thread TLS quarantine ownership only. It does not establish
cross-thread duplicate detection without global memory tagging, universal
double-free detection, or performance.

### Process-visible cross-thread delayed-free ownership

Commit `e6dc7d6` closes the cross-thread/raw-entry bypass left outside the
current-thread TLS guard.  A bounded 8-shard, 256-entry process-visible pointer
registry publishes ownership before memory-tag validation, recovery-record
consumption, statistics, cache mutation, copying, in-place realloc, or raw
release.  A pending RAII guard unregisters on panic before TLS publication;
normal release, eviction, and valid thread-exit drain unregister after record
authentication.  `GlobalAlloc::{dealloc,realloc}`, raw dealloc/realloc,
different-alignment move, and both semantic realloc implementations fail-stop
before touching a registered pointer.

The hosted and `fixed_heap` delayed-free filters pass `15/15` each.  Regressions
cover foreign explicit deallocation, ordinary `GlobalAlloc` dealloc/realloc,
same-class `SemanticAlloc` realloc, the pre-TLS-publication window, panic
rollback, and a full registry shard.  Registry-full and oversized objects use
immediate authenticated release rather than hidden TLS ownership.  The full
pre-commit workspace suite passes 668 UniAlloc unit tests and 430 std-bench
tests, and independent review found no equivalent dealloc/realloc bypass.

The boundary is deliberately narrow: this protects pointers after their
pending/quarantined ownership has been process-visibly registered.  It is not a
universal detector for illegal operations that linearize before registration,
not a general double-free security proof, and not performance evidence.

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

### Source-bound Oxipng run at `576df61`

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

### Source-bound Oxipng ownership-transfer run at `38b8b59`

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

### Historical presentation bundle at `a51960d`

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

### Latest preserved Oxipng one-shot before module-id hardening

The latest preserved external Rust application run is bound to source
`46d5aaa` and an instrumented Oxipng v4.0.3 copy. Build and functional run both
return zero, and the output SHA-256 is the expected `565f253e...`. The static
target-crate audit separates 6 direct, 367 semantic-scope, 320 Drop, 12/12
ownership-transfer candidate/applied, and 529 explicit fail-closed rows. The
runtime reports 59 type rows, transfer attempted/applied/rejected `3/1/2`,
whole-run recovery mismatch `0`, and corrupt/dropped `0/0`; the injected oracle
observes wrong-type non-reuse and exact same-type reuse.

The preserved summary is
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-typeiso-46d5aaa-20260712e/oxipng-realapp-repro-summary.json`
with SHA-256 `f5b8ad7c...`. Commit `0704852` and later focused fixes postdate
this run and cannot be rebound to its counts. This was one functional run with
no timing loop; it does not establish natural-application universal isolation,
whole-program coverage, a security proof, or performance.

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

## Presentation-ready type-isolation evidence at `41205d3`

- **Realistic actual rewrite and placement isolation (`f007c7b`).** One generated
  `ingest/transform/storage/handoff` Cargo application runs through the real
  `RUSTC_WRAPPER`: four allocation scopes and one `String::into_bytes` transfer
  are actually applied, compiler identities match runtime rows, and the
  String/Vec plus same-layout Box/Vec oracles show wrong-identity non-reuse and
  exact-identity reuse with fallback/raw/mismatch/corrupt/dropped all zero. A
  real `thread::spawn(move || ...)` body automatically receives placement
  `0x8000` / `auto_cross_thread_escape`, while its local control remains
  placement `0` / `default`; cross-to-local non-reuse and exact reuse in both
  classes pass, with typed alloc/dealloc/hit/insert `3/4/2/4`.

- **Pinned-nightly and exact split support (`38dfe17`, `522c7f5`).** The first
  commit restores the historical `alloc_c_string` feature gate for the pinned
  pre-release 1.64 nightly without enabling it on current rustc. The second
  treats only exact `str::Split` and `SplitInclusive` as borrowed non-owners in
  the by-value hazard scan. Current and pinned actual-rustc regressions apply
  both `collect::<Vec<&str>>()` scopes, preserve wrong-type non-reuse and
  exact-type reuse, and keep a custom raw-pointer iterator fail closed; the
  relevant fallback/raw/mismatch/corrupt counters are zero.

- **Test hygiene (`41205d3`).** Delayed-free regressions now install
  `SemanticStateCleanup`, and the test that temporarily extracts a delayed slot
  retains `PendingGlobalDelayedFreeOwnership` until release. This prevents
  test-order/state leakage; it is not a new production-behavior claim.

- **Current-source Oxipng one-shot (`41205d3`).** The pinned-nightly build/run
  returns `0/0` and output SHA-256 is `565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`.
  Direct/scope/Drop are `6/369/320`, static transfer is `12/12`, runtime transfer
  is `3/1/2`, typed allocations are `873/1070`, fallback allocations are `197`,
  there are 58 runtime rows and 527 fail-closed rows, corrupt/dropped are `0/0`,
  and the injected wrong-type non-reuse / same-type reuse oracle passes. The one
  whole-run mismatch is preserved as `recovery_corrected_non_exact`. Artifact:
  `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-typeiso-41205d3-20260712a/`;
  summary SHA-256
  `c95808092c197b01399d4723e52e47c92478c8dafd5c498148fa70560c2dc7f4`.
  Versus the prior `46d5aaa` summary, `+2` applied scopes and `-2` fail-closed
  rows are a direct arithmetic inference matching the two new exact Split rows,
  not a coverage percentage or performance result.

These entries are source-bound only to `41205d3`. They make no timing or
performance claim and do not establish whole-program, universal, or natural-
application isolation.

## Latest accepted external Rust application evidence at `19ffb71`

The latest accepted external-application checkpoint is bound to exact source
`19ffb710752466a140067034650190dfdad60328`.  A first attempt at the preceding
source was correctly rejected after another parallel lane changed
`type_isolation.rs` during collection; its raw build/run evidence remains
append-only but is not a current-source PASS.  After committing the repair and
freezing the claim-bearing source, the pinned Oxipng v4.0.3 application was
built once and invoked once under `nightly-2022-07-01`, without retry or timing.
The accepted run has identical start/end HEAD, empty scoped status, and scoped
fingerprint
`13f72a5a3a5cd02657ae89500ea165c5238e9128c8f75c8e88aaba5158f3722f`.

Build/run return codes are `0/0`, and the functional output SHA-256 is
`565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`.
The target-crate audit reports:

- direct/scope/Drop rewrites `6/310/320`;
- ownership-transfer candidate/applied/selected rows `12/12/12`, with every
  selected type/module/callsite identity nonzero;
- fail-closed semantic/Drop rows `516/119`, of which 117 Drop rows are
  multi-owner.

The runtime window reports typed allocation/deallocation `871/860`, fallback
allocation/deallocation `199/160`, dynamic transfer `3/1/2`, 53 type rows, and
corrupt/dropped counts `0/0`.  The injected oracle validates one bounded
same-layout sequence: the wrong identity does not reuse the producer address,
while the exact producer identity does.  The full workload still records one
recovery correction, so it is explicitly classified
`recovery_corrected_non_exact`; the oracle's zero mismatch delta cannot be used
to claim exact whole-application pairing.

Artifact:
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-head-19ffb71-20260712-one-shot/`.
Acceptance SHA-256:
`efa66ec14e491f27d5549d94d4a99761bc900ebaf57357c218833715778e9a2f`.
This is bounded functional/diagnostic evidence for an instrumented pinned
application.  It is not a benchmark, performance result, universal compiler
coverage result, natural-application isolation proof, or whole-program exact
pairing claim.

## Current-source compiler and segregated-cache hardening after `19ffb71`

Commits `9a02767` and `3ccd464` are current-source functional/safety evidence
collected after the accepted Oxipng checkpoint.  They deliberately are not
rebound to the `19ffb71` application run.

`9a02767` makes constructor provenance fail closed: an arbitrary local,
platform, or dependency factory that merely returns `Vec`, `String`,
`Result<T, E>`, or another supported-looking destination remains audit-only
unless the pass has an exact constructor or callee-body allocation proof.  The
bounded exact exceptions added in this revision are `Box::new` and
`String::with_capacity`; both require the expected alloc/std DefId/path and
exact destination/argument shape, so custom same-name helpers and allocator-
specific variants are not widened into rewrites.  Current and
`nightly-2022-07-01` dependency-factory probes observe zero caller-attributed
typed allocations for opaque direct/`Result` factories while the exact
`Vec::with_capacity` control still pairs one typed allocation/deallocation.
Commit `ab98075` updates the existing end-to-end type-isolation security probe
to validate this boundary row by row instead of requiring zero unresolved
factories.  On the current toolchain it accepts 86 audited fail-closed rows
(including 8 `producer_box` and 4 `consumer_box` callsites), while the exact
inner `Box::new` scopes still produce 12/12 typed allocation/deallocation
events, distinct compiler identities, wrong-type non-reuse, exact-type reuse,
and zero recovery mismatch or corrupt cache slots.

The same revision strengthens lifecycle evidence.  Its nested-unwind probe
keeps an exact outer `Vec<PostUnwindPayload>::extend` receiver scope active
while an inner `Vec<PanicOnClone>::extend_from_slice` unwinds and is caught:
the inner cleanup restores semantic depth to `1`, the outer return restores it
to `0`, and the outer allocation/Drop identity pairs.  The direct-local hidden
replacement regression also encodes the anti-reattribution boundary: a
cross-crate replacement pointer without an allocation recovery record remains
raw at its later non-local Drop instead of inheriting the surrounding semantic
scope.

`3ccd464` closes the metadata-segregated cache's exact-identity collision and
entry-tamper gaps without increasing the entry footprint.  Each occupied entry
now carries an unconditional keyed structural authenticator over the lookup
key, full callsite-agnostic allocator-visible identity, policy key, pointer,
layout, optional PAC/software-auth state, and metadata.  Inline and
materialized forced-key collisions are misses unless the exact identity
matches; protection/auth downgrade, discriminator, pointer (including null),
size, and alignment tampering fail stop.  Full-bucket replacement and retained-
byte/capacity projection authenticate the eviction candidate before reading
its size or selecting it.  Hosted and `fixed_heap` collision, structural-
tamper, full-bucket projection, metadata-segregated, and footprint regressions
pass.  This is bounded internal cache-integrity evidence, not a universal
memory-corruption, compiler-identity-uniqueness, or UAF guarantee.

The Oxipng artifact remains bound only to
`19ffb710752466a140067034650190dfdad60328` and is therefore stale relative to
the current development source, whose latest code-bearing checkpoint is
`3ccd464f06f07c478f48698091c168352e0f81d9`.  No current-source external-
application count, performance result, or publication-grade claim is made from
these two hardening commits.

## Exact HashSet allocation identity and composed split-realloc safety

Commit `39c827b` closes an actual compiler false negative for ordinary
`std::collections::HashSet::with_capacity`.  Before the repair, both current
and paper-pinned rustc classified an outer `HashSet` whose element contained a
`Vec` or `Box` as ambiguous and routed all three observed allocations through
fallback/raw paths.  The exact matcher now requires the std DefId/path, exact
HashSet destination, one `usize` argument, `RandomState`, and optional current
`Global` allocator.  Current and pinned actual-wrapper probes each report typed
alloc/dealloc `3/3`, cache hit/insert `1/3`, wrong-identity hit `0`, exact hit
`1`, and zero fallback/raw/mismatch/corrupt counters.  Custom same-name,
hashbrown, IndexSet, alternate-hasher, and allocator-specific constructors
remain fail closed.  This is a bounded functional/cache-selection result, not
external-application coverage or performance evidence.

Commit `7ec42d4` adds a test-only cross-thread split-metadata realloc regression
combining type isolation, memory tagging, delayed free, hugepage metadata, PAC,
and process-visible recovery.  Stale caller old metadata cannot override the
allocation record; the old recovery/tag state transfers transactionally, the
moved-from pointer remains authenticated under its old hugepage/quarantine
identity, duplicate free fails before stats/cache mutation, and the replacement
is recoverable only under its distinct ordinary-domain identity.  Hosted and
fixed-heap exact tests each pass `1/1`, with one audited old-identity mismatch,
two exact recovery matches, zero authentication failures, and empty final
recovery/tag/quarantine state.  The test found no production defect and does
not establish arbitrary-size/error behavior, hardware PAC/hugepage execution,
or performance.

## Recovery-layout, Unix TLS publication, and exact `String::from(&str)`

Commit `79d0184` makes `GlobalAlloc::dealloc` distinguish a missing recovery
record from a live record whose authoritative `Layout` disagrees with the
caller. A valid mismatch now fails closed before raw deallocation, fallback
accounting, or cache publication; the record remains intact, and an exact-layout
retry consumes it and routes the pointer under its allocation identity. The
same-size-class regression proves this bounded retry invariant, not arbitrary
forged-pointer safety or performance.

Commit `61233b6` makes Unix TLS publication transactional with pthread destructor
ownership. The generated path installs the pthread key and completes
`pthread_setspecific` before publishing the fast Rust TLS pointer. An injected
save failure leaves the pointer unpublished, returns it for reclamation, and
observes a null TLS load plus one failure. This is bounded Unix failure-path
evidence; Windows FLS is separate and no throughput claim is made.

Commit `08a1bbf` adds current and `nightly-2022-07-01` actual-`RUSTC_WRAPPER`
evidence for exact `String::from(immutable &str)`. The matcher requires the
exact core `From::from` DefId, exact alloc `String` destination, and one immutable
`&str`; other `From`/source shapes and arbitrary String factories remain
audit-only and fail closed. Both toolchains report typed allocation/deallocation
`3/3`, wrong-type non-reuse, exact reuse, and zero fallback/raw, mismatch, or
corrupt-slot events. This is bounded functional evidence, not universal
`String`, external-application, or performance evidence. Oxipng was not rerun;
the accepted `19ffb71` artifact remains stale and cannot be rebound.

- **Current-source closure (`c477339`, `6640305`, `95d3d8a`, `cfd887e`, `6b0747e`).**
  Redox/Linux tests type-check with target-gated, Linux/Darwin-portable `mincore`;
  Redox runtime still needs an external linker/runner. Cross-thread wrong-layout
  `GlobalAlloc` deallocation and reallocation fail before raw/cache mutation and
  exact retry consumes the record. Reallocation checks once before zero-size,
  active-scope, auto-metadata, quarantine, statistics, and raw dispatch; the
  regression covers each former bypass while preserving Missing-record fallback
  semantics. The current generated actual-wrapper app passes exact
  `Box<[u8]>` provenance, Box/Vec wrong-type non-reuse, exact Box reuse, and zero
  raw/fallback/mismatch/corrupt; `Box<[String]>` stays audit-only. This is bounded
  functional evidence, not universal-app, paper-percentage, or performance
  evidence; Oxipng `19ffb71` remains stale and is not rebound.

## Current-source external app and ownership/realloc closure (`d50795f`–`003704a`)

The one-shot Oxipng v4.0.3 run under `nightly-2022-07-01` is preserved at
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-head-d50795f-20260713-one-shot/`
(summary SHA-256 `e8958683d1957c40f76e4400b25c506b16d70489e9aa2526b1bb5674411d3635`).
At source `d50795f892bd38ca3e7fd083da7d6eacafd0db06`, its build and functional PNG
run pass with 6 direct rewrites, 236 semantic scopes, 320 Drops, and 12/12
ownership-transfer candidates/applied. Runtime evidence reports 856/846 typed
allocations/deallocations, 808 cache hits, 214/174 fallback
allocations/deallocations, and a passing injected wrong-type/exact-type address
oracle. It also reports 592 unresolved compiler candidates, one
recovery-corrected mismatch, and `whole_program_compiler_coverage=false`.
Therefore it is bounded, source-bound external-application functionality and
isolation evidence, not universal coverage, exact whole-app pairing, timing, or
publication-grade performance evidence. Later commits do not rebind it.

Commit `1827e4f` closes a separate local compiler realloc bug: a live recovery
record with a different old `Layout` now fails closed before allocator, cache,
statistics, fallback, or recovery mutation; an exact retry succeeds and the
pointer is released exactly once. Hosted and `fixed_heap` focused regressions
pass, and the integrated pre-commit suite passes 680 UniAlloc plus 430 std-bench
tests. Commit `003704a` adds exact `Vec<u8>::from(String)` ownership rebinding.
Current and `nightly-2022-07-01` actual-wrapper probes both report one
candidate/applied transfer and runtime attempted/applied/rejected `1/1/0`, with
pointer/payload/capacity preserved, wrong-String non-reuse, exact-Vec reuse, and
zero fallback/raw/mismatch/corrupt events. Reference, generic, and custom-
allocator shapes remain fail closed; the dynamic fixture does not establish
explicit `From<&str>` or custom-allocator execution coverage.

## Current-source recovery-layout closure and exact `str::to_owned`

Commits `1abb4dd` and `03a5ef0` close the remaining alignment-changing
`Allocator::{grow, shrink}` recovery-layout path and make its regression safe to
run against the old behavior. Before the fix, a live same-address record under a
different valid `Layout` was collapsed into the missing-record path: the
allocator could allocate and copy a replacement, then release the old address
under the caller's wrong layout. The checked tri-state preflight now returns
`AllocError` before allocation, copy, cache/delayed-free effects, or old-pointer
release while preserving the authoritative exact record. Commit `386759f`
applies the same rule to the default and `RustAllocator` `SemanticAlloc` split
realloc boundaries. Its minimized pre-fix zero-size case returned the aligned
zero-size success sentinel (`0x8`) for a mismatched old layout; the current path
returns null before zero-size success, in-place mutation, or moved reallocation.
Hosted and `fixed_heap` focused tests pass. These are bounded P0 fail-closed
correctness results, not arbitrary-pointer safety or performance evidence.

Commit `24bb079` adds an exact compiler proof for alloc's
`<str as ToOwned>::to_owned` with immutable `&str` input and `String` output.
The frozen `d50795f` Oxipng audit contains 14 unresolved candidates of this exact
surface; that preserved count motivated the matcher but is not rebound to the
new source. Actual `RUSTC_WRAPPER` probes pass on `nightly-2026-06-11` and
`nightly-2022-07-01`: the exact call is rewritten to a `String` semantic scope,
while slice, generic, and custom same-name controls remain audit-only. Runtime
reports three typed allocations and three typed deallocations, one exact-type
cache hit, wrong-type non-reuse, and zero fallback allocation/deallocation, raw
allocation/deallocation/reallocation, recovery mismatch, or corrupt cache slot.
This is exact-surface functional and isolation evidence, not universal
`ToOwned`/`String` coverage, a new Oxipng execution, or performance evidence.

A separate source-bound Oxipng v4.0.3 one-shot at
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-head-24bb079-20260713-one-shot/`
binds clean scoped source `24bb079846833dda1c3a30099903dcd37b80d989` and
pinned application `dea23211ae6259007e068c59ab16929798d00d96` on
`nightly-2022-07-01`. Build/run returned `0/0` and the output SHA-256 matched
`565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`.
The target-crate audit reports direct/scope/Drop `6/250/320`, static transfer
`12/12`, unresolved semantic/Drop `576/2`, and
`whole_program_compiler_coverage=false`; runtime reports typed alloc/dealloc
`856/846`, fallback alloc/dealloc `214/174`, cache hits `808`, a passing injected
wrong-type oracle, and one whole-run recovery mismatch
(`recovery_corrected_non_exact`). The runner did not emit raw-no-metadata
counters, so raw evidence is missing, not zero. Relative to the frozen
`d50795f` artifact, `236→250` applied scopes and `590→576` unresolved semantic
rows agree with the 14 exact `str::to_owned` rows changing from unresolved to
applied; this is cross-artifact arithmetic and matcher-consistent inference
only, not rebinding, whole-program proof, or performance evidence. Summary
SHA-256 is `113d2fadd5ba29f7e837c4a1a6931266c9ab1988e0ba46b5c92613da7dfda620`.

## Intentional `fmt::format` fail-closed boundary and rejected POP diagnostic

The current-source audit contains 11 unresolved rows for the exact
`alloc::fmt::format` surface. They intentionally remain
`semantic_scope_rewrite_skipped_unresolved_heap_object_type` /
`audit_only_unresolved_heap_object_type`, rather than being lowered to an outer
`String` scope. Exact callee and destination types are insufficient here:
`fmt::Arguments` may execute arbitrary, reentrant `Display` callbacks, and such
callbacks may allocate their own `Vec`, `Box`, or other heap objects. Scoping
the whole call as `String` could therefore misattribute callback allocations to
the returned `String` identity.

The adversarial two-crate actual-wrapper regression
`tools/unialloc-rustc-pass/test_mir_fmt_format_fail_closed.py` passes on both
`nightly-2026-06-11` and `nightly-2022-07-01`. Its helper crate is deliberately
excluded from rewrite, while the target crate calls exact `fmt::format` once
with a reentrant allocating `Display` implementation and once with a plain
string. Both runs preserve all exact `fmt::format` rows as audit-only and
report callback count `1`, typed allocation/deallocation/cache-hit/cache-insert
`0/0/0/0`, fallback allocation/deallocation `3/3`, raw-no-metadata
allocation/deallocation/reallocation `3/3/0`, and mismatch/corrupt `0/0`.
This is a bounded adversarial regression for this fixture and these two
toolchains; it is not universal formatting-safety proof or performance
evidence.

Commit `1fcb5c2` only repairs **future** Oxipng runner capture and summaries so
raw-no-metadata counters are preserved and reported as `reported`, `partial`,
or `missing` without inventing zeros. It did not rerun or rebind the preserved
`24bb079` one-shot. That artifact's raw counters therefore remain missing, not
zero.

## Current-source exact `[u8]::to_owned` and Oxipng one-shot

The source-bound Oxipng v4.0.3 artifact at
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-head-1a4127f-20260713-one-shot/`
binds HEAD `1a4127f`. Its summary SHA-256 is
`118894ee3e08990a4d616110ad9001976c5ef082e1d3225e88dc9d070c41ce78`.
The pinned build and functional invocation returned `0/0`, and the output hash
matched the expected output. The target-crate audit reports direct/scope/Drop
`6/256/320`, unresolved semantic/Drop `570/2`, and
`whole_program_compiler_coverage=false`.

Runtime reports typed allocation/deallocation `860/850`, fallback
allocation/deallocation `210/170`, explicit raw-no-metadata
allocation/deallocation/reallocation `183/144/26`, 808 cache hits, and dynamic
ownership transfer attempted/applied/rejected `3/1/2`. The sole recovery
identity mismatch is exactly localized to a cross-crate `PathBuf`: allocation
metadata was recorded by the Oxipng library crate, while Drop requested the same
type id under the binary crate's module id. Runtime safely uses the
allocation-time record, so the result remains `recovery_corrected_non_exact`
rather than whole-application exact pairing.

Relative to the source-bound `24bb079` artifact, scope count `250→256` and
unresolved semantic count `576→570` correspond exactly to six audited
`<[u8] as ToOwned>::to_owned(&[u8]) -> Vec<u8>` rows changing classification.
This is an exact-row, cross-artifact comparison only; it does not rebind either
artifact or establish a coverage percentage. The one-shot is bounded
functional/diagnostic evidence with no timing loop. It is not a benchmark,
universal compiler/isolation proof, paper percentage, or performance claim.

## Cross-crate returned-owner module-isolation regression

Commit `e352206` adds an actual-`RUSTC_WRAPPER` two-crate regression in
`tools/unialloc-rustc-pass/test_mir_crosscrate_returned_string_recovery.py`.
The producer returns `ReturnedString::Value(String)` to the application, which
holds and drops the returned owner. Current and `nightly-2022-07-01` runs pass.
The compiler assigns the same nonzero String type id in both crates and distinct
nonzero producer/application module ids.

The runtime exposes both allocation-identity corrections when the application
drops producer-owned values. A same-type request under the wrong module does not
reuse the producer address, while producer and application exact module
identities each recover their own address. The bounded run reports typed
allocation/deallocation `4/4`, recovery identity match/mismatch `2/2`, and zero
fallback allocation/deallocation, raw-no-metadata allocation/deallocation/
reallocation, or corrupt side-cache slots.

This is a bounded enum(`String`) functional security regression demonstrating
that allocation-time identity remains authoritative across this returned-owner
boundary. It is not proof that the Oxipng aggregate `OutFile`/`PathBuf` path is
closed, universal cross-crate owner coverage, or performance evidence.

## Current/pinned type-isolation security-probe compatibility closure

Commits `c9b2f4d..0cd7696` make the existing actual-`RUSTC_WRAPPER`
type-isolation security probe accept the two MIR exposure shapes without
weakening its runtime safety requirements. The current-toolchain run at
`c9b2f4d` reports `validated=true`; its summary SHA-256 is
`b302a0776d29e236521d8c22d58017cd6e3d3ea067da2a3d86e010306e5a2040`.
It records typed allocation/deallocation `12/12`, cache
hit/insert/bypass `4/12/8`, wrong-type non-reuse and exact-type reuse, and
corrupt/dropped `0/0`. Current rustc exposes no target Drop/deallocation rows
for either payload (`0/0`), so the selected pairing mechanism is
`allocation_side_recovery`; runtime recovery match/mismatch is `8/0`.

The final pinned `nightly-2022-07-01` run at `0cd7696` also reports
`validated=true`; its summary SHA-256 is
`cc242b2da1ebe7f013352c1571062b41342ae38f52f619175fa26014d335fc1d`.
Pinned rustc exposes exact requested identities for producer/consumer target
rows `4/4`, so the selected pairing mechanism is `exact_requested_identity`.
Runtime recovery match/mismatch remains `8/0`; typed
allocation/deallocation is `12/12`, cache hit/insert/bypass is `4/12/8`, and
corrupt/dropped is `0/0`. Its generic Drop helper remains fail closed as one
unresolved audit-only row and zero specialized generic-Drop rows
(`unresolved/specialized=1/0`), not an applied scope.

These results establish compatibility for two audited MIR exposure forms:
current rustc safely relies on allocation-side recovery, while the pinned
toolchain supplies exact requested identities. Both preserve wrong-type
separation, exact reuse, and zero recovery mismatch. This is a bounded
functional security-probe result, not universal toolchain compatibility,
whole-program isolation, or performance evidence.

## OutFile/PathBuf cross-crate mismatch-shape regression

Commit `247a599` adds
`tools/unialloc-rustc-pass/test_mir_crosscrate_outfile_pathbuf_recovery.py`, an
actual-`RUSTC_WRAPPER` two-crate fixture whose producer derives `Clone` for an
`OutFile`-like aggregate containing `Option<PathBuf>`. The compiler audit maps
the producer-side `<OutFile as Clone>::clone` allocation and both consumer-side
aggregate Drops to the same nonzero PathBuf type id
`441353361075010719`, while producer and application module ids remain
distinct. That type id is the same one observed for the localized Oxipng
recovery mismatch.

Current and `nightly-2022-07-01` runs both pass. Each of the two consumer Drops
adds exactly one visible allocation-identity correction. A same-type request
under the wrong module cannot reuse producer storage, while producer and
application exact identities each recover their own address. Runtime reports
recovery match/mismatch `2/2`, typed allocation/deallocation/cache-hit/
cache-insert `4/4/2/4`, and zero fallback allocation/deallocation,
raw-no-metadata allocation/deallocation/reallocation, or corrupt side-cache
slots.

This fixture minimizes the observed Oxipng `OutFile`/`PathBuf` mismatch shape
and validates the allocation-time recovery mechanism. It does not rebind or
close the whole Oxipng execution, prove universal cross-crate ownership, or
provide performance evidence.

A separate three-run diagnostic rejected an attempted inline type-cache POP
fast-path change. The baseline median was `116.40 ns` with range
`115.66–116.74 ns`; the modified path measured median `117.39 ns` with range
`116.51–118.19 ns`, a `+0.85%` slowdown. The code change was reverted exactly.
These small same-condition measurements only explain the rejection of that
specific optimization; they are not a UniAlloc or paper performance claim.
