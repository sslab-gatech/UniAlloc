# UniAlloc rustc allocation-site pass

`unialloc-rustc-allocation-sites.rs` is a standalone `rustc_driver` tool for the
C002/type-isolation work. It is deliberately **not** a Cargo workspace member, so
normal `cargo check` does not require `rustc-dev` or `RUSTC_BOOTSTRAP`.

The pass runs inside rustc after analysis and walks optimized MIR call
terminators.  The original `unialloc-rustc-allocation-sites.rs` entry point is
still the analysis/listing mode, but the companion
`unialloc-rustc-mir-rewrite-dry-run.rs` entry point now has real implementation
paths for:

- MIR semantic-scope insertion around solved heap-object operations;
- direct replacement of supported allocator calls with UniAlloc metadata ABI
  calls; and
- cross-thread recovery hints for MIR-visible heap-object escapes.

This is real compiler API validation, not a mock parser.  The remaining
claim-grade gap is full-surface production validation: selected probes and
selected `std_bench` runtime smokes prove the implementation path, while the
whole paper C002 surface still has to be covered before final claims are marked
complete.

`type_id` is deliberately site-scoped:
`fnv1a64(allocation-site-object-type-id-v2 || function || MIR location ||
source span || object type || callee)`.  The row also records
`object_type_id = fnv1a64(object type || callee)` so audits can distinguish
"same object/callee shape" from "same compiler allocation site".  The allocator
metadata path receives the site-scoped `type_id` plus the separate `callsite`
hash; runtime imports now check the `(type_id, callsite)` pair against the
compiler map instead of accepting a type-id-only match.

## Build

```sh
rustup component add rustc-dev rust-src llvm-tools-preview --toolchain "$(cat rust-toolchain)"
RUSTC_BOOTSTRAP=1 rustc +$(cat rust-toolchain) \
  tools/unialloc-rustc-pass/unialloc-rustc-allocation-sites.rs \
  -o /tmp/unialloc-rustc-allocation-sites
```

## Run on a source fixture

```sh
SYSROOT="$(rustc +$(cat rust-toolchain) --print sysroot)"
DYLD_LIBRARY_PATH="$SYSROOT/lib" /tmp/unialloc-rustc-allocation-sites \
  --unialloc-type-map-out /tmp/unialloc-rustc-type-map.json \
  --unialloc-pass-log-out /tmp/unialloc-rustc-pass.log \
  -- --sysroot "$SYSROOT" --edition=2021 /tmp/input.rs
```

## Run through the real Cargo bench target

The evaluation wrapper builds the pass, runs `cargo clean -p unialloc` to avoid a
cache-only false positive, then compiles `std_bench` with `RUSTC_WRAPPER` and
`--emit=metadata` so benchmarks are not executed:

```sh
python3 evaluation/scripts/evaluate.py audit-rustc-driver-allocation-sites \
  --run-id rustc-driver-allocation-sites-std-bench-$(date +%Y%m%d%H%M%S)
```

Raw outputs land under `evaluation/raw/<run-id>/`, and the latest aggregate audit
is mirrored to `evaluation/results/rustc_driver_allocation_site_audit.json`.

The JSON output includes `lowering_status: "analysis_only"` on purpose. Claim-grade
C002 still requires a compiler-instrumented runtime benchmark run where these IDs
are actually passed into UniAlloc and correlated with allocation events.

## Current targeted real validation

The fastest implementation gate is the focused real-probe set below.  These
commands build the rustc_driver tool, compile real Rust programs through the MIR
rewrite path, run the resulting binaries, and validate a no-wrapper negative
control.  They should be run before spending time on any larger performance
matrix:

```sh
python3 evaluation/scripts/evaluate.py collect-semantic-metadata-validation-probe \
  --run-id semantic-metadata-validation-probe-real-20260707a \
  --timeout 300 \
  --no-update-results

python3 evaluation/scripts/evaluate.py collect-rustc-driver-mir-semantic-scope-probe \
  --run-id rustc-driver-mir-semantic-scope-probe-nowrapper-readfix-20260707a \
  --timeout 420 \
  --no-update-results

python3 evaluation/scripts/evaluate.py collect-rustc-driver-direct-allocator-mir-probe \
  --direct-local-size-align-with-semantic-drop \
  --run-id rustc-driver-direct-allocator-mir-probe-nowrapper-readfix-20260707a \
  --timeout 420 \
  --no-update-results

python3 evaluation/scripts/evaluate.py collect-rustc-driver-mir-cross-thread-hint-probe \
  --run-id rustc-driver-mir-cross-thread-hint-probe-nowrapper-readfix-20260707a \
  --timeout 420 \
  --no-update-results
```

