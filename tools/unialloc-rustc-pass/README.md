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

## Restrict a Cargo wrapper run to selected crates

Set `UNIALLOC_RUSTC_TARGET_CRATES` to a comma-separated allowlist when using
`unialloc-rustc-mir-rewrite-dry-run` as `RUSTC_WRAPPER` for a real application:

```sh
SYSROOT="$(rustc +$(cat rust-toolchain) --print sysroot)"
DYLD_LIBRARY_PATH="$SYSROOT/lib" \
UNIALLOC_RUSTC_TARGET_CRATES="my-app,my-helper" \
UNIALLOC_REWRITE_AUDIT_DIR=/tmp/unialloc-rewrites \
UNIALLOC_CONTINUE_COMPILATION=1 \
RUSTC_WRAPPER=/tmp/unialloc-rustc-mir-rewrite-dry-run \
cargo +$(cat rust-toolchain) build
```

The wrapper reads both `--crate-name NAME` and `--crate-name=NAME`. Cargo
normalizes package hyphens to crate-name underscores, so allowlist matching
trims whitespace and treats `my-app` and `my_app` equivalently. Matching remains
case-sensitive. A nonempty allowlist makes every unselected dependency, build
script, and rustc capability probe execute the original rustc with its original
arguments and exit status; that bypass creates or updates no rewrite audit or
pass log. An absent or empty allowlist preserves the existing all-crates
behavior.

For direct wrapper invocation, the equivalent option is
`--unialloc-target-crates my-app,my-helper` (the `=...` form is also accepted).
The environment variable is the intended Cargo integration because Cargo's
`RUSTC_WRAPPER` setting names an executable rather than an argument vector.

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

These checks are intentionally non-claim-grade because selected-entrypoint and
compile-surface evidence do not establish full-application runtime allocation-event
coverage.  They are nevertheless real compiler/runtime validation: the implemented
pass path is a rustc_driver optimized-MIR pass, the
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
so no selected smoke may be promoted to universal or whole-application coverage.
Static candidate/applied/fail-closed coverage, dynamic typed/total allocation-event
coverage, and adversarial isolation-behavior coverage retain separate denominators.

## Multi-crate module isolation identity

Commit `0704852` removes the compiler driver's previous single-module constant
for external crates.  The pass now derives a nonzero module identity from the
normalized crate name plus rustc's `-C metadata` disambiguator.  Direct rustc
invocations without metadata fall back to the canonical primary input path,
then to the full rustc argv only when no file identity is available.  The
in-tree `unialloc` probe retains its legacy module id for ABI compatibility.
The selected id and algorithm are written to both JSON audit and pass log.

Run the bounded actual-rewrite regression with:

```sh
python3 tools/unialloc-rustc-pass/test_mir_multicrate_module_isolation.py
```

Two distinct Cargo packages deliberately expose the same rustc crate name and
identical `Payload` source, producing the same compiler type id.  The run must
observe distinct module ids, wrong-module non-reuse, same-module reuse, and zero
recovery mismatch or corrupt slots.  A second no-metadata direct-rustc control
must also derive distinct ids from distinct primary inputs.  This proves one
bounded compiler/runtime module-cache boundary; it is not collision-free
cryptographic naming, whole-application coverage, a benchmark, or a performance
claim.

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
`rustc_driver` compile.  The raw size/align allocator call itself remains
recovery-backed: optimized MIR does not expose a sound pointer-to-owner link
that would authorize a no-recovery `exchange_malloc` rewrite.

The semantic allocation/Drop scopes may independently select the `_local`
scope ABI, but only for a deliberately narrow zero-alias proof.  The allocation
must write one unprojected owner local, every candidate of the same heap-object
type must follow one acyclic non-cleanup normal path, and that exact local must
reach its exact `Drop` without any intervening borrow, raw pointer, copy, move,
call argument, projection, overwrite, branch, loop, or early exit.  Any such
use makes the whole same-type group recovery-backed.  Cross-function ownership
transfer is therefore not accepted as local merely because a matching type is
dropped elsewhere.

