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

The direct-local Layout ABI is a deliberate no-recovery path for compiler-proven
`std::alloc::Layout` alloc/realloc/dealloc calls.  In that mode,
`recovery_identity_matches=0` is expected because local dealloc uses the exact
compiler-supplied metadata directly instead of consulting the recovery side
table.  The evaluator therefore accepts zero recovery matches only when the
audit independently proves all of the following:

- real direct-local metadata ABI rewrites were applied;
- local realloc and dealloc rewrites were both present;
- no recovery-backed rows remain that still need recovery identity validation;
- the local size/align and recovery fallback contracts validated;
- runtime layout provenance validated row-by-row with no blockers;
- `recovery_identity_mismatches=0`.

This is not a general relaxation.  Recovery-backed MIR probes still require at
least one recovery identity match, and any reported mismatch remains a hard
failure for every probe.

Focused validation:

```sh
python3 evaluation/scripts/evaluate.py collect-rustc-driver-direct-allocator-mir-probe \
  --features stats,type_isolation \
  --direct-local-size-align-with-semantic-drop \
  --run-id rustc-mir-place-canonicalization-direct-local-gate-20260708a \
  --timeout 420 \
  --no-update-results
```

Current key result:

- companion status: `ready=true`, `blockers=[]`
- `runtime_probe_validated=true`
- `direct_local_no_recovery_metadata_abi_validated=true`
- `direct_size_align_recovery_backed_required_count=0`
- `semantic_metadata_recovery_identity_match_required=false`
- `semantic_metadata_recovery_identity_validated=true`
- `typed_allocations=84`, `typed_deallocations=84`, `fallback_allocations=0`
- `recovery_identity_matches=0`, `recovery_identity_mismatches=0`
- `runtime_layout_provenance_validated=true`

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

### Current Oxipng real-application compiler coverage boundary

Commit `88c35fd` adds exact `indexmap::map::IndexMap` and
`indexmap::set::IndexSet` heap-container identities to the compiler pass.  The
match is intentionally narrow: rustc crate disambiguators such as
`indexmap[hash]::set::IndexSet` are normalized, but the final def path must
exactly match a supported container path.  The pass does not infer arbitrary
custom ADTs, and the current real-application evidence keeps unsupported
`png::PngData`, `headers::Headers`, and `crossbeam_channel::Sender<_>` Clone
results unresolved.

A clean-HEAD Oxipng v4.0.3 smoke at `88c35fd` validated the actual rewrite path
without running a benchmark loop.  The pinned application built and ran once with
matching output SHA-256.  Its target-crate MIR audit reported 6 direct allocator
rewrites, 844 semantic-scope rewrites, 532 Drop rewrites, 4 semantic unresolved
candidates, and 0 Drop unresolved candidates.  The runtime recording window
reported 1058 typed allocations out of 1067 total allocation events, 9 fallback
allocations, and 0 type-isolation corrupt slots.  This is bounded functional
coverage and regression evidence only; it is not whole-program coverage,
unmodified-application deployment evidence, or a paper-performance result.
The durable summaries and target-crate audits for these real-Rust probes are in
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/type-isolation-real-rust-88c35fd-374d455-20260712/`.

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
- current Rust target: `hardware_pac_validated=false`,
  `software_fallback_validated=true`, `pac_probe_active_key=none`;
- cleanup: the direct probe removed its temporary cargo target directory after
  collecting the audit.

Therefore C006 remains non-claim-grade on this machine: the allocator path is
real and validated, but the current Rust/aarch64 target observes no hardware PAC
context binding, so the implementation honestly uses the software fallback.