On 2026-07-07 these gates passed on the repository/paper-pinned toolchain with
real runtime evidence:

- semantic metadata validation: `probe_validated=true`;
- semantic-scope MIR probe: `actual_semantic_scope_rewrite=true`,
  `runtime_probe_validated=true`, `typed_allocations=161`, and
  `typed_deallocations=161`;
- direct allocator MIR probe: `actual_allocator_call_replacement=true`,
  `direct_size_align_local_pairing_validated=true`, and
  `typed_allocations=80` / `typed_deallocations=80`;
- cross-thread hint MIR probe: `actual_semantic_scope_rewrite=true`,
  `cross_thread_recovery_hint_count=3`, and
  `runtime_probe_validated=true`.

On 2026-07-09 the same focused gate class also passed on the current local
`nightly` (`rustc 1.98.0-nightly (485ec3fbc 2026-06-10)`) through the
`unialloc_rustc_current` compatibility path.  The evaluator adds that cfg
automatically for modern nightly toolchains:

```sh
python3 evaluation/scripts/evaluate.py collect-rustc-driver-mir-semantic-scope-probe \
  --toolchain nightly \
  --run-id rustc-driver-mir-semantic-scope-probe-latest-nightly-20260709-post-cross-thread \
  --timeout 600

python3 evaluation/scripts/evaluate.py collect-rustc-driver-direct-allocator-mir-probe \
  --toolchain nightly \
  --run-id rustc-driver-direct-allocator-mir-probe-latest-nightly-20260709-post-cross-thread \
  --timeout 600

python3 evaluation/scripts/evaluate.py collect-rustc-driver-mir-cross-thread-hint-probe \
  --toolchain nightly \
  --run-id rustc-driver-mir-cross-thread-hint-probe-latest-nightly-20260709-currentpath-functions-spawn \
  --timeout 600
```

Current-nightly key results:

- semantic-scope MIR probe: `runtime_probe_validated=true`,
  `semantic_scope_rewrite_applied_count=116`, `typed_allocations=161`,
  `typed_deallocations=161`, and `fallback_allocations=0`;
- direct allocator MIR probe: `runtime_probe_validated=true`,
  `direct_allocator_rewrite_applied_count=36`,
  `direct_heap_object_solved_rewrite_applied_count=36`, and
  `runtime_layout_provenance_validated=true`;
- cross-thread hint MIR probe: `runtime_probe_validated=true`,
  `cross_thread_recovery_hint_count=4`,
  `arc_cross_thread_type_mapping_record_count=1`, and
  `recovery_identity_mismatches=0`.

The same day, a bounded real `std_bench` slice also passed with both semantic
scope and direct allocator rewriting enabled:

```sh
python3 evaluation/scripts/evaluate.py collect-rustc-driver-mir-semantic-scope-std-bench-runtime-smoke \
  --libtest-mode test-once \
  --bench-patterns 'binary_heap::bench_push,btree::map::find_seq_100,linked_list::bench_push_back' \
  --max-benchmarks 3 \
  --direct-allocator-rewrite \
  --direct-local-metadata-abi \
  --direct-local-size-align-with-semantic-drop \
  --run-id c002-std-bench-runtime-selected-direct-local-20260707a \
  --timeout 420 \
  --no-update-results
```

That artifact is intentionally non-claim-grade because it selects 3 of the 430
canonical `std_bench` benchmarks, but it is not a mock: it compiled the real
bench crate through rustc_driver MIR rewriting and reported
`actual_semantic_scope_rewrite=true`,
`actual_allocator_call_replacement=true`, and `runtime_returncode=0`.
The follow-up audit
`c002-std-bench-runtime-selected-direct-local-entrypoint-audit-20260707a`
also records conservative selected-entrypoint direct-rewrite counters.  In that
slice the whole compiled bench crate had 4 direct allocator rewrites, while the
3 libtest-selected benchmark entrypoints had 0 direct allocator rewrites; this
keeps compile-surface implementation evidence separate from runtime-selected
coverage.

## Claim-gate raw rewrite-map provenance

The C002 evaluator now treats the three small MIR companion probes as
fail-closed unless their audit JSON points back to a raw rewrite map under
`evaluation/raw/<run-id>/rewrites/`.  Summary booleans such as
`runtime_probe_validated=true` are not self-attesting: the corresponding
`target_rewrite.path` must load successfully, must have
`compiler_pass.kind=rustc_driver_optimized_mir_provider_override`, must identify
`optimized_mir` as the overridden rustc query, and must prove that cloned MIR
bodies were returned to rustc.

