# Resolved Root-Cause Disposition of RustSec Boundary Cases

Date: 2026-07-15

## Resolution

The original review screened **53 candidate advisories**. Post-source audit
removes RSH-006 from the heap allocator efficacy denominator: its dangling
target and vulnerability-relevant lifetime path are stack-local and have no
`GlobalAlloc`-mediated allocation, reclaim, or reuse edge. The corrected
heap-relevant scope therefore contains **52 cases**.
Forty-eight have executable vulnerable witnesses and controls. RSH-054,
RSH-056, RSH-059, and RSH-073 retain evidence-backed audit-only exclusions.
The frozen 49-case execution ledger retains RSH-006 as historical audit
provenance; presentation and efficacy figures apply the hash-bound scope
correction in
`evaluation/config/rustsec_heap_posthoc_scope_corrections.json`.

Allocator mechanisms provide qualified coverage for **43/48** executable
candidates: 31 exact duplicate-reclaim detections, 1 exact recovery-layout
validation, and 12 compiler-bound causal mitigations of measured cross-identity
reuse edges, with RSH-002 counted once. Eleven Type Isolation rows provide the
case's only qualified allocator-mechanism evidence; RSH-002 overlaps with a
separate `reclaim_checks` detection. Three cases have completed no-signal
results, and two remain outside the evaluated mechanism
contracts.

The former inconclusive queue now has these root-cause-specific dispositions:

- exact-reclaim detections for RSH-001 and RSH-060;
- exact recovery-layout validation for RSH-031;
- bounded Type Isolation reuse-edge mitigations for RSH-008, RSH-052, RSH-055,
  RSH-064, RSH-065, RSH-066, RSH-067, RSH-068, and RSH-069;
- completed no-signal results for RSH-049, RSH-050, and RSH-075;
- a post-source-audit stack-only scope exclusion for RSH-006; and
- allocator-mechanism boundaries for RSH-003 and RSH-019.

RSH-002, RSH-041, and RSH-042 complete the 12-row Type Isolation set. RSH-008
and RSH-041 use `derived_vulnerability_edge` scope; the other ten use
`source_shaped_derived` scope. None covers `published_source`. Automatic
source-level vulnerability detections remain **0/12**. Every Type Isolation row
records `source_vulnerability_detection_validated=false`,
`vulnerability_specific_detection_signal=false`, and `claim_grade=false`.

The editable presentation figures and their exact case-membership table are in
`docs/figures/rustsec-security-sets-20260715/`. The overview figure shows the
full corrected heap scope and the UAF subset. The UAF denominator becomes
**17 executable cases** after the stack-only correction: 14 have qualified
Type Isolation or reclaim-check evidence, two are same-object/pre-reuse
concurrency boundaries, and one is a foreign-allocator no-signal control.

## Evidence rules

An exact-reclaim detection requires all of the following:

1. the vulnerable system baseline reproduces in every repetition;
2. the feature-matched plain arm preserves the vulnerable behavior;
3. `reclaim_checks` emits the exact allocator diagnostic in every vulnerable
   repetition;
4. matched patched system, plain, and check controls remain valid; and
5. allocator and Cargo-feature provenance match the declared mechanism.

A recovery-layout validation requires source-level ground truth for the
allocation/deallocation mismatch, the exact allocator diagnostic in every
vulnerable treatment repetition, signal-free matched patched controls, and a
diagnostic bound to the recorded and submitted layouts at the same pointer.

A Type Isolation causal reuse-edge mitigation requires all of the following:

1. vulnerable system and `typed_plain` arms reuse the witness's reclaimed
   address in every repetition;
2. `typeiso` withholds that address in every repetition;
3. the stats record's `last_wrong_identity_retained_ptr` equals the harness's
   parsed `original` or `stale_value` pointer;
4. preregistered requested and victim contracts each bind one unique applied
   compiler-audit candidate;
5. runtime requested/retained identities, modules, requested callsite, size,
   and alignment join those compiler candidates; and
6. patched system, `typed_plain`, and `typeiso` controls exercise safe
   same-identity functionality without a matching attribution event.

Separate allocator unit controls preserve safe same-identity reuse and verify
that an eligible cross-identity cache entry is withheld. RSH-052 adds an exact
`String` allocation origin followed by an audited pointer-preserving
`String -> Box<str>` ownership transfer. RSH-068 and RSH-069 preserve identity
from their allocation origins through the relevant `String` and `CString`
ownership chains. RSH-065's reserve-relocation reduction and RSH-069's groomed
FFI edge retain explicit synthetic-reduction caveats.