`test_mir_direct_local_ownership_pairing.py` exercises actual two-crate
`RUSTC_WRAPPER` rewriting and runtime accounting.  It includes two same-type
owners with mixed local/escaping lifetimes, a conditional escape, a positive
unused-then-Drop owner, and a hidden `&mut owner` passed to a dependency that
uses `mem::replace`.  The adversarial paths must use recovery scopes and finish
with no raw deallocation lacking metadata; only the exact zero-alias owner may
use the local scope ABI.  The hidden-alias fail-first result was typed
allocation/deallocation `1/1`, fallback allocation/deallocation `1/1`, and
`raw_dealloc_no_metadata=1`.  At `26051b9`, the repaired hidden lane is typed
`1/2`, fallback `1/0`, and raw-without-metadata `0`; the positive aggregate is
typed `4/4`, fallback `0/0`, and raw `0`.  This is a bounded conservative
ownership check, not a general Rust escape analysis or universal ownership
proof.

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

### Multi-module application regression

`test_mir_realistic_multimodule_type_isolation.py` builds and runs a generated
Cargo application with separate ingest, transform, and storage modules through
the real `RUSTC_WRAPPER`/optimized-MIR rewrite path:

```sh
python3 tools/unialloc-rustc-pass/test_mir_realistic_multimodule_type_isolation.py
```

The gate requires four actually applied allocation scopes spanning `String`,
`Vec<u8>`, and `Box<[u8]>`, four distinct nonzero allocation callsites, and one
actually applied `String::into_bytes` ownership transfer.  Runtime rows must
match those compiler identities.  Its two address oracles require transferred
String storage to reject the old String identity and accept the exact Vec
identity, and same-layout Box storage to reject a Vec identity and accept the
exact Box identity.  Fallback, raw-without-metadata, recovery-mismatch,
corruption, and dropped-stat counters must remain zero.

- **Current closure (`c477339`, `6640305`, `95d3d8a`, `cfd887e`, `6b0747e`).**
  Redox/Linux tests type-check with portable `mincore`, but Redox runtime still
  needs an external linker/runner; cross-thread wrong-layout deallocation and
  reallocation preserve their record until exact retry. Reallocation rejects
  before zero-size, active, auto, quarantine, stats, or raw dispatch while
  Missing fallback remains. The current generated app proves exact `Box<[u8]>`,
  Box/Vec wrong-type non-reuse, exact Box reuse, and zero raw/fallback/mismatch/
  corrupt while `Box<[String]>` stays audit-only. This is bounded functional
  evidence, not universal-app, paper-percentage, or performance evidence; Oxipng
  `19ffb71` remains stale and is not rebound.

The same run also enables automatic cross-thread placement with no manual
placement value.  A `Vec<u8>` allocated in the MIR body that performs a real
`thread::spawn(move || ...)` must carry placement bit `0x8000` with basis
`auto_cross_thread_escape`, while a same-type/same-layout local helper remains
placement `0` with basis `default`.  A post-thread-creation runtime window then
requires cross-to-local non-reuse plus exact reuse in both placement classes;
its fallback/raw/mismatch/corruption counters remain zero.  A non-executed
spawn-shaped branch supplies the second automatically tagged allocation needed
for the exact cross-class reuse check without adding thread-runtime allocations
to that diagnostic window.

This is a single generated multi-module application and a bounded trusted-
metadata isolation regression.  It is not arbitrary external-application
coverage, a universal memory-safety proof, a benchmark, or a paper performance
claim.  The `module_id` remains crate-scoped; Rust source-module separation is
demonstrated by the applied MIR function paths and distinct callsites rather
than by claiming a per-source-module `module_id`.

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

## Companion runtime and real-application evidence boundaries

