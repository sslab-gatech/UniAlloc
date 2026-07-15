# Live Evaluation of All 53 RustSec Heap Candidates

## Answer and scope

All **53** rows are manually reviewed **heap-participating strong candidates**.
For each row, either Rust's global allocator owns the relevant storage or a
heap-owning payload reaches the unsafe transition exercised by the witness.
This selection statement establishes allocator relevance. Allocator-visible
duplicate-free and cross-type-reuse coverage are evaluated separately.

The final efficacy denominator is **49**. RSH-054, RSH-056, RSH-059, and
RSH-073 lack a root-cause-specific executable reproduction or required native
dependency. Their attempts and exclusion reasons remain in this 53-row audit,
and they stay outside every efficacy numerator and denominator.

The primary witness taxonomy is:

| Primary primitive | Cases | Typical failure shape |
|---|---:|---|
| Double free | **30** | Duplicated ownership is reclaimed twice, commonly after panic or unwind |
| Use after free | **19** | A stale pointer, reference, projection, or external view survives drop, reallocation, or a race |
| Uninitialized drop | **2** | Error handling drops fabricated or uninitialized heap-owning state |
| Invalid free | **1** | Allocation and deallocation use incompatible layouts |
| Out-of-bounds read | **1** | A wrong-length iterator exposes a freed or invalid slot |

RSH-002 and RSH-018 exercise more than one symptom. The table uses one reviewed
primary label while retaining the scenario oracle in the final column.

## What was run

Every candidate received a terminal attempt on 2026-07-14:

- **48** previously executable cases were re-executed from hash-verified frozen
  binaries: 98 live arms completed across the reclaim and Type Isolation
  matrices.
- **RSH-064** was promoted from blocked to executable. A real Node v20.20.2 host
  loaded the pinned Neon N-API addon and ran `system`, `reclaim_plain`,
  `reclaim_checks`, `typed_plain`, and `typeiso` for three repetitions each.
- The four non-evaluable candidates were reattempted with **23** captured
  diagnostic commands. Their required witness or native prerequisites remain
  unavailable, so the final scope records audit-only exclusions.
- On 2026-07-15, all 12 derived Type Isolation edges were rerun through the
  automatic compiler-edge mode with the manual metadata helper disabled. The
  rerun uses six matched arms and three fresh processes per arm; the durable
  records are under `docs/evidence/rustsec-typeiso-automatic-20260715/`.

The 48-case binary replay preserves the implementation digests recorded by the
original experiments. The earlier manually annotated Type Isolation matrices
remain frozen under UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The separate automatic-probe bundle under
`docs/evidence/rustsec-typeiso-automatic-20260715/` records its own per-case
implementation and input provenance. The complete report keeps replay,
historical manually annotated, and automatic compiler-edge epochs separate. A
unified single-source-snapshot rebuild of every evaluable case remains future
work.

## Strict result

| Strict disposition | Cases | Interpretation |
|---|---:|---|
| Other allocator feature only | **31** | Thirty reclaim detections and the RSH-031 recovery-layout validation provide the qualified allocator coverage |
| TypeIso measured reuse edge only | **11** | A compiler-bound derived or source-shaped cross-identity reuse edge collided in controls and was withheld in `typeiso` |
| Measured edge + other feature | **1** | RSH-002 has a separate reclaim detection and bounded Type Isolation reuse-edge mitigation |
| No exact allocator signal | **3** | RSH-049, RSH-050, and RSH-075 completed every registered matched mechanism experiment without a qualified allocator signal |
| Unresolved or inconclusive attribution | **3** | Two source witnesses exercise concurrency/lifetime races outside the evaluated allocator contracts; one matched abnormal-signal result lacks a source-bound fingerprint |

These rows partition the final efficacy scope:
**31 + 11 + 1 + 3 + 3 = 49**. Allocator mechanisms provide qualified coverage
for **43/49** executable candidates: 31 exact duplicate-reclaim detections, 1
recovery-layout validation, and 12 bounded Type Isolation reuse-edge
mitigations, with RSH-002 counted once. This ratio describes the evaluated
allocator mechanisms and witnesses. Source-level vulnerability detection by
Type Isolation remains **0/12**.