The attribution fields are `stats`-gated evaluation telemetry. The selected
retained pointer uses one companion atomic and leaves cache identity layouts and
the public semantic-stats snapshot unchanged. Production enforcement remains
the ordinary exact-identity cache decision. The completed paired
`typeiso_perf / typed_plain` ratio-of-ratios screen passed its merge gates:
Oxipng wall time changed **-1.6649%** and peak RSS changed **+0.583%**; the long
Ripgrep workload changed **+0.8107%** in wall time (95% bootstrap CI
**[-0.180%, +1.399%]**) and retained the same **7168 KiB** maximum RSS. The
gates require wall-time regression at or below 2%, Oxipng RSS regression at or
below 1%, and unchanged Ripgrep maximum RSS. Aggregate semantic-rewrite counts
were unchanged in these selected macros, so active new-site evidence comes
from the 12 RustSec matrices and compiler regression tests. Evidence is under
`docs/evidence/rustsec-typeiso-automatic-20260715/performance-screen/`.

A no-signal result is a completed feature-matched negative for one integrated
scenario. A result remains evidence-inconclusive when matched abnormal
termination lacks the normalized source-bound fault/checkpoint fingerprint
required for scenario-local attribution. A mechanism boundary identifies a
vulnerability whose causal requirement lies outside allocation reuse,
recovery-layout validation, or exact-address duplicate reclaim.

The fail-closed implementations of these rules live in
`evaluation/scripts/export_rustsec_mechanism_results.py`,
`evaluation/scripts/run_rustsec_complete_scope.py`, and
`evaluation/scripts/run_rustsec_heap_experiment.py`.

## Per-case root cause and final disposition