The compiler pass relies on runtime identity that survives policy control
changes.  Commit `6603460` preserves allocation-time metadata and coherent
configuration generations for global, thread-local, and layout-derived auto
metadata; it also prevents pre-enable raw pointers and unrecorded old realloc
pointers from inheriting a later policy.  Hosted `stats,type_isolation` tests
pass `720/720`, and fixed-heap tests pass `606/606`.  These are runtime lifecycle
checks, not compiler-coverage or performance denominators.

Windows readiness is also narrower than runtime validation.  Commit `f5c4935`
provides symmetric FLS PAL access, `df5f449` makes production `GlobalTcache`
fiber-local, and `f238f10` separates full current-owner teardown from a narrow
retained-cache drain for non-current fiber deletion.  Host regressions pass
`1/1` retained-drain and `69/69` thread-cache tests; the Windows GNU
cross-target type-check/cross-build and a Zig-linked test executable no-run
pass.  The fiber deletion and thread-exit regressions still require a real
Windows or Wine run, so this is compile/link readiness rather than Windows
runtime evidence.

The Oxipng run at `15d892e` is pre-`26051b9` ownership hardening.  Its exact
source-bound counts remain historical evidence and must not be rebound to the
current compiler pass.

The post-hardening one-shot application run is bound to source
`af342f7e26dc4a5e132acc18d7f7a450009e6517`; build/run return codes are both
zero and the output SHA-256 is
`565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`.
Its separate static categories are 6 direct, 843 semantic, and 278 Drop applied
rewrites plus 267 fail-closed candidates (265 multi-owner Drop and 2 semantic).
The runtime window reports 1061 typed out of 1070 allocation events and 9
fallback allocations; `9915 bp` is only that diagnostic event ratio.  The
injected address oracle reports wrong-type non-reuse, same-type reuse, mismatch
before/after `0/0`, and zero corruption.

The full workload nevertheless records 67 recovery identity corrections.  The
runtime uses the allocation-time record, so this is fail-closed
`recovery_corrected_non_exact` attribution, not allocator corruption and not an
exact whole-application compiler-pairing result.  It is distinct from both the
oracle window and the 267 static fail-closed candidates.  Validator `3dc1039`
only replays the preserved artifacts after separating those windows; it does
not rerun Oxipng.  See
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-typeiso-af342f7-20260712a/posthoc-preserved-run-validation.json`.
Static rows, runtime events, the bounded oracle, and whole-run corrections must
retain separate denominators; none is universal coverage or publication-grade
performance evidence.

Current pass commit `04b8108` is newer than that one-shot evidence.  It removes
the non-Clone first-owner shortcut: receiver-owned calls inspect only MIR
argument zero, other calls inspect only their destination, and the existing
bounded supported-owner scan of that selected receiver/destination must produce
exactly one owner before scope lowering; multi-owner or unresolved cases fail
closed.
`tools/unialloc-rustc-pass/test_mir_nonclone_multi_owner_fail_closed.py` drives
the real wrapper/pass over both an aggregate `(Vec<u8>, String)` factory and
`Vec<String>::resize`; each must produce one ambiguous audit-only row and no
applied/planned semantic scope while the application result remains correct.
That `04b8108` hardening round did not rerun Oxipng, so its `af342f7` rows were
not rebound to that source.

Commit `427583bc69b7fcba09f1c7d6b464b8bd7a9c68b7` extends that safety gate to
hidden owners in current-rustc custom-ADT fields and to heap owners consumed by
value.  The selected receiver or destination remains the attribution source;
by-value arguments are merged only as a conflict check.  A distinct owner or an
unresolved graph fails closed, while a duplicate same owner remains eligible
and is still actually rewritten.  Minimized actual-`RUSTC_WRAPPER` regressions
reproduced one recovery-identity mismatch before the fix for the hidden-ADT
case and for the conflicting consumed factory/receiver cases; the fixed paths
emit no semantic scope and report mismatch `0`.  Same-owner factory and
receiver positive controls remain applied with mismatch `0`.  The pinned pass
tests pass `15/15`, and the repository pre-commit suites pass 652 UniAlloc plus
430 std-bench tests.

A new one-shot instrumented Oxipng v4.0.3 build/run is bound directly to that
commit and `nightly-2022-07-01`.  Source binding is clean
(`scoped_status=""`), with scoped fingerprint
`5f36c0a7f1bad4284071cd3a8f6d50bb7a894282e5f76726e6f2b095d5bc49e8` and
pass-source SHA-256
`aedef38625f6096e3f5875b35f3d89f839709ac3277d7c79e4df6756da8a1373`.
The offline build and single functional run return zero and reproduce output
SHA-256 `565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`.
Its target-crate MIR audit reports 6 direct rewrites, 383 applied semantic
scopes, 320 applied Drop scopes, and 581 explicitly fail-closed candidates; the
runtime reports 64 type rows and zero corrupt slots.  The bounded injected
address oracle passes with wrong-type non-reuse, same-type reuse, zero oracle
recovery mismatch, and zero corruption.

The same current run records 13 whole-run recovery corrections, so its
whole-application status is still `recovery_corrected_non_exact`, not exact
compiler identity pairing.  The 13 are not evidence that any particular subset
of the older `af342f7` run's 67 corrections was repaired: those historical
events cannot be attributed to `427583b` or rebound to its source.  This is one
pinned, instrumented functional run, not an unmodified application, benchmark,
performance result, or publication-grade coverage percentage.  The durable
summary is
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-typeiso-current-427583b-20260712a/oxipng-realapp-repro-summary.json`.

