# Resolved Root-Cause Disposition of RustSec Boundary Cases

Date: 2026-07-15

## Resolution

The final review covers 53 strong heap-participating candidates. Forty-nine
cases have executable witnesses and controls; RSH-054, RSH-056, RSH-059, and
RSH-073 retain evidence-backed audit-only exclusions.

The strict 62-row mechanism ledger contains 32 `detected`, 12 `mitigated`, 12
`no_signal`, and 6 `inconclusive` rows. Its 44 positive mechanism rows cover 43
unique cases: 31 exact duplicate-reclaim detections, 1 exact recovery-layout
detection, and 12 Type Isolation reuse-edge mitigations. RSH-002 is the sole
cross-mechanism overlap.

The cases investigated through the former inconclusive queue now have these
root-cause-specific dispositions:

- **Two exact-reclaim detection true positives:** RSH-001 and RSH-060.
- **One exact recovery-layout detection true positive:** RSH-031.
- **Nine bounded Type Isolation mitigation true positives:** RSH-008, RSH-052,
  RSH-055, RSH-064, RSH-065, RSH-066, RSH-067, RSH-068, and RSH-069.
- **Three completed case-level no-signal results:** RSH-049, RSH-050, and
  RSH-075.
- **One evidence-inconclusive result:** RSH-006.
- **Two allocator-mechanism boundaries:** RSH-003 and RSH-019.

The complete Type Isolation-positive set also includes RSH-002, RSH-041, and
RSH-042, producing 12 manual derived-edge positives in total. Several positive
cases retain a separate source or reclaim no-signal/inconclusive row. The
exclusive 49-case partition is 31 other-feature-only, 11 Type-Isolation-only,
1 multiple-mechanism, 3 no-signal, and 3 inconclusive or unresolved. The
generated case report records 32 `detected`, 11 `mitigated`, 3 `no_signal`, and
3 `inconclusive` cases.

## Evidence rules

An exact-reclaim **detection true positive** requires all of the following:

1. the vulnerable system baseline reproduces in every repetition;
2. the feature-matched plain arm preserves the vulnerable behavior;
3. `reclaim_checks` emits the exact allocator diagnostic in every vulnerable
   repetition;
4. matched patched system, plain, and check controls remain valid; and
5. allocator and Cargo-feature provenance match the declared mechanism.

A recovery-layout **detection true positive** requires a source-level ground
truth for the allocation/deallocation mismatch, an exact allocator diagnostic
in every vulnerable treatment repetition, signal-free matched patched controls,
and a diagnostic bound to the recorded and submitted layouts at the same
pointer.

A Type Isolation **mitigation true positive** requires a different causal
matrix:

1. vulnerable system and `typed_plain` arms reuse the reclaimed address in
   every repetition;
2. `typeiso` blocks that address collision and emits a matching denial in every
   repetition;
3. every denial binds to the audited replacement allocation site;
4. patched system, `typed_plain`, and `typeiso` controls exercise safe
   same-identity reuse without a denial; and
5. the claim is limited to the annotated cross-identity reuse edge.

A **no-signal** result is a completed feature-matched negative for one
integrated scenario. Its scope covers the evaluated mechanism and event chain.
A result remains **evidence-inconclusive** when matched abnormal termination
lacks the normalized source-bound fault/checkpoint fingerprint required for a
scenario-local negative attribution. A **mechanism boundary** identifies a
vulnerability whose causal requirement lies outside allocation reuse,
recovery-layout validation, or exact-address duplicate reclaim.

The fail-closed implementations of these rules live in
`evaluation/scripts/export_rustsec_mechanism_results.py`,
`evaluation/scripts/run_rustsec_complete_scope.py`, and
`evaluation/scripts/run_rustsec_heap_experiment.py`.

## Per-case root cause and final disposition