The generated case report records 32 `detected`, 11 `mitigated`, 3 `no_signal`,
and 3 `inconclusive` cases. The 62-row mechanism ledger preserves those
machine states at scenario granularity. Every Type Isolation row records
`source_vulnerability_detection_validated=false`,
`vulnerability_specific_detection_signal=false`, and `claim_grade=false`.
RSH-013 uses a deterministic terminal `drop(values)` so corrupted-string
formatting cannot preempt lifecycle completion. RSH-020 uses a fail-closed
double-panic parser that accepts only repeated identical source checkpoints
followed by Rust's standard destructor-cleanup abort.

The automatic Type Isolation probe makes the compiler the sole identity source.
RSH-008 and RSH-041 have `derived_vulnerability_edge` scope; the other ten
Type Isolation rows have `source_shaped_derived` scope. None has
`published_source` scope. The preregistered requested and victim sites must each
bind one unique applied
compiler-audit row. Runtime requested/retained identities, module provenance,
requested callsite, and layout must bind to those rows. In every vulnerable
`typeiso` repetition, the stats record's `last_wrong_identity_retained_ptr`
must equal the harness's parsed `original` or `stale_value` pointer. This exact
pointer equality proves that the object withheld by the allocator is the
witness object, rather than an unrelated same-layout cache entry.

RSH-052 carries an additional ownership-chain proof: an exact `String` origin
site binds the initial allocation, an audited pointer-preserving
`String -> Box<str>` transfer binds the retained owner, and the same pointer is
withheld from the cross-identity replacement. RSH-068 and RSH-069 preserve
identity from allocation origins through the relevant `String` and `CString`
ownership chains. All patched arms exercise safe
same-identity functionality without a matching attribution event. Allocator
unit controls separately preserve same-identity reuse and withhold eligible
cross-identity reuse.

Coverage uses generic runtime identity for exact `Box<T>` and `Global
Vec::with_capacity<T>`, authenticated primitive `slice::to_vec`, and exact
source-shaped `String` sites/transfers. Custom allocators, unresolved or
ambiguous owners, lookalike methods, and user-defined `Clone`/drop element paths
fail closed. The attribution fields are `stats`-gated evaluation telemetry and
use one companion atomic for the selected retained pointer. Production
mechanism semantics remain the ordinary exact-identity cache decision.

RSH-002 additionally reaches the `type-cache pointer already retained`
duplicate-reclaim abort after its bound cross-identity reuse decision. The
Type Isolation row covers that earlier reuse decision; its separate
`reclaim_checks` row covers the later exact duplicate reclaim. The completed
paired `typeiso_perf / typed_plain` ratio-of-ratios screen passed its merge
gates. Oxipng wall time changed **-1.6649%** and peak RSS changed **+0.583%**;
the long Ripgrep workload changed **+0.8107%** in wall time (95% bootstrap CI
**[-0.180%, +1.399%]**) while maximum RSS remained **7168 KiB** in both
snapshots. The gates require wall-time regression at or below 2%, Oxipng RSS
regression at or below 1%, and unchanged Ripgrep maximum RSS. The selected
macro builds retained unchanged aggregate semantic-rewrite counts, so this is
an overhead screen; the 12 RustSec matrices and compiler regression tests
supply active new-site evidence. Results are retained under
`docs/evidence/rustsec-typeiso-automatic-20260715/performance-screen/`.

## Non-positive and audit-only dispositions

- **3 no-signal cases:** RSH-049, RSH-050, and RSH-075 have complete matched
  experiments and no qualified allocator signal.
- **3 unresolved or inconclusive cases:** RSH-003 and RSH-019 exercise
  same-object concurrency/lifetime races before a required allocator reuse
  event. RSH-006 has a matched SIGSEGV without a normalized source-bound
  fault/checkpoint fingerprint; source analysis indicates a stack-lifetime
  boundary, while empirical no-signal attribution remains withheld.