Commit `572bfab` is a later, method-specific coverage refinement and is not
rebound to that Oxipng run.  Only the six capacity-only `Vec` receiver methods
(`reserve`, `reserve_exact`, `try_reserve`, `try_reserve_exact`, `shrink_to`,
and `shrink_to_fit`) may use the direct outer `Vec` identity when the receiver
ADT path is exactly `std::vec::Vec` or `alloc::vec::Vec`; element-affecting
methods and `Drop` keep the full owner-graph fail-closed rule.  A current-rustc
`Vec<String>` versus same-layout `Vec<Vec<u8>>` regression observes distinct
nonzero compiler/runtime identities, wrong-type non-reuse, same-type exact
recovery, one cache hit, and zero mismatch/corruption, while
`Vec<String>::resize` remains ambiguous.  This is current-source bounded
capacity-path evidence, not updated Oxipng coverage or performance evidence.

### Exact `VecDeque` capacity outer-owner regression

Commit `c02baa6` adds an actual-`RUSTC_WRAPPER` regression on both the current
and pinned toolchains for exact std-owned `VecDeque::with_capacity` and
`reserve_exact` paths.  The runtime oracle reports typed
allocation/deallocation `4/4`, blocks wrong-type reuse, permits exact-type
reuse, and keeps fallback, raw allocation/reallocation/deallocation,
recovery-mismatch, and corrupt-slot counts at zero.  `push_back`, a custom
same-name helper, and multi-owner `Drop` remain fail closed.  This is bounded
diagnostic functional evidence, not universal container/compiler coverage or
performance evidence.

### Latest accepted Oxipng rewrite and isolation one-shot

Commit `19ffb71` is the latest accepted source-bound external-application
checkpoint in this evidence sequence.
The pinned Oxipng v4.0.3 application and `nightly-2022-07-01` were built once
and invoked once, with no retry or timing loop.  Start and end source bindings
both equal `19ffb710752466a140067034650190dfdad60328`; the scoped tree is clean and
the fingerprint is
`13f72a5a3a5cd02657ae89500ea165c5238e9128c8f75c8e88aaba5158f3722f`.
Build and run return `0/0`, and the output SHA-256 is
`565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`.