| Case | Root-cause event chain | Allocator-visible edge | Final disposition |
| --- | --- | --- | --- |
| RSH-001 (`chttp`) | A conversion constructs a `Vec` from storage still owned by a temporary `Box<[u8]>`. The temporary releases the pointer, and the returned `Vec` later releases the same pointer. The integrated source is `evaluation/harnesses/rustsec_heap/RSH-001/main.rs`. | One allocation followed by two reclaims of the same address. | **Exact-reclaim detection true positive.** `RSH-001-upstream` produces the bound `reclaim_checks` diagnostic in 3/3 vulnerable check runs with matched plain and patched controls. Evidence is in `docs/evidence/rustsec-rsh001-reclaim-20260714/`. |
| RSH-003 (`internment`) | Concurrent `ArcIntern` operations race reference-count decrement, container removal, destruction, and access to the same `RefCount<T>`. The stress witness is `evaluation/harnesses/rustsec_heap/RSH-003/main.rs`. | Same-object concurrent UAF can occur before any replacement allocation; later reuse can carry the same concrete identity. | **Allocator-mechanism boundary.** This case requires synchronization, race detection, generation validation, or another temporal-access mechanism. |
| RSH-006 (`arc-swap`) | `MapGuard` retains a pointer into a temporary stack guard, followed by stack overwrite and dereference. The witness is `evaluation/harnesses/rustsec_heap_expansion/RSH-006/main.rs`. | Source analysis indicates that the target object has no heap allocation/reclaim edge for `GlobalAlloc` to mediate. | **Evidence-inconclusive.** The matched plain/check SIGSEGV lacks a normalized source-bound fault/checkpoint fingerprint. Source analysis supports a stack-lifetime boundary; the empirical evidence does not satisfy the no-signal attribution gate. |
| RSH-008 (`lru`) | The published iterator signature permits a `String` reference to outlive the cache entry reclaimed by `pop`. The derived adapter requests an equal-layout, different-identity `Replacement` after reclaim in `evaluation/harnesses/rustsec_heap/RSH-008/derived_reuse.rs`. | The source path contains a stale reference. The derived path isolates a 48-byte, align-8 `LruEntry<u32, String> -> Replacement` reuse decision. | **Bounded manual Type Isolation mitigation true positive.** System and `typed_plain` collide in 3/3 runs; `typeiso` blocks and reports the audited edge in 3/3 runs; patched controls validate safe same-identity reuse. Evidence is in `docs/evidence/rustsec-rsh008-typeiso-20260714/`. |
| RSH-019 (`oneshot`) | Sender destruction and receiver poll/drop race over channel state and the stored waker. The witness is `evaluation/harnesses/rustsec_heap/RSH-019/main.rs`. | The same-object race and temporal access precede reuse. | **Allocator-mechanism boundary.** Exact identity cannot create the missing happens-before relation. A concurrency-aware mechanism remains required. |
| RSH-031 (`maligned`) | The vulnerable `Vec<u8>` path allocates 1,009 bytes at alignment 256 and later submits alignment 1 at Drop. The pinned upstream Miri witness establishes this mismatch. | The same pointer reaches deallocation with a recorded/requested layout mismatch. | **Recovery-layout detection true positive.** The derived matrix emits `unialloc_recovery_deallocation_layout_mismatch` in 3/3 vulnerable `typed_plain` and `typeiso` runs and 0/3 patched runs. This policy-independent row contributes zero Type Isolation credit. Evidence is in `docs/evidence/rustsec-rsh031-layout-20260715/`. |
| RSH-049 (`libflate`) | Panic unwinding drops fabricated uninitialized reader state and attempts to free a junk pointer from an uninitialized `Box` field. The witness is `evaluation/harnesses/rustsec_heap_expansion/RSH-049/main.rs`. | The first observed reclaim targets an address with no authoritative allocation publication. | **Completed no-signal control.** Current `reclaim_checks` covers exact duplicate reclaim. Strict invalid-free rejection requires a complete live-allocation provenance contract, including foreign and pre-tracking pointers. |
| RSH-050 (`image`) | The vulnerable HDR reader sets the output vector length before the truncated scanline fails, so unwinding drops an uninitialized `Box`. The witness is `evaluation/harnesses/rustsec_heap_expansion/RSH-050/main.rs`. | The first reclaim is derived from uninitialized payload state rather than a second reclaim of a known live allocation. | **Completed no-signal control.** The six matched arms reproduce the source oracle and controls without the exact duplicate-reclaim diagnostic. Initializedness or complete allocation-provenance validation defines the required extension. |
| RSH-052 (`string-interner`) | `Clone` deep-copies strings while copied raw map references still point into the old interner. Dropping the old interner releases the strings before the clone reads them. The derived adapter is `evaluation/harnesses/rustsec_heap_expansion/RSH-052/derived_reuse.rs`. | The source path has one valid reclaim followed by a stale read. The derived path creates a 64-byte `Box<str> -> Replacement` cross-identity reuse edge. | **Source reclaim scenario: completed no-signal control. Derived scenario: bounded manual Type Isolation mitigation true positive.** The derived matrix blocks and reports the edge in 3/3 `typeiso` runs with valid matched controls. Evidence is in `docs/evidence/rustsec-rsh052-typeiso-20260714/`. |
| RSH-055 (`heapless`) | `IntoIter::clone` clones a consumed inline slot after `next`; dropping the consumed element frees its one-word `Vec<u64>` payload before the clone reads that slot. The derived adapter is `evaluation/harnesses/rustsec_heap_expansion/RSH-055/derived_reuse.rs`. | One valid reclaim leaves a stale payload pointer; the derived path inserts an equal-layout eight-byte `Replacement`. | **Source reclaim scenario: completed no-signal control. Derived scenario: bounded manual Type Isolation mitigation true positive.** The six-arm matrix reports 3/3 plain collisions and 3/3 bound Type Isolation denials. Evidence is in `docs/evidence/rustsec-rsh055-typeiso-20260715/`. |
| RSH-060 (`toodee`) | The original witness uses an invalid `ExactSizeIterator` contract that exposes uninitialized storage. The alternate advisory path yields part of the requested input and then panics, leaving duplicated ownership that reclaims the same pointer twice. Sources are `evaluation/harnesses/rustsec_heap_expansion/RSH-060/main.rs` and `evaluation/harnesses/rustsec_heap_expansion/RSH-060/partial_panic.rs`. | The original path is initializedness/OOB. The partial-yield path contains two reclaims of the same address. | **Original scenario: completed reclaim no-signal control. Partial-yield scenario: exact-reclaim detection true positive.** `RSH-060-partial-panic` emits the exact allocator diagnostic in 3/3 vulnerable check runs with matched controls. Evidence is in `docs/evidence/rustsec-rsh060-partial-panic-20260714/`. |
| RSH-064 (`neon`) | Neon exposes a Rust `Vec<u8>` backing store as a V8 external `ArrayBuffer`, releases the `Vec`, and JavaScript retains a stale view. The host witness is `evaluation/harnesses/rustsec_heap_expansion/RSH-064/addon.rs`; the derived adapter is `evaluation/harnesses/rustsec_heap_expansion/RSH-064/derived_reuse.rs`. | The source path has one Rust reclaim followed by an FFI read. The derived path isolates a four-byte `Vec<u8>` backing-store-to-`Replacement` reuse edge. | **Source Type Isolation row: inconclusive automatic coverage. Derived scenario: bounded manual Type Isolation mitigation true positive.** The derived matrix records 3/3 system/plain collisions, 0/3 Type Isolation collisions, and 3/3 audited denials. Evidence is in `docs/evidence/rustsec-rsh064-typeiso-20260714/`. |
| RSH-065 (`mail-internals`) | `vec_insert_bytes` computes an insertion pointer before `Vec::reserve`; relocation can reclaim the old buffer before a copy through that pointer. The adapter models the relocation in `evaluation/harnesses/rustsec_heap_expansion/RSH-065/derived_reuse.rs`. | The synthetic reduction isolates a four-byte old-buffer-to-`Replacement` collision before the stale copy. | **Source reclaim scenario: completed no-signal control. Derived scenario: bounded manual Type Isolation mitigation true positive.** This row carries an explicit synthetic, manually modeled reserve-relocation caveat. Evidence is in `docs/evidence/rustsec-rsh065-typeiso-20260715/`. |
| RSH-066 (`rust-i18n-support`) | `AtomicStr::as_str` returns an unguarded reference; replacing the value and dropping the returned old `Arc<String>` reclaims the backing buffer while the reference remains live. | The derived path isolates a 4096-byte stale String-buffer-to-`Replacement` collision. | **Source reclaim scenario: completed no-signal control. Derived scenario: bounded manual Type Isolation mitigation true positive.** The matrix blocks and reports the cross-identity edge in all treatment repetitions. Evidence is in `docs/evidence/rustsec-rsh066-typeiso-20260715/`. |
| RSH-067 (`openssl`) | `select_next_proto` ties the returned slice lifetime only to the client list even though OpenSSL can return a pointer into the server buffer. Dropping the server buffer leaves the selected slice stale. | The derived path isolates a 64-byte server-buffer-to-`Replacement` collision before the stale read. | **Source reclaim scenario: completed no-signal control. Derived scenario: bounded manual Type Isolation mitigation true positive.** Evidence is in `docs/evidence/rustsec-rsh067-typeiso-20260715/`. |
| RSH-068 (`pared`) | `Parc::project` can retain a projection into an external `String`; dropping that String leaves the projection stale. | The derived path isolates a 64-byte String-buffer-to-`Replacement` collision before the stale projection read. | **Source reclaim scenario: completed no-signal control. Derived scenario: bounded manual Type Isolation mitigation true positive.** Evidence is in `docs/evidence/rustsec-rsh068-typeiso-20260715/`. |
| RSH-069 (`openssl`) | `Option::map_or` consumes and drops a `CString` while returning its raw pointer, then OpenSSL reads that pointer. The source witness is `evaluation/harnesses/rustsec_heap_expansion/RSH-069/main.rs`. | The source path has one valid Rust reclaim followed by an FFI read. The derived path inserts a synthetically groomed 4096-byte `Replacement` before that read. | **Source reclaim scenario: completed no-signal control. Derived scenario: bounded manual Type Isolation mitigation true positive.** The positive carries an explicit synthetic-grooming caveat. Evidence is in `docs/evidence/rustsec-rsh069-typeiso-20260715/`. |
| RSH-075 (`diesel`) | SQLite retains bytes supplied to `sqlite3_deserialize`; `SerializedDatabase::drop` releases serialized storage with `sqlite3_free`; a later query reads the released storage. The witness is `evaluation/harnesses/rustsec_heap_expansion/RSH-075/main.rs`. | Allocation and release flow through SQLite's C allocator and bypass Rust `GlobalAlloc`. | **Completed no-signal control.** The result establishes the foreign-allocator boundary for the current integration. SQLite allocator hooks or C allocator interposition define the required extension. |