- **5 retained source-evidence gaps:** RSH-001, RSH-003, RSH-008, RSH-019,
  and RSH-064 preserve their source-witness Type Isolation rows. Separate
  experiments give RSH-001 an exact reclaim detection and RSH-008/RSH-064 bounded derived
  Type Isolation reuse-edge mitigations, so only RSH-003 and RSH-019 remain
  unresolved at the case level. Every one of the 12 Type Isolation rows is an
  automatic compiler-bound derived or source-shaped edge with manual metadata
  disabled; automatic source-level vulnerability detections remain at zero.
- **4 audit-only exclusions:** RSH-054 lacks a published root-specific PoC; RSH-056
  reaches an earlier independent Miri failure; RSH-059 requires a native
  YottaDB installation and initialized database; RSH-073 requires the MetaCall
  core library and headers. These reviewed candidates remain in the attempts
  audit and outside the 49-case efficacy denominator.

RSH-064 supplies a concrete boundary example. Neon 0.10.0 exposes stale
external ArrayBuffer bytes in a live Node/V8 context, and Neon 0.10.1 rejects
the identical source through its added `'static` bound. Both `typed_plain` and
`typeiso` reproduce the stale read because the compiler audit finds no critical
identity rewrite. The frozen historical manually attributed scenario withheld the `Vec<u8>`
backing-store-to-`Replacement` reuse edge in 3/3 repetitions. The terminal
automatic probe supplies authenticated primitive `slice::to_vec` compiler
coverage, exact retained-pointer equality, and matched same-identity controls.
The complete Node/V8 Vec/external-buffer ownership chain remains a future
compiler contract.

## Per-case ledger

The root-cause synopsis comes from the representative catalog oracle. Excluded
rows use the retained terminal audit reason. `Executed` means the registered
witness or live frozen binary was run; mechanism outcomes continue to obey the
stricter control and compiler-coverage gates.