Each companion then validates the rewrite class it claims:

- semantic-scope probe: at least one applied semantic-scope rewrite from
  `unialloc/src/bin/rustc_driver_mir_semantic_scope_probe.rs`;
- direct allocator probe: at least one applied
  `actual_allocator_call_replacement_applied` row targeting a UniAlloc
  `with_metadata` ABI symbol from
  `unialloc/src/bin/rustc_driver_direct_allocator_mir_probe.rs`; and
- cross-thread hint probe: at least one applied
  `__unialloc_semantic_scope_push_hints` row with
  `cross_thread_recovery_hint=true` and
  `placement_hint_basis=auto_cross_thread_escape` from
  `unialloc/src/bin/rustc_driver_mir_cross_thread_hint_probe.rs`.

This is intentionally a gate, not a benchmark matrix.  It prevents handwritten
fixtures, copied summaries, or out-of-tree JSON from being promoted to
claim-grade type-isolation evidence while keeping the fast real rustc_driver
probes useful for implementation work.

## End-to-end source-span lowering fixture

For a stronger non-claim-grade check, the evaluation framework can use the
`rustc_driver` pass output to transform a generated fixture source: allocation
call spans discovered from optimized MIR are wrapped with
`__unialloc_semantic_scope_enter` / `__unialloc_semantic_scope_exit`, then the
fixture is compiled and run with `UniAlloc` as the global allocator.

```sh
python3 evaluation/scripts/evaluate.py collect-rustc-driver-lowering-fixture \
  --run-id rustc-driver-lowering-fixture-$(date +%Y%m%d%H%M%S)
```

A passing run proves that compiler-derived type IDs and callsite hashes can drive
real UniAlloc typed allocation/deallocation accounting. It is still not the final
paper-grade implementation because the transformation is a generated source-span
fixture, not an in-rustc MIR rewrite over the full `std_bench` surface.

## Selected real `std_bench` source-span lowering smoke

The evaluation framework can also validate the lowering bridge on a selected
real `std_bench` function without running the whole performance matrix:

```sh
python3 evaluation/scripts/evaluate.py collect-rustc-driver-std-bench-lowering-smoke \
  --run-id rustc-driver-std-bench-lowering-smoke-$(date +%Y%m%d%H%M%S) \
  --mir-functions 'btree::map::find_seq_100' \
  --bench-patterns 'btree::map::find_seq_100'
```

This command compiles `std_bench` through the `rustc_driver` pass, temporarily
rewrites selected source spans in the real benchmark source to call
`crate::__unialloc_rustc_driver_lowered_site`, runs only the selected benchmark
plus stats sentinels with layout auto metadata disabled, then restores the source
files. Typed allocation rows from module `0xC002_DA00_0000_0001` prove the
compiler-derived IDs reached UniAlloc; the typed rows are tagged with
`compiler-assigned-allocation-site-object-type-id-rustc-driver-source-span-lowered`
so the smoke cannot pass merely by seeing layout-derived or replayed IDs. The
audit still reports any fallback traffic because libtest itself can allocate;
fallback is not hidden or treated as claim-grade coverage.

## Full-surface source-span compile/list and runtime smoke

For implementation work, prefer the full-surface checks over slow timing
matrices.  They compile the real `std_bench` surface through the rustc_driver
pass, apply every safe source-span lowering that the pass marks eligible, and
restore the source files before exit:

```sh
python3 evaluation/scripts/evaluate.py audit-rustc-driver-std-bench-lowering-full-surface-compile \
  --run-id rustc-driver-std-bench-lowering-full-surface-$(date +%Y%m%d%H%M%S)

python3 evaluation/scripts/evaluate.py collect-rustc-driver-std-bench-lowering-smoke \
  --run-id rustc-driver-std-bench-full-surface-lowering-runtime-smoke-$(date +%Y%m%d%H%M%S) \
  --full-surface-lowering \
  --bench-patterns 'btree::map::find_seq_100,binary_heap::bench_push,linked_list::bench_push_back'
```

The pass output now carries an explicit `lowering_contract` and per-row
`lowering_eligible` / `no_instrument` fields.  Rows from the benchmark harness
and UniAlloc metadata helper functions are marked non-lowerable, and the
source-span lowering pipeline must honor that contract before rewriting.  This
prevents self-instrumentation of `crate::__unialloc_rustc_driver_lowered_site`
or its `Drop` guard, which can otherwise recurse through the allocator metadata
path.