The target-crate audit records direct/scope/Drop rewrites `6/310/320`,
ownership-transfer candidate/applied/selected rows `12/12/12` with nonzero
selected identities, and fail-closed semantic/Drop rows `516/119` (including
117 multi-owner Drop rows).  The runtime window records typed allocation/
deallocation `871/860`, fallback allocation/deallocation `199/160`, dynamic
transfer `3/1/2`, 53 type rows, and corrupt/dropped counts `0/0`.  The injected
oracle proves wrong-type non-reuse and exact same-type reuse for one bounded
same-layout address sequence.  The whole run retains one recovery correction,
so its status is `recovery_corrected_non_exact`, not exact whole-application
pairing.

The accepted artifact is
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-head-19ffb71-20260712-one-shot/`;
acceptance SHA-256 is
`efa66ec14e491f27d5549d94d4a99761bc900ebaf57357c218833715778e9a2f`.
This is functional/diagnostic evidence for an instrumented pinned application,
not timing, performance, universal coverage, natural-application isolation, or
whole-program exact-pairing evidence.

### Current-source fail-closed provenance and cache-integrity boundary

The current code-bearing checkpoint `3ccd464` is newer than the accepted
`19ffb71` Oxipng artifact. Commit `9a02767` keeps arbitrary local, platform,
and dependency factories audit-only when a supported-looking return type is
the only provenance. Exact
`Box::new` and `String::with_capacity` are rewritten only after alloc/std
DefId/path and destination/argument checks; custom same-name and allocator-
specific forms remain fail closed.  The current and pinned-toolchain opaque
dependency probes report zero caller-attributed typed allocations for direct
and `Result` factories, while the exact `Vec::with_capacity` control still
pairs its typed allocation and Drop.
The `ab98075` end-to-end type-isolation security validator now checks those
audit-only rows explicitly: its current-toolchain run validates 86 unresolved
rows (including 8 `producer_box` and 4 `consumer_box` opaque-factory callsites)
while retaining 12/12 exact inner typed allocation/deallocation events,
wrong-type non-reuse, exact-type reuse, and zero mismatch/corrupt-slot events.

That revision also makes the nested-unwind probe an exact receiver proof: an
outer `Vec::extend` scope remains at depth `1` after a caught inner
`Vec::extend_from_slice` panic and returns to depth `0` only after the outer
call, with the outer allocation/Drop identity paired.  The direct-local hidden
replacement regression intentionally leaves a cross-crate replacement pointer
raw when no allocation recovery record exists; the pass/runtime must not
fabricate typed attribution from a later surrounding scope.

Commit `3ccd464` separately authenticates every occupied metadata-segregated
cache entry before identity matching, reuse, accounting, or full-bucket
eviction projection.  The keyed structural authenticator covers the lookup
key, full callsite-agnostic identity, policy, pointer, layout, optional
PAC/software-auth state, and metadata.  Forced inline/materialized identity-key
collisions miss, and discriminator/auth downgrade, null/non-null pointer,
size/alignment, and full-bucket candidate tampering fail stop in hosted and
`fixed_heap` regressions.  This is bounded compiler/runtime safety evidence,
not universal factory coverage or arbitrary-memory-corruption protection.

The accepted Oxipng one-shot remains source-bound to
`19ffb710752466a140067034650190dfdad60328`; it is stale relative to current
development source and is not rebound to either hardening commit. Consequently
there is no current-source external-application count, performance result, or
publication-grade claim here.

### Exact `HashSet::with_capacity` outer-table regression

`test_mir_hashset_with_capacity_outer_owner.py` drives an ordinary Cargo
application through the actual `RUSTC_WRAPPER` on the selected toolchain.  It
proves only exact std-owned `HashSet::<T>::with_capacity` with one `usize`
argument, `RandomState`, and the optional current-rustc `Global` allocator.
Nested heap owners inside `T` are not identities for the empty table allocation.
`with_capacity_and_hasher`, `with_hasher`, allocator-specific constructors,
hashbrown `HashSet`, `IndexSet`, and local same-name helpers remain fail closed.

```sh
python3 tools/unialloc-rustc-pass/test_mir_hashset_with_capacity_outer_owner.py
UNIALLOC_RUSTC_TOOLCHAIN=nightly-2022-07-01 \
  python3 tools/unialloc-rustc-pass/test_mir_hashset_with_capacity_outer_owner.py