## Source, derived-edge, and automatic-coverage boundaries

All 12 Type Isolation positives use derived adapters with
`manual_exact_vulnerability_edge_identity` attribution. Their strict result
records have:

- `compiler_automatic_victim_coverage=false`;
- `source_vulnerability_detection_validated=false`; and
- `result_semantics=causal_mitigation_true_positive`.

These results establish one allocator decision: an eligible retained object
cannot satisfy the measured equal-layout allocation carrying a different exact
identity. The source stale pointer remains present. Access before reuse and
same-identity reuse remain outside the measured mitigation.

The source and derived scenarios retain separate meanings:

- the source scenario reproduces the advisory-shaped defect;
- the derived scenario isolates an exploit-enabling allocator reuse edge;
- the manual victim annotation supplies the bounded victim allocation/reclaim
  identity; and
- the compiler audit binds the distinct replacement allocation and emitted
  denial.

RSH-065 and RSH-069 carry the strongest synthetic-reduction caveats. RSH-065
models reserve relocation as a four-byte old-buffer edge. RSH-069 synthetically
grooms a 4096-byte FFI-read edge. The automatic full-source Type Isolation
true-positive count remains **0**.

RSH-064 keeps the Node/V8 witness as `RSH-064-node-addon` and the manual edge as
`RSH-064-derived-reuse`. Automatic compiler handling of the full
`Vec`/external-buffer ownership transfer remains deferred. RSH-052, RSH-055,
RSH-060, RSH-065, RSH-066, RSH-067, RSH-068, and RSH-069 preserve their
source-scenario no-signal outcomes beside a separate positive mechanism row.

## Evidence map

- Exact reclaim additions:
  `docs/evidence/rustsec-rsh001-reclaim-20260714/` and
  `docs/evidence/rustsec-rsh060-partial-panic-20260714/`
- Recovery-layout validation:
  `docs/evidence/rustsec-rsh031-layout-20260715/`
- Type Isolation derived edges:
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

Every positive mechanism artifact records at least three repetitions, matched
controls, explicit feature or policy provenance, and zero failed strict checks.
The final scope retains scenario-level negatives and mechanism boundaries as
first-class results while assigning each advisory one exclusive case outcome.