These checks are intentionally non-claim-grade until production MIR rewriting and
full-surface runtime allocation-event coverage exist.  They are nevertheless real
compiler/runtime validation: the pass is a rustc_driver optimized-MIR pass, the
compiled target is the actual Cargo `std_bench` bench target, and the runtime
smoke disables layout auto metadata so any lowered-module typed rows must come
from compiler-derived scope metadata.  The MIR semantic-scope pass now solves
heap object identity primarily from `rustc_middle::ty::TyKind::Adt` on the call
destination and arguments, with textual callee-return parsing kept only as a
fallback.  Candidate calls whose heap object type is not solved are emitted as
explicit `semantic_scope_unsolved_heap_object_candidate` audit rows and are not
lowered into typed evidence.

Automatic cross-thread recovery hints are also typed, not merely body-wide.
When `--unialloc-auto-cross-thread-recovery-hint` is enabled, the pass inspects
the MIR-visible `thread::spawn`/scope call arguments and ORs the cross-thread
placement bit only into solved heap-object scopes whose object type appears in
that escape set.  If rustc exposes no typed upvar/argument information the pass
falls back to the older conservative body-level behavior, but the real
cross-thread probe now keeps two same-function negative controls: a local
`VecDeque` semantic allocation and a local direct `std::alloc` Layout
allocation.  Both must report zero cross-thread hint rows, preventing a mere
`thread::spawn` in the same MIR body from over-tagging unrelated allocations.

The runtime import audit reports two different notions of coverage.  Allocation
coverage (`runtime_coverage_percent`) measures how many dynamic allocations were
typed; pair coverage measures how many distinct compiler-map `(type_id,
callsite)` pairs appeared at runtime.  A selected smoke can legitimately reach a
high dynamic allocation coverage while covering only a few allocation-site pairs,
so claim-grade C002 still requires production MIR lowering plus a full-surface
runtime run rather than treating a high selected-smoke percentage as final.

## Direct allocator-call MIR replacement ABI modes

`unialloc-rustc-mir-rewrite-dry-run.rs` can also replace supported
`std::alloc::{alloc, alloc_zeroed, realloc, dealloc}` and explicit
`GlobalAlloc::{alloc, alloc_zeroed, realloc, dealloc}` MIR calls with UniAlloc's
metadata ABI when `--unialloc-actual-mir-rewrite` is set.  The default allocation
ABI records a recovery side-table entry so unmodified drops can still recover
compiler-emitted metadata.

The direct path solves heap-object identity from compiler MIR provenance, not
from runtime guesses.  In addition to `Layout::new`/`array`/`for_value(_raw)`,
`Layout::align_to`/`pad_to_align`, `Layout::from_size_align`, and
`Layout::extend`/`repeat` (including packed variants), the pass now carries the
same solved object type through the fallible success-path wrappers
`Result<Layout>::ok` and `Option<Layout>::expect`/`unwrap`.  That propagation is
intentionally narrow: fallback-producing methods such as `unwrap_or_else` are
left unsolved because they can synthesize a different `Layout`.

The provenance lookup also canonicalizes narrow MIR place spelling differences
before falling back to a callsite-only type id.  In real optimized MIR, the same
`Layout` object can appear as `_N`, `copy _N`, `(*_N)`, or a tuple projection
such as `((*_N).0: std::alloc::Layout)` after reference temps and
`Layout::extend(...).expect(...)` projections.  The pass normalizes those
reference/tuple wrappers and then reuses the original compiler-solved object
identity.  This is fail-closed: arbitrary wrapper/fallback calls are still
unsolved, but layout provenance is not lost merely because rustc changed the
debug spelling of a `Place`.

Focused validation for this path:

```sh
python3 evaluation/scripts/evaluate.py collect-rustc-driver-direct-allocator-mir-probe \
  --features stats,type_isolation \
  --run-id rustc-mir-place-canonicalization-recovery-backed-20260708b \
  --timeout 420 \
  --no-update-results
```

The probe source includes a real `Layout::extend(...).expect(...)` pair that is
borrowed and allocated through the `.0` projection after `pad_to_align()`.  The
rewrite audit proves both the alloc and dealloc rows use the same semantic object
type,
`std::alloc::Layout::extend(std::alloc::Layout::new::<u8>, std::alloc::Layout::new::<[u64; 2]>)`,
instead of `<unknown-heap-object-type>`.  The runtime summary reported
`runtime_probe_validated=true`, `runtime_layout_provenance_validated=true`,
`recovery_identity_matches=76`, and `recovery_identity_mismatches=0`.
On newer Rust lowerings the direct probe may contain no `exchange_malloc`
size/align candidate at all; the evaluator therefore requires Box/ShallowInitBox
evidence only when such candidates are present, while always requiring the
Layout ABI provenance rows that appear in the current MIR.