| Case | Root-cause event chain | Allocator-visible edge | Final disposition |
| --- | --- | --- | --- |
| RSH-001 (`chttp`) | A conversion constructs a `Vec` from storage still owned by a temporary `Box<[u8]>`. The temporary releases the pointer, and the returned `Vec` later releases the same pointer. The integrated source is `evaluation/harnesses/rustsec_heap/RSH-001/main.rs`. | One allocation followed by two reclaims of the same address. | **Exact-reclaim detection.** `RSH-001-upstream` produces the bound `reclaim_checks` diagnostic in 3/3 vulnerable check runs with matched plain and patched controls. Evidence is in `docs/evidence/rustsec-rsh001-reclaim-20260714/`. |
| RSH-003 (`internment`) | Concurrent `ArcIntern` operations race reference-count decrement, container removal, destruction, and access to the same `RefCount<T>`. The stress witness is `evaluation/harnesses/rustsec_heap/RSH-003/main.rs`. | Same-object concurrent UAF can occur before any replacement allocation; later reuse can carry the same concrete identity. | **Allocator-mechanism boundary.** This case requires synchronization, race detection, generation validation, or another temporal-access mechanism. |
| RSH-006 (`arc-swap`) | `MapGuard` retains a pointer into a temporary stack guard, followed by stack overwrite and dereference. The witness is `evaluation/harnesses/rustsec_heap_expansion/RSH-006/main.rs`. | The dangling target and vulnerability-relevant lifetime path are stack storage. They have no `GlobalAlloc`-mediated allocation, reclaim, or reuse edge. | **Post-source-audit scope exclusion.** The case remains in the frozen execution audit and is removed from heap allocator efficacy denominators. A MIR move-after-borrow or self-reference detector would be a separate compiler mechanism. |
| RSH-008 (`lru`) | The published iterator signature permits a `String` reference to outlive the cache entry reclaimed by `pop`. The derived adapter requests an equal-layout, different-identity `Replacement` after reclaim in `evaluation/harnesses/rustsec_heap/RSH-008/derived_reuse.rs`. | The source path contains a stale reference. The derived path isolates a 48-byte, align-8 `LruEntry<u32, String> -> Replacement` reuse decision. | **Compiler-bound causal mitigation of the measured derived reuse edge.** The generic `Box<T>` runtime identity binds the victim with compiler-supplied identity. System and `typed_plain` collide in 3/3 runs; `typeiso` withholds the exact witness pointer in 3/3 runs; patched controls validate safe same-identity reuse. The published source row remains separate. |
| RSH-019 (`oneshot`) | Sender destruction and receiver poll/drop race over channel state and the stored waker. The witness is `evaluation/harnesses/rustsec_heap/RSH-019/main.rs`. | The same-object race and temporal access precede reuse. | **Allocator-mechanism boundary.** Exact identity cannot create the missing happens-before relation. A concurrency-aware mechanism remains required. |
| RSH-031 (`maligned`) | The vulnerable `Vec<u8>` path allocates 1,009 bytes at alignment 256 and later submits alignment 1 at Drop. The pinned upstream Miri witness establishes this mismatch. | The same pointer reaches deallocation with a recorded/requested layout mismatch. | **Exact recovery-layout validation.** The derived matrix emits `unialloc_recovery_deallocation_layout_mismatch` in 3/3 vulnerable `typed_plain` and `typeiso` runs and 0/3 patched runs. This policy-independent row contributes zero Type Isolation credit. Evidence is in `docs/evidence/rustsec-rsh031-layout-20260715/`. |
| RSH-049 (`libflate`) | Panic unwinding drops fabricated uninitialized reader state and attempts to free a junk pointer from an uninitialized `Box` field. The witness is `evaluation/harnesses/rustsec_heap_expansion/RSH-049/main.rs`. | The first observed reclaim targets an address with no authoritative allocation publication. | **Completed no-signal control.** Current `reclaim_checks` covers exact duplicate reclaim. Strict invalid-free rejection requires a complete live-allocation provenance contract, including foreign and pre-tracking pointers. |
| RSH-050 (`image`) | The vulnerable HDR reader sets the output vector length before the truncated scanline fails, so unwinding drops an uninitialized `Box`. The witness is `evaluation/harnesses/rustsec_heap_expansion/RSH-050/main.rs`. | The first reclaim is derived from uninitialized payload state rather than a second reclaim of a known live allocation. | **Completed no-signal control.** The six matched arms reproduce the source oracle and controls without the exact duplicate-reclaim diagnostic. Initializedness or complete allocation-provenance validation defines the required extension. |
| RSH-052 (`string-interner`) | `Clone` deep-copies strings while copied raw map references still point into the old interner. Dropping the old interner releases the strings before the clone reads them. The source-shaped derived adapter is `evaluation/harnesses/rustsec_heap_expansion/RSH-052/derived_reuse.rs`. | The source path has one valid reclaim followed by a stale read. The derived path creates a 64-byte `Box<str> -> Replacement` cross-identity reuse edge. | **Source reclaim scenario: completed no-signal control. Derived scenario: compiler-bound causal mitigation of the measured reuse edge.** An exact `String` allocation and audited pointer-preserving `String -> Box<str>` transfer bind the victim with compiler-supplied identity; the matrix withholds the exact witness pointer in 3/3 `typeiso` runs with valid controls. |
| RSH-055 (`heapless`) | `IntoIter::clone` clones a consumed inline slot after `next`; dropping the consumed element frees its one-word `Vec<u64>` payload before the clone reads that slot. The derived adapter is `evaluation/harnesses/rustsec_heap_expansion/RSH-055/derived_reuse.rs`. | One valid reclaim leaves a stale payload pointer; the derived path inserts an equal-layout eight-byte `Replacement`. | **Source reclaim scenario: completed no-signal control. Derived scenario: compiler-bound causal mitigation of the measured reuse edge.** Authenticated primitive `slice::to_vec` binds the `Vec<u64>` victim; the six-arm matrix records 3/3 plain collisions and withholds the exact witness pointer in 3/3 Type Isolation runs. |
| RSH-060 (`toodee`) | The original witness uses an invalid `ExactSizeIterator` contract that exposes uninitialized storage. The alternate advisory path yields part of the requested input and then panics, leaving duplicated ownership that reclaims the same pointer twice. Sources are `evaluation/harnesses/rustsec_heap_expansion/RSH-060/main.rs` and `evaluation/harnesses/rustsec_heap_expansion/RSH-060/partial_panic.rs`. | The original path is initializedness/OOB. The partial-yield path contains two reclaims of the same address. | **Original scenario: completed reclaim no-signal control. Partial-yield scenario: exact-reclaim detection.** `RSH-060-partial-panic` emits the exact allocator diagnostic in 3/3 vulnerable check runs with matched controls. Evidence is in `docs/evidence/rustsec-rsh060-partial-panic-20260714/`. |
| RSH-064 (`neon`) | Neon exposes a Rust `Vec<u8>` backing store as a V8 external `ArrayBuffer`, releases the `Vec`, and JavaScript retains a stale view. The host witness is `evaluation/harnesses/rustsec_heap_expansion/RSH-064/addon.rs`; the derived adapter is `evaluation/harnesses/rustsec_heap_expansion/RSH-064/derived_reuse.rs`. | The source path has one Rust reclaim followed by an FFI read. The derived path isolates a four-byte `Vec<u8>` backing-store-to-`Replacement` reuse edge. | **Source Type Isolation row: inconclusive full-source coverage. Derived scenario: compiler-bound causal mitigation of the measured reuse edge.** Primitive `slice::to_vec` binds the derived `Vec<u8>` victim with compiler-supplied identity; the matrix records 3/3 system/plain collisions, 0/3 Type Isolation collisions, and 3/3 exact retained-pointer matches. |
| RSH-065 (`mail-internals`) | `vec_insert_bytes` computes an insertion pointer before `Vec::reserve`; relocation can reclaim the old buffer before a copy through that pointer. The adapter models the relocation in `evaluation/harnesses/rustsec_heap_expansion/RSH-065/derived_reuse.rs`. | The synthetic reduction isolates a four-byte old-buffer-to-`Replacement` collision before the stale copy. | **Source reclaim scenario: completed no-signal control. Derived scenario: compiler-bound causal mitigation of the measured reuse edge.** Primitive `slice::to_vec` binds the `Vec<u8>` victim. This row retains an explicit synthetic, manually modeled reserve-relocation caveat. |
| RSH-066 (`rust-i18n-support`) | `AtomicStr::as_str` returns an unguarded reference; replacing the value and dropping the returned old `Arc<String>` reclaims the backing buffer while the reference remains live. | The derived path isolates a 4096-byte stale String-buffer-to-`Replacement` collision. | **Source reclaim scenario: completed no-signal control. Derived scenario: compiler-bound causal mitigation of the measured reuse edge.** The source-shaped exact `String` site is compiler-bound and moved into `AtomicStr`; the matrix withholds the exact witness pointer in every treatment repetition. |
| RSH-067 (`openssl`) | `select_next_proto` ties the returned slice lifetime only to the client list even though OpenSSL can return a pointer into the server buffer. Dropping the server buffer leaves the selected slice stale. | The derived path isolates a 64-byte server-buffer-to-`Replacement` collision before the stale read. | **Source reclaim scenario: completed no-signal control. Derived scenario: compiler-bound causal mitigation of the measured reuse edge.** Primitive `slice::to_vec` binds the dropped server-buffer owner with compiler-supplied identity. |
| RSH-068 (`pared`) | `Parc::project` can retain a projection into an external `String`; dropping that String leaves the projection stale. | The derived path isolates a 64-byte String-buffer-to-`Replacement` collision before the stale projection read. | **Source reclaim scenario: completed no-signal control. Derived scenario: compiler-bound causal mitigation of the measured reuse edge.** The String allocation-origin identity is carried through the projection owner with compiler-supplied identity. |
| RSH-069 (`openssl`) | `Option::map_or` consumes and drops a `CString` while returning its raw pointer, then OpenSSL reads that pointer. The source witness is `evaluation/harnesses/rustsec_heap_expansion/RSH-069/main.rs`. | The source path has one valid Rust reclaim followed by an FFI read. The derived path inserts a synthetically groomed 4096-byte `Replacement` before that read. | **Source reclaim scenario: completed no-signal control. Derived scenario: compiler-bound causal mitigation of the measured reuse edge.** The CString allocation-origin identity is carried through the groomed owner; the measured edge retains an explicit synthetic-grooming caveat. |
| RSH-075 (`diesel`) | SQLite retains bytes supplied to `sqlite3_deserialize`; `SerializedDatabase::drop` releases serialized storage with `sqlite3_free`; a later query reads the released storage. The witness is `evaluation/harnesses/rustsec_heap_expansion/RSH-075/main.rs`. | Allocation and release flow through SQLite's C allocator and bypass Rust `GlobalAlloc`. | **Completed no-signal control.** The result establishes the foreign-allocator boundary for the current integration. SQLite allocator hooks or C allocator interposition define the required extension. |