| Case | Advisory | Package | Primitive | Attempt | Mechanism | Strict outcome | Root-cause / witnessed failure synopsis |
|---|---|---|---|---|---|---|---|
| RSH-046 | RUSTSEC-2018-0003 | `smallvec` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free while unwinding SmallVec::insert_many |
| RSH-047 | RUSTSEC-2018-0009 | `crossbeam` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free when epoch reclamation drops a payload already moved from MsQueue |
| RSH-048 | RUSTSEC-2019-0009 | `smallvec` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free after grow(current_capacity) frees the spilled backing allocation |
| RSH-049 | RUSTSEC-2019-0010 | `libflate` | uninitialized drop | executed | `reclaim_checks` | no signal | The vulnerable plain/check arms share the same stable panic fingerprint, with no exact allocator diagnostic and clean patched controls |
| RSH-050 | RUSTSEC-2019-0014 | `image` | uninitialized drop | executed | `reclaim_checks` | no signal | AddressSanitizer reports a sanitizer fault (DEADLYSIGNAL/SEGV) while deallocating the uninitialized Box dropped after the HDR scanline error |
| RSH-001 | RUSTSEC-2019-0016 | `chttp` | double free | executed | `reclaim_checks` | detected | The converted Vec performs a second reclaim of storage still owned by the temporary Box; the exact lifecycle diagnostic appears in 3/3 treatment runs |
| RSH-051 | RUSTSEC-2019-0021 | `linea` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free after the supplied Mul implementation panics in Matrix::scale |
| RSH-052 | RUSTSEC-2019-0023 | `string-interner` | use after free | executed | Type Isolation | bounded reuse-edge mitigation | The source-shaped exact `String -> Box<str>` transfer is compiler-bound; its derived edge collides in system/plain and has its exact witness pointer withheld in Type Isolation, while the source reclaim row remains no signal |
| RSH-011 | RUSTSEC-2019-0034 | `http` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free when a forgotten HeaderMap Drain leaves a moved Box entry owned by the map |
| RSH-002 | RUSTSEC-2020-0007 | `bitvec` | use after free | executed | `reclaim_checks` + Type Isolation | detected + bounded reuse-edge mitigation | The published path triggers the exact reclaim diagnostic, and the separate derived edge is automatically bound through generic `Vec::with_capacity<T>` runtime identity before Type Isolation withholds the exact witness pointer from its cross-identity replacement; the later duplicate-reclaim abort remains a separate boundary |
| RSH-003 | RUSTSEC-2020-0017 | `internment` | use after free | executed | Type Isolation | unresolved | AddressSanitizer reports heap-use-after-free in ArcIntern::drop; the same-object concurrency race precedes a required cross-identity reuse event |
| RSH-053 | RUSTSEC-2020-0038 | `ordnung` | use after free | executed | `reclaim_checks` | detected | Miri reports undefined behavior from a dangling reference (use-after-free) when CompactVec drops duplicated ownership after the invalid-index panic |
| RSH-005 | RUSTSEC-2020-0047 | `array-queue` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free when the stale front Box is returned by the final pop_back |
| RSH-006 | RUSTSEC-2020-0091 | `arc-swap` | use after free | executed | `reclaim_checks` | inconclusive | The matched vulnerable plain/check arms terminate with the same normalized SIGSEGV, but the result lacks a normalized source-bound fault/checkpoint fingerprint; source analysis indicates a stack-lifetime boundary, so empirical no-signal attribution is withheld |
| RSH-054 | RUSTSEC-2020-0105 | `abi_stable` | double free | excluded after reattempt | — | outside efficacy scope | No published PoC; the reachable `RVec::retain` adapter yields only the injected predicate panic, without a root-specific duplicate-reclaim finding. |
| RSH-055 | RUSTSEC-2020-0145 | `heapless` | use after free | executed | Type Isolation | bounded reuse-edge mitigation | The compiler-attributed one-word consumed-slot `Vec<u64>`-to-`Replacement` edge collides in system/plain and has its exact witness pointer withheld in Type Isolation |
| RSH-056 | RUSTSEC-2021-0005 | `glsl-layout` | double free | excluded after reattempt | — | outside efficacy scope | No published PoC; ASan reaches only the injected panic, while Miri stops earlier on an independent uninitialized `[f32; 2]` diagnostic. |
| RSH-057 | RUSTSEC-2021-0009 | `basic_dsp_matrix` | use after free | executed | `reclaim_checks` | detected | Miri reports undefined behavior from a dangling reference (use-after-free) when TransformContent unwinds after duplicating array-element ownership |
| RSH-058 | RUSTSEC-2021-0010 | `containers` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free when BTree::insert_with unwinds through duplicated Box ownership |
| RSH-044 | RUSTSEC-2021-0018 | `qwutils` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports double-free |
| RSH-059 | RUSTSEC-2021-0022 | `yottadb` | use after free | excluded after reattempt | — | outside efficacy scope | The pinned builds require the unavailable native YottaDB library, pkg-config metadata, and an initialized database. |
| RSH-060 | RUSTSEC-2021-0028 | `toodee` | out of bounds read | executed | `reclaim_checks` | detected | The advisory-derived partial-yield/panic scenario reaches an exact duplicate reclaim and the treatment reports it in 3/3 runs; the original initializedness/OOB row remains no signal |
| RSH-009 | RUSTSEC-2021-0030 | `scratchpad` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free while unwinding from the supplied move_elements closure panic |
| RSH-045 | RUSTSEC-2021-0033 | `stack_dst` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports double-free |
| RSH-061 | RUSTSEC-2021-0039 | `endian_trait` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports double-free after the user Endian implementation panics |
| RSH-062 | RUSTSEC-2021-0040 | `arenavec` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports double-free when SliceVec resize unwinds through a panicking Drop |
| RSH-063 | RUSTSEC-2021-0042 | `insert_many` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports double-free when the ExactSizeIterator panics after ownership duplication |
| RSH-014 | RUSTSEC-2021-0047 | `slice-deque` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free after DrainFilter advances past a predicate panic and restores duplicate Box ownership |
| RSH-010 | RUSTSEC-2021-0049 | `through` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free when through unwinds through duplicated String ownership |
| RSH-012 | RUSTSEC-2021-0052 | `id-map` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free when clone_from panics after dropping the destination Box while its id remains live |
| RSH-013 | RUSTSEC-2021-0053 | `algorithmica` | double free | executed | `reclaim_checks` | detected | A deterministic terminal `drop(values)` preserves the vulnerable merge-sort transition; ASan reports double-free 3/3, the plain ablation exits cleanly 3/3, and `reclaim_checks` reports the exact diagnostic 3/3 |
| RSH-008 | RUSTSEC-2021-0130 | `lru` | use after free | executed | Type Isolation | bounded reuse-edge mitigation | The generic-`Box` compiler-attributed 48-byte LruEntry-to-Replacement edge collides in system/plain and has its exact witness pointer withheld in Type Isolation; the published source row retains a full-source coverage gap |
| RSH-064 | RUSTSEC-2022-0028 | `neon` | use after free | executed | Type Isolation | bounded reuse-edge mitigation | The primitive-`slice::to_vec` compiler-attributed Vec-backing-store-to-Replacement edge collides in system/plain and has its exact witness pointer withheld in Type Isolation; the Node source row retains a full ownership-chain gap |
| RSH-031 | RUSTSEC-2023-0017 | `maligned` | invalid free | executed | `recovery_layout_validation` | detected | The upstream Miri baseline reports the allocation/deallocation mismatch, and the derived native matrix emits the exact recovery-layout diagnostic in vulnerable typed arms only |
| RSH-065 | RUSTSEC-2023-0054 | `mail-internals` | use after free | executed | Type Isolation | bounded reuse-edge mitigation | The manually modeled reduction uses a compiler-attributed primitive `Vec<u8>` four-byte reserve-relocation edge; it collides in system/plain and the exact witness pointer is withheld in Type Isolation, with a synthetic-reduction caveat |
| RSH-066 | RUSTSEC-2024-0007 | `rust-i18n-support` | use after free | executed | Type Isolation | bounded reuse-edge mitigation | The exact source-shaped String/AtomicStr old-owner-to-Replacement edge collides in system/plain and is compiler-bound and has its exact witness pointer withheld in Type Isolation |
| RSH-067 | RUSTSEC-2025-0004 | `openssl` | use after free | executed | Type Isolation | bounded reuse-edge mitigation | The primitive-`slice::to_vec` compiler-attributed dropped server-buffer-to-Replacement edge collides in system/plain and has its exact witness pointer withheld in Type Isolation |
| RSH-068 | RUSTSEC-2025-0016 | `pared` | use after free | executed | Type Isolation | bounded reuse-edge mitigation | The String-origin identity carried into the compiler-attributed Parc projection owner-to-Replacement edge collides in system/plain and has its exact witness pointer withheld in Type Isolation |
| RSH-069 | RUSTSEC-2025-0022 | `openssl` | use after free | executed | Type Isolation | bounded reuse-edge mitigation | The synthetically groomed 4096-byte primitive-`slice::to_vec` victim preserves its CString allocation-origin identity; its CString-to-Replacement FFI-read edge collides in system/plain and has its exact witness pointer withheld in Type Isolation |
| RSH-070 | RUSTSEC-2025-0024 | `crossbeam-channel` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports double-free after discard_all_messages and Channel::drop reclaim the same list block |
| RSH-071 | RUSTSEC-2025-0044 | `slice-ring-buffer` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports double-free when cloned IntoIter values both reclaim the same String elements |
| RSH-018 | RUSTSEC-2025-0053 | `arenavec` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports double-free |
| RSH-019 | RUSTSEC-2026-0005 | `oneshot` | use after free | executed | Type Isolation | unresolved | Miri reports undefined behavior from the concurrency race during the 64-seed sweep; allocation identity supplies no synchronization edge |
| RSH-020 | RUSTSEC-2026-0103 | `thin-vec` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free; the fail-closed parser recognizes the stable repeated source panic followed by the standard destructor-cleanup abort in the plain ablation |
| RSH-072 | RUSTSEC-2026-0122 | `rkyv` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports double-free when panic-unsafe InlineVec::clear leaves a dropped Box counted as live |
| RSH-042 | RUSTSEC-2026-0128 | `emap` | use after free | executed | Type Isolation | bounded reuse-edge mitigation | The compiler-attributed primitive `Vec<u8>` victim and Replacement addresses collide after Keys::next in system/plain and split with exact retained-pointer equality in Type Isolation |
| RSH-043 | RUSTSEC-2026-0131 | `bitchomp` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free in Box<i32> deallocation |
| RSH-073 | RUSTSEC-2026-0139 | `metacall` | double free | excluded after reattempt | — | outside efficacy scope | The build requires the unavailable MetaCall core library, C headers, and a pinned installation prefix. |
| RSH-074 | RUSTSEC-2026-0142 | `mutringbuf` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports double-free when the vmem-backed ring drops bit-copied Vec ownership already released with the source Box |
| RSH-021 | RUSTSEC-2026-0143 | `oneringbuf` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports attempting double-free; glibc also aborts |
| RSH-041 | RUSTSEC-2026-0152 | `oneringbuf` | use after free | executed | Type Isolation | bounded reuse-edge mitigation | The compiler-attributed generic `Box<LocalHeapRB>` victim and Replacement addresses collide after reclaim in system/plain and split with exact retained-pointer equality in Type Isolation |
| RSH-075 | RUSTSEC-2026-0172 | `diesel` | use after free | executed | `reclaim_checks` | no signal | The matched plain/check arms preserve the same stable failure fingerprint; SQLite owns the relevant free path outside Rust GlobalAlloc |
| RSH-076 | RUSTSEC-2026-0205 | `scc` | double free | executed | `reclaim_checks` | detected | AddressSanitizer reports double-free when TreeIndex drop reclaims a Box owner bit-copied before the user comparator panicked |