For paired compiler lowerings where allocation and deallocation/reallocation
are both rewritten with exact metadata, pass
`--unialloc-direct-local-metadata-abi` (or
`UNIALLOC_DIRECT_LOCAL_METADATA_ABI=1`).  This selects the `_local` Layout
allocation, reallocation, and deallocation variants, which avoid recovery
side-table traffic on the hot path.  Local realloc consumes any old recovery
record first, then intentionally does not install a new one.  Local dealloc uses
the exact compiler-supplied metadata directly and does not consult recovery
records.  Consequently, a direct-local no-recovery probe can legitimately report
`recovery_identity_matches=0`; the evaluator accepts that only when the same
audit proves applied local metadata ABI rewrites, local realloc/dealloc coverage,
no remaining recovery-backed rows that still need identity validation, zero
local pairing/contract gaps, row-by-row runtime layout provenance, and
`recovery_identity_mismatches=0`.  Recovery-backed probes still require a
positive recovery identity match.  Size/align `exchange_malloc` lowering
intentionally keeps the default
recovery ABI until the matching drop/deallocation can also be lowered with exact
metadata.  The pass emits `metadata_pairing_contract` per rewrite row plus
summary counters such as `direct_size_align_recovery_backed_unpaired_dealloc_count`
and `direct_local_size_align_metadata_contract_violation_count` /
`direct_local_size_align_pairing_gap_count`, so this
fail-closed decision is machine-checkable in real probe artifacts instead of a
README-only convention.

For the `Box`/`exchange_malloc(size, align)` case there is a separate explicit
opt-in:
`--unialloc-direct-local-size-align-with-semantic-drop` (or
`UNIALLOC_DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP=1`).  This implies direct
local metadata ABI plus semantic Drop-scope MIR rewriting in the same
`rustc_driver` compile.  In that mode, the pass may rewrite size/align
allocation to the `_local` ABI only when it also emits a matching Drop scope with
the same heap-object type identity (`mir-heap-object-type-v1`).  The audit row
must carry `metadata_pairing_contract:
local_metadata_abi_semantic_drop_scope`, and the probe gate requires
`direct_size_align_local_pairing_validated=true` with zero
`direct_local_size_align_metadata_contract_violation_count` and zero
`direct_local_size_align_pairing_gap_count`.  The latter is computed per
compiler-assigned heap-object type identity, so a global "one local allocation
and one unrelated Drop scope somewhere" count is not accepted, while legitimate
cross-function moves into a typed Drop/consumer function remain valid.  Without
this paired proof, size/align lowering remains recovery-backed by default.
Do not use this mode for paths that may still deallocate through ordinary
`GlobalAlloc::dealloc` without compiler-emitted metadata; those paths need the
default recovery ABI.

The evaluation wrapper exposes the same mode for targeted real probes:

```sh
python3 evaluation/scripts/evaluate.py collect-rustc-driver-direct-allocator-mir-probe \
  --direct-local-metadata-abi \
  --run-id rustc-driver-direct-local-allocator-mir-probe-$(date +%Y%m%d%H%M%S)

python3 evaluation/scripts/evaluate.py collect-rustc-driver-direct-allocator-mir-probe \
  --direct-local-size-align-with-semantic-drop \
  --run-id rustc-driver-direct-local-sizealign-paired-drop-$(date +%Y%m%d%H%M%S)
```

This is an implementation/probe path, not a paper performance matrix.  It should
be used to validate rustc-driver rewriting and allocator semantics before any
larger timing run.

The selected real-`std_bench` runtime smoke can also opt into this combined
direct-allocator/semantic-Drop mode:

```sh
python3 evaluation/scripts/evaluate.py collect-rustc-driver-mir-semantic-scope-std-bench-runtime-smoke \
  --libtest-mode test-once \
  --bench-patterns 'btree::map::find_seq_100' \
  --max-benchmarks 1 \
  --direct-allocator-rewrite \
  --direct-local-metadata-abi \
  --direct-local-size-align-with-semantic-drop \
  --run-id std-bench-direct-local-sizealign-paired-drop-smoke-$(date +%Y%m%d%H%M%S)
```

Selected/slice smokes are raw implementation evidence only and must not replace
the claim-grade `evaluation/results/rustc_driver_mir_semantic_scope_std_bench_runtime_smoke_audit.json`;
the evaluator only refreshes that results mirror when the runtime artifact is
full-surface validated.