## Source, derived-edge, and automatic-coverage boundaries

All 12 Type Isolation measured-edge rows use the automatic compiler-edge probe on
derived or source-shaped derived adapters. The runner makes the historical
`manual_exact_vulnerability_edge_identity` helper a no-op, leaving the compiler
as the sole allocation-identity source. Their
strict result records have:

- `compiler_automatic_victim_coverage=true`;
- `manual_victim_identity_annotation=false`;
- `source_vulnerability_detection_validated=false`; and
- result semantics bounded to causal mitigation of the measured reuse edge.

These results establish one allocator decision: an eligible retained object
cannot satisfy the measured equal-layout allocation carrying a different exact
identity. The source stale pointer remains present. Access before reuse and
same-identity reuse remain outside the measured mitigation.

The source and derived scenarios retain separate meanings:

- the source scenario reproduces the advisory-shaped defect;
- the derived scenario isolates an exploit-enabling allocator reuse edge;
- the compiler audit binds unique preregistered victim and replacement sites;
  and
- the runtime report binds requested/retained identities, modules, callsite,
  and layout to those sites.

The coverage is deliberately narrow and fail closed. Exact `Global`
`Vec::with_capacity<T>` uses monomorphized runtime identity, authenticated
primitive `slice::to_vec` gets a whole-call scope, and exact source-shaped
`String` sites/transfers cover the remaining owner shapes. Custom allocators,
unresolved or ambiguous generic owners, lookalike methods, and user-defined
`Clone` or drop behavior remain neutral or audit-only.