## Evidence and claim boundary

The tracked historical attempts bundle is
[`evidence/rustsec-all-53-live-20260714/`](evidence/rustsec-all-53-live-20260714/).
It retains every reviewed candidate and failed integration attempt. The final
49-case efficacy source of truth is
`evaluation/config/rustsec_heap_complete_scope.json`; its four
`excluded_cases` retain the same audit reasons. Strict per-mechanism records
remain in `evaluation/config/rustsec_heap_mechanism_results.json`.

Rebuild the current 49-case efficacy JSON and CSV from the reconciled scope,
the frozen 48-case replay, the historical reattempt audit, and the current
RSH-064 experiment. The JSON keeps four exclusions and the RSH-064 transition
in separate audit sections:

```bash
uv run python evaluation/scripts/run_rustsec_complete_scope.py \
  --scope evaluation/config/rustsec_heap_complete_scope.json \
  --base-scope docs/evidence/rustsec-all-53-live-20260714/scope-before-rsh064.json \
  --executable-replay docs/evidence/rustsec-all-53-live-20260714/executable-replay-48.json \
  --blocked-attempts docs/evidence/rustsec-all-53-live-20260714/blocked-attempts/summary.json \
  --supplemental-executable docs/evidence/rustsec-all-53-live-20260714/rsh064/experiment.json \
  --output-json docs/evidence/rustsec-all-53-live-20260714/evaluable-49-report.json \
  --output-csv docs/evidence/rustsec-all-53-live-20260714/evaluable-49-cases.csv
```

The tracked attempts report validates `valid=true`, 53 unique reviewed rows,
49 completed cases, 4 audit-only exclusions, 103 live allocator arms, and 23
exclusion probes. The historical reattempt artifact also retains 14 RSH-064
probes from the stage before its promotion. These attempt counts provide audit
coverage; the efficacy denominator remains 49.

All results are exploratory (`claim_grade=false`). The corpus is purposive and
supports mechanism evaluation. RustSec prevalence and an ecosystem-wide
mitigation rate remain outside this study. A `detected` row claims the exact
registered allocator signal for the integrated witness. A Type Isolation `mitigated` row claims one compiler-bound causal mitigation
of a measured derived or source-shaped reuse decision with manual metadata
disabled. The Type Isolation source-detection count is zero, the
vulnerability-specific detector signal is false, and `claim_grade=false`.
