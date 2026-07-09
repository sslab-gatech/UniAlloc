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

These are bounded functionality probes, not the slow paper performance matrix.
They are intended to catch real compiler/runtime integration regressions quickly
before spending time on larger benchmark runs.

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