RSH-065 and RSH-069 carry the strongest synthetic-reduction caveats. RSH-065
models reserve relocation as a four-byte old-buffer edge. RSH-069 synthetically
grooms a 4096-byte FFI-read edge. The automatic source-level Type Isolation vulnerability-detection count remains **0/12**.

RSH-064 keeps the Node/V8 witness as `RSH-064-node-addon` and the automatic
derived edge as `RSH-064-derived-reuse`. Compiler handling of the full
`Vec`/external-buffer ownership chain remains deferred. RSH-052, RSH-055,
RSH-060, RSH-065, RSH-066, RSH-067, RSH-068, and RSH-069 preserve their
source-scenario no-signal outcomes beside a separate bounded reuse-edge mitigation row.

RSH-002's automatic Type Isolation denial occurs at the preregistered
`Vec`-backing-to-`Replacement` request. The stale duplicate owner can still
reach the later `type-cache pointer already retained` duplicate-reclaim abort.
This sequence validates the bounded
cross-identity edge and leaves ownership repair, clean completion, and the
separate `reclaim_checks` signal outside that Type Isolation claim.

## Evidence map

- Exact reclaim additions:
  `docs/evidence/rustsec-rsh001-reclaim-20260714/` and
  `docs/evidence/rustsec-rsh060-partial-panic-20260714/`
- Recovery-layout validation:
  `docs/evidence/rustsec-rsh031-layout-20260715/`
- Automatic compiler-edge Type Isolation bundle:
  `docs/evidence/rustsec-typeiso-automatic-20260715/` and its
  `derived-reuse-summary.json`
- Historical manually annotated Type Isolation calibration:
  `docs/evidence/rustsec-rsh002-typeiso-20260714/`,
  `docs/evidence/rustsec-rsh008-typeiso-20260714/`,
  `docs/evidence/rustsec-security-expansion-20260714/` (RSH-041 and RSH-042),
  `docs/evidence/rustsec-rsh052-typeiso-20260714/`,
  `docs/evidence/rustsec-rsh055-typeiso-20260715/`,
  `docs/evidence/rustsec-rsh064-typeiso-20260714/`, and
  `docs/evidence/rustsec-rsh065-typeiso-20260715/` through
  `docs/evidence/rustsec-rsh069-typeiso-20260715/`
- Feature-matched source/reclaim arms and controls:
  `docs/evidence/rustsec-reclaim-checks-20260714/summary-feature-matched-*.json`
- Authoritative merged mechanism ledger:
  `evaluation/config/rustsec_heap_mechanism_results.json`

Every qualified mechanism artifact records at least three repetitions, matched
controls, explicit feature or policy provenance, and zero failed strict checks.
The final scope retains scenario-level negatives and mechanism boundaries as
first-class results while assigning each advisory one exclusive case outcome.