```

The oracle checks distinct compiler identities, wrong-identity cache miss, exact
identity cache hit, and zero fallback/raw/mismatch/corrupt counters.  Stable
`HashSet` exposes no deterministic raw-table address, so this is bounded
identity-directed cache-selection evidence, not universal address behavior,
external-application coverage, or performance evidence.

### Recovery/TLS fail-closed fixes and exact `String::from(&str)`

Commit `79d0184` makes `GlobalAlloc::dealloc` treat a live recovery record with
a different valid caller `Layout` as a mismatch, not as missing. It returns
before raw/fallback/cache effects, preserves the authoritative record, and
accepts a later exact-layout retry. Commit `61233b6` publishes the Unix fast TLS
pointer only after pthread destructor ownership and `pthread_setspecific`
succeed; an injected save failure leaves it invisible and returns it for
reclamation. These are bounded correctness results, not performance claims.

`test_mir_string_from_str_outer_owner.py` drives an ordinary Cargo application
through the actual `RUSTC_WRAPPER` on the current toolchain and
`nightly-2022-07-01`. Commit `08a1bbf` rewrites only exact core `From::from` with
alloc `String` destination and one immutable `&str` source. Other `From`/source
shapes and arbitrary String factories remain audit-only and fail closed.

```sh
python3 tools/unialloc-rustc-pass/test_mir_string_from_str_outer_owner.py
UNIALLOC_RUSTC_TOOLCHAIN=nightly-2022-07-01 \
  python3 tools/unialloc-rustc-pass/test_mir_string_from_str_outer_owner.py
```

Both runs report typed allocation/deallocation `3/3`, wrong-type non-reuse,
exact reuse, and zero fallback/raw, recovery-mismatch, or corrupt-slot events.
This is bounded functional evidence, not universal `String`, external-app, or
performance evidence. Oxipng was not rerun; the accepted `19ffb71` artifact
remains stale and cannot be rebound.

### Current Oxipng one-shot and exact `Vec<u8>::from(String)`

The source-bound Oxipng v4.0.3 one-shot at
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-head-d50795f-20260713-one-shot/`
was built and run successfully with source `d50795f892bd38ca3e7fd083da7d6eacafd0db06`;
its summary SHA-256 is
`e8958683d1957c40f76e4400b25c506b16d70489e9aa2526b1bb5674411d3635`.
The pass reports 6 direct rewrites, 236 semantic scopes, 320 Drops, and 12/12
ownership-transfer candidates/applied. Runtime reports typed allocation/
deallocation `856/846`, 808 cache hits, fallback allocation/deallocation
`214/174`, and a passing injected type-isolation oracle. The same summary also
records 592 unresolved candidates, one recovery-corrected mismatch, and
`whole_program_compiler_coverage=false`; these counts are functional/diagnostic,
not timing, universal coverage, whole-app exact pairing, or performance claims.
Later commits do not rebind the artifact.

Commit `003704a` extends the existing exact String-to-bytes ownership proof to
the exact core `From::from` monomorphization whose concrete types are
`[Vec<u8, Global>, String]`. Run the current and pinned actual-wrapper probes:

```sh
python3 tools/unialloc-rustc-pass/test_mir_vec_from_string_rebind.py
python3 tools/unialloc-rustc-pass/test_mir_vec_from_string_rebind.py \
  --toolchain nightly-2022-07-01
```

Both report candidate/applied `1/1` and runtime attempted/applied/rejected
`1/1/0`, preserve pointer/payload/capacity, reject wrong-String reuse, allow
exact-Vec reuse, and keep fallback/raw/mismatch/corrupt counters at zero.
Reference, generic, and custom-allocator forms remain fail closed. The dynamic
fixture does not claim explicit `From<&str>` or custom-allocator coverage.
Commit `1827e4f` independently makes a mismatched recovery `Layout` in the local
compiler realloc ABI fail before mutation while preserving exact retry.
