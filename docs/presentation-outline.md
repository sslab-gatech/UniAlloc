# UniAlloc PhD Qualifier Presentation Blueprint

> Purpose: Present UniAlloc as a research argument the committee can test, with a focused thesis rather than a paper outline, feature inventory, or current engineering status report.
>
> Recommended version: **45-minute rehearsal target, 48-minute hard ceiling, plus 12--15 minutes for questions; 31 main slides plus 19 backup slides**.
>
> If the program explicitly requires a full 60-minute talk with questions handled separately, use the 53--55-minute extended version in this document and retain about 5 minutes of buffer.
>
> Current Type Isolation figure boundary: use `docs/figures/type-isolation-primary-suite/type-isolation-primary-suite.svg` as the lead figure. The title-free figure contains 14 horizontal harness rows across Collections, Oxipng, redb, Polars, SWC, RustPython, and Actix Web, with aligned execution-cost and peak-RSS panels. The 2026-07-14 readiness audit records `7/7` eligible targets, `34` harnesses, `102` warmups, and `510` measured processes. Policy-only execution cost is `1.0036x` (`+0.36%`) across all harnesses; fixed-work policy-only peak RSS is `1.0006x` (`+0.057%`) across 14 harnesses. Compiler-route equivalence passes `13/34`, so dagger-marked end-to-end rows retain explicit attribution limits. fd remains a compiler-path diagnostic.

## 1. One-Sentence Conclusion

### Recommended title

**Beyond Size: Compiler-Assisted, Retargetable Memory Allocation for Rust**

Recommended subtitle:

**UniAlloc as a Semantic Interface Between the Compiler, Allocation Policy, and Platform**

### The single central claim

> **The conventional Rust allocation boundary exposes layout but not language-level object semantics. UniAlloc carries trusted compiler-derived semantics through an optional channel, uses them for deployable policies, and separates those policies from platform-specific mechanisms.**

Interpretation: The conventional Rust allocation boundary exposes layout and runtime context while omitting compiler-known language semantics such as type and module. Under explicit trusted-metadata and coverage assumptions, UniAlloc sends those semantics through an optional compiler-to-allocator channel, uses them to drive selectable policies, and decouples policy from platform mechanism.

This framing is stronger as a qualifier thesis than describing UniAlloc as a faster, safer, feature-rich allocator because it provides all four of the following:

1. A specific systems-interface gap;
2. A falsifiable design proposition;
3. Three independently testable bodies of evidence;
4. Explicit assumptions, limitations, and next research questions.

## 2. Qualifier Success Criteria and Timing

Georgia Tech's public qualifier description emphasizes research readiness, depth, creativity, and the ability to continue explaining the work during oral examination. MIT's public qualifier guidance likewise emphasizes logic, foundations, methodological choices, critical thinking, and technical discussion. This presentation should demonstrate that I can formulate, design, evaluate, and critique a research question; a feature demonstration alone would be insufficient. References:

- [Georgia Tech Ph.D. CS Qualifier Exam Information](https://www.cc.gatech.edu/phd-cs-qualifier-exam-information)
- [MIT MechE Qualifying Exam Presentation](https://mitcommlab.mit.edu/meche/commkit/qualifying-exam-presentation/)
- [MIT NSE Doctoral Qualifying Exam Presentation](https://mitcommlab.mit.edu/nse/commkit/doctoral-qualifying-exam-presentation/)

### Two executable pacing options

| Scenario | Talk | Questions / buffer | Recommendation |
|---|---:|---:|---|
| One hour covers the full exam slot, or the committee may interrupt with questions | 45 min target; 48 min ceiling | 12--15 min | **Default**; prepare a trimmable 42-minute version |
| The program explicitly requires an uninterrupted talk of about one hour and handles questions separately | 53--55 min | 5--7 min | Add the 5 extension slides in Section 6 |

Do not rehearse to 59:30. Committee follow-ups, backup-slide switches, and equipment issues will consume time.

## 3. Research Question, Hypotheses, and Contributions

### Research question

> **Can a Rust allocator use compiler-visible heap-object semantics without breaking existing programs, and can the same allocator runtime be retargeted across userspace, kernels, and constrained systems?**

Paper basis: `../rust-alloc-paper/intro.tex:217-224`.

The phrase "without breaking existing programs" is shorthand for the paper's original question. The talk must use Slide 12's four-dimensional contract to limit it to source and execution compatibility on supported paths under a paired toolchain. Universal binary ABI, FFI, and cross-rustc compatibility remain outside this claim.

### Three testable hypotheses

| Hypothesis | Required evidence | Outside the required evidence |
|---|---|---|
| **H1 -- Semantic availability** | A paired compiler/runtime can deliver object semantics to the allocator; supported common paths require no source annotations, and unknown requests retain a conventional execution path | Semantic coverage at every allocation site or universal binary/FFI compatibility |
| **H2 -- Policy usefulness** | At least one representative policy uses semantics to change allocator behavior with measurable cost and boundaries | Elimination of UAF, universal speedups, or lower memory use on every workload |
| **H3 -- Retargetability** | Reusable boundaries exist among policy, cache/zone/backend, metadata layout, and PAL; retargeting tests whether the semantic channel is a reusable systems contract | Security benefit from H3 alone, zero-effort ports, or completed reproduction of every platform claim in the current source |

End each hypothesis section with these two fixed sentences:

1. **This evidence supports ...**
2. **It does not establish ...**

### Three contributions; keep the feature list separate

1. **Semantic allocation API**: An optional metadata argument that preserves the conventional allocation path.
2. **Compiler-assisted extraction**: Changes to the rustc/core allocation path make validated exact constructors, conversions, and lifecycle matchers, including the canonical `Box`/`Vec` paths listed here, produce metadata without manual annotations on those supported paths. This claim excludes automatic metadata for arbitrary `Box<T>`/`Vec<T>` uses.
3. **Retargetable allocator runtime**: Separates semantic policy from platform memory acquisition, cache primitives, and metadata layout.

Paper basis: `../rust-alloc-paper/intro.tex:246-272`. Type isolation, metadata segregation, hugepages, and PAC are **case studies** that demonstrate architectural capability; each remains subordinate to the central thesis.

## 4. Executable Slide-by-Slide Outline for 31 Main Slides

Rule: Phrase every title as a conclusion the committee should remember. Avoid noun-only labels such as "Background," "Design," and "Evaluation."

The slide budgets below total **45:00**. The 3 minutes from 45 to 48 remain unassigned as a buffer for transitions, brief interruptions, and equipment.

### A. Hook and gap -- 0:00--8:00 (Slides 1--6)

| # | Recommended English title | Sole purpose of this slide | Simplest clear visual | Time / expected follow-up |
|---:|---|---|---|---|
| 1 | **Beyond Size: Compiler-Assisted, Retargetable Memory Allocation for Rust** | State name, topic, and one-sentence thesis; omit biography | Title plus one `type -> metadata -> allocator policy` arrow | 1:00; take no questions |
| 2 | **The conventional Rust allocation boundary omits language-level semantics.** | State the thesis directly and preview the three contributions | Three horizontal layers: Compiler semantics / Policy / Platform | 1:30; make the claim immediately clear |
| 3 | **Same-size reuse can turn a temporal bug into type confusion.** | Establish the running example: after A is freed, same-size B occupies its slot | 4-frame UAF sequence showing only two types and one slot | 1:30; prepare for "Does this prevent every UAF?" |
| 4 | **Rust preserves type semantics until the allocation boundary discards them.** | Show that `Box<T>`/`Vec<T>` retain `T` while `GlobalAlloc` ultimately sees only `Layout` | Typed object on the left, `size + align` on the right, semantics grayed out between them | 1:30; the most important gap figure |
| 5 | **The conventional Rust API exposes layout, not language-level type or module semantics.** | Briefly show that allocators also use thread/address/history while the API lacks language-level semantics | cache -> zone -> backend, annotated with available inputs | 1:15; avoid absolute "size only" wording |
| 6 | **Prior systems obtain policy inputs from runtime state, manual classes, or fixed hardening mechanisms.** | Position snmalloc/mimalloc/Temeraire and Slitter/hardened malloc/Scudo as nearest neighbors, then highlight UniAlloc's compiler-derived input | Three-row table: metadata source / policy / deployment | 1:15; prepare for novelty questions |

**Transition:** "The bottleneck is not another free-list optimization; it is the information boundary."

### B. Question, criteria, and answer -- 8:00--12:00 (Slides 7--9)

| # | Recommended English title | Sole purpose of this slide | Visual | Time / expected follow-up |
|---:|---|---|---|---|
| 7 | **Retargetability tests whether the semantic channel is a reusable contract rather than a point integration.** | Split the original RQ into two connected questions: semantic compatibility and abstraction generality; H3 alone provides no security-benefit proof | RQ-A -> stable contract -> RQ-B | 1:15 |
| 8 | **Three hypotheses make the thesis falsifiable.** | Present H1/H2/H3 and their success criteria | Three columns: hypothesis -> measurement | 1:30; tell the committee how later evidence will determine success |
| 9 | **UniAlloc answers with an API, compiler extraction, and a reusable runtime.** | Present the three contributions and identify features as case studies | Three numbered blocks with colors reused throughout | 1:15 |

### C. H1: Semantic availability without abandoning compatibility -- 12:00--23:00 (Slides 10--16)

| # | Recommended English title | Sole purpose of this slide | Visual | Time / expected follow-up |
|---:|---|---|---|---|
| 10 | **The security scope trusts compiler/runtime metadata and separates only covered cross-class reuse.** | Define the attacker, TCB, and non-goals: metadata is trusted; an internal cache lookup-key collision receives an exact identity check; spoofed or identical metadata, compiler type-ID collisions, same-type reuse, fallback, and metadata corruption remain boundaries | TCB boundary plus "Protects / Does not protect" | 1:30; state limitations before the committee asks |
| 11 | **UniAlloc separates semantic input, allocation policy, and platform mechanism.** | Establish the system-wide mental model | Redraw `fig/overview.pdf` as three horizontal lanes with progressive reveal | 2:00; keep the original figure in backup |
| 12 | **Optional metadata preserves supported source-level execution, not universal ABI compatibility.** | Explain `AllocationMetadata`, the semantic API, and `GlobalAlloc` fallback; preview the four-dimensional compatibility contract | Two paths plus a four-row source/toolchain/FFI/ABI table | 1:30; prepare for ABI and compatibility questions |
| 13 | **Compiler extraction removes source annotations from supported exact Rust allocation paths.** | Show how `T` from one validated exact matcher reaches the metadata ABI through an optimized MIR rewrite | supported exact `Box`/`Vec` path -> MIR -> metadata ABI -> allocator, 4 nodes | 2:00; prepare for rustc-fragility questions |
| 14 | **Correct semantics require pairing allocation, reallocation, drop, unwind, and thread transfer.** | Use actual-rustc evidence to distinguish direct neutral delegation, allocation-side recovery, and the provider observability boundary | Object-lifecycle state diagram with three pairing lanes | 1:30; prepare for cross-thread questions |
| 15 | **Fallback preserves execution when semantics are absent, but it also bounds protection.** | Show compatibility and security coverage in one figure | Coverage circle: typed/known versus unknown/fallback | 1:30; describe fallback as a coverage boundary |
| 16 | **H1 is supported by source-bound probes and an instrumented real application, with version and coverage limits.** | Summarize H1: actual-rewrite probes and Oxipng integration support feasibility; comprehensive coverage, unmodified-application deployment, and a stable ABI remain unestablished | Two boxes: `Supports / Does not establish` | 1:00; add an evidence badge |

Key implementation evidence:

- `AllocationMetadata` in `unialloc/src/alloc_api/type_isolation.rs`: metadata fields/flags and the unknown state.
- The `SemanticAlloc` trait, `impl SemanticAlloc for RustAllocator`, and `__unialloc_semantic_scope_push*`/`__unialloc_semantic_scope_pop` in the same file: semantic alloc/dealloc/realloc API and scope ABI.
- `unsafe impl GlobalAlloc for RustAllocator` in `unialloc/src/cache/mod.rs`: the semantic path and conventional `GlobalAlloc` fallback.
- `record_or_rewrite_semantic_scope_candidates`, `push_semantic_scope_pop_block`, and `record_or_rewrite_semantic_ownership_transfers` in `tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs`: the metadata ABI, scope push/original call/pop, ownership transfer, and unwind cleanup. Refresh line numbers once before deck freeze; symbols are the durable entry points.
- `docs/allocator-mir-and-backend-validation.md:38-84,170-300`: real rustc-driver and bounded functionality probes; classify them as functionality rather than performance evidence.
- `94b2523d...` source-bound presentation snapshot (source digest `56a912ef...`): the direct path observed 36 actual rewrites and typed runtime `84/84`; the semantic-scope path observed 116 rewrites, 28 drop rewrites, and typed runtime `161/161`; the cross-thread path observed 4 hints, 3 recovery matches, and 0 mismatches. Evidence resides in `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-current-head-94b2523d8823-20260712T060330Z/`. Development HEAD has advanced, so this is precisely bound recent functionality evidence; live-HEAD universal coverage and performance remain outside its scope.
- `4715d46...` clean-HEAD compiler-driven type-isolation probe: ordinary `Box<T>` source contains no handwritten metadata/allocator ABI; the real rustc-driver applies 21 semantic-scope and 4 Drop rewrites, with no supported direct allocator-call replacement candidate in this probe, and produces distinct compiler-derived IDs for two same-layout Rust types. Hosted and `fixed_heap` each PASS once: wrong-type reuse is blocked, the producer identity recovers all 4/4 of its addresses, and the target-type drop/deallocation scope is `0/0`, requiring allocation-side recovery for this lifecycle; corrupt slots and recovery mismatches are both 0. Evidence resides in `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/current-source-typeiso-oxipng-4715d46-20260712a/`. It supports a bounded compiler-derived identity -> runtime-isolation path; universal UAF prevention, same-key/collision/spoofing/fallback coverage, and performance claims remain excluded.
- `3d08399...` actual-rustc `Box<[T], A> -> Vec<T, A>` ownership-transfer probe: exact DefId plus structural type proof rewrites ordinary Rust ownership transfer to `__unialloc_semantic_box_slice_into_vec`, preserves pointer/payload, and rebinds only the live-recovery `type_id` from the Box owner to a distinct compiler-derived Vec identity; module, flags, hints, and allocation callsite stay bound to the original allocation. One run of `python3 tools/unialloc-rustc-pass/test_mir_box_slice_into_vec_rebind.py` passes with wrong-type non-reuse, same-Vec-type exact reuse, typed alloc/dealloc `3/3`, and raw fallback, recovery mismatch, and corrupt slots all `0`. The independent runtime regression `box_slice_into_vec_rebind_rejects_memory_tagged_record_without_mutation` verifies fail-closed handling of a memory-tagged record, preservation of the old identity, and zero mutation. This is a bounded functional probe; it excludes universal coverage, performance, and paper-percentage claims.
- `9c04871...` wrapped actual-rustc ownership-transfer probe: an ordinary Rust helper accepts `Result<Option<Box<[u8]>>, u8>` and calls `into_vec` after `?`, `Option::expect`, and an explicit move; the audit applies the direct and wrapped transfer candidates exactly `2/2`. The wrapped runtime preserves pointer/payload, blocks wrong-type reuse, permits same-Vec-type reuse, and reports recovery mismatch `0`. This is bounded Result/Option passthrough and ownership-transfer functionality evidence; general container coverage and performance remain outside its scope.
- `718aab9...` cross-thread actual-rustc `Box<[u8]> -> Vec<u8>` probe: an ordinary Rust program allocates Box in main, moves it to a distinct worker, and calls `into_vec` there; this lane explicitly configures a cross-thread recovery placement policy and does not test automatic escape inference. Audit candidate/applied is `1/1`, and runtime transfer attempted/applied/rejected is `1/1/0`. Pointer/payload survive the thread transfer, wrong Box identity does not reuse, and the same Vec identity reuses exactly; fallback alloc/dealloc, raw alloc/dealloc/realloc-without-metadata, mismatch, corrupt, and dropped are all `0`. Authoritative entry point: `tools/unialloc-rustc-pass/test_mir_cross_thread_box_slice_into_vec_rebind.py`. This is bounded functional/safety evidence; universal coverage, performance, and paper-percentage claims remain excluded.
- `718aab9...` actual-rustc `String::with_capacity -> String::into_bytes -> Vec<u8>` probe: exact non-generic helper candidate/applied is `1/1`; compiler-derived String/Vec IDs are `11507945832468554002 / 13513741751600386252`. Runtime is `1/1/0`; pointer/capacity/payload are preserved, wrong String identity does not reuse, the same Vec identity reuses exactly, and fallback/raw/mismatch/corrupt/dropped are all `0`. The independent safety regression in `unialloc/tests/string_into_bytes_rebind.rs` also verifies that a wrong expected ID, memory-tagged source, and missing record all fail closed; aggregate is `1 applied / 3 rejected`, hosted and `fixed_heap` each PASS `1/1`, and no trusted-record mutation, fabricated record, or mismatch occurs. Authoritative entry points: `tools/unialloc-rustc-pass/test_mir_string_into_bytes_rebind.py` and that integration test. This is bounded functional/safety evidence; universal coverage, performance, and paper-percentage claims remain excluded.
- `99762a9...` actual-rustc `Vec<T, A> -> Box<[T], A>` shrink-aware ownership-transfer probe: exact structural matching applies precisely `2/2` exact-capacity and spare-capacity candidates. In one runtime, `capacity == len` preserves pointer/payload with no allocation/deallocation/cache lifecycle event; spare shrink moves the pointer while preserving payload. Transfer attempted/applied/rejected is `2/2/0`: wrong Vec identity does not reuse Box storage, the same Box identity can reuse it, and the old spare Vec identity can reuse the released old storage; fallback alloc/dealloc, raw alloc/dealloc/realloc-without-metadata, mismatch, and corrupt are all `0`. Independent review first found that rejected wrong/tagged shrink lost source policy and that missing record plus outer scope could receive false outer/auto attribution. After repair, wrong/tagged exact/moved paths preserve source policy/tag, missing plus outer plus auto is suppressed, and the RAII guard restores state after panic/unwind without consuming the finite compiler stream. Hosted and `fixed_heap` focused tests each PASS `3/3`. This is bounded functional/safety evidence; universal coverage, performance, and paper-percentage claims remain excluded.
- `5eb25f5...` actual-rustc `VecDeque` same-layout isolation: ordinary Rust source gives Alpha and Beta, two same-layout element types, distinct nonzero compiler identities. Hosted and `fixed_heap` each PASS once: the address sets for 4 Alpha and 4 Beta objects are disjoint, then 4 Alpha objects recover their original set exactly; typed alloc/dealloc is `12/12`, cache hit is `4`, and fallback/raw/mismatch/corrupt/dropped are all `0`. This is bounded container-lifecycle evidence for Slide 14 and covered-path reuse evidence for Slide 17; universal container coverage, a security proof, and performance results remain outside its scope.
- `c02baa6...` exact `VecDeque` capacity owner: actual `RUSTC_WRAPPER` on current and pinned nightly applies the outer ring-buffer identity to std-owned `with_capacity` and `reserve_exact`. One probe reports typed alloc/dealloc `4/4`, grow alloc/dealloc `1/1`, wrong-type cache-hit delta `0`, exact-type delta `1`, exact address recovery, and fallback/raw/mismatch/corrupt all `0`. `push_back`, a custom same-name helper, and multi-owner `Drop` remain fail closed. This supports exact capacity-path functional feasibility; full container coverage and performance remain excluded.
- `32c3c5b...` P0 ownership-transfer pairing: actual-rustc applies the sole `Vec<T,A> -> IntoIter<T,A>` candidate `1/1`; runtime `1/1/0` preserves pointer/payload, blocks wrong-Vec reuse, permits exact-IntoIter reuse, and reports implicit Drop mismatch `0`. It applies the exact/spare `String -> Box<str>` candidates `2/2`; runtime `2/2/0` covers pointer-preserving exact rebind and moved shrink, with wrong-String non-reuse, same-Box reuse, and old-String recovery of old storage. Corresponding fail-closed regressions cover wrong/tagged/missing inputs; hosted/fixed PASS, and fallback/raw/mismatch/corrupt/dropped are `0`. This is Slide 14 evidence for ownership-consuming pairing; it does not prove all standard-library conversions or a stable rustc ABI.
- `2e3c0e2...a7b5f75...` actual-rustc deterministic replay across three lanes: an allocation for a same-class `Layout` shrink establishes a nonzero identity, while realloc/dealloc with zero type/module/flags/hints use strict-neutral delegation to inherit it. For `63 -> 57` at alignment 64, pointer/layout/payload remain valid; realloc typed alloc/dealloc is `1/1`, final typed dealloc is `1`, and fallback/mismatch/corrupt are all `0`. In the partial-coverage lane, seed/recover helpers each have one actual `Vec::with_capacity` scope while target-helper Drop rows are `0`, explicitly requiring allocation-side recovery; raw Clone alloc/dealloc is exactly `1/1`, cannot obtain the protected address, and the supported `Vec` later recovers it exactly. Generic-helper runtime typed-dealloc/fallback-dealloc/cache-insert is `4/0/4`, while `optimized_mir` exposes no independent generic-helper row, so no generic skip is claimed. Evidence binds exactly to source `2e3c0e2` and validator `a7b5f75`, with `collector_runs=3`, `reruns=0`, and preserved-raw replay only. This is functional evidence, excluding benchmarks, percentages, and universal coverage. Artifact: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/type-isolation-actual-rustc-2e3c0e2-a7b5f75-20260712/`.
- `7096fc6...37ea7cd...` compiler-driven `Vec` transfer-before-growth probe: the creator thread allocates capacity 1 for an ordinary `Vec<ProducerPayload>`, then transfers it to a distinct worker. Before growth, the worker checks pointer/capacity/payload, then performs typed realloc from capacity `1 -> 8` and final Drop. Hosted and `fixed_heap` each PASS once on clean `37ea7cd`: the audit has `2` function-bound direct positive-control replacements, `11` semantic scopes, `4` Drop rewrites, and `0/0` unsolved; allocation and growth type IDs match, growth typed alloc/dealloc is `1/1`, worker Drop typed dealloc is `1`, raw realloc/dealloc and old-metadata fallback are `0`, wrong-type reuse is blocked, same-type recovery is true, and mismatch/corrupt is `0/0`. The direct control runs independently after lifecycle snapshots. This is bounded actual-rewrite/realloc/drop/thread-transfer/isolation functionality evidence; universal container coverage and performance remain excluded.
- `97c7aae...d394f19...` nested-unwind actual-rewrite probe: a Clone panic inside `Vec::extend_from_slice` pops only the inner scope through real MIR cleanup; runtime must restore the still-active outer `Box` scope at depth `1`, then return to depth `0` after the outer return. The subsequent `Box` allocation/Drop uses the same nonzero compiler-derived type ID. Hosted and `fixed_heap` each PASS once with typed alloc/dealloc `1/1`, fallback `0`, mismatch/corrupt `0`, and an audit of `8` enter/exits, `8` Drops, `5` unwind pops, and `0` unsolved. Because the workspace dev profile uses `panic=abort`, the probe subprocess locally sets `CARGO_PROFILE_DEV_PANIC=unwind`. This is bounded unwind-pairing evidence; it does not establish every panic or MIR shape.
- `52a7002...88c35fd...` current-rustc ICE fix and fail-closed boundary for the Clone classifier: plain `Clone::clone` is lowered only when its result has one supported heap owner; nonheap Clone is skipped, ambiguous owners and raw-pointer wrappers remain unsolved, and const-generic Clone with type/const parameters remains unresolved, avoiding an ICE-inducing Copy query against `TypingEnv::fully_monomorphized()`. `88c35fd` adds only exact `indexmap::map::IndexMap` / `indexmap::set::IndexSet` heap-container identities through normalized exact def-path matching and does not broaden matching to arbitrary custom ADTs; PngData, Headers, and crossbeam Sender retain fail-closed boundaries.
- `dd30004...` supported plain-Clone positive plus ambiguous negative: real optimized MIR applies exactly one semantic-scope rewrite to ordinary `Option<Vec<ProducerPayload>>::clone`; Producer type/module is `11653960357981974603 / 13835860698770440193`, while same-layout Consumer uses distinct type `17450045950661180065`. Runtime Option Clone typed alloc/dealloc/cache-hit/cache-insert is `1/1/1/1`, fallback/raw is `0`, the Producer protected address is recovered exactly, and the Consumer address is never obtained. Ambiguous `Result<Vec<ProducerPayload>, String>::clone` remains one fail-closed row with raw alloc/dealloc `1/1`. This is source-bound functionality evidence for one supported Clone callsite plus one ambiguous control; all-Clone/all-application coverage and performance remain excluded.
- `1956350...` Cargo multi-crate allowlist regression: a POSIX temporary fixture truly compiles and runs both the selected bin and a path dependency, producing exactly `target=7 dependency=11 sum=18`; the dependency passes through the compiler shim while the selected target is handled inside the rustc-driver. The test requires one target audit/log, an actual semantic rewrite of `selected_value`, and zero MIR rows for the dependency; exact unittest `1/1` PASS. This establishes only that target/dependency non-interference path; arbitrary dependency graphs, direct allocator-call coverage, runtime isolation, and performance remain outside its scope.
- `374d455...` ambiguous Clone fallback regression: ordinary `Result<Vec<ProducerPayload>, String>::clone` has exactly one ambiguous/fail-closed row in a real rustc audit and no applied/planned semantic scope. Independent hosted and `fixed_heap` verification each observes typed Clone allocation `0`, raw fallback alloc/dealloc `1/1`, a correct independent clone buffer, recovery mismatch `0`, and corrupt slots `0`. This proves conventional execution safely handles this bounded unsupported path; fallback is outside type-isolation coverage.
- `f8612de...b5b70ed...` `Layout` fallback-provenance regression: the real rustc-driver rewrites `Layout::new::<[u64; 4]>().align_to(64).expect(...)` and runtime observes a matching nonzero compiler-derived identity for `32B/align64` alloc/dealloc. `align_to(3).unwrap_or_else(|_| Layout::new::<[u8; 37]>())` must use an unknown-object direct-callsite fallback identity rather than inherit the source `[u64; 4]` identity. One clean `b5b70ed` run passes with distinct identities, typed alloc/dealloc `2/1`, mismatch/corrupt `0/0`, and an exact dealloc-alignment check of `64`. It establishes only these two bounded Result/Layout shapes; universal transformer coverage and performance remain excluded.
- `addd743...` instrumented Oxipng integration: a detached copy of `dea2321...` (`v4.0.3`) adds the UniAlloc dependency/global allocator, runtime counters, a symbol-visibility hook, and limited build plumbing (`lock_api`, `[workspace]`, and an updated `Cargo.lock`); the saved source/build patch omits the generated `Cargo.lock` diff. This historical run applies 6 allocator-call replacements, 846 semantic scopes, and 532 Drop rewrites to real Oxipng library/binary MIR, leaving 9 semantic unsolved and 0 Drop unsolved. One pinned PNG invocation returns 0, and output SHA-256 matches the earlier clean harness. The recording window in instrumented `main` observes `1058/1067` typed allocation events, a counter-truncated coverage of `9915 bp` (direct ratio about `99.16%`), fallback `9`, and type-isolation corrupt slots `0`; pre-main and post-snapshot events are outside that denominator. This is source-bound historical functionality evidence for exact `nightly-2022-07-01`; it excludes unmodified applications, whole-process coverage, general output equivalence, live HEAD, object coverage, and performance.
- `e466831...` pre-multi-owner-fix Oxipng runtime-class smoke: pinned Oxipng v4.0.3 builds once and runs once, with an exact output-SHA match; target-crate audit reports `6` direct rewrites, `848` semantic scopes, `535` Drop rewrites, semantic unresolved `4`, and Drop unresolved `0`. Runtime preserves all `140/140` type-class rows, reports typed allocation events `1058/1067`, and binds 5 compiler identities to runtime lifecycle rows. This is precisely source-bound earlier functionality evidence. Because `532435a` later fixed aggregate Drop first-owner misattribution, the higher applied Drop count cannot represent current safety coverage.
- `9c74b95...6aae903...` safer-Drop plus injected Oxipng address oracle: the `9c74b95` source snapshot/pass explicitly fails closed on 265 multi-owner Drop rows and applies 278 expressible Drop rows; it also reports 853 semantic scopes, 6 direct rewrites, 4 semantic fail-closed rows, 131 complete runtime rows, and 2 natural lifecycle matches. After the correctness repair there is no natural same-layout pair, so that result is diagnostic only. A separately labeled injected oracle uses two actual-MIR identities with the same module and `64B/align8`: producer type `15719177160194310719` has alloc=2/hit=1, wrong type `11520851239810895908` has alloc=1; addresses are producer `4349034560`, wrong `4349034624`, and producer recovery `4349034560`; executed producer/wrong Drop identities are `2/1`, mismatch/corrupt/dropped are `0/0/0`, and the PNG hash matches. Both actual build/runs succeed, while collectors expose two specific validator defects: requiring an unexecuted cleanup row to have a runtime row, and forcing a natural pair. After repair, only preserved raw data is replayed offline; no third run occurs. Artifact SHA-256 is `debb31038ab3912bdd07260de7facd394f8152ba086a4054e9b098b68b3aab0a`. It supports one injected compiler-identity-bound address sequence; natural Oxipng isolation coverage, whole-program/all-address guarantees, a security proof, and performance remain excluded.
- `26051b9...` direct-local ownership hardening: `_local` scope requires an unprojected owner, one acyclic normal path, and an exact `Drop` zero-alias proof; any borrow/ref/raw pointer, copy/move, call argument, projection, overwrite, branch/loop, or early exit makes the entire same-type candidate group recovery-backed, while raw `SizeAlign`/`exchange_malloc` always remains recovery-backed. An actual two-crate `RUSTC_WRAPPER` regression passes hidden `&mut owner` to dependency `mem::replace`: fail-first observed typed `1/1`, fallback `1/1`, and `raw_dealloc_no_metadata=1`; after repair the hidden lane is typed alloc/dealloc `1/2`, fallback alloc/dealloc `1/0`, raw `0`, while the positive aggregate is typed `4/4`, fallback `0/0`, raw `0`. This is a bounded conservative ownership proof; general escape analysis remains outside its scope.
- `15d892e...` **pre-ownership-hardening** Oxipng v4.0.3 functional run: one successful build/run has an actual-MIR audit of 843 semantic scopes, 278 Drop rewrites, 265 multi-owner Drop fail-closed rows, 2 semantic fail-closed rows, 6 direct rewrites, and 131 runtime rows. The two specific `PngData::clone` sites each identify only `Vec<u8>` as the allocation owner and are applied; `Headers` fails closed due to multiple owners, and `Sender` fails closed because the pass cannot bind the dependency version. The change from 853 to 843 is semantic refinement after removing false-positive scopes for standalone `Arc`/`Rc` handle Clone, rather than a coverage regression. Functional output SHA-256 is `565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`, and the injected address oracle passes. Artifact: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-arc-vec-15d892e-20260712/`. It cannot be rebound to source after `26051b9` and does not establish universal compiler coverage or publication-grade performance.
- `af342f7...3dc1039...` **post-ownership-hardening Oxipng v4.0.3 one-shot**: the application build/run binds exactly to code-bearing source `af342f7e26dc4a5e132acc18d7f7a450009e6517`; both return codes are `0`, and output SHA-256 exactly matches `565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`. Static target-crate MIR denominators are 6 direct rewrites, 843 semantic-scope rewrites, 278 Drop rewrites, and 267 explicit fail-closed candidates (265 multi-owner Drop plus 2 semantic); among 1121 applied semantic/Drop rows, 23 satisfy the `_local` zero-alias proof and 1098 conservatively use recovery. The runtime window separately reports `1061/1070` typed allocation events, 9 fallback allocations, 129 complete type rows, 0 dropped events, and 0 corrupt slots; `9915 bp` is only a diagnostic dynamic-event counter ratio. Injected-oracle addresses are producer `4379656256`, wrong type `4379656320`, and same-type recovery `4379656256`; wrong-type reuse is blocked, same-type reuse is exact, oracle mismatch before/after is `0/0`, and corrupt is `0`. The full workload separately has `recovery_identity_mismatches=67`: runtime uses recorded allocation-time identity for fail-closed correction, classifying this as `recovery_corrected_non_exact` compiler attribution and explicitly precluding a whole-application exact-pairing claim; it has a different denominator from the 267 static fail-closed candidates. `3dc1039` repairs the validator by separating the bounded oracle from later workload counters and replays preserved artifacts offline only, with **no application rerun**. Artifact: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-typeiso-af342f7-20260712a/posthoc-preserved-run-validation.json`. This is one-shot functional/diagnostic evidence; benchmarks, universal coverage, and publication-grade performance remain excluded.
- `04b8108...` **post-Oxipng compiler fail-closed hardening**: a non-Clone receiver call examines only the first MIR receiver, while a factory/constructor examines only the destination; the existing bounded supported-owner scan for that selected receiver/destination must produce exactly one owner, with multiple or unresolved owners failing closed, and no identity inherited from arbitrary arguments or textual return types. A real `RUSTC_WRAPPER` regression requires one ambiguous audit row and zero applied/planned scopes for both a `(Vec<u8>, String)` factory and `Vec<String>::resize`, while normal program results remain correct; the positive direct-local probe, pinned pass compile, and 15/15 unit tests PASS. This commit follows the `af342f7` one-shot, so old Oxipng counts cannot be rebound; Oxipng was not rerun in the `04b8108` hardening round.
- `2ff8770...` **recovery layout/auth fail-closed hardening**: TLS and process-visible recovery lookup distinguish `Missing / Mismatched / Exact`. A valid but wrong layout/auth for a live pointer is rejected before stats/cache/delayed/raw paths on both FFI and `SemanticAlloc` dealloc, preserving the exact record; conservative/recovery-backed FFI single/split realloc ABI wrappers likewise return null before copy/dealloc, preserve payload and record, and permit an exact retry. Two new regressions, adjacent recovery tests, independent review, and the full pre-commit suite of 652 UniAlloc tests plus 430 std-bench tests PASS. This is current-source correctness evidence, excluding exploit-corpus and performance conclusions.
- `0026dfe...` **active recovery-scope P0 hardening**: fail-first falsely attributed a raw pointer created before the scope and placed it in delayed-free (`occupied_slots=1`, expected `0`). After repair, `Missing` takes unknown/raw fallback, releases, and records exactly one fallback; moved raw realloc preserves the prefix, creates a new recovery identity for the replacement, and records the old raw release once; `Exact` uses the recorded identity, while `Mismatched` fails closed without consuming the record and permits an exact retry. Hosted and `fixed_heap` focused filters each PASS `2/2`. This is correctness evidence, excluding performance conclusions.
- `427583b...` **earlier hidden/consumed-owner P0 hardening plus source-bound Oxipng one-shot**: in actual-`RUSTC_WRAPPER` fail-first regressions, conflicting paths for a hidden custom ADT and a consumed by-value factory/receiver each tighten from mismatch `1` to audit-only fail-closed with mismatch `0`; same-owner factory/receiver positive controls still rewrite and retain mismatch `0`. The instrumented Oxipng v4.0.3 at that time builds once and runs functionally once on pinned `nightly-2022-07-01`, both PASS, with a matching output SHA-256. Source has `scoped_status=""`, scoped fingerprint `5f36c0a7f1bad4284071cd3a8f6d50bb7a894282e5f76726e6f2b095d5bc49e8`, and pass-source SHA-256 `aedef38625f6096e3f5875b35f3d89f839709ac3277d7c79e4df6756da8a1373`. Target-crate audit is 6 direct, 383 semantic, 320 Drop applied, and 581 fail-closed; runtime has 64 type rows and corrupt `0`. The bounded injected address oracle passes with wrong-type non-reuse, same-type reuse, and oracle mismatch/corrupt `0/0`, while 13 whole-run recovery corrections retain status `recovery_corrected_non_exact` and preclude whole-application exact pairing. The 13 corrections cannot be attributed to, or rebound as a repaired subset of, the earlier 67 corrections at `af342f7`. This is pinned instrumented functional/diagnostic evidence, excluding unmodified applications, benchmarks, performance, and publication-grade coverage. Artifact: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-typeiso-current-427583b-20260712a/oxipng-realapp-repro-summary.json`.
- `572bfab...` **capacity-only Vec outer-owner coverage**: direct outer `Vec` identity is used only for `reserve/reserve_exact/try_reserve/try_reserve_exact/shrink_to/shrink_to_fit` when the receiver ADT path is exactly `std::vec::Vec`/`alloc::vec::Vec`; `resize/extend/push/clone_from/Drop/factory` retain full owner-graph or consumed-owner fail-closed handling. Current-rustc `Vec<String>` and same-layout `Vec<Vec<u8>>` receive distinct nonzero compiler/runtime IDs, with wrong-type non-reuse, exact same-type reuse, 1 cache hit, and mismatch/corrupt `0/0`; `Vec<String>::resize` stays ambiguous. The `427583b` artifact predates this commit and cannot be rebound to `572bfab`; the later current-content run appears below. This is not performance evidence.
- `576df61...9bb9f8d...f8f0d90...` **source-bound Oxipng v4.0.3 run at `576df61`**: the first pinned old-nightly build exposes incompatibility in `GenericArg::as_type`; after a minimal cfg adapter, byte-identical content is committed as `9bb9f8d`, independent review is `APPROVE`, and old-nightly compile, the current actual-rustc Box probe, and embedded tests `16/16` all PASS. Functional collection occurs at HEAD `576df61` while the adapter is stable uncommitted content; `9bb9f8d` matches all 60 scoped collection inputs. Later `f8f0d90` matches 58/60; only the post-run summarizer and its test differ, while compiler-pass and allocator/runtime inputs remain byte-identical, which does not imply a clean full working tree. Oxipng build/run return `0`, and the output hash matches exactly; audit is 6 direct plus 383 semantic plus 320 Drop rewrites, with 6 actual `Box<[u8]> -> Vec<u8>` ownership-transfer rewrites in real functions and 575 other fail-closed candidates. Runtime has typed allocations `904/1070`, 64 rows, and dropped/corrupt `0/0`; the injected wrong-type non-reuse/same-type reuse oracle passes, while 13 whole-run corrections keep pairing at `recovery_corrected_non_exact`. Enriched artifact: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-typeiso-current-576df61-20260712c-enriched/`, summary SHA-256 `cd14bf7bdcbff8fe99d5fc6d97888ce2065050c527b423baa63b856857bdd43b`; deterministic replay from preserved raw audits records exactly 6 candidates/6 applied/6 selected rows separately from 6 direct rewrites, with **no application rerun**. Original `...20260712b` summary SHA `054b...` remains unchanged. This is one functional run without a timing loop and excludes performance, paper percentages, universal coverage, and natural-application isolation coverage.
- `6d955c0...38b8b59...` **source-bound Oxipng ownership-transfer run at `38b8b59`**: `6d955c0` preserves the immediate `Box<[T; N]>` allocation owner on the optimized `vec!` path, and `38b8b59` recognizes optimized storage markers at the actual `into_vec` callsite. The successful run binds start/end to HEAD `38b8b59c9748691d07b0ac9c0ad7c6adf94396cf`, pass SHA-256 `c2c83bec49001c0b40d045a32daaed10d4094afb7eea2415685670a756fe6d10`, and 60-file scoped fingerprint `8609ff8ff261f27998779613eefb739ddfcc0c682ac1a76ddefaf9dadbd2eb29`; static transfer candidates/applied are `6/6`, and dynamic workload attempted/applied/rejected delta is `1/1/0`. Execution occurs at `png::PngData::output`; the old owner is `Box<[u8; 8]>`, basis is `exact_immediate_box_array_unsize`, and a pointer-preserving rebind follows to `Vec<u8>`. Functional output SHA-256 is `565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`. Successful artifact: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-ownership-runtime-38b8b59-20260712d-success/`, summary SHA-256 `292c8ff8479887f4bfa90fc58b48ce60326e27f89a9f26baf7ac50cce1a0e113`. Earlier `.../oxipng-ownership-runtime-6d955c0-20260712c-failed-after-first-repair/` remains historically failed: the functional process returned 0, while dynamic transfer was `1/0/1`, so it cannot be reported as PASS. This is single-run bounded diagnostic functionality evidence, excluding whole-program coverage, benchmarks, performance, and paper percentages.
- `a51960d...` **historical presentation bundle at `a51960d`**: source HEAD `a51960d92a7c72deabaf25fc23e985c3b26c09a5`, scoped fingerprint `f23eda54db834f2a81475ea84f28d70a55a54fb5502c379d7b36dc355a6e1d8f`. One pinned instrumented build/run passes with output SHA `565f253e...`; static audit separately reports 6 direct, 383 scope, 320 Drop, 12/12 ownership-transfer candidate/applied, and 575 fail-closed, while dynamic transfer is `3/1/2`. The injected oracle has wrong-type non-reuse, same-type reuse, and mismatch/corrupt `0/0`; 13 whole-run corrected mismatches are listed separately, retaining pairing status `recovery_corrected_non_exact`. Full Cargo on the same source passes 660 UniAlloc unit plus 430 std-bench tests. Artifact summary SHA is `bc6f32805e58cb021223dde2e01a91887cbe36e653f5ff73257823349a12e685`. Slide 16 should use this source-bound bundle rather than rebinding old counts across revisions; natural-application universal isolation, whole-program coverage, a security proof, and performance remain outside its scope.
- `ed188ca...4cd0d7f...997e840...` **post-bundle type-isolation safety and compiler-attribution hardening**: a new hosted/fixed-heap regression covers cross-thread Drop after String-to-Vec ownership transfer, requiring payload preservation, no reuse under the old String identity, reuse under the exact Vec identity, and mismatch 0; exact `Vec::with_capacity` destination attribution now applies only to the outer Vec backing of nested `Vec<Vec<u8>>`, and both current/legacy actual-rustc probes PASS; exact `Result<T,E>` factories allow only `Ok(T)` as return-allocation identity, while `Err(E)` is a fail-closed hazard. Result A/B/C actual-rustc probes respectively verify no scope for Err-only, actual rewrite for `Ok(Vec)`, and ambiguity for conflicting Ok/Err owners; current/legacy and clean-tree Clone/Layout gates all PASS, with pre-commit full suite 660+430. Oxipng was not rerun after `997e840`, so the old bundle's 13 corrections cannot be claimed as zero or rebound to the new HEAD.
- `681398e...` **borrowed slice-iterator hazard-only coverage**: only the by-value hazard scan treats exact `core` DefPaths `slice::Iter`/`IterMut` as borrowed non-owners, allowing ordinary Rust `input.iter().copied().collect::<Vec<u8>>()` to receive an actual Vec semantic-scope rewrite in current and `nightly-2022-07-01` actual-rustc probes. Runtime verifies correct payload, wrong String non-reuse, exact Vec reuse, transfer `2/2/0`, and fallback/raw/mismatch/corruption all `0`; a custom raw-pointer iterator stays unresolved, Zip Drop containing `IterMut` and `IntoIter` stays at `2` unresolved and `0` applied, and general/Drop/Clone/transfer scans do not expand. Full pre-commit suite is 660+430. Oxipng was not rerun after this commit, so the historical 458-row unresolved denominator and old bundle counts cannot be rebound to the new HEAD. This is bounded actual-rewrite and isolation-effect evidence, excluding universal coverage and performance.
- `8e4d37c...` **canonical-Vec actual-rewrite soundness closure**: fail-first shows that an external fake crate with `[lib] name="alloc"` can provide callback-bearing `FromIterator`, and the old matcher would wrongly apply its fake `Vec<u8>` based only on alloc-path shape. After repair, the destination must be both rustc's canonical sysroot `Vec` diagnostic item and from the `alloc` crate; current and pinned exact actual-wrapper probes PASS, canonical `slice::Iter<u8>.copied().collect::<Vec<u8>>()` remains applied, and the fake-alloc destination remains unresolved with `audit_only_unresolved_heap_object_type`. This closes one exact matcher fail-closed; it does not prove arbitrary `Iterator::collect`, every `Vec` construction, or whole-program coverage. The `a57d318` Oxipng counts predate this compiler commit and cannot be rebound to `8e4d37c`.
- `9240fc6...` **memory-tagged ownership-transfer P0 closure**: fail-first ordinary Rust `String::into_bytes` under policy flags `129` has an actual MIR rewrite but runtime transfer only `1/0/1`, followed by reuse under the old String identity and no reuse under the new Vec identity. After repair, recovery auth and matching software memory-tag auth replace only `type_id` and commit together; TLS/global fast plus overflow tables perform exact `0/1/>1` classification, while duplicate, cross-domain duplicate, and layout/metadata/auth discrepancies fail closed, and tag rolls back if recovery commit fails. Current and `nightly-2022-07-01` actual-rustc probes are each `1/1/0`; payload/pointer/capacity preservation, wrong String non-reuse, exact Vec reuse, and tag cleanup all PASS, with fallback/raw/mismatch/corruption `0`. Hosted/fixed-heap, local/global, rollback/duplicate regressions and independent review PASS; full suite is 662+430. Evidence covers only the pointer-preserving `String -> Vec<u8>` actual-rewrite contract; all ownership transfers, invalid concurrent linearization, latest-HEAD Oxipng coverage, and performance remain excluded.
- `2b33401...` **cross-thread tagged composition closure**: the preceding runtime contract combines with cross-thread recovery in one ordinary Rust actual-rustc path. The main thread creates `String`, a worker calls `String::into_bytes` without handwritten metadata, and scope rows carry flags `129` plus placement `32768`. Current and `nightly-2022-07-01` each have one functional run with transfer `1/1/0`; payload/pointer/capacity preservation, wrong String non-reuse, exact Vec reuse, and tag-cleanup reuse all PASS, while fallback/raw/mismatch/corrupt/dropped are `0`. Independent hosted/fixed-heap handwritten-boundary regressions each PASS `1/1`, and full pre-commit suite is 662+430. Transfer-audit placement comes from explicit manual policy. This evidence excludes automatic escape analysis, real external applications, benchmarks, universal coverage, and performance.
- `46d5aaa...` **latest source-bound Oxipng one-shot before module-ID hardening**: one pinned instrumented Oxipng v4.0.3 build/run returns `0` for both stages and matches output SHA `565f253e...`. Target-crate audit is 6 direct, 367 semantic, 320 Drop, 12/12 ownership-transfer candidate/applied, with 529 explicit fail-closed rows; runtime has 59 type rows, whole-run mismatch `0`, corrupt/dropped `0/0`, dynamic transfer `3/1/2`, and an injected oracle that passes wrong-type non-reuse/same-type reuse. Artifact: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-typeiso-46d5aaa-20260712e/`, summary SHA `f5b8ad7c2fbb0a9f28ccbe052ac7dca6d7c40c98c3482c61d09b6fefcc4e274b`. This is one functional/diagnostic run without a timing loop and excludes natural-application universal isolation, whole-program coverage, and performance.
- `0704852...` **multi-crate module-isolation closure**: external crates previously shared a fixed module ID. The compiler pass now prioritizes crate name plus rustc `-C metadata`, then canonical primary input when metadata is absent, and finally full rustc argv. Two Cargo packages deliberately use the same rustc crate name, identical source, and compiler type ID `13297006753675728434`; actual-rustc must produce different module IDs, wrong-module non-reuse, same-module reuse, and mismatch/corrupt `0/0`. Two same-name direct-rustc crates without metadata must also remain separated. Current/legacy pass compile, 738 allocator plus 432 std-bench functional tests, and independent review all PASS. This commit follows the `46d5aaa` Oxipng run, so the new module-ID algorithm cannot be rebound to old application counts; the 64-bit hash remains within trusted-metadata/collision boundaries, and this is not performance evidence.
- `eadfa9c...` **lifetime-hint cache-key safety regression**: type/module/flags/placement remain fixed while only an explicit lifetime hint changes; an address freed under `0x11` cannot be reused by `0x22`, and returning to `0x11` must recover the original address exactly. Hosted and `fixed_heap` each PASS `1/1`, with fallback allocation/deallocation, recovery mismatch, and corrupt slot all `0`. This is covered-path allocator evidence for a handwritten-metadata lifecycle; it excludes compiler-derived lifetime coverage, universal isolation, and performance.
- `a379f23...` **placement-hint cache-key safety regression**: type/module/flags/lifetime/layout remain fixed while only explicit placement changes; an address freed under `0x21` cannot be reused by `0x22`, and returning to `0x21` must recover the original address exactly. Hosted and `fixed_heap` each PASS `1/1`, with fallback allocation/deallocation, recovery mismatch, and corrupt slot all `0`. This is covered-path allocator evidence for a handwritten-metadata lifecycle; it excludes automatic compiler placement inference, universal isolation, and performance.
- `05d18be...` **current-thread duplicate-quarantine fail-stop**: after a pointer enters delayed-free TLS quarantine, a second dealloc fail-stops before recovery consumption, stats, cache mutation, or raw free even if it clears the occupancy hint and omits `FLAG_DELAYED_FREE` to attempt the raw/compiler fast path; quarantine/accounting remain unchanged and type cache is not polluted. Hosted/fixed-heap focused tests, adjacent delayed-free `11/11`, full suite `663+430`, and independent verification all PASS. The boundary is current-thread TLS quarantine ownership; this does not prove cross-thread duplicate detection without memory tagging and is not performance evidence.
- `ded36de...` **actual-rustc `Box<str> -> String` ownership pairing**: ordinary Rust `Box<str, Global>::into_string` has one candidate applied `1/1`, with runtime transfer `1/1/0`; compiler-derived Box/String identities are distinct nonzero values, pointer/payload/length/capacity remain intact, the old Box identity cannot reuse, and the exact String identity can reuse, while fallback/raw/mismatch/corrupt/dropped are all `0`. Hosted/fixed-heap regressions cover accepted, wrong-old-ID, missing-record, and authenticated memory-tagged paths; adjacent current actual-rustc transfer probes, embedded pass tests `16/16`, legacy pass compile, full suite `663+430`, and independent review all PASS. This is ordinary-Rust actual-rewrite and bounded isolation-effect evidence, excluding external applications, universal coverage, benchmarks, and performance; Oxipng is neither rerun nor rebound.
- `e6dc7d6...` **process-visible cross-thread delayed-free ownership**: a bounded registry of 8 shards x 32 slots publishes pending/quarantined pointer ownership before memory-tag validation, recovery consumption, stats/cache mutation, copy/in-place realloc, and raw free. RAII rolls back a panic before TLS publication, and release/eviction/valid thread exit authenticates then unregisters. Real `GlobalAlloc::dealloc/realloc`, raw entry, different-alignment move, and explicit `SemanticAlloc` same-class realloc all fail-stop before touching the pointer. Hosted/fixed-heap delayed-free filters each PASS `15/15`, full workspace is `668+430`, and independent verifier says APPROVE. Full capacity or oversized records use authenticated immediate release without creating a hidden TLS owner. The boundary covers pointer ownership after successful process-visible registration; it does not prove general concurrent double-free prevention or performance.
- `5408c04...` **actual-rustc `CString::into_bytes_with_nul -> Vec<u8>` ownership pairing**: the sole ordinary-Rust candidate applies `1/1`, with runtime transfer `1/1/0`; the 258-byte pointer/payload/capacity is preserved, compiler-derived CString/Vec identities are distinct nonzero values, old CString identity cannot reuse, exact Vec identity can reuse, and fallback/raw/mismatch/corrupt/dropped are all `0`. Hosted/fixed-heap direct regressions each PASS `1/1`, covering accepted, wrong-old-ID, missing-record, and authenticated memory-tagged paths; independent review says APPROVE. This proves only the exact bounded `CString -> Vec<u8, Global>` actual rewrite/isolation effect; other CString APIs, custom allocators, external applications, benchmarks, and performance remain excluded.
- `f007c7b...` **realistic multi-module actual-`RUSTC_WRAPPER` application**: a generated Cargo application spans `ingest/transform/storage/handoff` and requires 4 actual allocation scopes, 4 distinct nonzero callsites, 3 distinct nonzero String/Vec/Box type IDs, and `String::into_bytes` transfer `1/1`. Two runtime oracles independently verify wrong-identity non-reuse and exact-identity reuse for String -> Vec and same-layout Box/Vec; fallback/raw/mismatch/corrupt/dropped are `0`. The same run provides no manual placement: a Vec in the same MIR body as real `thread::spawn(move || ...)` automatically receives placement `0x8000` / `auto_cross_thread_escape`, while a local control receives placement `0` / `default`; cross-to-local reuse is blocked and each class reuses exactly, with window typed alloc/dealloc/hit/insert `3/4/2/4`. This is a generated multi-module application's bounded actual-rewrite/placement-isolation regression, excluding arbitrary external applications, whole-program claims, universal/natural-application isolation, and performance.
- `38dfe17...522c7f5...` **pinned-nightly CString compatibility plus exact `str` split coverage**: `38dfe17` restores the `alloc_c_string` feature gate for the paper-pinned prerelease 1.64 nightly without carrying the stabilized gate into current rustc. `522c7f5` treats only exact `std/core::str::Split` and `SplitInclusive` as borrowed non-owners in the by-value hazard scan; current/pinned actual-rustc regressions apply one scope to each of two `collect::<Vec<&str>>()` paths, block wrong-type reuse, permit exact-type reuse, keep a custom raw-pointer iterator fail closed, and report fallback/raw/mismatch/corrupt all `0`. Arbitrary iterators, arbitrary rustc ABIs, coverage percentages, and performance remain excluded.
- `41205d3...` **delayed-free test-state cleanup**: adds `SemanticStateCleanup` only in regression tests and keeps process-visible ownership with `PendingGlobalDelayedFreeOwnership` while a test temporarily removes a delayed-free slot, preventing test-order/state leakage. This supports no new production-behavior or performance claim.
- `41205d3...` **current-source Oxipng v4.0.3 one-shot**: pinned `nightly-2022-07-01` build/run return code is `0/0`, with output SHA-256 `565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`. Target-crate audit is direct/scope/Drop `6/369/320`, static transfer `12/12`, and fail-closed `527`; runtime transfer is `3/1/2`, typed `873/1070`, fallback `197`, 58 rows, corrupt/dropped `0/0`, and the injected wrong-type non-reuse/same-type reuse oracle passes. Whole-run mismatch is `1`, explicitly yielding `recovery_corrected_non_exact`, so whole-application exact pairing cannot be claimed. Artifact: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-typeiso-41205d3-20260712a/`, summary SHA-256 `c95808092c197b01399d4723e52e47c92478c8dafd5c498148fa70560c2dc7f4`. Direct arithmetic against the older `46d5aaa` summary's `367/529` gives exactly `+2` applied scopes and `-2` fail-closed, consistent with two actual `Split<char> -> Vec<&str>` rows; this is a direct inference, excluding timing, performance, coverage percentages, whole-program/universal claims, and natural-application isolation. Every number binds only to `41205d3` and cannot be rebound across revisions.
- `e243779...` **PAC x hugepage policy-composition regression**: ordinary `type-isolated + PAC` policy and `type-isolated + PAC + hugepage-metadata` policy with the same type/module/layout cannot consume each other's cache entries, and each must later reuse exactly. Hosted and `fixed_heap` targeted tests each PASS `1/1`, with fallback allocation, identity mismatch, PAC/software-auth failure, and corrupt slot all `0`. The test requires execution through hardware PAC or a safe software fallback sign/verify; on the current host without real PAC/hugepage backing, it establishes safe fallback and policy-domain identity rather than hardware-path or performance evidence.
- `5d0822a...` **cross-thread authenticated five-policy composition regression**: one allocator lifecycle combines type isolation, memory tagging, delayed free, hugepage metadata, PAC, and process-visible cross-thread recovery. When a foreign worker frees under a wrong Drop identity, authoritative allocation-time metadata must win and recovery/tag must be consumed exactly once; duplicate free fail-stops before stats/cache/registry mutation, quarantine release publishes only the correct hugepage-domain cache entry, ordinary domain and wrong identity both miss, and exact identity/domain alone reuses. Hosted and `fixed_heap` targeted tests each PASS `1/1`; verification through PAC hardware or safe software fallback increases, with auth failure `0`. This is test-only composition coverage, found no new production defect, and excludes real PAC/hugepage hardware and performance.
- `7ec42d4...` **cross-thread authenticated split-realloc composition regression**: a foreign worker uses hints split-metadata FFI with a stale old identity while the allocation-time old policy combines type isolation, memory tagging, delayed free, hugepage metadata, PAC, and process-visible recovery; the replacement uses a distinct ordinary-domain identity. The test requires one mismatch audit under old recovery authority, transactional consumption of old recovery/tag plus publication of new recovery/tag, and quarantine of moved-from storage under authenticated old hugepage identity; duplicate old free fail-stops before stats/cache mutation, old/new identity and domain each permit exact reuse only, and final recovery/tag/quarantine state is empty. Hosted/fixed-heap exact tests each PASS `1/1`, with no production defect found. Arbitrary size/error paths, real PAC/hugepage hardware, and performance remain excluded.
- `b3e793e...` **exact `Arc::new` outer-allocation identity**: the compiler pass selects outer `Arc<T>` identity only with alloc-crate exact DefId/DefPath, exact `Arc<T, Global>` destination, one `T` parameter, and matching destination payload; matching does not extend to `ArcLike`, `new_in`, `Rc`, or arbitrary constructors. Current and `nightly-2022-07-01` actual-`RUSTC_WRAPPER` probes each report 3 typed alloc/dealloc, 1 exact cache hit, and 3 inserts; same-layout `Arc<ProducerWithVec>` / `Arc<ConsumerWithBox>` receive distinct nonzero IDs, block wrong-ID reuse, permit exact-ID reuse, and report fallback/raw/mismatch/corrupt all `0`, while custom `ArcLike::new` remains ambiguous fail closed. The 7 Arc-constructor fail-closed rows in the old `41205d3` Oxipng artifact remain only historical inputs that exposed this gap; Oxipng was not rerun after this commit, so external-application counts cannot be claimed as changed. Universal Arc safety and performance remain excluded.
- `1bd0c9d...` **exact `Rc::new` outer-allocation identity**: the compiler pass selects outer `Rc<T>` identity only with alloc-crate exact DefId/DefPath, exact `Rc<T, Global>` on current or `Rc<T>` on paper-pinned nightly, one `T` parameter, and matching destination payload; matching does not extend to `RcLike`, `new_cyclic`, `new_in`, `Weak`, or arbitrary factories. Before repair, exact `Rc::new` is audit-only ambiguous on current/pinned probes, with typed alloc/dealloc `0/0`, fallback/raw `3/3`, and immediate same-layout wrong-type address reuse. After repair, both toolchains report typed alloc/dealloc `3/3`, cache hit/insert `1/3`, wrong-ID non-reuse, exact-ID reuse, and fallback/raw/mismatch/corrupt all `0`. This is bounded actual-rewrite and isolation-effect evidence; Oxipng was not rerun, and universal Rc safety, cross-thread behavior, and performance remain excluded.
- `715ba13...` **exact `HashMap::with_capacity` outer-table identity**: the compiler pass accepts only std-crate exact DefId/DefPath, exact `HashMap<K, V, RandomState[, Global]>` destination, and one `usize` argument; hashbrown, HashSet, IndexMap, `with_hasher`, `with_capacity_and_hasher`, `new_in`, and local same-name constructors remain excluded. Current and `nightly-2022-07-01` actual-`RUSTC_WRAPPER` probes produce distinct nonzero outer IDs for two same-geometry HashMaps whose keys contain Vec and Box nested owners respectively; both report typed alloc/dealloc `3/3`, wrong-ID cache-hit delta `0`, exact-ID delta `1`, fallback/raw/mismatch/corrupt `0`, while custom same-name remains ambiguous fail closed. Stable HashMap exposes no deterministic raw-table address, so this oracle establishes identity-directed cache selection only for this run; universal address behavior, external-application coverage, and performance remain excluded.
- `39c827b...` **exact `HashSet::with_capacity` outer-table identity**: the compiler pass accepts only std-crate exact DefId/DefPath, exact `HashSet<T, RandomState[, Global]>` destination, and one `usize` argument; `with_capacity_and_hasher`, `with_hasher`, allocator-specific constructors, hashbrown, IndexSet, and local same-name helpers remain excluded. Before repair, current/pinned actual wrappers classify outer HashSet and nested Vec/Box element owners as ambiguous, with typed alloc/dealloc `0/0` and fallback/raw `3/3`. After repair, both toolchains report typed alloc/dealloc `3/3`, cache hit/insert `1/3`, wrong-ID hit `0`, exact-ID hit `1`, and fallback/raw/mismatch/corrupt all `0`. This is bounded actual-rewrite and cache-selection evidence; stable HashSet exposes no deterministic raw-table address, so universal address behavior, external-application coverage, and performance remain excluded.
- `8bc2809...6700ca1...` **plain type-cache hash-collision fail-closed**: fail-first tests force inline and linked plain caches to share a 64-bit cache key and type ID while module/flags/lifetime/placement differ; the old implementation returns a foreign pointer. After repair, both paths store and exactly compare compact callsite-agnostic allocator-visible identity, and linked colliding identities occupy separate slots in a bounded probe table. Hosted/fixed-heap collision tests each PASS `2/2`, with expanded type-cache families `54/54` and `50/50`; the 64-bit cost is about `+1040 B/thread` of TLS. This closes runtime cache-key collision only; identical-metadata spoofing/compiler type-ID collision, probe exhaustion, UAF, hash DoS, and performance remain excluded.
- `d87d5e0...25d316c...` **semantic-cache footprint reduction with saturation repair**: hosted cold-bucket depth changes `8→4`, memory-tag/recovery fast tiers `256→128`, and empty records become demand-zero representation; `fixed_heap` retains original capacities. Independent review deterministically triggers an erroneous overflow into the adjacent frame after a matching bucket fills; `25d316c` repairs it so matching depth/per-bucket/aggregate saturation bypasses directly while distinct hash collisions retain bounded probing. Hosted `308/308` and fixed-heap `277/277` type-isolation filters plus a new 512 KiB aggregate-cap no-replacement regression PASS. Another source-bound 64-thread diagnostic compares baseline `318b66c` to current `25d316c` in 3 interleaved measurements each; ready/peak/idle/max RSS medians change from `9.578/66.969/71.188/71.922 MiB` to `7.500/64.953/69.141/69.484 MiB`, with overlapping max ranges. This supports directional regression evidence only and cannot yield a paper percentage.
- `2e3bc4c...` **plain linked-cache bounded-probe exhaustion regression**: 4 distinct exact identities forced to one lookup key fill the bounded probe window, and 8 later colliders must fail closed; the test requires rejected push to leave node header, all 64 slots, and retained-byte accounting unchanged, requires lookup never to return a foreign pointer, and requires each of the 4 retained pointers to be recovered only by its exact owner. Hosted/fixed-heap targeted tests each PASS `1/1`, with type-cache families `55/55` and `51/51`. The test reproduces no new production defect and supports only fail-closed behavior for single-thread internal collision/exhaustion; hash DoS, compiler type-ID uniqueness, identical-metadata spoofing, cross-thread behavior, and performance remain excluded.
- `19ffb71...` **earlier accepted Oxipng v4.0.3 actual-rewrite/isolation one-shot**: a preceding parallel collection is correctly rejected by the source-binding gate because `type_isolation.rs` changes during execution. After the repair commit freezes claim-bearing source, exactly one build and one functional invocation run without retries or timing. Start/end HEAD is `19ffb710752466a140067034650190dfdad60328`, scoped status is empty, fingerprint is `13f72a5a...`, build/run is `0/0`, and output SHA is `565f253e...`. Target-crate direct/scope/Drop is `6/310/320`, transfer candidate/applied/selected is `12/12/12` with all selected identities nonzero, and fail-closed semantic/Drop is `516/119` (117 multi-owner Drop). Runtime typed alloc/dealloc is `871/860`, fallback `199/160`, transfer `3/1/2`, 53 rows, corrupt/dropped `0/0`; the injected wrong-type non-reuse/exact-type reuse oracle passes. Whole-run mismatch `1` retains status `recovery_corrected_non_exact`, preventing a whole-application exact-pairing claim. Artifact acceptance SHA is `efa66ec1...`. This is bounded functional/diagnostic evidence for an instrumented pinned application; benchmarks, performance, universal/natural-application isolation, and whole-program coverage remain excluded.
- `9a02767...ab98075...` **current-source factory provenance plus lifecycle fail-closed hardening**: local/platform/dependency factories no longer receive caller-side typed attribution merely for returning `Vec`, `String`, or `Result`; without exact constructor/body-allocation proof, only an audit row remains. New exact `Box::new` and `String::with_capacity` matchers require alloc/std DefId/path, exact destination, and argument shape, while custom same-name/allocator-specific paths remain fail closed. Current and `nightly-2022-07-01` opaque-dependency probes have direct/Result typed allocation `0`, while the exact `Vec::with_capacity` control retains typed alloc/dealloc `1/1`. `ab98075` also repairs the end-to-end security validator: the current toolchain accepts 86 audited fail-closed rows line by line, including opaque `producer_box/consumer_box` at `8/4`, while exact inner scope retains typed alloc/dealloc `12/12`, wrong-type non-reuse, exact-type reuse, and mismatch/corrupt `0/0`. The nested-unwind probe now uses an exact outer `Vec::extend` receiver as evidence: after inner `Vec::extend_from_slice` panic cleanup, depth returns to `1`, then to `0` after outer return, and outer allocation/Drop identities pair. A direct-local hidden replacement without an allocation-recovery record must remain raw at a later non-local Drop and cannot fabricate typed attribution from the surrounding scope. This is bounded actual-rewrite/fail-closed evidence, excluding arbitrary factory completeness and universal unwind proof.
- `3ccd464...` **current-source metadata-segregated collision/tamper fail-stop**: each occupied entry has a keyed structural authenticator binding lookup key, full callsite-agnostic allocator-visible identity, policy, pointer, layout, optional PAC/software auth, and metadata. Forced key collisions in inline/materialized storage hit only on exact identity; protection/auth downgrade, identity/policy changes, pointer changes including null, and size/align tampering all fail-stop, while full-bucket replacement and retained-byte projection authenticate the eviction candidate before reading it. Hosted/fixed-heap collision, structural-tamper, full-bucket projection, metadata-segregated, and footprint regressions PASS without increasing entry footprint. This supports bounded internal cache integrity and excludes compiler type-ID uniqueness, arbitrary metadata corruption, UAF elimination, and performance.
- `79d0184...61233b6...` **recovery-layout and Unix TLS publication fail-closed**: when `GlobalAlloc::dealloc` sees a live recovery record but caller `Layout` does not match exactly, it rejects before raw/fallback/cache mutation and preserves the record; only an exact-layout retry consumes and publishes under the original identity/layout. Unix TLS first establishes pthread-destructor ownership and writes the pthread slot, then publishes the fast Rust TLS pointer; on injected save failure, the pointer stays invisible and returns for reclamation. Both are bounded correctness evidence, excluding forged pointers, Windows FLS, and performance conclusions.
- `08a1bbf...` **exact `String::from(immutable &str)` actual rewrite**: the matcher accepts only exact core `From::from` DefId, exact alloc `String` destination, and one immutable `&str` source; other `From`/source shapes and arbitrary String factories remain audit-only fail closed. Current and `nightly-2022-07-01` actual wrappers each report typed alloc/dealloc `3/3`, wrong-type non-reuse, exact reuse, and fallback/raw/mismatch/corrupt all `0`. This is bounded functional evidence, excluding universal String behavior, external applications, and performance; Oxipng was not rerun, and `19ffb71` remains stale and cannot be rebound.
- `c477339...6640305...95d3d8a...cfd887e...6b0747e...` **current platform/recovery/generated-app closure**: Redox `--tests` and Linux `--lib --tests` type-check PASS; `mincore` compiles only on supported targets, with a portable Linux/Darwin residency-byte ABI; Redox runtime still lacks an external linker/runner. Cross-thread `GlobalAlloc` dealloc/realloc with wrong layout rejects before raw/cache mutation and preserves the record, while only exact retry consumes it; the realloc gate precedes zero-size, active, auto, quarantine, and stats/raw dispatch, and Missing-record fallback remains. Current generated multi-module actual wrapper passes exact `Box<[u8]>`: Box/Vec wrong-type non-reuse, exact Box reuse, and raw/fallback/mismatch/corrupt `0`; `Box<[String]>` remains audit-only. This is bounded functional evidence, excluding universal applications, paper percentages, and performance; Oxipng `19ffb71` remains stale and unbound.
- `465234c...504ed10...` **custom ADT discovery and fail-closed ownership boundary**: the positive-only `465234c` attempt broadens destination-owner discovery for `Buffer { bytes: Vec<u8> }`, while independent review uses an actual-wrapper negative that returns an existing `Buffer` and only allocates/frees an unrelated `String` internally, observing mismatch `1`; that version cannot serve as safe rewrite evidence. After `504ed10`, custom aggregate factories, pure passthrough, and passthrough with unrelated allocation retain unresolved audit rows only. Current and `nightly-2022-07-01` still rewrite exact inner `Vec::with_capacity`, unrelated `String::with_capacity`, and backing `Vec` Drop; runtime typed alloc/dealloc is `2/2`, fallback/raw/mismatch `0`, and both passthrough mismatch deltas are `0`. Multi-owner remains ambiguous audit-only, raw-pointer remains unresolved audit-only, and borrowed/`PhantomData` is either not lowered or unresolved. This evidence shows the control layer caught and repaired a real misattribution; arbitrary wrapper-allocation provenance, external-application coverage, and performance remain excluded.
- `1a4127f...` **earlier source-bound Oxipng v4.0.3 one-shot with explicit raw counters**: artifact `oxipng-current-head-1a4127f-20260713-one-shot` binds exactly to HEAD `1a4127f`; pinned build/run is `0/0` with a matching output hash. Target-crate direct/scope/Drop is `6/256/320`, unresolved semantic/Drop is `570/2`, and `whole_program_compiler_coverage=false`. Runtime typed alloc/dealloc is `860/850`, fallback alloc/dealloc `210/170`, raw-no-metadata alloc/dealloc/realloc `183/144/26`, cache hits `808`, and dynamic transfer `3/1/2`. The sole recovery mismatch is exact: `PathBuf` records allocation identity in the Oxipng library crate and requests Drop in the binary crate; type ID matches while module ID differs, and runtime safely corrects using the allocation-time record, preventing a whole-application exact-pairing claim. Relative to artifact `24bb079`, scopes `250→256` and unresolved `576→570` correspond only to a six-row exact cross-artifact comparison for `<[u8] as ToOwned>::to_owned(&[u8]) -> Vec<u8>`, rather than rebinding. Summary SHA-256 is `118894ee3e08990a4d616110ad9001976c5ef082e1d3225e88dc9d070c41ce78`. This is one bounded functional/diagnostic run without timing, universal coverage, a security proof, or performance.
- `f5c4fa4...` **earlier source-bound Oxipng one-shot after duplicate-free closure**: artifact `oxipng-current-head-f5c4fa4-20260713-one-shot` binds clean scoped source `f5c4fa4`, summary SHA-256 `448a634ec918fd8a9e9911fd092d0ef842e97074287ca2fe334514c31c78e5c5`; pinned build/run is `0/0` with matching output hash. Direct/scope/Drop is `6/256/320`, unresolved semantic/Drop `570/2`, and whole-program coverage false; runtime typed is `860/850`, fallback `210/170`, raw `183/144/26`, cache hit/insert/bypass `808/840/62`, transfer `3/1/2`, and corrupt/dropped `0/0`. The injected wrong-type non-reuse/exact-type reuse oracle passes; the sole mismatch is the cross-crate `PathBuf` module difference, retaining status `recovery_corrected_non_exact`. This historical one-shot has no timing and excludes universal, whole-program, and performance claims; it cannot be rebound to later source.
- `a57d318...` **latest external-application source-bound Oxipng one-shot before `8e4d37c`**: artifact `oxipng-current-a57d318-20260713` binds `a57d3189d1cce3265170a69801d63a6ff5b0157b` in a clean detached worktree; `acceptance.json` SHA-256 is `b84e5a56c19e9f79537dc33cc5a09cd2e9d184f10e6052de40c9f34252d3651b`, and pinned Oxipng v4.0.3 build/run is `0/0` with matching output hash. Actual direct/scope/Drop/ownership rewrites are `6/260/320/12`; four natural `reduced_alpha_*` functions hit the supported canonical `Vec<u8>` repetition matcher. Present unresolved semantic/Drop `566/2`, multi-owner Drop `117`, and `whole_program_compiler_coverage=false` alongside those counts. Runtime fallback alloc/dealloc is `210/170`, raw-no-metadata alloc/dealloc/realloc `183/144/26`, with one retained cross-crate `PathBuf` recovery-identity mismatch and status `recovery_corrected_non_exact`. This is one bounded functional/diagnostic smoke without a timing loop, excluding whole-application exact pairing, universal safety/coverage, and performance. The later `8e4d37c` closes only an independent current/pinned exact wrapper and cannot rebind this Oxipng run to new compiler source.
- `e352206...` **actual-wrapper cross-crate returned-owner isolation regression**: a producer crate returns `ReturnedString::Value(String)`, and a consumer crate owns and Drops it; current and `nightly-2022-07-01` both PASS. Both crates derive the same nonzero type ID for the same `String` and distinct nonzero module IDs; two cross-crate Drops explicitly record allocation-identity correction, wrong-module identity does not reuse producer storage, and producer/application exact module identities each recover their own addresses. Runtime typed alloc/dealloc is `4/4`, recovery match/mismatch `2/2`, and fallback, raw-no-metadata, and corrupt-slot are all `0`. This is a bounded enum(`String`) functional security test showing allocation-time identity stays authoritative across cross-crate return; it excludes proof for Oxipng aggregate/`PathBuf`, universal cross-crate coverage, and performance.
- `c9b2f4d...0cd7696...` **current/pinned type-isolation security-probe compatibility closure**: both toolchains report `validated=true`, typed alloc/dealloc `12/12`, cache hit/insert/bypass `4/12/8`, wrong-type blocked/exact reuse, and corrupt/dropped `0/0`. Current summary SHA is `b302a0776d29e236521d8c22d58017cd6e3d3ea067da2a3d86e010306e5a2040`; MIR exposes producer/consumer target Drop rows `0/0`, so it uses `allocation_side_recovery`, with recovery match/mismatch `8/0`. Pinned final summary SHA is `cc242b2da1ebe7f013352c1571062b41342ae38f52f619175fa26014d335fc1d`; it exposes exact requested-identity target rows `4/4`, retains recovery `8/0`, and leaves generic Drop unresolved/specialized audit-only at `1/0`. Both MIR-exposure shapes are safe with zero mismatch; universal toolchain/isolation and performance remain excluded.
- `247a599...` **OutFile<Option<PathBuf>> mismatch-shape mechanism regression**: a two-crate actual wrapper passes on current and pinned; producer `<OutFile as Clone>::clone` allocation and consumer aggregate Drops map to nonzero PathBuf type ID `441353361075010719`, matching the Oxipng mismatch type ID, while module IDs remain distinct. Both corrections are visible, with wrong-module non-reuse, producer/application exact reuse, recovery match/mismatch `2/2`, typed alloc/dealloc/hit/insert `4/4/2/4`, and fallback/raw/corrupt all `0`. This is a minimal mechanism validation of the Oxipng mismatch shape; it neither rebinds nor closes full Oxipng and excludes universal/performance evidence.
- `32eafa2...` **OutFile/PathBuf cleanup-unwind recovery**: a two-crate actual-`RUSTC_WRAPPER` fixture enables `panic=unwind` only for this generated Cargo build, and returned `OutFile` is destroyed in the consumer through an existing MIR cleanup Drop; because that Drop already resides in a cleanup block, no claim is made that the compiler inserted an extra unwind-pop edge. One fresh current `nightly-2026-06-11` run reports recovery match/mismatch `1/1`, typed alloc/dealloc/cache-hit/cache-insert `2/2/1/2`, and fallback/raw/corrupt/dropped all `0`; after panic, the producer exact identity recovers the released address exactly. Normal-return sibling `247a599` carries the wrong-module non-reuse/module-isolation oracle. This extension establishes only bounded cleanup-unwind recovery lifecycle and excludes full Oxipng, universal memory safety, and performance.

Slide 14 should use `5eb25f5`, `32c3c5b`, `ed188ca...2b33401`, `0704852`, `f007c7b`, `9a02767`, `08a1bbf`, `cfd887e`, `e352206`, `c9b2f4d...0cd7696`, `247a599`, and `32eafa2` to show "ordinary Rust source -> exact/fail-closed MIR provenance -> allocation/transfer/Drop/unwind pairing -> bounded reuse effect"; candidate rows or code presence alone never count as an actual rewrite. Slides 15/16 must separate four evidence classes: **static compiler rows** (candidate/applied/fail-closed), **runtime transfer/allocation events** (with their own windows and denominators), **bounded isolation oracle** (an explicitly executed adversarial lifecycle), and **whole-run recovery corrections** (non-exact pairing between requested identity and the allocation-time record). Never interchange the four denominators or time windows. Slide 16 uses the `a57d318` `oxipng-current-a57d318-20260713` one-shot as the latest external-application source-bound actual-rewrite/runtime evidence and presents current compiler soundness closure `8e4d37c` as independent exact-wrapper evidence; do not rebind them across revisions. The Oxipng slide must show its sole cross-crate `PathBuf` mismatch and `recovery_corrected_non_exact`; `e352206`, `c9b2f4d...0cd7696`, `247a599`, and `32eafa2` are independent mechanism/security regressions and cannot imply closure of full Oxipng or universal coverage. `f5c4fa4...`, `1a4127f...`, `19ffb71...`, `41205d3...`, `46d5aaa...`, `a51960d...`, `38b8b59...`, `576df61...9bb9f8d...`, `427583b`, and `af342f7` retain only revision-specific historical evidence and cannot be rebound across revisions. Address-level effects may cite only explicitly labeled injected oracles or independent adversarial-lifecycle regressions, never natural Oxipng counters; the oracle's zero delta also cannot hide a whole-run correction.

Slide 16/B18 must use only the denominator/status table below and must never add values across revisions or windows:

| Evidence surface | Exact result | Status / boundary |
|---|---:|---|
| Oxipng static direct / scope / Drop / ownership | `6 / 260 / 320 / 12` | latest external-app source-bound run at `a57d318`; unresolved semantic/Drop `566/2`, multi-owner Drop `117`; whole-program coverage is false; not rebound to `8e4d37c` |
| Oxipng exact canonical `Vec<u8>` repetition row change | `4` natural functions; scope `256→260`, unresolved `570→566` | exact-row cross-artifact comparison against historical `f5c4fa4`; supported matcher only, not rebinding or a coverage percentage |
| Oxipng runtime typed / fallback alloc/dealloc | `860/850`; `210/170` | recording-window events; diagnostic, not object coverage |
| Oxipng runtime raw alloc/dealloc/realloc | `183 / 144 / 26` | explicitly reported raw-no-metadata counters; not type-isolation coverage |
| Oxipng runtime ownership transfer | `3 / 1 / 2` attempted/applied/rejected | dynamic execution observed; separate denominator |
| Oxipng whole-run recovery mismatch | `1` | exact cross-crate `PathBuf` recorded-library/requested-binary module mismatch; `recovery_corrected_non_exact` prevents whole-app exact-pairing claim |
| C002 compiler functional coverage | `430 / 430`, `99.851437%` | G001 read-only freeze digest `235549b2...`; historical/freeze-bound PASS, not live G002 rebinding |
| Current performance percentage | `N/A` | full reproduction deferred; reduced runs are diagnostic only |

#### Compatibility contract required on Slide 12

| Dimension | Supported claim | Excluded claim |
|---|---|---|
| **Application source** | Validated exact `Box`/`Vec` constructor, conversion, and lifecycle paths require no manual application annotation; conventional execution remains available when metadata is absent | Semantics at arbitrary `Box<T>`/`Vec<T>` or every Rust allocation site |
| **Toolchain** | A paired rustc/core rewrite and runtime ABI can carry metadata | Automatic compatibility with any unmodified rustc or every future rustc release |
| **Custom allocator / FFI / unsafe path** | Requests that can enter the conventional path can fall back | Semantic protection on those paths, or UniAlloc control over a custom allocator that bypasses UniAlloc completely |
| **Binary ABI** | This work demonstrates execution compatibility for a prototype integration | Stable binary ABI across compiler/runtime versions; that contract remains future work |

### D. H2: Semantic policy case study -- 23:00--31:00 (Slides 17--21)

| # | Recommended English title | Sole purpose of this slide | Visual | Time / expected follow-up |
|---:|---|---|---|---|
| 17 | **Distinct trusted allocator-visible identities separate ordinary cross-class reuse.** | Return to Slide 3 and make a bounded claim only for covered requests, distinct trusted exact identities, and ordinary reuse paths | Before/after slot figure with trust and coverage printed on it | 1:45; internal lookup-key collisions receive exact checks; identical/spoofed metadata and compiler type-ID collisions remain open |
| 18 | **Type isolation and quarantine defend different exploitation steps.** | Compare mechanisms with Scudo/quarantine without generalizing to an overall performance ranking | 2×2 with time on the horizontal axis and identity on the vertical axis | 1:15; mechanisms can compose |
| 19 | **The runtime uses a hashed semantic identity whose trust and stability are part of the contract.** | Show the 64-bit allocator-visible key over type/module/policy/lifetime/placement and explain that callsite is absent from the reuse key | Identity-key puzzle plus collision/spoofing boundary | 1:30; prepare for type-ID collision and stability questions |
| 20 | **Per-type caching trades reuse separation for retention and fragmentation.** | Discuss memory cost, empty slabs, bounded TLS cache, and workload sensitivity directly | A mechanism figure labeled `CONCEPTUAL`, or real historical per-workload RSS points; omit fabricated quantitative curves | 1:45; make memory overhead explicit |
| 21 | **H2 supports policy feasibility, not universal security or optimality.** | Summarize H2: type isolation changes the reuse rule; coverage, same-type reuse, memory, and performance remain boundaries | `Supports / Does not establish` | 1:45; reduce to 1:00 in the interruption-heavy version |

Key implementation evidence:

- `unialloc/src/alloc_api/type_isolation.rs:347-353,4454-4486`: 64-bit hashed allocator-visible identity; it is not a collision-free cryptographic type identity.
- `unialloc/src/alloc_api/type_isolation.rs:5432-5501`: typed versus fallback classification.
- `unialloc/src/alloc_api/type_isolation.rs:6492-6611,8444-8484,8554-8604`: allocation/deallocation/recovery in the ordinary matching-key cache.
- `unialloc/src/cache/thread_cache.rs:22-38,1576-1655` and `unialloc/src/zone.rs:15-43,83-165`: hot path and retention bounds.
- `../rust-alloc-paper/intro.tex:274-286`: the paper's explicit defense-in-depth and coverage limitations.
- `94b2523d...` source-bound H2 snapshot: type-ID cache separation, cross-thread recovery metadata, and moved-realloc corrupt-tag transaction exact regressions each report `1/1`, and the semantic-metadata probe also passes. Evidence: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-h2-20260712T0604Z/audit.json`. The snapshot supports lifecycle and routing correctness for that source; new live-HEAD security regressions appear below. Neither source supports paper performance percentages.
- `3acbd6d...` adversarial-reuse regression: 4 same-layout objects are freed into one worker TLS cache through process-visible cross-thread recovery; consumer and producer module/flags/lifetime/placement match exactly, and only `type_id` differs. The consumer must receive none of the producer addresses, and the producer identity must then recover all 4 addresses without duplicates. Hosted and `fixed_heap` each PASS `1/1`; evidence: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/type-isolation-cross-thread-security-3acbd6d/`. This remains a covered-path mechanism test and excludes universal UAF/exploit-success proof.
- `ca9462e...` mismatch/quarantine regression: allocation uses process-visible cross-thread recovery, type isolation, and delayed free while a foreign thread holds the wrong Drop identity. The global recovery record may be consumed once only; the wrong identity cannot cancel quarantine or observe the address; after quarantine release, the address can enter only the allocation-side cache. Hosted and `fixed_heap` each PASS `1/1`. This combines cross-thread, mismatched metadata, delayed free, and cache poisoning in one adversarial lifecycle while excluding arbitrary forged metadata and a complete exploit corpus.
- `13807b3...` memory-tagged cross-thread regression: after an allocation with global recovery and a memory tag encounters a wrong Drop identity on a foreign thread, the correct recovery identity can quarantine it exactly once; duplicate typed free must fail-stop without a second enqueue or pollution of the wrong type cache. This closes the combined path through the memory-tag side table, cross-thread recovery, mismatch, quarantine, and duplicate free, while excluding hardware tagging, arbitrary forged metadata, and complete exploit coverage.
- `27fc0c8...` split-realloc stale-identity regression: both public split-metadata FFIs must treat the allocation-completion recovery record as the authoritative old-object identity and must reject caller-supplied stale old metadata as authority to relabel the same address. One parameterized test covers ordinary and hints ABIs in a loop; hosted and fixed-heap each PASS `1/1`. The test also requires one mismatch record for a wrong request, pairing of a committed move with authoritative old identity, payload preservation, routing old storage into the recorded delayed-free/type-cache domain, and binding requested new metadata only to the replacement. The full pre-commit repository suite passes; independent realloc-family verification is hosted `46/46` and fixed-heap `48/48` PASS. This closes two public split FFIs protected by recovery records and excludes claims that metadata is unforgeable or every custom-allocator ABI is protected.
- `2eea36f...` cross-thread type-changing realloc regression: the old recovery record must be consumed exactly, the replacement must publish a new identity and preserve payload, the new type cannot observe the old buffer while it is in delayed-free quarantine, only the old type can recover it after release, and cleanup must leave no recovery record. Hosted `696/696` and fixed-heap `582/582` full suites PASS. This closes the combined invariant over realloc, cross-thread recovery, quarantine, and type-cache routing while excluding forged metadata and every application realloc path.
- `48cdfcf...ae923c6...` cross-thread realloc policy-domain regression: the old hugepage-policy identity and replacement ordinary metadata-segregated identity must enter their respective cache keys, with one old-recovery consumption and preserved payload. A hosted direct snapshot additionally shows the old pointer in physical hugepage side-cache and the replacement in ordinary inline cache. `fixed_heap` explicitly maps both policies to the ordinary physical domain, so that configuration claims policy-key/identity separation only, rather than physical hugepage partitioning. Focused tests for both configurations each PASS `1/1`; compiler coverage and performance remain excluded.
- `6fd22fb...` checked semantic-snapshot ABI regressions: the semantic-stats, fallback-attribution, and metadata-validation checked snapshots fail closed for null/undersized buffers and return correct fields for exact/oversized buffers; hosted and `fixed_heap` focused tests each PASS `3/3`. This protects the size-negotiated C inspection contract used by probes/platforms and does not constitute external-platform runtime evidence.
- `7096fc6...37ea7cd...` `Vec` transfer-before-growth adversarial lifecycle: producer and consumer elements have the same 64-byte layout but must receive different compiler-derived identities. The producer allocates on the creator thread; after pointer/capacity/payload transfer, the worker performs typed realloc and Drop, then verifies wrong-type non-reuse, same-type exact recovery, and zero mismatch/corruption. Hosted and `fixed_heap` each PASS once on clean `37ea7cd`. This covers a real Rust container path where cross-thread realloc follows transfer, limited to that bounded Vec lifecycle.
- `c426a2f...` Arc+Vec multi-owner worker-Drop evidence: the companion recalculates results from validated raw target rewrites and runtime type rows rather than trusting the summary. Function-bound Arc/Vec use distinct nonzero identities and the same module; the sole multi-owner closure skip satisfies the full contract; each identity has exact allocation/deallocation `1/1`, and recovery mismatch is `0`. This closes bounded Arc+Vec worker-Drop pairing and excludes complete escape analysis, universal container coverage, and performance. Artifact: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/cross-thread-multi-owner-pairing-c426a2f-20260712/`.
- `909a7ad...68749b9...` realloc-to-zero identity retirement: an old identity with process-visible recovery is reallocated to zero on a foreign thread using a distinct same-layout requested type. The test requires an aligned sentinel, exact consumption of the old record, no replacement/stale slow path, wrong-type miss, old-type one-shot recovery, and payload preservation. Hosted/fixed-heap focused tests each PASS `1/1`, with mismatch/corrupt `0/0`. PAC is disabled, so this supplies no PAC-failure evidence; it supports only ordinary TLS identity-retirement/cache-routing invariants.
- `9ca5a10...` memory-tagged realloc-to-zero retirement: both local and process-visible recovery must return an aligned sentinel, remove the old memory-tag record, and consume recovery identity exactly once; a second dealloc must panic before cache/raw free while leaving the side-cache snapshot unchanged. Hosted/fixed-heap focused tests each PASS `2/2`. This covers fail-stop double-free under `FLAG_MEMORY_TAGGING` and excludes hardware memory tagging, arbitrary stale pointers, and performance.
- `422c91f...` cross-thread overflow-realloc failure invariant: the creator publishes old recovery identity and payload, then a foreign worker attempts invalid-layout realloc with a distinct requested type and `usize::MAX`. It must return null with old record/count, payload, validation, cache, and delayed-free state unchanged; normal dealloc then consumes old identity exactly once, new type misses, and old type recovers once. Hosted/fixed-heap focused tests each PASS `1/1`. Stats are disabled and PAC is not requested, so PAC and performance claims remain excluded.
- `6603460...` auto-metadata lifecycle hardening: disable/reconfigure no longer clears live allocation-recovery records; global, thread-local, and layout-derived policies preserve allocation-time exact metadata/generation, and a long-lived worker lazily restarts from the first ID of a new compiler-ID stream after generation changes. Pre-enable raw dealloc and unrecorded old-pointer realloc/move cannot inherit current auto policy; a concurrent regression covers control changes overlapping record publication. Hosted `stats,type_isolation` suite `720/720` and fixed-heap suite `606/606` PASS. This closes policy-generation identity lifecycle and does not expand cross-thread local-only recovery API or provide performance conclusions.
- `4dc6814...` recovery matcher replaces two hashed-key comparisons with exact comparison of allocator-visible fields, eliminating hash-collision false matches in recovery agreement and removing two identity hashes. A directional median from 5+5 same-machine probes changes `3.497 ms → 2.366 ms`, with a wide cold-start range; treat this only as diagnostic direction and never as a paper performance percentage. Evidence: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/type-isolation-exact-recovery-4dc6814/`.
- `4937f4f...` hot-path optimization: `auto_metadata_allocations_exhausted()` first checks a sticky exhaustion atomic and reads `AUTO_METADATA_CONFIG` only when the consuming stream is exhausted, removing one `RwLock` read from the common layout-derived/cyclic auto-metadata alloc/dealloc gate. Independent review confirms unchanged generation-reset and revalidation semantics; targeted type-isolation tests `194/194` PASS. Exactly one pre and one post run of the same leaf diagnostic changes `13.0/30.97 ns` (off/on) to `12.92/29.09 ns`; the variant is explicitly `layout-derived-size-align`, `compiler_site stream=none`, and `semantic_policy.ready=false`. Treat `-1.88 ns` only as a directional signal to retain the optimization; stable percentages, statistical conclusions, compiler-attributed cost, and paper results remain excluded.
- `1228f71...` plain-cache accounting/cap repair: the first cold growth after reset/untrusted scans 64 slots to build a retained-byte aggregate, followed by O(1) updates on healthy push/pop; corruption/accounting repair marks the aggregate untrusted, and the next growth performs one bounded rebuild. Inline and cold slots now share a 512 KiB cap, and an empty inline slot cannot bypass aggregate headroom. Regressions cover exact cap, push/pop, inline/cold replacement, drain, and corruption rebuild. One same-condition micro diagnostic with 24 distinct identities, one 64-byte layout, one thread, and 2.4M typed operations changes `72.338 -> 54.808 ns/op` (`-24.233%`); both sides have 1.2M hits, 1.2M inserts, and 0 bypass. This is directional only, with **no median/range**, and cannot support general-application, paper, publication-grade, or stable-percentage claims. Slide 20 may present O(1) accounting and the 512 KiB bound; place performance values only in speaker notes/backup with this boundary.

Precise threat model for Slides 10/17: the attacker can trigger a temporal bug and heap grooming. The TCB trusts metadata produced by the compiler/runtime or supplied by a trusted semantic caller and assumes input metadata is not forged. For covered requests with distinct allocator-visible identities, ordinary cross-class reuse is separated. Plain inline/linked caches require exact callsite-agnostic identity matching even under an internal 64-bit lookup-key collision. `3ccd464` additionally authenticates occupied-entry structure, exact identity, and full-bucket eviction projection in the metadata-segregated cache before use, so these bounded internal-tamper regressions fail-stop; arbitrary-memory-corruption protection remains outside the claim. **Identical or forged input metadata, compiler type-ID collision, same-type reuse, metadata corruption outside the cache, and unknown/fallback/custom-allocator paths remain outside this bounded guarantee.**

### E. H3: Retargetability is an architectural boundary -- 31:00--36:00 (Slides 22--24)

| # | Recommended English title | Sole purpose of this slide | Visual | Time / expected follow-up |
|---:|---|---|---|---|
| 22 | **Retargetability comes from stable boundaries, not from feature flags alone.** | Explain the responsibilities of policy, cache/zone/backend, metadata allocator, and PAL | Interface-boundary figure labeling reused versus adapted | 1:45; prepare for "Is this only cfg?" |
| 23 | **Hosted and constrained targets reuse the policy while changing memory acquisition.** | Compare mmap/VirtualAlloc with fixed heap and show that each target still needs an adapter | Two-column deployment recipe | 1:45; avoid zero-porting claims |
| 24 | **The paper reported five retargeting environments; current functional readiness and external validation remain separate.** | Use a historical badge for the paper's five environments. Separately list G002 macOS PASS; the independent source-bound Windows Wine 10 earlier-lifecycle `3/3` and current-source FLS failure-path `1/1` runs without adding them; current fixed-heap/hosted smoke; current-source Redox build/codegen/ABI PASS; historical artifact-hash-bound Redox runtime evidence without a captured source revision; and Rust-for-Linux/BlogOS current-source no_std final-link contracts PASS. Current target runtime for the final three still depends on external runners/assets | Five-row platform matrix: paper report / reused layer / adapted layer / current functional status | 1:30; complete H3, then introduce evidence tiers |

Key implementation evidence:

- `unialloc/src/lib.rs:129-150`: Windows `VirtualAlloc`, Darwin/Linux/Unix `mmap`, and fixed-heap selection.
- `unialloc/Cargo.toml:63-95`: configuration surface for fixed heap, alternate slab backend, hugepage, type isolation, metadata segregation, PAC/MTE/MPK/guard/quarantine, and related features.
- `unialloc/src/sc/mod.rs:1-24` and `unialloc/src/sc/backend.rs:1-18`: separate-metadata/bitmap backend is a real backend choice rather than a naming-only flag.
- `../rust-alloc-paper/sys.tex:52-114`: paper architecture decomposition.
- `94b2523d...` source-bound H3 smoke: fixed-heap `small_heap` and a hosted 4-thread allocator workload both PASS without compiler warnings; the fixed-heap semantic C ABI observes typed alloc/dealloc pairing, while the hosted workload proves only the global-allocator/thread-cache/platform path. Evidence: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-h3-20260712T060354Z-94b2523d8823/`. The ordinary hosted Cargo run has no MIR rewrite, so its `typed_allocations=0` cannot be interpreted as H1 failure. Development HEAD has advanced, and this snapshot is not current-HEAD.
- `f5c4935...` closes only symmetric PAL FLS accessors (`FlsSetValue`/`FlsGetValue`); `df5f449...` changes production `GlobalTcache` ownership to fiber-local. `f238f10...` further handles registration/save failure and teardown: current-owner callback performs a full drain; `DeleteFiber(B)` while A is current first narrow-drains OS-thread-shared retained semantic caches, then reclaims B while preserving live recovery/tag records, active scopes, and compiler cursor; temporary bind/clear failure fails safe by leaking rather than creating UAF/double free. Host retained-drain test `1/1`, thread-cache filter `69/69`, Windows GNU cross-target type-check/cross-build, and Zig-linked test executable no-run all PASS. Using the Wine 10 runner introduced by `0bd84c1...`, a fresh cross-build at HEAD `38b8b59...` runs three full-module exact-lifecycle tests at `1/1` each, totaling `3/3` PASS; executable SHA-256 is `7d4e060d7fc08a32134776f30e45550a8a4561373e15e5bcf077844115c8379b`. Durable transcripts, source hashes, and Wine image/runner identity reside in `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/windows-wine10-fls-38b8b59-20260712b/summary.json` with SHA-256 `d44dbe74b7a5fd30776185ee5be4e7f7254eed2e3be5b5ec9bf08f086835a5a2`. Earlier Wine 8 lacks `bcryptprimitives.dll`, a missing runner dependency rather than an allocator failure. This supports a bounded Wine functional path and excludes native-Windows universality and performance.
- `3c725a9...` **current-source Windows FLS failure-path runtime**: a fresh `x86_64-pc-windows-gnu` test executable runs exact ignored test `global_thread_cache_fls_failures_do_not_lose_cache_ownership_on_windows` on pinned Wine 10 image `sha256:a784009cceed4cfd7a29e39c8199a6200281c2bdc168a4cc7eff6f453e39b4f6`, yielding `1/1` PASS. It verifies that injected `FlsAlloc` failure publishes neither key nor TLS owner and permits reset/retry; injected `FlsSetValue` failure returns an unpublished pointer still owned by the caller, avoids false publication, and allows the next metadata allocation to reuse the address exactly after explicit reclaim. Artifact: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/windows-wine10-fls-failure-3c725a9-20260713T122658Z/summary.json`, summary SHA-256 `f122afa583d1e9c490e8c24c02696d8e37c7a9cae6b2f8715f66e039de12c1cf`. This `1/1` and earlier `3/3` at `38b8b59` bind different source revisions and must be reported separately, never as `4/4`; both are bounded Wine functional evidence and exclude native-Windows universality, whole-platform closure, and performance.
- `b3f2cad...` (mainline-equivalent commit `2199624...`) current-source Redox contract: allocator library and `small_heap` example check PASS on the real `x86_64-unknown-redox` target; the generated object is verified as ELF64 little-endian x86-64 `ET_REL`, and `llvm-nm` finds the complete constrained fixed-heap/metadata/semantic-stats/boot C ABI symbol set. Evidence: `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/redox-current-source-b3f2cad-20260712/`. This closes current-source build/codegen/interface contracts only. Final link/run still requires redoxer/QEMU, so external runtime validation is missing rather than an allocator functional failure. The 2026-07-07 real-target transcript in `docs/c007-redox-boot-evidence.md` binds binary/image/config/emulator hashes but captures no Git commit or source digest; classify it only as historical artifact-hash-bound runtime evidence and never rebind it to current HEAD.
- `00a187b...a581cb4...` BlogOS local contract: an independent `x86_64-unknown-none` no_std final crate connects boot-heap publication, an initialization-state-guarded `#[global_allocator]`, real `Box` allocation/deallocation, panic, and allocation-error halt handlers; linked ELF and allocator/handler symbol checks PASS. `a581cb4` adds `5/5` host regressions over the same no_std state machine for first publish, same-range idempotence, different-range rejection, failed retry, and concurrent waiter; final ELF SHA is `dcb668f...d994b`. Real BlogOS image/bootloader/QEMU external validation remains missing. This is behavior/wiring/build evidence, excluding boot/runtime and performance.
- `bd9d927...` Rust-for-Linux force-link closure: `rust_bench.rs` explicitly consumes Makefile-provided `--extern unialloc=...`, preventing the final crate from dropping the rlib when code otherwise reaches it only through an `extern "C"` bridge. A checked-in no_std final-link regression reuses real `unialloc_bridge.rs`; linked ELF contains all `11/11` fixed-heap init/extend/ready, alloc/dealloc/realloc, and semantic-snapshot symbols. An independent negative control removes the force-link import and produces the corresponding undefined-symbol link failure. Real kernel-module load/run still requires an external Rust-for-Linux kernel tree/runner; this carries no runtime or performance claim.
- `315cfc4...` constrained-platform ABI parity: the same no_std final crate compares at compile time the 5 ABI versions, record sizes/alignments, and all 75 field offsets between the UniAlloc runtime and Rust-for-Linux bridge; a C11 header also statically asserts all 75 offsets across the same 5 records. Rust layout is checked in an `x86_64-unknown-none` final crate. The C header is checked only by local 64-bit Apple clang (`arm64-apple-darwin25.5.0`), rather than the actual Rust-for-Linux kernel compiler. Focused contract and independent review PASS. This is layout/build evidence, excluding kernel load/runtime and performance.

### F. Evaluation and evidence boundary — 36:00--42:00 (Slides 25--28)

| # | Suggested English title | Purpose of this slide | Figure | Time / likely question |
|---:|---|---|---|---|
| 25 | **The evaluation asks feasibility, cost, coverage, and retargeting as separate questions.** | Present RQ→metric→baseline→threat before presenting results; show the same three actual-MIR variants across eligible Rust harnesses | Lead with the complete title-free seven-target SVG; keep the 34-harness table in backup | 1:30; distinguish the exact current-version suite, adaptive-RSS diagnostics, and historical paper methodology |
| 26 | **The paper reported less than 2% average performance difference in its tested aggregate, with workload-dependent memory retention.** | Historical result 1: specify the tested baselines, workloads, and aggregation; most memory results were comparable, while Collections/Rust-Redis had higher peaks | Two takeaways; put the exact comparison table in B14/B16 | 1:30; footer says paper-reported historical |
| 27 | **The paper reported 5--14% type-isolation slowdown and 72.17% object coverage under its original setup.** | Address only H2 cost and coverage; move metadata segregation, hugepage, and PAC to backup | Two number tiles plus the coverage boundary; keep other features separate | 1:30; do not describe this as a current reproduction |
| 28 | **Current functional evidence is source-bound; full paper-performance reproduction is intentionally deferred.** | Show the four-tier evidence ladder and G001→G002 status; this is the credibility slide | Four steps: Historical / Current probe / Historical partial record / Deferred claim | 1:30; the committee may examine the methodology here |

Slide 25 must state the original paper methodology precisely: `nightly-2021-08-04`; six runs per item, reporting the geometric mean of the final five; UniAlloc as the normalization baseline; and MPK/MTE simulation excluded from performance claims. It must also state proactively that the methodology reports no separate uncertainty, confidence interval, or significance analysis, and that six runs plus a geometric mean do not replace statistical uncertainty analysis. Source: `../rust-alloc-paper/eval.tex:63-91`.

Slide 26 must list the paper's six baselines in B14/B16: tcmalloc, glibc `malloc`, mimalloc, jemalloc, snmalloc, and Scudo; UniAlloc optional features were disabled, and baselines used default settings (`eval.tex:93-107`). The paper text says only, "On average, UniAlloc differs by less than 2%." Without rechecking the aggregation axis from raw data, describe this only as **the paper's tested aggregate**; do not imply a per-workload or per-baseline upper bound.

The numbers on Slides 26--27 may use only this wording:

> **The paper reported ... under its original toolchain and methodology. These results are historical reference results, not yet a current-source claim-grade reproduction.**

Locations of the original paper numbers:

- default performance: `../rust-alloc-paper/eval.tex:168-175`;
- workload-dependent peak memory: `../rust-alloc-paper/eval.tex:193-210`;
- type isolation and coverage: `../rust-alloc-paper/eval.tex:261-303`;
- backup only: metadata segregation **4% speedup** (`eval.tex:233-252`), PAC **1--3% slowdown**, and hugepage **2--4% speedup** (`eval.tex:314-344`);
- historical retargeting: `../rust-alloc-paper/eval.tex:380-455`.

#### Slide 28 four-tier evidence ladder

| Evidence tier | Supported statement | Unsupported statement | Status in this review |
|---|---|---|---|
| **Paper-reported historical** | The original prototype reported a result under its original toolchain and hardware | The current source has reproduced it | Present it with an explicit historical label |
| **Current implementation/probe** | A current mechanism or path builds or runs; a bounded probe observed a behavior | A complete performance, coverage, or platform claim | Extensive mechanism evidence exists |
| **Historical source-bound record (partial)** | A record was bound to the read-only G001 freeze and passed its own validation | Allocator comparison, a complete cell, or a complete claim | 20 accepted records; the campaign stopped, and all records are historical/diagnostic only |
| **Current source-bound claim (complete)** | Current source, complete required evidence, provenance, and thresholds jointly support the claim | — | Performance claims are explicitly deferred; label them neither pass nor fail |

The status snapshot (`2026-07-13`) must distinguish the **read-only G001 evidence freeze**, the **current G002 development tree**, and **deferred paper-performance work**:

1. The read-only historical freeze is at `/Users/hqzhao/Downloads/UniAlloc-G001-freeze-235549b2`, HEAD `0df377b2...`, authoritative digest `235549b228dec87334abc90c91c0b4cc8bbf6dfb1c2d5ebaed00d8a97746b451`, clean.
2. The G001 formal campaign stopped after 20/1008 accepted records because of an explicit user objective change; its status is `stopped_by_explicit_user_objective_change`. These 20 records cannot support paper performance percentages or allocator comparisons.
3. The original G001 performance goal remains incomplete and is marked superseded; the active goal is implementation-first G002. Keep superseded distinct from complete.
4. The G002 development tree continues to change. During deck production and rehearsal, read the live HEAD with `git rev-parse HEAD`; do not pin a short-lived commit in the main deck. Current evidence focuses on actual MIR rewrite, the type-isolation lifecycle, fixed-heap/hosted runtime, and platform adapter functionality.
5. C002 provides functional/compiler evidence; C006 covers PAC functionality with cost deferred; C007 is the platform-functionality backlog. C001/C003/C004/C005 and the C006 performance percentage are all `deferred_by_explicit_user_scope_change`, neither pass nor fail.

G002 closure speaker-note checkpoint (parent HEAD `364d786`): exact
lifecycle history uses a recent eight-way window per secondary history bucket,
keyed by exact address with a separate epoch per entry. A ninth distinct exact key displaces
the oldest `Missing` record back to raw-only epoch-zero semantics; exact keys and
epochs eliminate unrelated same-home false positives, while a known-generation
`Absent` raw reclaim becomes `Tracked`. Reviewed realloc closure covers in-place
record consumption, prevention of recovery-required scope reattribution for
unrecorded old storage, compiled quarantine alignment grow, and admission before
observable mutation. With
`GLIBC_TUNABLES=glibc.pthread.rseq=0`, hosted Type Isolation tests pass `732/732`
serially and in parallel; `fixed_heap` Type Isolation passes `602/602` serially
and in parallel; `quarantine` Type Isolation passes `738/738` serially and in
parallel. Standard `cargo test` exits `0`,
including `semantic_std` `12/12` and `std_bench` `430/430`; the default-parallel
`semantic_std` loop passes `30/30`. The evaluator doctor is ready with the paper
checkout absent, quick end-to-end diagnostics pass `3/3`, and the realistic
multi-module actual-rustc probe validates after installing the required compiler
components. This remains implementation/probe-tier functional evidence. It
supports no universal UAF/double-free, forged-metadata, universal compiler-
coverage, external-platform runtime, or publication-grade performance claim.

#### Current source-bound real-world Type Isolation diagnostic

Use this matrix on Slide 28 or a current-source backup slide. Every timing cell
is `median wall seconds / median peak RSS KiB`. The seven routes are native,
jemalloc `0.5.4`, mimalloc `0.1.25`, UniAlloc without semantic rewriting,
actual-MIR `typed_plain`, stats-free `typeiso_perf`, and statistics-enabled
`typeiso_coverage`. fd's native source already selects jemalloc `0.5.4`, so its
native and explicit jemalloc rows are route controls.

| Application | Native | jemalloc | mimalloc | UniAlloc | `typed_plain` | `typeiso_perf` | `typeiso_coverage` |
|---|---:|---:|---:|---:|---:|---:|---:|
| ripgrep | `0.086309096 / 5120` | `0.086702833 / 6144` | `0.088868793 / 11812` | `0.090066579 / 6144` | `0.092411192 / 6144` | `0.092586986 / 6144` | `0.094015251 / 6144` |
| fd | `0.136296730 / 6144` | `0.136304143 / 6144` | `0.129672706 / 18872` | `0.167541836 / 6144` | `0.464682945 / 6144` | `0.465833677 / 6144` | `0.491091350 / 6144` |
| Oxipng | `1.712608439 / 46400` | `1.727841580 / 49088` | `1.634668906 / 71180` | `1.736975509 / 44260` | `1.766903750 / 44508` | `1.754456338 / 44524` | `1.754579386 / 44640` |

`typeiso_coverage` has statistics enabled and is performance-ineligible. Show
its time and RSS only as artifact-completeness metadata. The primary incremental
ratio is `typeiso_perf / typed_plain`; the native ratio includes allocator,
compiler rewrite, recovery, and policy effects.

| Application | Type Isolation / typed plain | Incremental time | Type Isolation / native | End-to-end time | Allocation-event coverage |
|---|---:|---:|---:|---:|---:|
| ripgrep | `1.001902302050` | `+0.190230%` | `1.072737292950` | `+7.273729%` | `2266/22725 = 9.97%` (`997` bp) |
| fd | `1.002476380966` | `+0.247638%` | `3.417790558878` | `+241.779056%` | `616262/831549 = 74.11%` (`7411` bp) |
| Oxipng | `0.992955240488` | `-0.704476%` | `1.024435182057` | `+2.443518%` | `9661/10368 = 93.18%` (`9318` bp) |

Type Isolation peak-RSS ratios versus typed plain / native are
`1.000000000000 / 1.200000000000` for ripgrep,
`1.000000000000 / 1.000000000000` for fd, and
`1.000359485935 / 0.959568965517` for Oxipng.

The runs use physical CPU 6 and NUMA node 0. ripgrep and fd use full inputs,
two warmups, and 9 and 7 measured repetitions. Oxipng uses the quick input, one
warmup, and 5 repetitions. Every variant produces the same application output
hash. The measured artifact implementation-bundle SHA-256 is
`7e98e63ce2fbeccc361ea57bd26773ccdb02664b83d772f0475161c980c55929`;
the pass source SHA-256 is
`ae7dd0da2368c670323287647c94ce5a90069b6f2e4a3d51b298d48cb9a5ac63`.
The current runner bundle is
`94ede1223c7b348639a7041a40a5b1840a6840cc18b4dbcf7d0f7a6ef8bb2cf4`
after post-measurement binary-reuse validation hardening; allocator and pass
sources remain unchanged.

Coverage means typed allocation events divided by total allocation events for
the exact application revision and input. Report it per application. It has no
source-line, type, byte, or universal-program denominator, and its denominator
differs from the paper's 72.17% result. The matrix is a measured-source
diagnostic; the paper reproduction and publication-grade inference remain
deferred.

The matrix retains default libc-managed rseq and sets no glibc tunable. Linux
rseq permits this configuration, and the production allocator hot path makes no
rseq call. The allocator's private rseq self-registration tests alone require
`GLIBC_TUNABLES=glibc.pthread.rseq=0` at process startup.

Two final implementation changes are relevant in backup discussion. Hosted
recovery records stay in the pointer-derived home shard and home overflow, with
legacy exhaustive search available only after legacy non-home state is
observed; `fixed_heap` preserves bounded cross-shard inline capacity. Generic
raw allocation misses publish strict lifecycle state once and then complete
semantic admission through an after-publication path; cache hits, guarded
mappings, and raw alias rejection retain their original boundaries. Ordinary
semantic scopes remain conservative for cross-thread recovery, explicit
`_local` scopes remain local, and exact `Absent` history observations hold an
eviction lease through admission. The eight-way history fails stop when all
ways are leased simultaneously.

On the pinned fd full input, these changes move `typed_plain` from
`0.611635718 s` to `0.464682945 s` (`-24.026192%`) and `typeiso_perf` from
`0.573481469 s` to `0.465833677 s` (`-18.770928%`), while both remain at
`6144 KiB` median peak RSS and event coverage remains `74.11%`. The native route
moves by `-0.218373%`. Treat this as a source-bound directional optimization
result for one input, with no stable cross-workload speedup claim.

Use this safe wording in the main talk:

> **Full paper performance reproduction was intentionally deferred after 20 source-bound historical records. Current claims are limited to functional and mechanism evidence; reduced benchmark numbers are diagnostic only, and no publication-grade percentage claim is made from them.**

Main Slide 28 should show only this stable conclusion and the four-tier ladder. Put live HEAD, probe artifacts, the freeze digest, and the stop record in B18/speaker notes, and refresh them on the defense date.

Do not compare the raw timings of these 20 historical records. Do not describe diagnostic smoke, plan readiness, or incomplete timing records as performance conclusions. Relevant entry points:

- `evaluation/results/claim_check_current.json`
- `evaluation/results/overclaim_worklist.json`
- `evaluation/results/paper_performance_gap_plan.json` (historical/deferred)
- `evaluation/results/platform_matrix_audit.json`
- `docs/evaluation-gap-analysis.md` (the opening supersession note takes priority over the historical queue)
- `.omx/handoff/g001-performance-campaign-stop-user-objective-change-20260712T030350Z.json`
- `/Users/hqzhao/Downloads/UniAlloc-G001-freeze-235549b2/evaluation/raw/source-freeze-required-bound-plan-235549b2-20260711a/final-verification.json`
- `.omx/ultragoal/ledger.jsonl` (entry point for G001 supersession, G002 functional evidence, and current-source probes)

Regenerate this slide's live HEAD and current probe summaries before the formal defense. Preserve the historical freeze digest and 20-record stop status unchanged.

### G. Judgment, agenda, and close — 42:00--45:00 (Slides 29--31)

| # | Suggested English title | Purpose of this slide | Figure | Time / likely question |
|---:|---|---|---|---|
| 29 | **The scientific contribution is a semantic allocation contract bounded by trust, coverage, and deployment.** | Synthesize the contribution, alternatives, and limitations; preserve the scientific conclusion beyond replication status | 3 rows: contract / demonstrated use / boundary | 1:15 |
| 30 | **The next falsifiable question is the minimal stable identity contract under partial coverage and FFI.** | Dissertation agenda: identity stability/collision/TCB, partial coverage, FFI/cross-language, and exploit corpus; the complete source-bound matrix is validation infrastructure | Research question → discriminating experiment → possible outcome | 1:15 |
| 31 | **Preserve semantics, separate policy, and make evidence provenance explicit.** | Close with three sentences, return to Slide 2, and enter Q&A | Three contributions in the same colors; add no new data | 0:30 |

Recommended three closing sentences:

1. **UniAlloc shows that compiler-known heap semantics do not have to disappear at the allocation boundary.**
2. **Those semantics can drive policies without binding the policies to one platform mechanism.**
3. **The next scientific question is the minimal stable identity contract that remains useful under partial coverage, FFI, and adversarial conditions.**

## 5. Interruptible 42-minute version

When the committee asks questions during the talk, preserve a measured pace and cut slides in the following order until the estimate reaches 42 minutes. Use the final two cuts only for longer interruptions:

1. Move Slide 18 (type isolation vs quarantine) to backup; add one spoken sentence to Slide 17.
2. Compress Slide 20 (memory retention) to 45 seconds; move the detailed figure to backup.
3. Compress Slide 23 (two target classes) into one build animation on Slide 22.
4. Merge Slides 26--27 into one "historical result ranges" slide; retain the Slide 28 evidence boundary.
5. Present only the first two next steps on Slide 30.

Always retain Slides 2, 4, 7--9, 10--16, 17, 21, 24, 25, 28, 29, and 31. They form the complete argument chain.

## 6. 5 extension slides for a required 53--55-minute main talk

Insert the following slides into the main deck; avoid filling time with additional feature slides.

| Insert after | Extension slide title | Value | Approximate time |
|---|---|---|---:|
| Slide 5 | **Allocation separates a hot path from refill and raw-memory acquisition.** | Explain cache/zone/backend in more detail for committee members outside allocator research | 1:30 |
| Slide 13 | **The MIR transformation preserves the original call and its cleanup behavior.** | Use a real but simplified before/after MIR example to demonstrate the compiler work | 1:45 |
| Slide 19 | **Allocation and deallocation must recover the same semantic identity.** | Explain the invariant across direct-local, recovery, and cross-thread paths | 1:30 |
| Slide 23 | **Retargeting is a recipe of PAL, concurrency, and metadata choices.** | Compare the actual adaptation surfaces for hosted, kernel, and fixed heap | 1:30 |
| Slide 25 | **A claim is only as current as its source binding and complete matrix.** | Explain repetitions, geomean, raw evidence, fingerprint, and the fail-closed gate | 1:30 |

The five extension slides have a 7:45 content budget. Added to the 45-minute main line, they reach about 52:45; transitions and one brief interruption should bring the talk to 53--55 minutes. Preserve time for questions within a 60-minute slot.

## 7. Slide production system for faster preparation and easier Q&A

### 7.1 Fixed visual grammar

Use only four semantic colors throughout the deck:

- **Blue**: compiler/semantic information;
- **Orange**: allocation policy;
- **Green**: platform/backend/PAL;
- **Gray**: conventional fallback or material outside the current claim scope.

Reuse these colors on every architecture/mechanism slide. The colors should identify the active layer immediately for the committee.

### 7.2 Evidence badge

Every results slide must carry one badge in the lower-right corner:

- `PAPER-REPORTED HISTORICAL`
- `CURRENT FUNCTIONALITY PROBE`
- `HISTORICAL SOURCE-BOUND RECORD — PARTIAL`
- `CURRENT SOURCE-BOUND CLAIM — COMPLETE`
- `PLANNED / INCOMPLETE`

Before current claim closure, do not use `CURRENT SOURCE-BOUND CLAIM — COMPLETE`. Stopped G001 records may use only `HISTORICAL SOURCE-BOUND RECORD — PARTIAL`, never a current badge. Under each number, also state toolchain/hardware, N, baseline, and source digest/date; link to a backup slide when this information is too long.

### 7.3 Minimum template for every slide

Use the same five lines in every slide's speaker notes:

```text
Takeaway: one sentence
45-second path: what I will say first
Likely question: the most probable interruption
Short answer: 20-30 seconds
Deep answer: backup slide number
```

This reduces both production cost and Q&A switching cost.

### 7.4 Strategy for existing figures

| Original figure | Treatment in main deck | Rationale |
|---|---|---|
| `../rust-alloc-paper/fig/overview.pdf` | **Redraw** as a 3-lane progressive architecture; put the original in backup | Comprehensive, but too dense for the main talk |
| `../rust-alloc-paper/fig/bg-alloc.pdf` | Redraw as cache→zone→backend→PAL | Suited to the paper rather than spoken instruction |
| `default-perf.pdf`, `perf-type.pdf` | In the main deck, redraw only the aggregate/per-workload takeaways used on Slides 26--27; put the originals in backup | A multi-baseline bar chart cannot be read in 30 seconds |
| `metadata-separation.pdf`, `hugepage.pdf`, `perf-pac.pdf` | Backup only; label the historical direction: 4% speedup, 2--4% speedup, 1--3% slowdown | They are outside the evidence required for the main H2 argument, and the current claim has not reproduced them |
| Windows/macOS result figures | Use paper figures only as backup/historical; list G002 functional status separately | Keep historical figures separate from live five-platform validation; show current functional PASS, current probes, and external validation gaps in separate columns |

Principle: **Each slide asks the committee to compare only one dimension.** Do not paste paper screenshots or paragraphs onto slides.

### 7.5 Claim ledger

Maintain the following compact table while producing the deck. Every claim slide must have one row:

| Slide | Claim | Evidence tier | Source | Assumption | Does not prove | Backup |
|---:|---|---|---|---|---|---:|
| 16 | real compiler-to-runtime path exists | latest external-app source-bound evidence + current bounded probes | `a57d318...` `oxipng-current-a57d318-20260713` acceptance + `8e4d37c` canonical-Vec spoof regression + generated multi-module probe | Oxipng counts bind only to `a57d318`; current exact-wrapper results bind to `8e4d37c`; separate revisions and static/runtime denominators | universal coverage/unmodified-app deployment/stable ABI/whole-app exact pairing/performance | B3--B4/B18 |
| 21 | ordinary cross-class reuse is separated for distinct trusted identities | current adversarial regression + implementation | `3acbd6d...`, `5eb25f5...`, `1228f71...`, `8bc2809...`, `6700ca1...`, `2e3bc4c...`, `3ccd464...`, `ee9d0c6...` tests + type-isolation code | covered path, trusted exact identity; bounded internal segregated-entry and delayed-owner integrity | universal memory safety/identical or spoofed input metadata/compiler type-ID collision/arbitrary corruption protection | B7--B10 |
| 24 | paper reported five environments and runtime has retargeting boundaries | historical + current functional probes | paper eval + PAL/fixed heap + platform artifacts; Wine `38b8b59` lifecycle `3/3` and `3c725a9` FLS-failure `1/1` remain separate | tested adapter/path and source-bound run | zero-porting/native Windows universality/current five-platform aggregate closure | B13/B17 |
| 26--27 | original prototype observed reported ranges | historical | paper eval | original setup | current reproduction | B14--B16 |
| 28 | paper-performance reproduction was explicitly deferred while functional work continues | current audit snapshot | G001 stop handoff + G002 probes | exact source binding and evidence tier | mechanism is absent or deferred claims failed | B18 |

## 8. Q&A method: answer directly, then expand the evidence boundary

Use this sequence consistently:

> **Claim → Mechanism → Evidence → Boundary → Next discriminating test**

- **20-second version**: direct yes/no plus boundary.
- **90-second version**: all five steps.
- **3-minute version**: switch to one backup slide, then explain the alternative/tradeoff.

When evidence is missing, do not guess:

> **I do not yet have evidence for X. The current implementation establishes Y under assumption Z. The discriminating experiment would be W.**

This demonstrates the research judgment required for a qualifier more clearly than an imprecisely expanded claim.

### Most likely committee questions and safe answer outlines

| Question | Direct first sentence | Required boundary / backup |
|---|---|---|
| **1. What is novel relative to mimalloc, snmalloc, Temeraire, and Scudo/hardened malloc?** | UniAlloc's central new input is compiler-provided semantics, together with automatic extraction and policy/mechanism separation; the novelty claim does not rest on another size-class free list. | Explain that the systems can complement each other; do not claim that every mechanism appears here for the first time. B1/B8 |
| **2. Why not leave everything to the compiler or Rust type system?** | The compiler supplies semantics, while the allocator controls physical reuse; unsafe code, FFI, and unsound APIs still manifest as heap reuse. | Defense-in-depth, not a type-system replacement. B9 |
| **3. Why Rust rather than starting with C/C++?** | Rust's `Box<T>`, `Vec<T>`, and centralized allocation path let the research isolate the interface question first. | Cross-language generality remains unproven. B3 |
| **4. What exactly does type isolation guarantee?** | For covered requests, when trusted allocator-visible metadata are distinct, ordinary cross-class reuse is separated; the plain cache performs an exact identity check even after an internal 64-bit lookup-key collision, and metadata-segregated occupied entries undergo keyed structural authentication before reuse, accounting, or eviction projection. | Identical or spoofed input metadata, compiler type-ID collision, corruption outside the cache, and uncovered/fallback paths remain outside this limited guarantee; it does not eliminate UAF. B7/B9 |
| **5. Why not use quarantine?** | Quarantine constrains reuse time; type isolation constrains reuse identity, addressing different steps and composing naturally. | Draw no performance winner from configurations that were not tested. B8 |
| **6. How is `type_id` unique and stable, and how are collisions avoided?** | The current runtime uses a 64-bit hashed allocator-visible identity; the stable compiler-level identity, collision policy, and trust contract still require explicit definition. | Do not describe the helper hash as cryptographic, collision-free, or stable across compilations. B9 |
| **7. Why does reuse identity exclude callsite?** | Allocation and drop may occur at different callsites; including callsite in identity would break valid pairing within one object category. | Callsite can still support provenance/policy, but should not be the default reuse key. B9 |
| **8. Why do the ABI/API changes preserve existing programs?** | Supported paths under the paired toolchain require no application source annotation; unknown metadata can use the conventional fallback. | This does not guarantee cross-rustc binary ABI, arbitrary custom allocator/FFI compatibility, or universal semantic coverage. B1--B3 |
| **9. How do realloc, drop, unwind, and cross-thread deallocation match?** | In the actual-rustc probe, alloc establishes identity, while realloc/dealloc can delegate recovery through strict-neutral metadata; when split FFI also receives stale explicit old metadata, the allocation-completion record remains authoritative for the old object, while the requested new identity binds only the replacement. When partial/generic helper Drop is not exposed by the provider, allocation-side recovery completes pairing. An independent nested-unwind probe also verifies that inner cleanup pops only the inner scope, restores the depth-1 outer scope, and returns to depth 0 after the outer return. | `Vec`, Layout shrink, split FFI, partial/generic handling, and nested unwind are each bounded lifecycles; the provider exposed no generic helper row, so the evidence cannot invent a generic skip or establish complete escape analysis. B4 |
| **10. Will the MIR pass be brittle across rustc versions?** | Yes. This is an explicit maintenance cost of compiler integration. | Present a stable ABI/contract and a versioned regression suite as the next step. B3 |
| **11. Can per-type/per-thread caches cause memory blowup?** | They increase retention/fragmentation risk; the current design mitigates that risk with bounded caches and empty-slab controls. | Show the historical anomalous workloads and current footprint controls. B10/B11 |
| **12. Is "retargetable" merely `cfg`/feature flags?** | The semantic policy and allocator pipeline are reused; PAL, raw memory, concurrency, and metadata layout are adapted. | Each target still requires real runtime evidence and platform work. B12/B17 |
| **13. How do you prove that hugepages were actually used rather than ordinary-page fallback?** | Current HEAD has `17/17` hugepage/ordinary-domain and fallback tests, while the local direct probe observed no real hugepage backing; macOS returned `KERN_INVALID_ARGUMENT`, so backing remains missing. | Domain separation/fallback PASS does not equal mapping/backing PASS; closure requires a suitable host and a fresh probe consistent with the current three-object side-cache materialization. B13 |
| **14. What has PAC actually validated so far?** | Current HEAD validates the allocator PAC metadata's safe software fallback and typed side-cache reuse; an independent `no_std` consumer contract isolates std-only dev-dependencies and permits a real arm64e allocator runtime probe built with `rust-src`. | External ABI evidence cannot replace allocator runtime evidence; only a source-bound arm64e `no_std` probe can support hardware functional evidence, and C006 cost/percentage remains deferred. B12 |
| **15. What is the denominator for 72.17%, and is it current?** | The original paper describes "72.17% of objects" in the standard Rust `alloc` benchmark; it is not a closed current source-bound number. | When raw evidence does not define the event/object denominator, preserve the original terminology; present the original method, fallback, and current audit. B14/B18 |
| **16. Why does 99.851437% coverage appear now?** | The repository cargo-test hook observed the `std_bench` test-mode `430/430` finite inventory; this is not independent actual-wrapper compiler coverage, performance, or a whole-program denominator, and it has no independent log artifact. The historical `99.851437%` remains G001 freeze-bound evidence; the latest source-bound Oxipng one-shot at `a57d318` separately reports actual direct/scope/Drop/ownership counts of `6/260/320/12`, while retaining unresolved `566/2`, multi-owner `117`, and `whole_program=false`. The later `8e4d37c` reports only the independent current/pinned exact-wrapper soundness closure; results from different revisions or denominators cannot be converted or merged into the older percentage. | Check the source digest, denominator, actual-rewrite/dynamic-execution evidence, and evidence tier first; do not rebind across revisions or present the value as a performance claim. B18 |
| **17. Is the evaluation fair?** | Comparison requires the same workload, baseline, configuration, repetitions, explicit normalization, raw provenance, and source binding. | State proactively the limits of the original paper's old toolchain/hardware, simulation, and absence of separate uncertainty/significance analysis. B14--B16 |
| **18. Has the security benefit actually been measured?** | Current adversarial reuse regressions use same-layout objects, cross-thread recovery, and different trusted `type_id` values to show that the covered cache path blocks cross-type address reuse; the plain cache also has a forced lookup-key collision regression, though no systematic exploit-success study yet exists. | Same-type reuse, fallback, identical/spoofed metadata, compiler type-ID collision, and a real exploit corpus remain uncovered; the next measurements are reuse-success rate and attacker capabilities. B19 |
| **19. Does the current source support five platforms?** | G002 has a macOS functional PASS; Windows has two separate Wine 10 PASS results: `38b8b59` lifecycle `3/3` and current-source `3c725a9` FLS failure-path `1/1`. It also has current-source Redox build/codegen/ABI, Rust-for-Linux and BlogOS no_std final-link contracts, and current fixed/hosted smoke. Current Redox runtime, Rust-for-Linux kernel load/run, and BlogOS boot validation still depend on external runners/assets. | The three lifecycle tests at `38b8b59` and the exact `FlsAlloc`/`FlsSetValue` ownership-retention regression at `3c725a9` bind to different revisions and cannot be added together; the latter proves only owner preservation/reclamation after failure and exact allocation reuse. Wine/local-link evidence does not establish native Windows universality or current-HEAD five-platform hardware closure. B17/B18 |
| **20. What is the most important dissertation next step?** | Identify the minimal stable identity contract that remains useful under partial coverage, FFI, and adversarial metadata. | The source-bound matrix, exploit corpus, and cross-platform experiments are infrastructure for testing that question; then extend cross-language support and policy automation. B19 |

### High-risk wording to avoid

- "UniAlloc **prevents UAF**."
- "UniAlloc **proves Rust memory safety**."
- "The 64-bit semantic key is **collision-free, unforgeable, or stable across compilers**."
- "Fallback proves **binary ABI and FFI compatibility**."
- "The current version **runs on five platforms**."
- "Current coverage **is 72.17%**" or "**is 99.85%**" without the denominator, digest, and evidence tier.
- "PAC's **1--3% overhead has been reproduced by the current source**" when the evidence includes only a hardware functionality probe and no complete cost matrix.
- "Hugepages **are used**" without backing/mapping evidence.
- "All tests pass, therefore the paper claims are reproduced."
- "Retargeting requires no platform work."
- "The allocator has no overhead."

## 9. 19 backup slides ordered by question probability

Use `B1`--`B19` directly as slide numbers and record each jump target in the main-talk speaker notes. To reduce first-pass production cost, **B1--B10 are required in the first pass; B11--B19 belong to the second/appendix pass.**

| Backup | Title / content | Primary question answered |
|---:|---|---|
| B1 | `GlobalAlloc` vs semantic alloc/dealloc/realloc signatures | compatibility/ABI |
| B2 | `AllocationMetadata` fields, flags, unknown/fallback state | metadata semantics |
| B3 | rustc optimized-MIR rewrite before/after | compiler automation/fragility |
| B4 | alloc→realloc→drop→unwind→cross-thread lifecycle | pairing/recovery |
| B5 | Original complete `overview.pdf` | architecture details |
| B6 | Cargo feature/configuration matrix and invalid combinations | configurability/test burden |
| B7 | Trusted hashed-key cache invariant and ordinary matching path | exact guarantee |
| B8 | Type isolation vs quarantine/Scudo | alternative mechanism |
| B9 | Type-ID stability, collision, same-type limitation, threat model | security boundary |
| B10 | Thread cache, zone, empty slab, RSS/fragmentation controls | memory overhead |
| B11 | Metadata layouts: in-band/segregated/compressed/hybrid | locality/security/footprint tradeoff |
| B12 | PAC/MTE/MPK/guard/quarantine: hardware vs software vs simulation | feature evidence |
| B13 | Hugepage mapping/fallback and fixed-heap/PAL adapters | backing/retargeting |
| B14 | Original benchmark suite, baselines, hardware, toolchain | method validity |
| B15 | Six runs, discard first, geomean, normalization | statistics |
| B16 | Original per-workload plots; aggregate only after raw view | outliers/fairness |
| B17 | Platform-by-platform adapter and evidence matrix | retargeting claim |
| B18 | Current source fingerprint, C001--C007, worklist, gap plan | provenance/current status |
| B19 | Future experiment design: stable identity contract, exploit corpus, source-frozen matrix, FFI frontend | dissertation direction |

## 10. Opening, transition, and closing scripts

### 60-second opening (rehearse verbatim)

> Allocators can observe layout and runtime state, but the conventional Rust allocation API does not expose language-level information such as type or module context. Rust already knows that information when many heap objects are created. My research question is whether a paired compiler/runtime can carry those semantics without requiring source annotations on supported paths, and whether the resulting policy boundary remains reusable across userspace, kernels, and constrained systems. UniAlloc explores that question through an optional semantic API, compiler-assisted extraction, and a retargetable allocator runtime. I will show what this architecture enables, its trust and compatibility contract, and what the evidence does and does not establish.

### Three key transitions

1. **Gap → design**: "If the missing resource is semantic information, the first design question is how to carry it without making compatibility conditional."
2. **Design → policy**: "A semantic channel matters only if it changes a meaningful allocator decision; type-isolated reuse is the representative case study."
3. **Policy → evaluation**: "The right evaluation is therefore not one benchmark number; it is a set of separate tests for feasibility, cost, coverage, and retargeting."

### 30-second closing

> UniAlloc’s central result is that heap semantics do not have to disappear at the allocator boundary. A compiler-assisted, optional channel can expose trusted semantics while preserving a conventional fallback, and a policy/mechanism separation can reuse the allocator across different deployment environments. The next falsifiable question is the minimal stable identity contract that remains useful under partial coverage, FFI, and adversarial conditions; the source-bound evaluation matrix is how we will test that contract, not the contribution itself.

## 11. Rehearsal and completion criteria

### Four rehearsal passes

1. **Logic rehearsal, untimed**: State only one takeaway per slide; merge slides whose takeaways are identical.
2. **45-minute rehearsal**: Record the actual time for every section, not only the total; 48 minutes is the hard ceiling.
3. **Interruption rehearsal**: Ask someone to interrupt randomly on Slides 4, 13, 17, 22, and 28; practice a 20-second answer and return to the argument chain.
4. **42-minute rehearsal**: Apply the cuts from Section 5 and verify that the H1/H2/H3 closure remains intact.

MIT's practical guide recommends writing one takeaway per slide and rehearsing with people from different technical backgrounds. That advice motivates this template's answer-title format. Reference: [Practical Advice for Preparing Your Qualifying Exam Presentation](https://mitcommlab.mit.edu/meche/2021/04/20/practical-advice-for-preparing-your-qualifying-exam-presentation/).

### Definition of done for the main deck

- [ ] The 31-slide main deck targets 45 minutes in rehearsal, and every complete rehearsal stays within 48 minutes.
- [ ] Every title is a complete claim sentence.
- [ ] Every number includes an evidence badge, baseline, N, toolchain/hardware, and date/digest.
- [ ] H1, H2, and H3 each have a `Supports / Does not establish` summary.
- [ ] Historical paper results and current implementation/current claim-grade evidence are visually distinct.
- [ ] All 19 backup slides are reachable within 10 seconds.
- [ ] All 20 table questions can be answered in both 20-second and 90-second versions.
- [ ] The 60-second opening and 30-second closing can be delivered without notes.
- [ ] Regenerate Slide 28/B18 live HEAD, H1/H2/H3 functional probe summaries, and deferred-claim status before the defense.

## 12. Minimal Source Map

Open these files first when building the slides. Copy no factual claims from chat history.

| Purpose | Authoritative entry |
|---|---|
| Research question/contributions/scope | `../rust-alloc-paper/intro.tex:217-286` |
| Design principles | `../rust-alloc-paper/rethink.tex:1-57` |
| Paper architecture | `../rust-alloc-paper/sys.tex:52-114` |
| Related-work/novelty boundary | `../rust-alloc-paper/discuss.tex:6-66` |
| Original evaluation method/results | `../rust-alloc-paper/eval.tex:63-91,168-210,233-303,314-344,380-455` |
| Runtime/platform selection | `unialloc/src/lib.rs:89,129-150` |
| Semantic metadata/API | `unialloc/src/alloc_api/type_isolation.rs` (`AllocationMetadata`, semantic alloc/dealloc/realloc) |
| GlobalAlloc semantic/fallback routing | `unialloc/src/cache/mod.rs` (`unsafe impl GlobalAlloc for RustAllocator`) |
| Compiler MIR rewrite | `tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs` |
| Copied-byte collect canonical-Vec spoof regression | `tools/unialloc-rustc-pass/test_mir_slice_iter_hazard_nonowner.py` (`8e4d37c`; current/pinned actual wrapper; canonical `Vec<u8>` applied, callback-bearing fake `[lib] name="alloc"` destination unresolved/audit-only; exact functional soundness only) |
| Delayed-free + metadata-segregated rejection safety regression | `unialloc/src/alloc_api/type_isolation.rs` (`ee9d0c6`; forced inline/bucket rejection, terminal-release barrier, owner continuity, duplicate-release guard; hosted/fixed exact `1/1`; bounded test-only evidence) |
| Vec realloc/isolation actual-rewrite probe | `unialloc/src/bin/rustc_driver_mir_vec_realloc_identity_probe.rs`, `tools/unialloc-rustc-pass/test_mir_vec_realloc_identity_probe.py` |
| Cross-thread Box-to-Vec actual-rewrite/safety probe | `tools/unialloc-rustc-pass/test_mir_cross_thread_box_slice_into_vec_rebind.py` |
| String-to-Vec actual-rewrite/safety probe | `tools/unialloc-rustc-pass/test_mir_string_into_bytes_rebind.py`, `unialloc/tests/string_into_bytes_rebind.rs` |
| Exact str-to-owned String actual-rewrite/isolation probe | `tools/unialloc-rustc-pass/test_mir_str_to_owned_outer_owner.py` (`24bb079`; current/pinned actual wrapper, slice/generic/custom fail-closed controls, functional only) |
| Exact fmt-format adversarial fail-closed probe | `tools/unialloc-rustc-pass/test_mir_fmt_format_fail_closed.py` (two-crate current/pinned actual wrapper; reentrant allocating `Display`; functional only) |
| Vec-to-boxed-slice actual-rewrite/safety probe | `tools/unialloc-rustc-pass/test_mir_vec_into_boxed_slice_rebind.py`; runtime regressions `vec_into_boxed_slice_transfers_exact_and_moved_shrink_identities`, `vec_into_boxed_slice_rejection_preserves_exact_source_policy`, `vec_into_boxed_slice_missing_record_suppresses_outer_and_auto_attribution` |
| VecDeque same-layout actual-rewrite/isolation probe | `tools/unialloc-rustc-pass/test_mir_vecdeque_same_layout_type_isolation.py` (`5eb25f5`; hosted/fixed one-shot functional evidence) |
| VecDeque capacity outer-owner actual-rewrite probe | `tools/unialloc-rustc-pass/test_mir_vecdeque_capacity_outer_owner.py` (`c02baa6`; current/pinned exact capacity paths, fail-closed negatives, functional only) |
| Rc outer-owner actual-rewrite/isolation probe | `tools/unialloc-rustc-pass/test_mir_rc_new_outer_owner.py` (`1bd0c9d`; current/pinned functional evidence, exact constructor boundary) |
| HashMap capacity outer-owner actual-rewrite probe | `tools/unialloc-rustc-pass/test_mir_hashmap_with_capacity_outer_owner.py` (`715ba13`; current/pinned functional evidence, cache-counter oracle only) |
| HashSet capacity outer-owner actual-rewrite probe | `tools/unialloc-rustc-pass/test_mir_hashset_with_capacity_outer_owner.py` (`39c827b`; current/pinned functional evidence, cache-counter oracle only) |
| Multi-crate same-type-id module isolation probe | `tools/unialloc-rustc-pass/test_mir_multicrate_module_isolation.py` (`0704852`; Cargo metadata plus no-metadata direct-rustc fallback; functional only) |
| Lifetime-hint cache isolation probe | `unialloc/tests/type_isolation_lifetime_hint.rs` (`eadfa9c`; hosted/fixed-heap manual metadata lifecycle; functional only) |
| Placement-hint cache isolation probe | `unialloc/tests/type_isolation_placement_hint.rs` (`a379f23`; hosted/fixed-heap manual metadata lifecycle; functional only) |
| Plain type-cache forced-key-collision regressions | `unialloc/src/alloc_api/type_isolation.rs` (`8bc2809`/`6700ca1` exact identity + `2e3bc4c` bounded-probe exhaustion; hosted/fixed functional only) |
| Metadata-segregated collision/tamper fail-stop | `unialloc/src/alloc_api/type_isolation.rs` (`3ccd464`; forced inline/materialized exact-identity collisions, structural discriminator/null-pointer tamper, and authenticated full-bucket projection; hosted/fixed functional only) |
| Cross-thread authenticated policy composition regression | `unialloc/src/alloc_api/type_isolation.rs` (`5d0822a`; exact test `cross_thread_authoritative_policy_composition_quarantines_and_reuses_exact_identity`; hosted/fixed functional only) |
| Cross-thread authenticated split-realloc composition regression | `unialloc/src/alloc_api/type_isolation.rs` (`7ec42d4`; exact test `cross_thread_split_realloc_preserves_authenticated_old_and_new_identities`; hosted/fixed functional only) |
| Custom ADT destination discovery / fail-closed provenance | `tools/unialloc-rustc-pass/test_mir_non_generic_adt_destination_owner.py` (`465234c` unsafe positive-only attempt superseded by `504ed10`; current/pinned actual wrapper, exact internal constructors + Drop only) |
| Delayed-free duplicate-quarantine regression | `unialloc/src/alloc_api/type_isolation.rs` (`05d18be`; current-thread TLS quarantine, stale-mask and omitted-flag bypass; hosted/fixed-heap functional only) |
| Vec-to-IntoIter and String-to-Box-str pairing probes | `tools/unialloc-rustc-pass/test_mir_vec_into_iter_rebind.py`, `tools/unialloc-rustc-pass/test_mir_string_into_boxed_str_rebind.py`, `unialloc/tests/string_into_boxed_str_rebind.rs` (`32c3c5b`) |
| Box-str-to-String actual-rewrite/safety probe | `tools/unialloc-rustc-pass/test_mir_boxed_str_into_string_rebind.py`, `unialloc/tests/boxed_str_into_string_rebind.rs` (`ded36de`; current-rustc functional only) |
| Supported/ambiguous Clone actual-rewrite probe | `unialloc/src/bin/rustc_driver_mir_ambiguous_clone_fallback_probe.rs`, `tools/unialloc-rustc-pass/test_mir_ambiguous_clone_fallback_probe.py` |
| Cargo multi-crate target allowlist regression | `tools/unialloc-rustc-pass/test_mir_wrapper_target_allowlist.py` |
| Nested unwind actual-rewrite probe | `unialloc/src/bin/rustc_driver_mir_semantic_scope_unwind_probe.rs`, `tools/unialloc-rustc-pass/test_mir_semantic_scope_unwind_probe_runner.py` |
| Arbitrary dependency-factory fail-closed provenance | `tools/unialloc-rustc-pass/test_mir_dependency_factory_provenance_fail_closed.py` (`9a02767`; current/pinned actual wrapper, exact control only) |
| Clone classifier fail-closed fixture | `tools/unialloc-rustc-pass/fixtures/mir_clone_candidate_classification.rs`, `tools/unialloc-rustc-pass/test_mir_type_isolation_security_probe.py` |
| Bounded current mechanism validation | `docs/allocator-mir-and-backend-validation.md` |
| Current actual-rustc identity/recovery replay | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/type-isolation-actual-rustc-2e3c0e2-a7b5f75-20260712/validation-summary.json` |
| Earlier presentation bundle at `a51960d` | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-type-isolation-a51960d-20260712T202813Z/summary.json` (HEAD `a51960d92a7c72deabaf25fc23e985c3b26c09a5`; scoped fingerprint `f23eda54...`; Oxipng build/run PASS; 6/383/320 direct/scope/Drop; static transfer `12/12`; dynamic `3/1/2`; oracle wrong-type non-reuse/same-type reuse, mismatch/corrupt `0/0`; whole-run corrected mismatch `13`, fail-closed `575`; 660 UniAlloc + 430 std-bench PASS; summary SHA `bc6f32805e58cb021223dde2e01a91887cbe36e653f5ff73257823349a12e685`) |
| Earlier source-bound H1 actual-rewrite evidence | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-current-head-94b2523d8823-20260712T060330Z/identity-hash-manifest.json` (historical to exact source; do not rebind) |
| External Rust application rewrite/isolation evidence | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-typeiso-current-576df61-20260712c-enriched/oxipng-realapp-repro-summary.json` (collection HEAD `576df61` + stable adapter, byte-identical commit `9bb9f8d`; after `f8f0d90`, 58/60 scoped hashes, with only post-run summarizer/test changes; one-shot build/run; 6/383/320 applied + 6 exact Box-to-Vec transfer candidates/applied/selected rows, 575 fail-closed, runtime `904/1070`/64 rows, 13 recovery-corrected non-exact, bounded oracle PASS; summary SHA-256 `cd14bf7bdcbff8fe99d5fc6d97888ce2065050c527b423baa63b856857bdd43b`; deterministic enrichment replay preserved raw evidence, program rerun=false, and counted direct rewrites independently). This does not establish a clean full working tree or performance/natural-app universal isolation evidence. The original `...20260712b` and historical `427583b` and `af342f7` artifacts remain append-only and cannot be rebound or attributed across revisions. |
| Earlier source-bound external Rust functional run | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-typeiso-46d5aaa-20260712e/oxipng-realapp-repro-summary.json` (HEAD `46d5aaa...`; pass SHA `52ab1465...`; one-shot build/run; output SHA `565f253e...`; 6/367/320 direct/scope/Drop; transfer static `12/12`, dynamic `3/1/2`; 529 fail-closed; 59 runtime rows; whole-run mismatch `0`; oracle PASS; summary SHA `f5b8ad7c...`). This run predates `0704852` module-id hardening and cannot be rebound across revisions. It supports no timing/performance claim. |
| Earlier accepted source-bound external Rust functional run | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-head-19ffb71-20260712-one-shot/acceptance.json` (claim-bearing source `19ffb710...`; pinned Oxipng v4.0.3 and `nightly-2022-07-01` one-shot build/run `0/0`; output SHA `565f253e...`; direct/scope/Drop `6/310/320`; static transfer candidate/applied/selected `12/12/12`, runtime `3/1/2`; fail-closed semantic/Drop `516/119`; 53 runtime rows; injected wrong-type non-reuse / same-type reuse PASS; whole-run mismatch `1` with status `recovery_corrected_non_exact`; acceptance SHA `efa66ec1...`). This artifact supports only the bounded functional/diagnostic claim for revision `19ffb71`. It cannot be rebound and does not support external-app counts for other revisions, whole-app exact pairing, universal coverage, or performance. |
| Latest external-app source-bound Rust functional run before `8e4d37c` | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-a57d318-20260713/{acceptance.json,oxipng-realapp-repro-summary.json}` (code-bearing HEAD `a57d3189d1cce3265170a69801d63a6ff5b0157b`; acceptance SHA `b84e5a56c19e9f79537dc33cc5a09cd2e9d184f10e6052de40c9f34252d3651b`; build/run `0/0`, output hash match; direct/scope/Drop/ownership `6/260/320/12`, unresolved semantic/Drop `566/2`, multi-owner Drop `117`, whole-program false; typed alloc/dealloc `860/850`, fallback `210/170`, raw alloc/dealloc/realloc `183/144/26`, dynamic transfer `3/1/2`; injected oracle wrong-type non-reuse / exact-type reuse PASS; the sole mismatch is the cross-crate `PathBuf` recorded-library/requested-binary module difference, with status `recovery_corrected_non_exact`). This is one-shot source-bound functional/diagnostic evidence only, with no timing loop. It supports no performance, whole-program, or universal claim and cannot be rebound to `8e4d37c`. |
| Cross-crate returned-owner isolation regression | `tools/unialloc-rustc-pass/test_mir_crosscrate_returned_string_recovery.py` (`e352206`; current + `nightly-2022-07-01` PASS; same nonzero String type id / distinct nonzero module ids; two visible allocation-identity corrections; wrong-module non-reuse, per-module exact reuse; typed `4/4`, recovery `2/2`, fallback/raw/corrupt `0`). Bounded enum(`String`) actual-wrapper functional security test only; it is not proof for the Oxipng aggregate/`PathBuf`, universal coverage, or performance. |
| Earlier source-bound external Rust ownership-transfer execution | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-ownership-runtime-38b8b59-20260712d-success/oxipng-realapp-repro-summary.json` (HEAD `38b8b59c...`, pass SHA `c2c83b...`, scoped fingerprint `8609ff...`, static transfer `6/6`, dynamic `1/1/0`, `png::PngData::output` old owner `Box<[u8; 8]>` / `exact_immediate_box_array_unsize`, output SHA `565f...`, summary SHA `292c8f...`). The earlier `...6d955c0...failed-after-first-repair` artifact remains as the historical `1/0/1` failure. One-shot diagnostic functional evidence only; it is not whole-program, benchmark, or performance evidence. |
| Source-bound H2 lifecycle evidence | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-h2-20260712T0604Z/audit.json`, `sha256sums.txt` |
| Source-bound H3 fixed/hosted smoke evidence | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-h3-20260712T060354Z-94b2523d8823/sha256-manifest.json` |
| Earlier Wine 10 FLS lifecycle evidence | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/windows-wine10-fls-38b8b59-20260712b/summary.json` (source `38b8b59`; fresh Windows GNU cross-build; exact tests `3/3`; exe SHA `7d4e060d...`; Wine image, runner, source-input hashes, and transcripts preserved; summary SHA `d44dbe74...`). Bounded Wine functional evidence only; it does not establish native-Windows universality or performance. |
| Current-source Wine 10 FLS failure-path evidence | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/windows-wine10-fls-failure-3c725a9-20260713T122658Z/summary.json` (source `3c725a9`; Wine image `sha256:a784009c...`; exact ignored test `global_thread_cache_fls_failures_do_not_lose_cache_ownership_on_windows` `1/1` PASS; FlsAlloc/FlsSetValue failure retains ownership, supports reclaim and exact allocation reuse; summary SHA `f122afa5...`). Report separately from the earlier `38b8b59` `3/3`; do not add the counts. Bounded Wine functional evidence only; it does not establish native-Windows universality or performance. |
| Current-source Redox build/codegen/ABI evidence | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/redox-current-source-b3f2cad-20260712/summary.json` |
| BlogOS fixed-heap publication/final-link contract | `tools/blogos-contract/` (`00a187b`, `a581cb4`; no_std ELF/global allocator/boot init/panic handlers + 5 host state-machine regressions; external boot runtime missing) |
| Rust-for-Linux final-crate force-link contract | `tools/rust-for-linux-link-contract/`, `kernel/kernel-modules/benchmarking/rust_bench.rs` (`bd9d927`; real bridge and 11 required symbols; external kernel runtime missing) |
| Constrained-platform ABI layout parity | `tools/rust-for-linux-link-contract/src/abi_layout_contract.rs`, `test_rust_for_linux_link_contract.py` (`315cfc4`; 5 records, 75 offsets/language; local-host C compiler boundary) |
| Cache/footprint controls | `docs/allocator-memory-footprint.md` (`d87d5e0`, `25d316c`; hosted footprint reduction + matching-saturation correctness repair; diagnostic only) |
| PAC functionality vs cost boundary | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/pac-current-head-94b2523d8823-20260712T060647Z/`, `docs/evaluation-toolchains.md:272-280` |
| Hugepage domain/fallback vs backing boundary | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/hugepage-domain-smoke-20260712a/hugepage-domain-smoke-summary.json`; current domain/fallback PASS, real backing MISSING |
| Current claim status | `evaluation/results/claim_check_current.json` |
| Missing claim requirements | `evaluation/results/overclaim_worklist.json` |
| Deferred paper-performance scope | `evaluation/results/paper_performance_gap_plan.json`, `.omx/handoff/g001-performance-campaign-stop-user-objective-change-20260712T030350Z.json` |
| Platform evidence | `docs/c007-redox-boot-evidence.md`; `evaluation/results/platform_matrix_audit.json` is a historical/stale aggregate from 2026-07-10 that still contains the resolved Redox blocker and old source digest, so it must not be cited as the current aggregate |
| Historical G001 freeze/partial records | `/Users/hqzhao/Downloads/UniAlloc-G001-freeze-235549b2/evaluation/raw/source-freeze-required-bound-plan-235549b2-20260711a/final-verification.json`; 20 accepted records remain historical/diagnostic only |
| Active implementation goal/status | `.omx/ultragoal/goals.json`, `.omx/ultragoal/ledger.jsonl`, live `git rev-parse HEAD` |

### Current-Source Evidence Boundary Four Days Before the Defense

- **Latest lifecycle and generation closure:** The G002 closure patch based on
  parent `364d786` keeps exact lifecycle history in a recent
  eight-way window per secondary history bucket. Every entry stores its exact
  address and its own epoch. A ninth distinct exact key displaces the oldest `Missing` record
  back to raw-only epoch-zero semantics. Exact keys and per-entry epochs prevent
  unrelated same-home addresses from inheriting stale-release rejection, while
  a raw reclaim with a known generation and `Absent` lookup is admitted as
  `Tracked`. Realloc admission carries the pre-lookup observation and pointer
  through metadata resolution, tag preflight, mutation, rollback, and release.
  Reviewed fixes consume the old record exactly once after successful in-place
  realloc, prevent unrecorded old storage from inheriting a recovery-required
  active or compiler-auto scope, compile and test alignment-changing grow under
  `quarantine`, and acquire admission before observable counters, selector
  consumption, or replacement publication.
  Deterministic exact-address ABA regressions cover `GlobalAlloc`,
  `SemanticAlloc`, and split-metadata FFI realloc; the local-tag alignment
  regression rejects corruption before replacement publication. With
  `GLIBC_TUNABLES=glibc.pthread.rseq=0`, hosted Type Isolation tests pass
  `732/732` serially and in parallel, `fixed_heap` Type Isolation passes
  `602/602` serially and in parallel, and `quarantine` Type Isolation passes
  `738/738` serially and in parallel. Standard `cargo test` exits
  `0`, including `semantic_std` `12/12` and `std_bench` `430/430`; a default-
  parallel `semantic_std` loop passes `30/30`. The evaluator doctor is ready
  with the paper checkout absent, quick end-to-end diagnostics pass `3/3`, and
  the realistic multi-module actual-rustc probe validates after installing
  `rustc-dev`, `rust-src`, and LLVM tools. The final Python suite result is
  `610/610`. This is source-bound
  functional evidence with publication performance deferred. Universal UAF or
  double-free prevention, stale-address detection after an exact generation
  ages out of its eight-way history bucket, forged-metadata resistance,
  universal compiler coverage, external-platform runtime closure, and
  publication-grade performance remain outside its scope.
- **P0 duplicate-free ownership closure:** `8a1cb06` first added plain,
  metadata-segregated, and cross-thread-hinted typed-cache duplicate-free
  regressions. `a93cf97` then introduced a bounded, allocation-free, process-visible
  type-cache pointer registry. Cache publication first acquires exclusive ownership; duplicates
  fail-stop immediately, while registry pressure bypasses cache and raw-frees. Pop, eviction, thread
  drain, and delayed-free -> type-cache transfer retire/register in pairs, closing the window in which one address
  could enter two TLS caches simultaneously. The checkpoint `654e1d7` validation gate passed hosted
  `685/685` and fixed-heap `650/650`. This P0 correctness closure covers
  retained-cache ownership. Arbitrary already-reused stale pointers and performance
  remain outside its scope.
- **Registry saturation / tombstone safety:** The deterministic regression in `45c5e8c`
  constructs `PROBE_LIMIT + 1`
  synthetic aligned keys in one ownership shard/probe window. It verifies that a full window returns `Full`, a lookup still
  crosses a tombstone to find/reject a duplicate after an intermediate owner is deleted, the tombstone is then safely reused, and
  the global ownership count returns to `0` during cleanup. These keys participate only in hash/store/compare operations; they are never
  dereferenced or passed to the allocator. The hosted and `fixed_heap` exact regressions each
  passed `1/1`. This is bounded registry-pressure correctness evidence. Arbitrary address-space
  collisions and concurrent linearizability remain outside its scope.
- **Type-cache ownership dispatch guards and real-free pressure:** The three white-box
  regressions in `bbdda3c` verify distinct properties. After bounded registry ownership is published manually,
  raw dealloc/realloc on separate live pointers fail-stop before freeing, copying, or modifying storage
  and leave bytes/count unchanged. Isolated child death tests call
  `abort` inside the panic hook, preventing panic unwind across `GlobalAlloc::{dealloc,realloc}`. When the probe window of a real typed
  object is filled with synthetic colliders, semantic free observes
  cache bypass `1` and insert/hit `0`; it publishes no target pointer and leaves existing colliding
  owners undisturbed. The entrypoint tests inject registry state rather than a complete real cache
  insertion. The pressure test uses a real allocation, while its pressure keys are synthetic.
  Hosted/fixed targeted tests both passed `3/3`, and the normal pre-commit gate passed allocator `689/689`.
  The repository cargo-test hook observed the finite `std_bench` test-mode inventory at `430/430`.
  That hook result provides neither independent actual-wrapper compiler coverage nor a
  whole-program denominator, and it has no independent log artifact. These results verify only the dispatch/bypass behavior of
  already-published bounded ownership state. General
  double-free, UAF, arbitrary concurrent
  races, universal memory-safety proofs, and benchmark claims remain outside this evidence.
- **Single-owner `Result` Clone actual rewrite:** `55148cd`, with formatting finalized in
  `52342fb`, gives ordinary Rust
  `Result<Vec<ProducerPayload>, u8>::clone` exactly one applied semantic scope through the actual `RUSTC_WRAPPER`.
  The compiler/runtime type id is `11653960357981974603` in both cases,
  while the same-layout Consumer identity differs. Runtime typed
  alloc/dealloc/cache-hit/cache-insert is `1/1/1/1`, fallback/raw is `0`, and clone
  reuses only exact Producer storage and never obtains Consumer storage. The existing ambiguous
  `Result<Vec<ProducerPayload>, String>::clone` retains one fail-closed raw
  fallback. The current-source artifact is
  `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/result-clone-current-head-52342fbe-20260713/`,
  whose `summary.json` SHA-256 is
  `b7bbe7e16249d594da895a6a170d1b85120176dc1d7513ca4406897e7919c3e9`.
  This is a bounded functional/security probe for one supported single-owner case and one
  ambiguous negative control on current `nightly-2026-06-11`. Universal `Clone` coverage and performance
  remain outside its scope. A pinned-nightly PASS remains unsupported: that
  attempt encountered the existing explicit `std::mem::drop` audit-shape
  validator boundary before entering the new Result gate.
- **`Result` Clone partial unwind:** The generated Cargo app in `4d6a219` runs ordinary
  `Result<Vec<ProducerPayload>, u8>::clone` under the actual
  `RUSTC_WRAPPER`, with the third element clone panicking. The audit
  requires the Result scope to be actually applied and an unwind pop to be inserted. Runtime scope depth changes
  `1 -> 0`; `2` elements clone successfully; cleanup witness Drop runs `1` time; and typed dealloc/cache insert for the
  partial Vec buffer of `256 B` is observed exactly at `1/1`. The exact
  Producer then recovers the seeded address, while the same-layout Consumer cannot obtain Producer storage.
  Fallback/raw/mismatch/corrupt/dropped are all `0`. The initial `2/2` came from a statistics window that counted
  `catch_unwind` panic-transport cleanup; source double-free and pass
  cleanup bugs were excluded. The final witness narrows the window to partial-buffer cleanup, and independent
  review APPROVED. The `summary.json` SHA-256 for artifact
  `result-clone-partial-unwind-main-4d6a219-20260713` is
  `3d9704c2c2fb5d43809b2007a9bf8f42266975a292a32dbc8dd1490a4dcb8629`.
  This is bounded functional/security evidence, not universal unwind coverage
  or performance evidence.
- **PAL mutex handoff:** The OS-thread regression in `b197d4a` uses a zero-capacity channel
  to coordinate holder/observer without relying on sleep or timing guesses. It verifies that
  `try_lock` reports busy while the holder owns the lock, and that after release the observer
  sees the write and returns the new value to main. The exact test `1/1` and `sync::tests` `2/2` PASS. This is exclusion/visibility functional evidence for the current hosted pthread PAL;
  it does not replace runtime validation on other platforms.
- **Real Cargo generic/concrete boundary:** `654e1d7`
  `test_mir_generic_vec_type_isolation_fail_closed.py` creates a real Cargo application and
  compiles and runs it with an actual `RUSTC_WRAPPER`, `UNIALLOC_ACTUAL_MIR_REWRITE=1`, and
  `UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE=1`; this is not a dry-run.
  `Vec<T>::with_capacity` in `generic_roundtrip<T>` remains unresolved/audit-only,
  with no planned/applied row. Runtime generic typed counts are `0/0/0/0`, fallback/raw
  alloc/dealloc are `8/8`, and mismatch/corrupt/dropped are `0/0/0`, proving that an unknown generic identity
  safely fails closed. In the same invocation, same-layout `Vec<Producer>` / `Vec<Consumer>`
  each have one actually applied scope and distinct nonzero compiler type IDs. Runtime
  typed alloc/dealloc are `12/12`, cache hit/insert are `4/12`, and wrong-type address sets are disjoint;
  the recovered Producer set exactly equals the original Producer set, while fallback/raw/mismatch/corrupt
  are all `0`. This concrete positive control proves one bounded actual-rewrite
  type-separation path. **Generic positive isolation is still missing** and requires
  monomorphization-aware type evidence before a safe rewrite is possible.
- **Real Cargo cross-thread same-layout isolation:** `8e462ad` extends the multi-module Cargo
  actual-`RUSTC_WRAPPER` fixture. The main thread allocates `Vec<Producer>`, moves the owner
  to a worker, and drops it there. The compiler generates distinct
  nonzero type IDs `11365312940488603059 / 17474015272962783245` for Producer/Consumer; all three scopes
  receive cross-thread placement from `auto_cross_thread_escape`, rather than a manual metadata
  hint. The worker then allocates same-layout `Vec<Consumer>` and must not receive the Producer address; a later
  `Vec<Producer>` must recover the original address exactly. One fresh current-source validation reports typed
  alloc/dealloc/cache-hit/cache-insert `2/3/1/3`, with fallback/raw, recovery mismatch,
  corrupt slot, and dropped stats all `0`. This is bounded actual-rewrite,
  thread-transfer, and address-oracle evidence from a generated multi-module Cargo application;
  it does not establish arbitrary external-application coverage, universal memory safety, or performance.
- **Latest external-app source-bound real app before `8e4d37c`:** The pinned Oxipng v4.0.3
  one-shot build/run at `a57d318` completed `0/0` with a matching output hash. Artifact
  `oxipng-current-a57d318-20260713/acceptance.json` has SHA-256
  `b84e5a56c19e9f79537dc33cc5a09cd2e9d184f10e6052de40c9f34252d3651b`.
  Audit direct/scope/Drop/ownership counts are `6/260/320/12`, unresolved semantic/Drop
  are `566/2`, multi-owner Drop is `117`, and `whole_program=false`; runtime typed
  counts are `860/850`, fallback `210/170`, raw `183/144/26`, and the injected oracle passed. The sole
  cross-crate `PathBuf` module mismatch was safely corrected using allocation-time identity,
  with status `recovery_corrected_non_exact`. This proves that bounded actual rewrites on supported
  exact paths and the isolation mechanism execute in a real application; it does not prove whole-program coverage,
  universal isolation, or performance. This run had no timing loop, so performance wording is diagnostic-only;
  the later compiler closure at `8e4d37c` cannot be rebound to these application counts.
- **Coverage / performance boundary:** The repository cargo-test hook observed the `std_bench` test-mode `430/430` finite inventory. This is not
  independent actual-wrapper compiler coverage or a whole-program/universal denominator,
  it has no independent log artifact, and it does not rebind the historical `99.851437%` across sources. Small
  benchmarks support diagnostic optimization decisions only. The full paper performance
  matrix was explicitly deferred by the user's scope change, so reduced smoke runs support no paper
  percentage claim.
- **Real Rust application:** The Oxipng one-shot artifact
  `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-head-d50795f-20260713-one-shot/`
  binds `d50795f892bd38ca3e7fd083da7d6eacafd0db06`, with summary SHA-256
  `e8958683d1957c40f76e4400b25c506b16d70489e9aa2526b1bb5674411d3635`.
  Build/run passed; there were 6 direct rewrites, 236 scopes, 320 Drops, and 12/12 transfers;
  runtime typed alloc/dealloc were `856/846`, cache hits were `808`, and the injected isolation
  oracle passed. Present `592` unresolved rows, one recovery-corrected mismatch,
  and `whole_program_compiler_coverage=false` at the same time. This is functionality/isolation evidence, not a benchmark.
- **Realloc safety:** `1abb4dd` + `03a5ef0` make alignment-changing
  `Allocator::{grow,shrink}` return
  `AllocError` before mutation on a recovery-layout mismatch. The pre-fix error path could allocate/copy first,
  then release the old address using the caller's incorrect layout. `386759f` likewise fixes default/direct `SemanticAlloc`:
  the minimized pre-fix zero-size case incorrectly returned the `0x8` success sentinel; it now
  returns null before zero/in-place/move mutation. Both paths preserve the authoritative exact record,
  with hosted/fixed focused PASS. This is P0 correctness evidence, not a performance result.
- **Ownership transfer:** `003704a` makes exact `Vec<u8>::from(String)` actual-rewrite PASS under both current
  and pinned toolchains, with static/runtime `1/1` and `1/1/0`;
  wrong String does not reuse the address, while exact Vec does. Do not claim dynamic coverage for explicit `From<&str>` or
  custom allocators.
- **Exact `str::to_owned`:** `24bb079` lowers exact allocation
  `<str as ToOwned>::to_owned(&str) -> String` into a String scope. This gap appears 14 times in the frozen
  `d50795f` Oxipng audit, without cross-revision rebinding.
  Actual `RUSTC_WRAPPER` passes under `nightly-2026-06-11` and `nightly-2022-07-01`;
  slice/generic/custom controls fail closed, runtime typed alloc/dealloc are `3/3`,
  exact cache hit is `1`, wrong-type reuse is blocked, and fallback/raw/mismatch/corrupt are all
  `0`. This is exact-surface functionality/isolation evidence, not universal coverage or performance.
- **`24bb079` current-source Oxipng one-shot:** Artifact
  `oxipng-current-head-24bb079-20260713-one-shot` binds clean scoped `24bb079`
  and pinned Oxipng `dea2321`; build/run were `0/0`, with a matching output hash. Audit
  direct/scope/Drop were `6/250/320` and transfer was `12/12`, while semantic/Drop
  unresolved remained `576/2` and `whole_program_compiler_coverage=false`. Runtime typed
  were `856/846`, fallback `214/174`, cache hits `808`, and the injected oracle passed;
  whole-run mismatch was `1`, with status `recovery_corrected_non_exact`. The runner did not emit
  raw-no-metadata counters, so raw is missing rather than `0`. Relative to frozen
  `d50795f`, `236→250` / `590→576` is only cross-artifact arithmetic/inference consistent with
  14 exact matcher rows; it is not rebinding or a whole-program/performance conclusion.
- **`1a4127f` earlier source-bound Oxipng one-shot:** Artifact
  `oxipng-current-head-1a4127f-20260713-one-shot` binds HEAD `1a4127f`, with summary
  SHA-256 `118894ee3e08990a4d616110ad9001976c5ef082e1d3225e88dc9d070c41ce78`;
  build/run were `0/0`, with a matching output hash. Audit direct/scope/Drop were `6/256/320`,
  unresolved semantic/Drop were `570/2`, and `whole_program_compiler_coverage=false`;
  runtime typed were `860/850`, fallback `210/170`, raw alloc/dealloc/realloc
  `183/144/26`, cache hits `808`, and transfer `3/1/2`. The sole mismatch was
  cross-crate `PathBuf`: the allocation record came from the library crate and the Drop request from the
  binary crate. The type id matched, the module id differed, and the runtime used allocation-time identity
  correction, so this is not whole-app exact pairing. Relative to `24bb079`,
  scope `250→256` / unresolved `576→570` only corresponds to the six exact
  `<[u8] as ToOwned>::to_owned(&[u8]) -> Vec<u8>` rows in a cross-artifact comparison;
  it cannot support rebinding, a coverage percentage, a universal claim, or a performance result.
- **Cross-crate returned-owner security regression:** The actual-wrapper
  fixture at `e352206` makes the producer return `ReturnedString::Value(String)`, which the application
  then owns and drops; both current and pinned nightly PASS. The same nonzero String type id
  has distinct nonzero module ids in the two crates; two allocation-identity
  corrections are visible, the wrong module does not reuse an address, and each exact producer/application module
  recovers its own address. Runtime typed are `4/4`, recovery match/mismatch are `2/2`,
  and fallback/raw/corrupt are all `0`. This is independent bounded enum(`String`) functional
  security evidence; it does not prove aggregate Oxipng `OutFile`/`PathBuf` paths, universal
  cross-crate coverage, or performance.
- **Exact `fmt::format` intentionally fails closed:** 11 exact rows in current source
  remain unresolved/audit-only. The reason is `fmt::Arguments`,
  which can execute arbitrary, reentrant `Display` callbacks; a callback can allocate its own `Vec`/`Box`,
  and wrapping the whole call in an outer `String` scope would assign the wrong inherited identity. The new two-crate actual-wrapper
  probe passes under both current and `nightly-2022-07-01`; the helper is explicitly excluded from rewriting,
  and runtime on both sides reports callback `1`, typed alloc/dealloc/cache-hit/cache-insert
  `0/0/0/0`, fallback/raw alloc `3/3`, fallback/raw dealloc `3/3`,
  and mismatch/corrupt `0/0`.
  This is a bounded adversarial regression, not a universal proof or performance result.
- **Runner and historical artifact boundary:** `1fcb5c2` only makes future Oxipng runs retain raw
  counters. It neither reruns nor rebinds the `24bb079` one-shot, so raw in the older artifact
  remains missing rather than `0`.
- **Rejected hot-path optimization:** Three diagnostic baselines for inline type-cache POP had
  median/range `116.40 ns` / `115.66–116.74 ns`, versus `117.39 ns` /
  `116.51–118.19 ns` after the change, a `+0.85%` direction (slower). The change was precisely reverted.
  This explains why the optimization was rejected; it is not a performance claim.
- **Rejected ThreadCache lookup optimization:** The exact Collections one-shot measured
  `14.44 ns/iter` at earlier source `24ff781` and `16.02 ns/iter` at source `21c9e2b`,
  which includes P0 ownership changes. The revisions differ, so the delta cannot be attributed to lookup
  reuse, and the direction was unconfirmed. Therefore, `f5c4fa4` precisely reverted the optimization and stopped repeated timing.
  These one-shot values record only the reject/revert decision; they do not form a stable percentage or paper claim.
- Numbers for `d50795f`, `24bb079`, `1a4127f`, `f5c4fa4`, and `a57d318` bind only to their corresponding sources. Cross-artifact
  comparisons are not rebinding. They support no timing, paper percentage, universal
  coverage, whole-app exact-pairing, or performance claim.

---

**Final choice:** Build the main deck as one argument: "conventional Rust semantic gap → trusted optional compiler channel → bounded representative policy → retargetable boundary → evidence judgment." This makes slide production easier because every slide serves one hypothesis, and it simplifies Q&A because every answer returns to H1/H2/H3, the compatibility/TCB contract, the evidence tier, and an explicit boundary.

## Current implementation-first presentation checkpoint

- **Retained-cache fail-stop:** `e9d56f1` uses a real retained typed entry to
  verify that raw deallocation and reallocation fail before mutation, while an
  exact typed pop still recovers the entry precisely and performs one terminal
  release. This is bounded retained-cache ownership evidence, with no universal
  UAF or double-free detection claim.
- **Two owner boundaries for actual rewrites:** The ordinary `Result::clone`
  `Err` path in `c55883e` preserves Producer/Consumer isolation and completes
  typed cleanup `3/3` through normal Drop. The cross-thread `Vec -> IntoIter`
  path in `cf1e685` preserves the pointer and payload, prevents wrong-Vec reuse,
  and allows exact-IntoIter reuse, with static rewrite `1/1` and runtime transfer
  `2/2/0`. The corresponding final summary SHA-256 values are
  `000423fca7ca07b2c03d020d85f59766b4e546ce70d0de0b14b71c7ec301b844`
  and `0a9c868c8274485a4fc4597d04897b3a39f6e5cb49254fb57a18da7b0bac6269`.
  Each is source-bound by its own manifest. Placement in the latter is manual,
  with no automatic escape-inference claim.
- **Cleanup-funclet P0 closure:** The pinned baseline reproduces
  `funclet ... has 2 parents`. After `ce52203`, current and pinned HashMap/Vec
  probes plus nested unwind pass; the current cleanup call preserves
  `Terminate(InCleanup)`, and callback-capable HashMap paths remain audit-only.
  The failed `bc150b3` artifact remains append-only and is superseded only for
  current execution by successful evidence.
- **Earlier source-bound real Rust application:**
  `oxipng-final-ce52203-20260713-success` is a pinned, instrumented Oxipng v4.0.3
  one-shot with summary SHA-256
  `aee2957266eddfb88eee21fb6b689e45402bd7b230ec84fae6e55ef196fce39c`.
  Build/run is `0/0`, the output hash matches, direct/scope/Drop is `6/256/320`,
  static transfer is `12/12`, and runtime transfer is `3/1/2`. The same slide
  must show unresolved semantic/Drop `570/2`, multi-owner `117`, and
  `whole_program_compiler_coverage=false`.
- **Runtime oracle and wording boundary:** Typed is `860/850`, fallback is
  `210/170`, raw is `183/144/26`, cache hit/insert/bypass is `808/841/61`, and
  recovery is `830/1`; wrong-type non-reuse and exact reuse hold, with
  corrupt/dropped `0/0`. The PathBuf mismatch state is
  `recovery_corrected_non_exact`. This is instrumented, pinned functional
  evidence with no unmodified or universal application, performance, or paper
  percentage claim. The repository cargo-test hook observed a finite
  `std_bench` test-mode inventory of `430/430`. That observation supplies
  neither independent actual-wrapper compiler coverage nor a whole-program
  denominator, and it has no standalone log artifact.
- **`982ee0b` type-cache safety regressions:** The 16-thread same-key publication
  test requires exactly one owner: one `Inserted`, fifteen `Duplicate`, and zero
  `Full`, followed by a return to baseline. Foreign-thread raw deallocation and
  reallocation fail before allocator, TLS, or payload mutation for a real
  retained entry. The owner then performs exact typed reuse, unregisters, and
  releases once. Hosted and `fixed_heap` exact tests pass. This is test-only,
  bounded safety evidence with no forged-metadata, stale-pointer, or universal
  linearizability proof.
- **Generic fallback reallocation:** `c6c0152` verifies that
  `Allocator::allocate -> grow` without semantic metadata preserves the payload
  prefix and retains the raw fallback. Automatic allocation records, typed
  attribution, the type cache, delayed free, and stale ownership remain zero or
  unchanged. Hosted and `fixed_heap` exact tests pass. This supports one generic
  grow functional invariant, with no compiler-derived identity, full collection
  coverage, or performance claim.
- **Cross-thread plus unwind actual rewrite:** The ordinary Rust fixture in
  `87725dc` plus `4999ddc` moves `Vec<ProducerPayload>` from main to a worker.
  The actual wrapper rewrites `reserve(usize::MAX)`; represented depth is `1`
  during panic and `0` after unwind and at completion, while the pointer and
  payload survive. After worker Drop, a Consumer with the same layout cannot
  reuse the storage, while an exact Producer can. The isolated cleanup window
  records typed deallocation/insertion `2/2`, `1` each for Producer and Consumer;
  fallback/raw/mismatch/corrupt/dropped are all `0`, all 7 negative controls
  are rejected, and independent review reports `APPROVE`. Artifact
  `cross-thread-unwind-cleanup-feff198-20260713` has summary/audit SHA prefixes
  `7f150b77...` / `822bfaf6...`. Placement is manual, with no automatic escape
  inference, universal thread/unwind coverage, or performance claim.
- **Same-type-ID and full-identity recovery:** `107cabe` constructs FFI metadata
  with an identical `type_id` but different module, lifetime, placement, and
  callsite values. The runtime recovers and caches only by allocation-time full
  identity, so the colliding requested identity cannot recover that address.
  Mismatch diagnostics preserve the identical requested and recorded `type_id`.
  Hosted and `fixed_heap` regressions pass. This is a runtime recovery defense,
  with no proof of compiler hash collision freedom or global `type_id`
  uniqueness.
- **Delayed-free to type-cache ownership handoff:** `b2d5eab` makes the common
  reclaim guard check delayed free before the type cache, matching the actual
  D-to-T publication order, and makes resolved deallocation use the same guard.
  A deterministic race regression covers the raw, `GlobalAlloc`, semantic, and
  resolved reclaim entry points. The owner can still pop exactly, validate the
  payload, and perform one terminal release. Hosted/fixed exact tests and the
  full `693/693` suite pass. This closes the D-to-T registry observation gap,
  with no universal UAF/double-free detector or linearizability proof for every
  state transition.
- **Terminal retained-ownership release:** `1d0d13f` closes the window in which
  delayed free and the type cache withdrew process-visible ownership before
  terminal raw release. Two deterministic tests fail first under the old order;
  after the fix, hosted and fixed each pass `2/2`. All six terminal release
  points complete the sole backend release before unregistering. An unavailable
  backend preserves ownership fail-safe. The pre-commit full suite passes
  `696/696`, and C002 passes `430/430`. This conclusion covers the concurrent
  terminal-release interval, with no coverage of arbitrary stale pointers after
  release and no general UAF/double-free guarantee.
- **Current gates:** The C002 current-source finite inventory remains `430/430`.
  It supplies neither a whole-program denominator nor a replacement for
  actual-wrapper evidence.
- **Two exact byte-Vec compiler surfaces:** `87e81e6` first makes the current and
  pinned actual wrappers each apply a direct `Vec<u8>` scope to
  `slice::Iter<u8>.copied().collect::<Vec<u8>>()`. The fail-first regression in
  `8e4d37c` then shows that a fake `[lib] name="alloc"` plus callback-bearing
  `FromIterator` was previously applied incorrectly, and tightens the matcher to
  the canonical rustc `Vec` diagnostic item. After the fix, the current and
  pinned canonical rows are applied, while the fake-alloc row is unresolved and
  audit-only. A custom raw-reference iterator fails closed, and Drop for
  `Zip<IterMut, IntoIter>` remains unresolved because its hidden
  `Vec -> IntoIter` transfer is not modeled. `3fc5a19` plus `a57d318` support only
  exact `vec![0u8; n]` for the canonical sysroot `Vec`; generic, custom-Clone,
  same-name, and `--extern alloc` spoofs all fail closed. Both surfaces show
  wrong-type non-reuse and exact-Vec reuse, with no arbitrary iterator, general
  `vec![value; n]`, or whole-program coverage claim.
- **Cache-rejection owner continuity:** Deterministic tests in `3044166` show
  that ordinary plain-cache rejection retains the type-cache owner before the
  sole raw release, while delayed-free plain-cache rejection removes only the
  temporary type-cache registration and retains the delayed owner. With two
  synthetic inline keys and a full aggregate budget, `ee9d0c6` deterministically
  forces delayed-free plus metadata-segregated insertion rejection. At the
  terminal raw-release barrier, delayed owner count is `1`, temporary type-cache
  owner count is `0`, the payload is intact, and foreign reclaim fails closed.
  Both registries return to zero afterward, side-cache corruption is `0`, and
  two immediate raw allocations have distinct, independently writable
  addresses, excluding duplicate backend release or a free-list alias. The
  owner-thread RAII fixture cleanup receives independent `APPROVE` review;
  hosted/fixed exact tests each pass `1/1`, the default pre-commit suite passes
  `699/699`, and the finite C002 inventory passes `430/430`. This evidence closes
  this forced rejection and terminal-release combination, with no general
  UAF/double-free or arbitrary metadata-corruption proof.
- **Latest generated multi-module source-bound check before `8e4d37c`:** The
  most recent generated Cargo actual-wrapper build/run at `a57d318` reports
  `validated=true`: 4 actual scope rows, transfer `1/1`, wrong-record and
  wrong-Blob-Vec non-reuse, exact Vec/Box reuse, and cross-thread same-layout
  Producer/Consumer wrong-type blocking plus exact-owner reuse. All
  fallback/raw/mismatch/corrupt/dropped counters are `0`. This is one generated
  application, with no arbitrary external-application, whole-program coverage,
  or performance evidence. It must not be rebound to a later compiler commit.
- **Latest external-application source-bound real Rust application before
  `8e4d37c`:** The same code-bearing `a57d318` is compiled and run once with
  pinned Oxipng `v4.0.3` in a clean detached worktree. Artifact
  `oxipng-current-a57d318-20260713/acceptance.json` has SHA-256
  `b84e5a56c19e9f79537dc33cc5a09cd2e9d184f10e6052de40c9f34252d3651b`;
  build/run is `0/0`, and the output hash matches exactly. The actual pass applies
  direct/scope/Drop/ownership `6/260/320/12`; 4 natural `reduced_alpha_*`
  functions hit the supported canonical `Vec<u8>` repetition scope. A separately
  labeled address oracle shows wrong-type non-reuse, same-type reuse, and corrupt
  `0`. The boundary must also show unresolved semantic/Drop `566/2`, multi-owner
  Drop `117`, `whole_program=false`, 1 PathBuf recovery correction, fallback
  allocation/deallocation `210/170`, and raw allocation/deallocation/reallocation
  `183/144/26`. This is a bounded functional pass with no exact whole-application
  pairing, universal safety or coverage, or performance conclusion. The one-shot
  has no timing loop; related performance numbers remain diagnostics and cannot
  support a paper or publication percentage. Later `8e4d37c` claims rely only on
  current/pinned exact-wrapper passes and do not reinterpret this Oxipng run.
- **Earlier compiled-Rust application check:** At code-bearing `1d0d13f`,
  `test_mir_realistic_multimodule_type_isolation.py` uses a real
  `RUSTC_WRAPPER`/MIR pass to compile and run a multi-module Cargo application
  once successfully. It observes 4 actual scope rows, String-to-Vec ownership
  transfer, Box/Vec wrong and exact reuse, automatic cross-thread placement, and
  same-layout Producer/Consumer wrong-type non-reuse plus exact-type reuse. All
  fallback/raw/mismatch/corrupt/dropped counters are `0`. Results are stored in
  artifact `current-source-realistic-typeiso-1d0d13f-20260713`. This is bounded
  functional evidence, with no arbitrary external-application, whole-program
  coverage, safety proof, or performance claim.
- **Historical single-sample diagnostic performance:** Default is
  `16.59 ns/iter`, type isolation is `25.47 ns/iter`, and the ratio is `1.5353` /
  `+53.526%`; each has n=1 on Darwin/current toolchain with layout-derived
  size/alignment identity, compiler-site replay disabled, and no median, range,
  or variance. Later bounded profile samples do not reproduce the `+53%`
  direction and likewise exercise only the layout-derived/raw allocator path,
  not the compiler typed ABI. No optimization was adopted from this sample, and
  no stable percentage is reported. These values supply no compiler-pass
  overhead, paper claim, or publication-grade result.
- **Profile-guided bounded A/B:** `908e12f` adds an inline fast path for an empty
  delayed-owner lookup. The source-bound A/B compares baseline `3fc5a199` with
  detached candidate `7964094`, which contains the same six-line patch, using
  one warmup followed by 3 interleaved runs each. For the same
  `vec::bench_with_capacity_1000`, baseline median/range is `15.36` /
  `[15.27,15.46] ns/iter`, candidate median/range is `14.41` /
  `[14.38,14.51] ns/iter`, and the direction is `-6.185%`. This single-machine
  Darwin, layout-derived/raw microbenchmark diagnostic supports only the
  directional decision to retain the candidate. It supplies no stable
  percentage, compiler-pass overhead, paper claim, or publication-grade result.
