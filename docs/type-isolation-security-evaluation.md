# Type Isolation Security Evaluation: RustSec and Rudra-PoC Corpus

## Corrected terminal scope

The review initially screened **53** candidates. A post-source audit excludes
RSH-006 from the heap-allocator efficacy scope: its dangling target and
vulnerability-relevant lifetime path have no `GlobalAlloc`-mediated
allocation, reclaim, or reuse edge. The corrected scope therefore retains
**52 heap-relevant candidates**: **48**
executable vulnerable witnesses with required controls and **4** audit-only
integration exclusions. The frozen 53-row screen and 49-executable report
remain historical provenance for the original execution campaign.

Allocator mechanisms provide qualified coverage for **43/48** executable
candidates. The mechanism sets contain 31 exact `reclaim_checks` detections,
1 exact recovery-layout validation, and 12 compiler-bound causal mitigations
of measured cross-identity reuse edges. RSH-002 appears in both the reclaim and
Type Isolation sets and contributes once to the 43-case union. Eleven cases
receive their only qualified allocator-mechanism evidence from Type Isolation.

The 12 Type Isolation experiments comprise two
`derived_vulnerability_edge` scopes (RSH-008 and RSH-041) and ten
`source_shaped_derived` scopes (RSH-002, RSH-042, RSH-052, RSH-055, and
RSH-064 through RSH-069). None has `published_source` coverage. They establish
mitigation of one measured reuse decision. Their automatic source-level
vulnerability-detection count is **0/12**. Every row records
`source_vulnerability_detection_validated=false`,
`vulnerability_specific_detection_signal=false`, and `claim_grade=false`.

The automatic probe makes the historical manual metadata helper a no-op, so the
compiler supplies every semantic allocation identity. Each accepted repetition
requires unique applied compiler-audit bindings for both requested and victim
sites, a runtime identity/module/callsite/layout join to those sites, and exact
retained-pointer equality: `last_wrong_identity_retained_ptr` must equal the
harness's parsed `original` or `stale_value` pointer. This last gate proves that
the object withheld by Type Isolation is the witness object.

RSH-052 adds a two-stage ownership proof from an exact compiler-bound `String`
allocation origin through an audited pointer-preserving `String -> Box<str>`
transfer. RSH-068 and RSH-069 preserve identity from allocation origins through
their `String` and `CString` ownership chains. All patched system,
`typed_plain`, and `typeiso` controls exercise safe same-identity functionality
without a matching attribution event. Separate allocator unit controls preserve
same-identity reuse and withhold eligible cross-identity reuse.

The attribution fields are `stats`-gated evaluation telemetry. The selected
retained pointer uses one companion atomic and leaves cache layouts and the
public semantic-stats snapshot unchanged. Production enforcement remains the
ordinary exact-identity cache decision. The completed paired
`typeiso_perf / typed_plain` ratio-of-ratios screen passed its merge gates:
Oxipng wall time changed **-1.6649%** and peak RSS changed **+0.583%**; the long
Ripgrep workload changed **+0.8107%** in wall time (95% bootstrap CI
**[-0.180%, +1.399%]**) while maximum RSS remained **7168 KiB** in both
snapshots. The gates require wall-time regression at or below 2%, Oxipng RSS
regression at or below 1%, and unchanged Ripgrep maximum RSS. The selected
macro builds retained unchanged aggregate semantic-rewrite counts; active
new-site evidence comes from the 12 RustSec matrices and compiler regression
tests. Evidence is under
`docs/evidence/rustsec-typeiso-automatic-20260715/performance-screen/`.

The corrected 48-case exclusive partition is 30 reclaim-only, 1
recovery-layout-only, 11 TypeIso measured-reuse-edge-only, 1 measured reuse
edge plus reclaim checks, 3 no-signal, and 2 mechanism-boundary cases. The
no-signal set is RSH-049, RSH-050, and RSH-075. RSH-003 and RSH-019 exercise
same-object or pre-reuse concurrency/lifetime requirements outside the
evaluated allocator contracts. The corrected UAF scope contains 18 retained
cases: 17 executable plus audit-only RSH-059. Allocator mechanisms cover 14/17
executable UAF cases. The historical all-53 bundle retains RSH-006 and the four
audit-only attempts solely as execution provenance.

The authoritative scope and figure are in
[`rustsec-security-scope-evaluation.md`](rustsec-security-scope-evaluation.md),
and the complete attempt ledger is in
[`rustsec-all-53-live-evaluation.md`](rustsec-all-53-live-evaluation.md). The
editable presentation figures, case membership table, and machine-readable
corrected counts are under
[`figures/rustsec-security-sets-20260715/`](figures/rustsec-security-sets-20260715/).
The post-source-audit correction is hash-bound in
`evaluation/config/rustsec_heap_posthoc_scope_corrections.json`. The
automatic per-case evidence and `derived-reuse-summary.json` are under
`docs/evidence/rustsec-typeiso-automatic-20260715/`. The frozen manually
annotated calibration matrices retain implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`
and remain a separate historical provenance epoch.

## Status and claim boundary

The repository now contains a pinned, machine-auditable classification corpus,
a source-pinned study inventory, and a repository materialization bundle for
studying UniAlloc against real Rust heap-safety failures:

- `evaluation/config/rustsec_heap_security_corpus.json`
- `evaluation/config/rustsec_heap_candidate_inventory.json`
- `evaluation/config/rustsec_temporal_reclaim_review.json`
- `evaluation/config/rustsec_heap_harnesses.json`
- `evaluation/config/rustsec_heap_expansion_harnesses.json`
- `evaluation/config/rustsec_heap_strong_batch_{a..f}_harnesses.json`
- `evaluation/config/rustsec_heap_complete_scope.json`
- `evaluation/config/rustsec_heap_posthoc_scope_corrections.json`
- `evaluation/config/rustsec_heap_mechanism_results.json`
- `evaluation/config/rustsec_heap_mechanism_amendments.json`
- `evaluation/config/rustsec_heap_primitive_overrides.json`
- `evaluation/harnesses/rustsec_heap/`
- `evaluation/harnesses/rustsec_heap_expansion/`
- `evaluation/scripts/audit_rustsec_heap_corpus.py`
- `evaluation/scripts/inventory_rustsec_heap_candidates.py`
- `evaluation/scripts/run_rustsec_heap_harness.py`
- `evaluation/scripts/run_rustsec_heap_experiment.py`
- `evaluation/scripts/run_rustsec_heap_sweep.py`
- `evaluation/scripts/plot_rustsec_security_sets.py`
- `evaluation/scripts/summarize_rustsec_heap_expansion.py`
- `evaluation/scripts/test_run_rustsec_heap_harness.py`
- `evaluation/scripts/test_run_rustsec_heap_experiment.py`
- `evaluation/scripts/test_run_rustsec_heap_sweep.py`
- `evaluation/scripts/test_rustsec_heap_corpus.py`

The strict feature-matched reclaim evidence is retained in:

- `docs/evidence/rustsec-reclaim-checks-20260714/feature-matched-campaign.json`;
- `docs/evidence/rustsec-reclaim-checks-20260714/summary-feature-matched-*.json`;
- `docs/evidence/rustsec-reclaim-checks-20260714/mechanism-results-feature-matched-*.json`;
- `docs/evidence/rustsec-reclaim-checks-20260714/mechanism-results.json`.

The frozen pilot corpus contains **40 RustSec advisories**. Nineteen are present in the
pinned Rudra-PoC snapshot: eighteen contain source-level reproducers and one
(`calamine`, Rudra 0095) explicitly says that the issue was reported without a
PoC. The other twenty-one extend the set with allocator, temporal, FFI,
spatial, initialization, and provenance cases through 2026.

The 40 rows are the frozen source-ready pilot denominator. The full-database expansion
inventory identifies 53 currently strong Rust-global temporal/reclaim research
candidates, including 36 absent from the pilot. The expanded screening and
manual allocator-ownership review are documented in
`docs/rustsec-heap-corpus-expansion.md`.

The frozen pilot schema contains **45 source-pinned
scenarios across all 40 distinct advisories (100% of the 40-case corpus)**:

- 18 external source PoCs from the pinned Rudra-PoC snapshot, awaiting the
  same repository materialization/build integration as the harness bundle;
- 27 scenarios in 22 repository case bundles: 24 mechanically adapted
  (`mechanical_adapter`) scenarios grounded in published, upstream, or advisory
  sources and 3 derived (`derived_adapter`) scenarios. Two derived scenarios
  exercise cross-identity reuse; one constructs a minimal CFB workbook for the
  calamine bounds-check regression.

Scenario count and advisory count answer different questions. RSH-017 contains
two secp256k1 scenarios, RSH-018 contains three arenavec scenarios, and RSH-002
and RSH-008 each add a derived reuse scenario beside the published witness.
These five additional programs raise 40 source-covered advisories to 45
source-ready scenarios. RSH-001 now has an upstream mechanical witness; its
separately preregistered A-to-B reuse experiment remains a future efficacy
adapter rather than a source-readiness gap.

The mutable live harness catalog now contains **28 repository scenarios**:
the frozen 27-scenario pilot plus the supplemental
`RSH-031-derived-layout-validation` adapter. This raises the current pilot
inventory to 46 source-pinned scenarios (24 mechanical repository adapters, 4
derived repository adapters, and 18 Rudra source PoCs) while preserving the
frozen sweep's 27-scenario denominator.

The executable expansion adds 5 advisory IDs and 7 repository scenarios: 5
published/upstream witnesses and 2 derived reuse probes. Across the current
pilot catalog and expansion catalog, the repository records 45 advisory IDs,
53 source-pinned scenarios, and 35 repository scenarios in 27 case bundles.
These numbers describe inventory coverage. The frozen 27-scenario sweep, the
supplemental RSH-031 layout-validation adapter, and the seven-scenario
expansion retain separate experiment matrices and denominators.

`source-ready` means that the repository contains or pins the program bytes and
records its oracle. It carries no mitigation or detection result. The frozen
full sweep supplies baselines, patched controls, and matched diagnostics for
all 27 pilot repository scenarios. The expansion supplies five published scenarios, represented by ten split
system/typed input matrices, and two derived reuse matrices described below. Both
catalogs remain `claim_grade=false`; the derived expansion explicitly marks
victim identity attribution as manual in that historical snapshot. The
terminal automatic probe uses separate records with manual metadata disabled.

## Pinned source frame

The corpus binds all selected source bytes to these upstream snapshots:

| Source | Commit | Snapshot facts |
|---|---|---|
| [RustSec advisory-db](https://github.com/RustSec/advisory-db/tree/9f3e138091487e69144f536d36976e427a7a3307) | `9f3e138091487e69144f536d36976e427a7a3307` | 1,140 advisory files; 253 use the `memory-corruption` category; 276 match the recorded category-or-keyword screening query |
| [Rudra-PoC](https://github.com/sslab-gatech/Rudra-PoC/tree/6226dd030fffbed5601099cb0e24f73e4150a7f5) | `6226dd030fffbed5601099cb0e24f73e4150a7f5` | 166 Rust PoC files; 165 parseable; 129 unique RustSec mappings; 19 selected IDs |

Both pins matched their upstream remote `HEAD` when collected on 2026-07-14.

The 276-row screen remains broader than “heap bug.” RustSec's memory-corruption
labels also include stack, aliasing, concurrency, FFI, and general soundness
failures. The screen is one discovery channel: 30 selected rows match it, while
the Rudra and manual allocator-boundary strata add 10 rows outside it. The 40
cases form a purposive stratified subset of the pinned advisory database and
support mechanism evaluation. They provide no prevalence estimate for RustSec
or the Rust ecosystem.

The generated 348-row candidate inventory now preserves the original 276-row
metadata screen, adds high-precision title/keyword signals, and records source
digests and mechanism-review queues. The temporal/reclaim manual review records
83 independent units as 50 clear Rust-global candidates, 7 conditional cases,
and 26 evidence-backed exclusions. Pinned Rudra source evidence adds three
strong duplicate-reclaim candidates whose advisory titles were too generic for
the strict temporal/reclaim screen. These ledgers establish the expansion
queue; executable efficacy still requires the materialization and coverage
gates below.

Verify the tracked schema alone:

```bash
python3 evaluation/scripts/audit_rustsec_heap_corpus.py
```

Verify every selected advisory and Rudra file against local pinned checkouts:

```bash
python3 evaluation/scripts/audit_rustsec_heap_corpus.py \
  --rustsec-db /path/to/advisory-db \
  --rudra-poc /path/to/Rudra-PoC \
  --output evaluation/results/rustsec_heap_corpus_audit.json
```

The source-verified audit distinguishes the frozen sweep from the mutable live
catalog:

- frozen sweep: 40 cases, with 19 `rudra_poc` and 21 `rustsec_extended`;
- frozen sweep: 45 source-pinned scenarios across all 40 distinct advisories;
- 18 pinned Rudra source PoCs;
- current live catalog: 28 repository scenarios in 22 case bundles, including
  24 `mechanical_adapter` scenarios and 4 `derived_adapter` scenarios;
- the current catalog's additional derived adapter is the supplemental RSH-031
  recovery-layout validation scenario, outside the frozen sweep denominator;
- 0 source-readiness gaps in the classified corpus;
- 40 published advisory witnesses preregistered as Type Isolation
  no-direct-effect cases;
- 3 preregistered A-to-free-to-B research hypotheses, of which 2 have
  repository reuse-derived adapters in the frozen pilot;
- 13 conditional UniAlloc allocator-boundary detection candidates;
- 24 primary-axis expected-no-direct-effect controls. Auxiliary guard-page,
  force-initialize, delayed-free, and tagging arms retain separate hypotheses.

The separate expansion catalog adds 5 advisory IDs, 5 published/upstream
scenarios, and 2 reuse-derived scenarios. It does not change the frozen pilot
audit denominator.

Thirteen advisories provide no upstream patched version. Those rows require a
separately hashed minimal source patch as the positive safe control. A missing
or semantically invalid local patch keeps the row outside every efficacy
denominator.

## Repository harness bundle

`evaluation/config/rustsec_heap_harnesses.json` is the execution-readiness
catalog for 28 scenarios in 22 repository case bundles. Each entry binds every
archive-backed vulnerable or patched dependency to an exact crates.io URL,
archive byte count, archive SHA-256 value, version, and upstream commit. Cases
without an upstream patched release reuse the pinned vulnerable archive and add
a scenario-specific hashed local patch. The catalog also binds each harness
source, vulnerable and patched `Cargo.lock`, local patch, and patched source
override to its repository path and SHA-256. Local-patch controls record the
expected post-patch source hashes. The audit rejects missing repository files,
changed repository bytes, cross-case paths, inconsistent corpus references,
and count drift. A `hashed_subject_patch` may expose a private API identically
on both archive arms; the catalog pins the patch and each variant's resulting
source hashes, keeping that mechanical adapter separate from the upstream fix.
The runner verifies the byte count and SHA-256 of an actual
cached or downloaded crate archive before extraction.

In the frozen sweep, twenty-six of the 27 repository scenarios can receive
force-loaded `typed_plain` and `typeiso` arms. RSH-030 intentionally declares `MiMalloc` as
its global allocator because the advisory targets that allocator's alignment
behavior; a second global allocator would invalidate the witness. RSH-030 is a
system/MiMalloc ground-truth and allocator-boundary control. RSH-028 supports
the force-loaded arms, while its raw-`unialloc` arm is excluded because the
pinned nix harness requires exact `libc=0.2.99` and direct UniAlloc requires
exact `libc=0.2.183`.

Execution-input integrity and origin attribution have different evidence
levels. The catalog cryptographically pins every local harness, lockfile,
patch, patched result, and crate archive used by the repository materializer.
The `mechanical_adapter` versus `derived_adapter` provenance label remains a
human-reviewed classification: 17 of 24 mechanical scenarios currently omit an
`origin_content_sha256`, and cataloged upstream commit IDs are recorded metadata
rather than fetched-content proofs. These provenance strata therefore remain
`claim_grade=false`. A claim-grade provenance release must pin an origin commit,
path, and content digest for every mechanical adapter and verify the retrieved
bytes in the audit.

List the current 28 repository scenarios without downloading or executing code:

```bash
python3 evaluation/scripts/run_rustsec_heap_harness.py --action list
```

Materialize one exact vulnerable or patched project. Downloads require the
explicit `--allow-download` flag; a retained materialization requires an empty
`--work-dir`:

```bash
python3 evaluation/scripts/run_rustsec_heap_harness.py \
  --action materialize \
  --scenario RSH-002-published \
  --variant vulnerable \
  --allow-download \
  --work-dir /tmp/unialloc-rsh-002-vulnerable
```

Run a contained compile check. The runner fetches locked dependencies in the
only container with write access to the shared Cargo cache, then mounts that
cache read-only in a separate network-disabled `cargo check --locked`
container. Both uniquely named containers have host wall-clock deadlines and
forced cleanup:

```bash
python3 evaluation/scripts/run_rustsec_heap_harness.py \
  --action check \
  --scenario RSH-002-published \
  --variant vulnerable \
  --allow-download
```

Native execution requires two explicit choices: `--action run-native` and
`--execute-unsafe`. The execution container has no network, a read-only root
filesystem, UID/GID 65534, all Linux capabilities dropped,
`no-new-privileges`, a no-exec temporary filesystem, CPU/memory/PID limits, and
a hard wall-clock deadline. The in-container deadline sends `TERM`, escalates
to `KILL` after two seconds, and a host-side fallback force-removes the
container if the Docker call outlives its grace period:

```bash
python3 evaluation/scripts/run_rustsec_heap_harness.py \
  --action run-native \
  --scenario RSH-016-advisory \
  --variant vulnerable \
  --allow-download \
  --execute-unsafe \
  --timeout 30
```

This native path builds and runs an ordinary Rust binary. Scenarios whose
catalog oracle names ASan, Miri, Clang-instrumented C, optimization-sensitive
code, or platform-specific virtual memory require the recorded dedicated
environment. The catalog pins `nightly-2026-06-11` for sanitizer work and a
digest-pinned Rust 1.85 image for compile smoke checks. RSH-031 additionally
pins `nightly-2022-07-01` for its historical Miri oracle. Sanitizer and Miri
outputs serve as separate ground-truth arms because instrumentation can change
allocation, scheduling, layout, and failure timing. RSH-030 remains
environment-sensitive because its oracle depends on observing a misaligned
allocation. RSH-032 requires its recorded optimization and UB-check flags.

Compilation still crosses the Docker daemon and executes historical crate
build logic in the build container. A disposable VM provides the stronger
boundary for hostile-source execution. Materialization and compilation prove
input integrity and build readiness; they do not establish a reproduced
vulnerability or a UniAlloc security effect.

## What Type Isolation can establish

The strongest defensible security interpretation is:

> For compiler-attributed, cache-eligible lifecycles, UniAlloc constrains heap
> reuse to an exact allocator-visible identity. This can remove a
> cross-identity reuse edge from a temporal exploit chain. UniAlloc's tracked
> lifecycle mechanisms can also fail-stop selected invalid reclaim transitions
> while the relevant ownership or history record remains published.

The exact cache identity includes `type_id`, `module_id`, policy flags,
`lifetime_hint`, and `placement_hint`
(`unialloc/src/alloc_api/type_isolation.rs:5980-5989`). Cache participation
requires a nonzero type, `FLAG_TYPE_ISOLATED`, a non-layout-derived identity,
and an eligible layout (`type_isolation.rs:7090-7146`). The plain cache starts
at three machine words, and semantic caching stops above 64 KiB
(`type_isolation.rs:190-200,7112-7126`).

These requirements make the causal claim precise:

```text
Victim allocation under identity A
  -> reclaim under the same recorded lifecycle
  -> attacker allocation under distinct identity B and the same layout/class
  -> Type Isolation withholds A's retained object from B
```

The corresponding controls are essential:

```text
A -> free -> A     exact-identity reuse remains allowed
A -> stale load    access before reuse remains unchecked
fallback/raw path  no exact-identity reuse contract applies
```

## Mechanism-to-bug matrix

| Bug class | Type Isolation hypothesis | Separate UniAlloc mechanism | Required boundary |
|---|---|---|---|
| Published UAF plus a derived A-to-free-to-B grooming witness | Published witness: expected no direct effect; derived witness: conditional reuse-edge blocking | Delayed free can extend the reuse interval | Separately hashed derived adapter, distinct exact identities, same layout/class, compiler coverage, cache eligibility, and zero critical fallback/mismatch |
| Same-identity UAF or stale access before reuse | Expected no direct effect | Delayed free may change timing | Ordinary loads and stores carry no allocator check; exact-identity reuse is intentional |
| Double free or double drop | Expected no direct effect | Conditional fail-stop through retained cache ownership, quarantine ownership, lifecycle epochs, or software tag records | Both reclaims reach UniAlloc while a matching record/history window remains live |
| Invalid layout/type/module on reclaim | Expected no direct effect | Conditional recovery-layout or software memory-tag validation | An exact record exists; foreign and unknown pointers remain outside general provenance coverage |
| Heap OOB read/write | Expected no direct effect | Guard pages can fault selected page-scale boundary crossings | Small, intra-object, arena-internal, and fixed-heap accesses remain outside that policy |
| Uninitialized-memory exposure | Expected no direct effect | Force-initialize can zero selected allocation-backed byte regions | Zeroing does not create every valid Rust value and does not cover stack/foreign/same-identity stale bytes |
| Aliasing, pinning, alignment, and language-level type confusion | Expected no direct effect | None at ordinary access time | A later allocator lifecycle error is a separate event |
| Arena, mmap, custom allocator, or foreign allocator internals | Expected no per-object effect | Depends on the owning allocator | Coverage of an outer allocation does not imply coverage of privately managed inner objects |

`FLAG_MEMORY_TAGGING` is an allocator-side software record. It records
pointer/layout/metadata at allocation and validates them during deallocation
and reallocation
(`type_isolation.rs:9384-9591`). It performs no tag check on ordinary loads or
stores. The retained-lifecycle path similarly makes its scope explicit:
untracked raw addresses can follow conventional behavior, while retained,
released, and stale-epoch records fail-stop
(`type_isolation.rs:10725-10835`).

Guard pages and force initialization are distinct treatment arms. Guarded
hosted allocations require a page-sized eligible object and map inaccessible
pages around the payload (`type_isolation.rs:9600-9634`). Force initialization
zeros returned storage and reallocation growth tails on requested semantic
paths. Combining these flags with Type Isolation in the primary comparison
would confound attribution.

## Corpus taxonomy

The 40 rows use two linked taxonomies.

### Failure taxonomy

- **Temporal:** dangling lifetime or use-after-free.
- **Reclaim:** double free, double drop, invalid free, or deallocation-layout
  mismatch.
- **Spatial:** heap out-of-bounds read or write.
- **Initialization:** uninitialized or invalid-value exposure.
- **Concurrency:** same-object lifetime race.
- **Provenance:** aliasing, pinning, alignment, or language-level type
  confusion.

### Mechanism profiles

- `reuse_dependent_uaf`: published-witness negative control plus a separately
  derived conditional Type Isolation reuse-edge experiment;
- `same_identity_or_pre_reuse_uaf`: access-time negative control;
- `allocator_visible_double_free`: Type Isolation no-direct-effect case and
  bounded UniAlloc reclaim detection candidate;
- `panic_double_drop`: Type Isolation no-direct-effect case and
  unwind-sensitive UniAlloc reclaim detection candidate;
- `invalid_deallocation`: Type Isolation no-direct-effect case and
  record-dependent UniAlloc detection candidate;
- `heap_oob`: spatial negative control;
- `uninitialized_exposure`: initializedness negative control;
- `concurrency_unsoundness`: synchronization negative control;
- `language_invariant_violation`: language-semantics negative control;
- `foreign_allocator_boundary`: allocator-coverage negative control.

Each profile records an allocator-event sequence, required conditions,
boundaries, auxiliary policies, and confidence. Each case records storage
domain, allocator path, root-cause cluster, primitive, lifecycle phase, reuse
relation, vulnerable replay pin, patched-control requirement, source hash, and
execution readiness.

Mixed-storage cases retain the failing allocator boundary explicitly. In
RSH-015 (`arrow2`), RSH-018 (`arenavec`), and RSH-021 (`oneringbuf`), duplicated
heap-owning payloads reach the Rust global allocator; the surrounding FFI,
arena, or mmap storage remains outside per-object Type Isolation coverage.

## Required experiment design

### Matched arms

Run each executable case in an isolated child process under these arms:

1. **System allocator:** establish the published vulnerable behavior.
2. **UniAlloc plain:** distinguish generic allocator substitution effects.
3. **`typed_plain`:** exact compiler metadata with Type Isolation policy bit
   disabled.
4. **`typeiso`:** identical source, dependencies, compiler, optimization,
   workload, and metadata pipeline with only Type Isolation enabled.
5. **Patched version:** verify the semantic safe oracle.

Run quarantine, software memory tagging, guard pages, force initialization,
and metadata authentication as separate follow-up arms. The existing
`typed_plain`/`typeiso` real-world matrix already enforces the policy-bit
ablation shape in `evaluation/scripts/realworld_type_isolation_matrix.py`.

### Outcome taxonomy

- `baseline_not_reproduced`
- `reuse_edge_blocked`
- `detected_fail_stop`
- `still_exploitable`
- `inconclusive`
- `build_blocked`
- `toolchain_blocked`
- `patched_control_unavailable`
- `out_of_scope`

A clean process exit carries no security meaning by itself. A generic panic or
signal also remains inconclusive. `detected_fail_stop` requires an
allocator-specific diagnostic or structured counter before unsafe reclaim
publication. `reuse_edge_blocked` applies only to a separately hashed derived
A-to-free-to-B grooming adapter and requires all of the following:

1. the separately hashed `mechanical_adapter` for the published/advisory
   witness reproduces under both `typed_plain` and `typeiso`, confirming its
   preregistered no-direct-effect result; retain any available verbatim archival
   witness as a separate provenance stratum;
2. `typed_plain` reproduces the derived cross-identity reuse witness;
3. the upstream patched version or hashed local patch reaches its positive safe
   oracle;
4. the dynamic trace proves same-layout, distinct-identity replacement;
5. the compiler audit covers every critical allocation/reclaim/replacement
   site;
6. critical events show exact metadata, cache eligibility, zero fallback, and
   zero recovery mismatch;
7. `typeiso` blocks wrong-identity reuse;
8. a matched same-identity control still demonstrates the contract boundary.

This result establishes a blocked reuse edge in the derived exploit experiment.
The source vulnerability and its published witness remain present.

An unreproduced baseline stays in the corpus denominator and enters the
`baseline_not_reproduced` attrition bucket. It remains outside the efficacy
denominator.

### Runtime controls

- pin exact `.crate` archives, lockfiles, source hashes, and historical
  toolchains;
- force vulnerable dependencies through the UniAlloc compiler wrapper;
- preserve the unmodified archival replay and hash any current-toolchain
  adapter separately;
- rotate arm order and isolate `CARGO_TARGET_DIR`;
- bound stdout/stderr and kill the full process group on timeout;
- retain exit code, signal, logs, compiler audit, runtime counters, binary hash,
  pass hash, and allocator implementation digest;
- repeat probabilistic grooming/race cases and report trial-level results with
  Wilson confidence intervals;
- use ASan or Miri only as separate ground-truth runs because they can change
  allocation behavior.

Claim-grade execution must place historical vulnerable crates and their build
scripts inside a disposable container or VM. Stage pinned dependencies first,
then disable network access; mount subject source read-only; use disposable
writable target storage; isolate user and PID namespaces; apply CPU, memory,
process, file-size, and timeout limits; and use an explicit seccomp policy. The
tracked native-run path implements the documented read-only, unprivileged,
network-disabled execution boundary. A disposable VM supplies the stronger
boundary for the build phase. This containment is a prerequisite for executing
the corpus, independent of allocator isolation.

### Feature-matched reclaim attribution

The final reclaim comparison uses six logical arms for every selected scenario:

```text
vulnerable, patched x system, reclaim_plain, reclaim_checks
```

The system arm establishes the cataloged vulnerability oracle and patched
behavior. `reclaim_plain` and `reclaim_checks` both use
`--no-default-features` with `stats`; the treatment adds only
`reclaim_checks`. Cargo metadata attestation records the exact resolved feature
sets as `['stats']` and `['reclaim_checks', 'stats']`, respectively. The original
direct campaign binds to UniAlloc implementation digest
`36bc040455f8c5fa6142a91b2321bc9d018aad08764f7d8c8e5554b3e4460847`.
The deterministic RSH-013 refresh binds all six arms to digest
`b9bd5442c557b3d39c34cf391e8c32388ba84ea643e6adc91ea4987d85adbc5a`.

The original campaign covers 42 reclaim scenarios with three requested
repetitions. Its direct portion contains 168 arms, 492 runtime executions, and
4 expected matched compile-rejection arms. The RSH-001 lifecycle matrix and
RSH-060 partial-panic matrix raise the final ledger to 44 scenario rows. The
strict observations include 31 vulnerable `reclaim_checks` exact signals, zero
vulnerable `reclaim_plain` exact signals, and zero patched exact signals. The
strict exporter requires all six arms,
stable vulnerable and patched outcomes, exact repetition counts, one stable
allocator finding signature, and matching feature provenance.

The frozen pre-correction reclaim ledger is **31 detected**, **12 no signal**,
and **1 inconclusive**. RSH-013 completes its plain ablation through a deterministic
terminal `drop(values)`. RSH-020 has one stable plain outcome through the
fail-closed double-panic parser. RSH-001 supplies the added duplicate-ownership
detection, and RSH-060's partial-yield/panic scenario supplies the second added
detection. The original RSH-049, RSH-052, RSH-060, RSH-069, and RSH-075
scenarios have terminal matched `no_signal` classifications. The historical
ledger records RSH-006 as inconclusive because its matched SIGSEGV lacks a
normalized source-bound fault/checkpoint fingerprint. The post-source audit
establishes that its dangling target is stack storage, so the corrected heap
efficacy scope excludes the row. It contributes no allocator miss.
All 31 raw check-arm observations pass strict feature attribution.

The reclaim feature detects an exact duplicate release while the address
lifecycle record remains published. It supplies no ordinary load/store bounds
check, stale-access check, or general pointer-provenance guarantee. Type
Isolation retains a separate causal contract: it withholds an eligible retained
object from a request carrying a different exact identity.

## Full frozen-pilot 27-scenario matched-allocator sweep

The frozen-pilot 2026-07-14 sweep executed every pilot repository scenario
with two repetitions per runtime arm. For each scenario, the matrix contains
vulnerable and patched archives under system, raw UniAlloc, `typed_plain`, and
`typeiso` configurations. The canonical raw summary is
`evaluation/raw/rustsec-heap-full-sweep-20260714/sweep-summary.json`, SHA-256
`2e38d8ad34f6de8bdedd4e099ae29fa34eba0f15e08f7d1652bb2398d73d11e9`.
The tracked compact record is
`docs/evidence/rustsec-security-evaluation-20260714/experiment-summary.json`.

| Sweep property | Result |
|---|---:|
| Repository scenarios | 27 / 27 successful |
| Matrix arms | 216 / 216 terminal |
| Supported arms | 208 |
| Planned topology exclusions | 8 |
| Expected compile-rejection arms | 32 |
| Runtime witness executions | 352 / 352 |
| Unexpected arms | 0 |
| Vulnerable system oracles reproduced | 27 / 27 |
| Patched system controls reproduced | 27 / 27 |
| Input drift | 0 |

The full run used sequential scenario orchestration and froze the following
identities from start to finish:

- harness catalog:
  `fc83229a1fe21901bb416aecc4dc6d433465301597b17c564dc2804bbf937c26`;
- experiment runner:
  `13fcce760f3e49bc901527c6eddab199548a4a753d2b82f49a680477d7db36e2`;
- sweep runner:
  `4d73135c76a18f12741ff33d949672b42207455d08e552fc32c821958b8387b4`;
- UniAlloc implementation digest:
  `159806869e12b58903f0f941b016ff60fed6e53a842cca35b5434f54a3d9da09`.

The eight topology exclusions are two raw-UniAlloc RSH-028 arms and six
RSH-030 allocator-substitution arms. RSH-028's exact historical libc pin cannot
co-resolve with direct UniAlloc; its `typed_plain` and `typeiso` arms completed.
RSH-030 preserves its subject-owned MiMalloc allocator in the system arm, so a
UniAlloc substitution would invalidate the allocator-under-test topology.

All 54 Type Isolation cells were accounted for: 52 supported arms completed and
the two RSH-030 arms were topology-excluded. Among the completed arms, compiler
audit routes were 35 semantic, 8 direct-only, and 9 with no observed compiler
rewrite. **Zero of 52 supported Type Isolation arms has validated critical-site
coverage, and zero is efficacy-eligible.** Every arm records
`mitigation_inferred=false`.

The allocator outcomes are consequently diagnostic:

- native clean exits are inconclusive because sanitizers and Miri stay on the
  separate system ground-truth arms;
- native signals and allocator diagnostics show that a process encountered a
  failure; Type Isolation specificity and coverage remain unestablished;
- `typed_plain` often produces the same diagnostic as `typeiso`, which rules
  out attributing that observation to the isolation policy alone;
- at that frozen historical stage, the automatic-compiler RSH-002 derived
  witness observed address reuse
  in both Type Isolation repetitions, localizing a critical-site attribution
  gap; the separately labeled manual-identity follow-up below exercises the
  allocator policy after supplying that missing identity;
- spatial OOB witnesses remain expected-no-direct-effect controls because Type
  Isolation adds no access-time bounds check to ordinary loads and stores.

Two oracle controls require explicit version-sensitive handling. RSH-031's
system arms use `nightly-2022-07-01` Miri, v3 lockfiles, no current-only lint
allowances, and Rust's default allocator. The vulnerable arm reproduces the
1009-byte, alignment-256 allocation deallocated with alignment 1; the patched
arm exits cleanly. Current Miri misses this historical mismatch. RSH-032 uses
`-Copt-level=0 -Coverflow-checks=off -Zub-checks=no` before ASan instrumentation
so LLVM does not elide the witness; the vulnerable system arm reports the
cataloged high-address deadly fault and the patched arm reaches its safe panic.

These results support a complete frozen-pilot executable statement:

> We reproduced the preregistered vulnerable and patched system oracles for all
> 27 repository-materializable scenarios and collected every supported matched
> allocator arm. The experiment supplies diagnostic coverage evidence. Security
> efficacy remains blocked by exact critical-site coverage validation.

At the frozen-pilot stage, the other 18 source-pinned Rudra programs lacked
repository materializers, matched patched controls, and allocator matrices.
They stayed outside that historical executed denominator. The later frozen
execution program admitted 49 cases and retained four non-evaluable candidates
as audit-only exclusions. The post-source-audit presentation removes
stack-only RSH-006, producing the current 48-case heap efficacy denominator.
The pilot evidence bundle documents all per-scenario result hashes and
classifications in
`docs/evidence/rustsec-security-evaluation-20260714/`.

## Automatic compiler-edge validation

The terminal `--automatic-edge-identity-probe` rerun evaluates 12 derived or
source-shaped cross-identity reuse edges with the historical harness metadata
helper disabled. The compiler is the sole source of semantic allocation
identity. A result qualifies as a causal reuse-edge mitigation only when every
vulnerable repetition satisfies all of these gates:

1. system and `typed_plain` reuse the harness's reclaimed address;
2. `typeiso` withholds that address;
3. preregistered requested and victim contracts each select one unique applied
   compiler-audit candidate with an allowlisted rewrite status;
4. runtime requested/retained identities, module IDs, requested callsite, size,
   and alignment bind to those candidates; and
5. `last_wrong_identity_retained_ptr` equals the harness's parsed `original` or
   `stale_value` pointer.

All three patched controls exercise safe same-identity functionality and
complete without a matching attribution event. Allocator unit controls
separately preserve safe same-identity reuse and verify withholding of an
eligible cross-identity entry. These controls establish exact-identity routing
and rule out unrelated same-layout cache entries, generic allocation failure,
and address nondeterminism as explanations.

The 12 rows have two explicit coverage scopes. RSH-008 and RSH-041 are
`derived_vulnerability_edge`; RSH-002, RSH-042, RSH-052, RSH-055, and RSH-064
through RSH-069 are `source_shaped_derived`. None is `published_source`.
Accordingly, the automatic source-level vulnerability-detection count is
**0/12**. Every record sets `source_vulnerability_detection_validated=false`,
`vulnerability_specific_detection_signal=false`, and `claim_grade=false`.
The supported statement is a validated causal compiler-bound mitigation of the
measured cross-identity reuse edge.

Three conservative compiler paths close the previously observed identity gaps:

- exact `Global` `Vec::with_capacity<T>` calls use monomorphized runtime
  `TypeId`;
- compiler-authenticated `alloc::slice::to_vec` receives a whole-call semantic
  scope only for primitive scalar elements; and
- exact source-shaped `String` allocation sites preserve identity through
  audited ownership movement or transfer.

RSH-008 and RSH-041 use the existing generic `Box<T>` runtime identity path.
RSH-052 binds an exact `String` origin and then an audited pointer-preserving
`String -> Box<str>` transfer. RSH-066 moves its exact `String` into
`AtomicStr`. RSH-068 and RSH-069 preserve allocation-origin identity through
their relevant `String` and `CString` ownership chains. RSH-065 retains a
synthetic reserve-relocation reduction, and RSH-069 retains synthetic grooming
of the FFI-read edge.

The extensions fail closed. Allocator-specific `Vec` owners, unresolved or
ambiguous generic owners, custom `slice::to_vec` lookalikes, and element types
with user-defined `Clone` or drop behavior remain neutral or audit-only. Exact
canonical crate and DefId checks prevent path-text matches from authorizing
reuse. RSH-064's real Node/V8 source witness therefore remains a complete
ownership-chain coverage gap while its concrete derived `Vec<u8>` edge is
compiler-bound.

The attribution JSON is `stats`-gated experimental telemetry. The selected
retained pointer is exported through one companion atomic; cache entries,
identity layouts, and the public semantic-stats snapshot remain unchanged.
Production enforcement uses the ordinary exact-identity hit/miss decision. The
telemetry establishes experimental causality and carries no
vulnerability-specific detector claim. The completed paired performance screen
passed its wall-time and RSS gates: Oxipng was **-1.6649%** in wall time and
**+0.583%** in peak RSS; long Ripgrep was **+0.8107%** in wall time (95%
bootstrap CI **[-0.180%, +1.399%]**) with maximum RSS unchanged at
**7168 KiB**. Selected macro aggregate semantic-rewrite counts were unchanged,
while the 12 RustSec matrices and compiler tests provide active new-site
coverage. The compact result is
`docs/evidence/rustsec-typeiso-automatic-20260715/performance-screen/summary.json`.

RSH-002 demonstrates the outcome boundary. Type Isolation withholds the bound
`Vec`-backing-to-`Replacement` pointer before the stale duplicate owner later
reaches `type-cache pointer already retained`. Its Type Isolation row covers the
earlier cross-identity reuse decision. Its distinct `reclaim_checks` row covers
the later exact duplicate reclaim.

The durable automatic records and machine-readable `derived-reuse-summary.json` are under
`docs/evidence/rustsec-typeiso-automatic-20260715/`.

## Frozen automatic-compiler pilot calibration (historical)

The diagnostic run `rustsec-typeiso-pilot-20260714` exercised the two existing
derived reuse adapters with one force-loaded UniAlloc identity and the pinned
`nightly-2026-06-11` compiler pass. `typed_plain` used policy flags `0` and
`typeiso` used policy flags `1`; source, lockfile, optimization, force-loaded
rlib, and compiler wrapper stayed matched. Ten fresh child processes were run
per case and policy, for 40 treatment executions. The retained raw artifact is
`evaluation/raw/rustsec-typeiso-pilot-20260714/manifest.json`, with SHA-256
`802157a514f9ad49eed4669dd7e9a6257de8786b1e837c5cc7f668c28df19b2d`.

| Case | `typed_plain` | `typeiso` | Compiler audit | Classification |
|---|---|---|---|---|
| RSH-002 derived bitvec reuse | address collision 10/10; exit 0 10/10 | address collision 10/10; exit 0 10/10 | `bitvec` applied no direct allocation or semantic-scope rewrite; one ownership-transfer rewrite did not cover the critical allocation/reclaim edge | `inconclusive_critical_site_fallback`; reuse edge observed intact |
| RSH-008 derived lru reuse | address collision 10/10; SIGSEGV 10/10 | address collision 10/10; SIGSEGV 10/10 | `lru` applied no direct allocation, semantic-scope, or ownership-transfer rewrite; the harness rewrite covered the replacement allocation rather than the vulnerable entry lifecycle | `inconclusive_critical_site_fallback`; reuse edge and fault observed intact |

At this historical pilot stage, the frozen automatic compiler path preserved
both intended cross-identity reuse edges under these adapters. Runtime
statistics and pass
audits localize the immediate experimental blocker to missing exact metadata at
the vulnerable third-party allocation/reclaim sites. These rows stay outside
the efficacy denominator and keep `claim_grade=false`. The next causal step is
to extend the sound MIR object solver for the bitvec and lru allocation shapes,
verify zero critical fallback in the audit, and repeat the same frozen matrix.
The terminal automatic compiler-edge validation above completes that follow-up
for the derived edges while preserving the published-source boundary.

### Reproducible matched-allocator smoke matrices

The following smoke matrices predate and calibrate the full sweep above. Their
individual hashes remain useful for debugging; the 27-scenario artifact is the
canonical corpus result.

`evaluation/scripts/run_rustsec_heap_experiment.py` now materializes the pinned
vulnerable and patched archives and runs matched system, raw UniAlloc,
`typed_plain`, and `typeiso` arms with one recorded environment. The runner
keeps sanitizer/Miri catalog oracles on the system arm and runs allocator arms
as native diagnostics. All artifacts in this section retain `claim_grade=false`,
`mitigation_inferred=false`, and `efficacy_eligible=false`; the compiler audit
validation currently proves target-crate presence and force-load topology;
critical-site coverage remains unvalidated.

#### RSH-002 derived cross-identity reuse

The smoke artifact
`evaluation/raw/rustsec-heap-experiment-20260714-smoke/experiment.json` used two
fresh processes per arm: 8 completed arms, 16 executions, and 0 unexpected arms.
The manifest SHA-256 is
`f2f82049045d78707822e216f896c2ea1188064ee8415e987b2d567cc757889b`; the runner
SHA-256 is
`4fb4e5ce4e3433ef3756da426aa6fb6b611c29fb4bbba366ef94d0a99151cfa2`.

| Arm | Vulnerable RSH-002 derived witness | Patched control | Interpretation |
|---|---|---|---|
| System ground truth | ASan `bad-free` and address reuse 2/2 | address reuse and clean ASan execution 2/2 | vulnerable and patched catalog oracles reproduced |
| Raw UniAlloc | `pointer already released` allocator diagnostic 2/2 after address reuse | address reuse and clean native execution 2/2 | allocator-specific diagnostic observation; zero mitigation inference |
| `typed_plain` | address reuse and clean native execution 2/2 | address reuse and clean native execution 2/2 | native diagnostic; clean exit remains inconclusive |
| `typeiso` | address reuse and clean native execution 2/2 | address reuse and clean native execution 2/2 | native diagnostic; the intended reuse edge remained present |

The `typed_plain` and `typeiso` arms validated pass-audit target crates
`bitvec` and `rsh_002_harness`. Coverage of every critical allocation, reclaim,
and replacement site remains unvalidated. Clean allocator-arm runtime stats were
validated. The vulnerable `typeiso` stats recorded three total allocations: one
typed allocation and two fallback allocations, with one cache insert and zero
cache hits. The efficacy blocker is `critical_site_coverage_not_validated`, with
`critical_site_coverage_validated=false`.

Reproduce the same matrix with the verified archive cache:

```bash
python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --action run \
  --scenario RSH-002-derived-reuse \
  --variants system,unialloc,typed_plain,typeiso \
  --archive-variants vulnerable,patched \
  --repetitions 2 \
  --cache "$HOME/.cache/unialloc/rustsec-heap" \
  --output-dir evaluation/raw/rustsec-heap-experiment-20260714-smoke \
  --execute-unsafe
```

#### RSH-029 spatial negative control

The linked-list-allocator smoke artifact
`evaluation/raw/rustsec-heap-rsh029-20260714/experiment.json` used two fresh
processes per arm: 8 completed arms, 16 executions, and 0 unexpected arms. The
manifest SHA-256 is
`561b1d3c1b4ccb6daa151ae79ff28360293b5985243c553eec7b9b2951bb1a34`; the runner
SHA-256 is
`4fb4e5ce4e3433ef3756da426aa6fb6b611c29fb4bbba366ef94d0a99151cfa2`.

| Arm | Vulnerable RSH-029 witness | Patched control | Interpretation |
|---|---|---|---|
| System ground truth | ASan `heap-buffer-overflow` 2/2 | safe panic control 2/2 | vulnerable and patched catalog oracles reproduced |
| Raw UniAlloc | clean native execution 2/2 | Rust panic diagnostic 2/2 | native diagnostic; spatial negative-control boundary preserved |
| `typed_plain` | clean native execution 2/2 | Rust panic diagnostic 2/2 | native diagnostic; Type Isolation policy bit absent |
| `typeiso` | clean native execution 2/2 | Rust panic diagnostic 2/2 | native diagnostic; heap OOB remains outside Type Isolation's access-time scope |

RSH-029 is a spatial expected-no-direct-effect control. These native allocator
observations provide no reuse-edge mitigation evidence and remain outside the
efficacy denominator.

Reproduce the same matrix with the verified archive cache:

```bash
python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --action run \
  --scenario RSH-029-advisory \
  --variants system,unialloc,typed_plain,typeiso \
  --archive-variants vulnerable,patched \
  --repetitions 2 \
  --cache "$HOME/.cache/unialloc/rustsec-heap" \
  --output-dir evaluation/raw/rustsec-heap-rsh029-20260714 \
  --execute-unsafe
```

#### RSH-030 fixed-allocator boundary

The mimalloc smoke artifact
`evaluation/raw/rustsec-rsh030-fixed-allocator-smoke/experiment.json` ran only the two
system arms: 2 completed arms and 2 executions. The manifest SHA-256 is
`8dc314f3fe02eec082fd4cc8ace893f6509174df73a05eb2f79256c505846d87`; the runner
SHA-256 is
`4fb4e5ce4e3433ef3756da426aa6fb6b611c29fb4bbba366ef94d0a99151cfa2`.

| Arm | Vulnerable RSH-030 witness | Patched control | Interpretation |
|---|---|---|---|
| System with subject MiMalloc preserved | native alignment assertion observed 1/1 | clean native execution 1/1 | fixed-allocator oracle reproduced while preserving subject allocator |
| Raw UniAlloc / `typed_plain` / `typeiso` | unsupported substitution topology in preflight | unsupported substitution topology in preflight | the subject owns `#[global_allocator]`; substituting UniAlloc would change the witness |

The full preflight records six unsupported substitution arms: raw UniAlloc,
`typed_plain`, and `typeiso` for both vulnerable and patched archives. The study
design classifies these as expected topology exclusions; orchestration remained
complete.
Its artifact is
`evaluation/raw/rustsec-rsh030-preflight-final/preflight.json`, with SHA-256
`1bcd096edfb187dd71db1314a8e486f0e272ea1f660372e8ce640ce0e0befdd2`.
Those exclusions preserve the MiMalloc advisory boundary and keep RSH-030 out of
the UniAlloc treatment denominator.

The commands above execute historical crate build scripts and memory-unsafe
witnesses on the host. Claim-grade runs require the disposable-VM boundary
described in the runtime controls above.

### Why the pilot still reused the victim address

The compiler audit identifies an asymmetric metadata path in both experiments:
the harness replacement `Box` receives an exact Type Isolation scope, while the
victim allocation inside the generic dependency stays on the fallback path.
In RSH-002, `Vec<T>::with_capacity` and the `Vec<T>`-to-`Box<[T]>` conversion
still contain unresolved type parameters when the definition-level
`optimized_mir` provider sees them. In RSH-008, `Box<LruEntry<K, V>>::new` and
the generic `Drop` of the removed entry have the same limitation. The pass
deliberately rejects unresolved constructor owners and generic drops
(`tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs:3354-3366,
4010-4023,4285-4294,4372-4383,10611-10624,10721-10731`). Its current query
override runs on definition-level optimized MIR
(`tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs:10950-11020`).

The runtime result follows directly from that coverage boundary. A typed
replacement checks its semantic cache and uses the underlying raw allocator on
a cache miss (`unialloc/src/alloc_api/type_isolation.rs:12513-12567`). The raw
victim free therefore makes the same address available to that miss. Once a
victim allocation has an exact recovery record, an otherwise unscoped
deallocation can recover its metadata
(`unialloc/src/cache/mod.rs:1001-1007`). Exact victim-allocation coverage is the
critical prerequisite for this experiment.

### Explicit denial telemetry and the historical annotated RSH-002 follow-up

The historical evaluation introduced the stats-only
`typed_cache_wrong_identity_denials` event. The current automatic experiment
also exports the exact selected retained pointer through one companion atomic.
A qualified automatic record requires that pointer to equal the harness's
`original` or `stale_value` pointer. The bounded wrong-identity scan and its
fields are gated by slow-path stats recording; production builds retain the
ordinary exact-identity miss path.

In that historical snapshot, the automatic compiler-coverage result above
remained unchanged. The follow-up
uses an explicitly labeled manual identity annotation around the vulnerable
`BitVec::with_capacity` allocation and the `BitVec`-to-`BitBox` reclaim. The
replacement `Box<Replacement>` keeps its compiler-derived identity. This
special-case adapter tests the allocator policy after supplying the exact
victim identity that the current definition-level compiler pass cannot derive.
Its claim scope is allocator-policy behavior under manual victim attribution.
The terminal automatic probe supersedes this coverage limitation for the
derived RSH-002 edge; the annotated artifact remains calibration evidence.

The pinned two-repetition matrix is stored at
`evaluation/raw/rustsec-rsh002-denial-20260714/experiment.json` (SHA-256
`692458fca66409d69e614a3b74b8dea76c64d7967ac29aad73ea434c895a8e2b`).
The durable compact record is
`docs/evidence/rustsec-security-evaluation-20260714/rsh002-annotated-reuse-edge.json`
(SHA-256
`ea21df35d28bc2df614776f68f447d367556d4663e0bfd5ad0d701f36ce27727`).
The matrix completed all six requested arms with zero unexpected arms.

| Archive / allocator | Address collision | Denial event | Runtime result |
|---|---:|---:|---|
| Vulnerable / system + ASan | 2/2 | N/A | expected invalid free, 2/2 |
| Vulnerable / `typed_plain` | 2/2 | 0/2 | clean native execution, 2/2 |
| Vulnerable / `typeiso` | 0/2 | 2/2 | retained-pointer fail-stop after the report, 2/2 |
| Patched / system + ASan | 2/2 | N/A | clean patched control, 2/2 |
| Patched / `typed_plain` | 2/2 | 0/2 | clean native execution, 2/2 |
| Patched / `typeiso` | 0/2 | 2/2 | clean native execution, 2/2 |

For each vulnerable `typeiso` repetition, the event reported a 1016-byte,
8-byte-aligned retained victim with type ID `5932164307604209665` and module ID
`5932164307604209666`. The requested identity differed, and the event callsite
matched exactly one compiler audit row for
`Box<witness::Replacement, Global>` at `src/witness.rs:50:23-50:74`. The
`typed_plain` ablation reused the victim address and emitted no denial. This is
direct evidence that exact-identity routing blocked and reported the derived
cross-type reuse edge.

The patched `typeiso` arm emitted the same denial because the corrected BitVec
conversion also releases the old allocation before the distinct replacement.
The denial signal therefore identifies a cross-identity reuse decision.
Source-level stale-pointer localization requires separate evidence. The
supported historical claim is a bounded reuse-edge policy-enforcement
observation in this manually annotated adapter, paired with the vulnerable
baseline, address oracle, ablation, and compiler-audit binding.
Vulnerability-specific detection, root-cause localization, and automatic
coverage remain outside that calibration result. The terminal automatic probe
above supplies the later compiler-backed derived-edge result.

The implementation priorities are:

1. add a generic-owner scope whose canonical semantic type identity is resolved
   after monomorphization, using the same identity namespace as concrete sites;
2. extend the generic `Vec<T>`-to-`Box<[T]>` transfer helper so old and new
   canonical identities are resolved after substitution;
3. move the solver to monomorphized `Instance` MIR as the broader compiler
   design, reusing the existing concrete matchers and hash contract.

Crate-specific BitVec/LRU matchers and broad scopes around `put`, `pop`, or
conversion calls would combine multiple allocation and drop effects. Such
scopes belong only in explicitly labeled runtime-isolation adapters. The
compiler-coverage claim requires the generic, exact-identity solution above.

## Executable expansion beyond the frozen pilot

The expansion catalog is
`evaluation/config/rustsec_heap_expansion_harnesses.json`. It adds five
non-overlapping advisory IDs and seven scenarios. Each arm used
three fresh executions under `nightly-2026-06-11`. The complete raw matrices,
compact summaries, commands, and machine-readable artifact bindings are retained in
`docs/evidence/rustsec-security-expansion-20260714/`.

### Five published/upstream witnesses

| Case | System baseline | Matched patched control | `typed_plain` / `typeiso` result | Attribution |
|---|---|---|---|---|
| RSH-041 / `oneringbuf` | ASan UAF 3/3 | clean 3/3 | no allocator signal in either arm; all observed allocations fell back | direct Type Isolation effect absent; critical-site coverage gap |
| RSH-042 / `emap` | ASan UAF 3/3 | clean 3/3 | no allocator signal in either arm | published witness contains no measured A-to-B replacement decision |
| RSH-043 / `bitchomp` | ASan double free 3/3 | clean 3/3 | pointer-already-released 3/3 in both arms | common UniAlloc tracked-reclaim effect |
| RSH-044 / `qwutils` | ASan double free 3/3 | expected safe Rust panic 3/3 | pointer-already-released 3/3 in both arms | common UniAlloc tracked-reclaim effect |
| RSH-045 / `stack_dst` | ASan double free 3/3 | expected safe Rust panic 3/3 | pointer-already-released 3/3 in both arms | common UniAlloc tracked-reclaim effect |

The published denominator therefore contains 5/5 reproduced vulnerable
baselines, 5/5 matched patched controls, 3/5 common UniAlloc allocator-boundary
signals, and 0/5 Type-Isolation-specific published signals. The three
double-reclaim signals occur under both `typed_plain` and `typeiso`, so their
owner is the shared tracked-reclaim mechanism. The UAF rows contribute one
compiler-coverage negative result and one witness-shape negative result.

The machine-readable aggregation is
`docs/evidence/rustsec-security-expansion-20260714/published-summary.json`.
The bundle manifest supplies its current digest, and all ten input matrices
remain beside it under `raw/published/`.

### Two derived cross-type reuse probes (historical annotated snapshot)

| Scenario | Layout | Vulnerable system / `typed_plain` | Vulnerable `typeiso` | Patched `typeiso` |
|---|---|---|---|---|
| RSH-041 derived | 40 bytes, align 8 | exact address reuse 3/3 | reuse 0/3; historical stats attribution event 3/3 | safe address reuse 3/3; denial 0/3 |
| RSH-042 derived | 64 bytes, align 1 | exact address reuse 3/3 | reuse 0/3; historical stats attribution event 3/3 | safe same-identity reuse 3/3; denial 0/3 |

At that historical evidence stage, both scenarios validated a manually
attributed cross-identity reuse decision:
the `typed_plain` ablation reuses the victim address, while `typeiso` withholds
and reports that address for the distinct replacement identity. The replacement
site is compiler-audit bound in all six Type Isolation treatment executions.
The victim identity was supplied manually in both scenarios, so that snapshot
recorded automatic compiler victim coverage of 0/2 and validated
source-vulnerability detection of 0/2. The terminal automatic probe now covers
both derived edges without manual metadata; validated source-vulnerability
detection remains 0/2. The result covers two exact allocator decisions; it
leaves the stale handle, same-identity reuse against a stale handle, access
before reuse, and general UAF mitigation outside the claim.
The historical RSH-041 snapshot excluded the `oneringbuf` subject crate from
compiler rewriting so its reviewed `LocalHeapRB` annotation remained
authoritative. The automatic probe targets both subject and harness and binds
the generic `Box<LocalHeapRB>` runtime identity plus the replacement allocation
to exact audit rows.
The runner keeps `vulnerability_specific_detection_signal=false`; the derived
reuse decision is encoded separately from source-level detection. The
paper-facing source result also uses
`source_vulnerability_detection_validated=false`.

The compact record is
`docs/evidence/rustsec-security-expansion-20260714/derived-reuse-summary.json`.
Its machine-readable artifact bindings and the expansion bundle manifest are
the provenance authority for the RSH-041 and RSH-042 raw matrices; this prose
does not duplicate mutable digest values.

## Remaining claim-grade work

Review is complete for the original 53-candidate screen. The post-source audit
excludes stack-only RSH-006 and leaves 52 heap-relevant candidates: 48
executable cases and 4 audit-only exclusions. The remaining work raises
evidence quality for the rows that the strict ledger withholds:

| Work item | Cases | Required evidence |
|---|---|---|
| Automatic Type Isolation full-source coverage | RSH-002, RSH-008, RSH-041, RSH-042, RSH-052, RSH-055, RSH-064, RSH-065, RSH-066, RSH-067, RSH-068, RSH-069 | Execute the published source path with exact compiler attribution across its complete ownership chain and a source-vulnerability outcome causally linked to the blocked reuse edge; this gate adds full-source evidence beyond the automatic derived-edge result |
| Concurrency/lifetime mechanisms | RSH-003, RSH-019 | A synchronization, generation-tagging, or temporal-access mechanism with source-level causal evidence |
| Automatic Vec/external-buffer transfer | RSH-064 | One canonical final-`Vec` identity spanning allocation, pointer-preserving ownership transfer, unwind cleanup, and `Vec` drop |
| Future scope expansion from audit-only exclusions | RSH-054, RSH-056, RSH-059, RSH-073 | Root-cause-specific PoCs or the required native runtime context before admission to the efficacy scope |
| Claim-grade release | Entire scope | Retrieved-origin byte proofs, stronger build containment, independent reruns, and a preregistered analysis release |

The present artifacts keep `claim_grade=false`. They establish a terminal,
machine-auditable 48-case heap mechanism-evaluation scope, preserve the frozen
53-screen/49-executable campaign as provenance, and retain the four audit-only
exclusions in the attempts audit.

## Qualifier and paper presentation

Use the editable SVGs in
`docs/figures/rustsec-security-sets-20260715/` with this five-slide evidence
sequence:

1. **Selection:** 1,140 pinned RustSec records, 875 active records, 439
   high-recall review rows, 83 independent temporal/reclaim units, and 53
   initially screened candidates. Show the RSH-006 source-audit correction:
   its dangling target is stack storage, so 52 heap-relevant candidates remain.
   Label the scope purposive and report zero ecosystem-prevalence inference.
2. **Scope gate:** 48 executable advisories enter the heap efficacy denominator;
   four reviewed candidates remain audit-only exclusions with explicit
   reasons. Show the retained primitives: 30 double free, 18 UAF, 2
   uninitialized drop, 1 invalid free, and 1 OOB read. The executable UAF
   subset contains 17 cases; audit-only RSH-059 raises the retained UAF count to
   18.
3. **Allocator-mechanism coverage:** the reclaim set contains 31 cases, the
   Type Isolation set contains 12, and RSH-002 is their sole overlap. One
   recovery-layout validation adds RSH-031. Their union covers 43/48
   executable cases and 14/17 executable UAF cases.
4. **Causal attribution:** show the six reclaim arms and exact feature delta;
   then show the Type Isolation requested-site binding, victim-site binding,
   retained-pointer equality, and patched same-identity controls. Use RSH-052
   to show the `String` origin followed by `String -> Box<str>` transfer.
5. **Boundary:** two Type Isolation rows are `derived_vulnerability_edge`, ten
   are `source_shaped_derived`, and zero are `published_source`. Report
   source-level vulnerability detections as 0/12,
   `vulnerability_specific_detection_signal=false`, and `claim_grade=false`.

Use this result form:

> At RustSec advisory-db commit
> `9f3e138091487e69144f536d36976e427a7a3307` and Rudra-PoC commit
> `6226dd030fffbed5601099cb0e24f73e4150a7f5`, we initially screened 53
> candidates. A source audit excluded stack-only RSH-006 from heap allocator
> efficacy, leaving 52 heap-relevant candidates: 48 executable witnesses with
> matched controls and four audit-only exclusions. UniAlloc mechanisms provide
> qualified coverage for 43/48 executable cases: 31 exact duplicate-reclaim
> detections, one recovery-layout validation, and 12
> compiler-bound causal mitigations of measured cross-identity reuse edges,
> with RSH-002 counted once. The Type Isolation matrices bind unique requested
> and victim compiler sites, exact retained-pointer equality, and patched
> same-identity controls. Their automatic source-level vulnerability-detection
> count is 0/12, their vulnerability-specific detector signal remains false,
> and all artifacts retain `claim_grade=false`.

Keep advisory IDs, published witnesses, derived probes, raw allocator signals,
strictly attributed mechanism results, and compiler-covered source results as
distinct denominator units.

## Complete frozen 40-case pilot catalog

`poc_source_available` identifies one of the 18 pinned Rudra source PoCs.
`pinned_harness_source_available` identifies one of the 22 repository bundles.
Together these two readiness states cover all 40 advisories.

| Case | Advisory / crate | Primary primitive | Preregistered system effect | Execution readiness |
|---|---|---|---|---|
| RSH-001 | [RUSTSEC-2019-0016](https://rustsec.org/advisories/RUSTSEC-2019-0016.html) / `chttp` | `use_after_free` | published: no direct; derived A-to-B: conditional reuse-edge block | `pinned_harness_source_available` |
| RSH-002 | [RUSTSEC-2020-0007](https://rustsec.org/advisories/RUSTSEC-2020-0007.html) / `bitvec` | `use_after_free` | published: no direct; derived A-to-B: conditional reuse-edge block | `pinned_harness_source_available` |
| RSH-003 | [RUSTSEC-2020-0017](https://rustsec.org/advisories/RUSTSEC-2020-0017.html) / `internment` | `use_after_free` | expected no direct effect | `pinned_harness_source_available` |
| RSH-004 | [RUSTSEC-2020-0049](https://rustsec.org/advisories/RUSTSEC-2020-0049.html) / `actix-codec` | `use_after_free` | expected no direct effect | `pinned_harness_source_available` |
| RSH-005 | [RUSTSEC-2020-0047](https://rustsec.org/advisories/RUSTSEC-2020-0047.html) / `array-queue` | `uninitialized_read` | expected no direct effect | `poc_source_available` |
| RSH-006 | [RUSTSEC-2020-0091](https://rustsec.org/advisories/RUSTSEC-2020-0091.html) / `arc-swap` | `stack_lifetime_dangling_target` | excluded from heap allocator efficacy after source audit | `poc_source_available` |
| RSH-007 | [RUSTSEC-2021-0044](https://rustsec.org/advisories/RUSTSEC-2021-0044.html) / `rocket` | `use_after_free` | expected no direct effect | `poc_source_available` |
| RSH-008 | [RUSTSEC-2021-0130](https://rustsec.org/advisories/RUSTSEC-2021-0130.html) / `lru` | `use_after_free` | published: no direct; derived A-to-B: conditional reuse-edge block | `pinned_harness_source_available` |
| RSH-009 | [RUSTSEC-2021-0030](https://rustsec.org/advisories/RUSTSEC-2021-0030.html) / `scratchpad` | `double_free` | Type Isolation: no direct; UniAlloc boundary: conditional detection | `poc_source_available` |
| RSH-010 | [RUSTSEC-2021-0049](https://rustsec.org/advisories/RUSTSEC-2021-0049.html) / `through` | `double_free` | Type Isolation: no direct; UniAlloc boundary: conditional detection | `poc_source_available` |
| RSH-011 | [RUSTSEC-2019-0034](https://rustsec.org/advisories/RUSTSEC-2019-0034.html) / `http` | `double_free` | Type Isolation: no direct; UniAlloc boundary: conditional detection | `poc_source_available` |
| RSH-012 | [RUSTSEC-2021-0052](https://rustsec.org/advisories/RUSTSEC-2021-0052.html) / `id-map` | `double_free` | Type Isolation: no direct; UniAlloc boundary: conditional detection | `poc_source_available` |
| RSH-013 | [RUSTSEC-2021-0053](https://rustsec.org/advisories/RUSTSEC-2021-0053.html) / `algorithmica` | `double_free` | Type Isolation: no direct; UniAlloc boundary: conditional detection | `poc_source_available` |
| RSH-014 | [RUSTSEC-2021-0047](https://rustsec.org/advisories/RUSTSEC-2021-0047.html) / `slice-deque` | `double_free` | Type Isolation: no direct; UniAlloc boundary: conditional detection | `poc_source_available` |
| RSH-015 | [RUSTSEC-2022-0012](https://rustsec.org/advisories/RUSTSEC-2022-0012.html) / `arrow2` | `double_free` | Type Isolation: no direct; UniAlloc boundary: conditional detection | `pinned_harness_source_available` |
| RSH-016 | [RUSTSEC-2022-0078](https://rustsec.org/advisories/RUSTSEC-2022-0078.html) / `bumpalo` | `use_after_free` | expected no direct effect | `pinned_harness_source_available` |
| RSH-017 | [RUSTSEC-2022-0070](https://rustsec.org/advisories/RUSTSEC-2022-0070.html) / `secp256k1` | `invalid_free` | Type Isolation: no direct; UniAlloc boundary: conditional detection | `pinned_harness_source_available` |
| RSH-018 | [RUSTSEC-2025-0053](https://rustsec.org/advisories/RUSTSEC-2025-0053.html) / `arenavec` | `double_free` | Type Isolation: no direct; UniAlloc boundary: conditional detection | `pinned_harness_source_available` |
| RSH-019 | [RUSTSEC-2026-0005](https://rustsec.org/advisories/RUSTSEC-2026-0005.html) / `oneshot` | `use_after_free` | expected no direct effect | `pinned_harness_source_available` |
| RSH-020 | [RUSTSEC-2026-0103](https://rustsec.org/advisories/RUSTSEC-2026-0103.html) / `thin-vec` | `double_free` | Type Isolation: no direct; UniAlloc boundary: conditional detection | `pinned_harness_source_available` |
| RSH-021 | [RUSTSEC-2026-0143](https://rustsec.org/advisories/RUSTSEC-2026-0143.html) / `oneringbuf` | `double_free` | Type Isolation: no direct; UniAlloc boundary: conditional detection | `pinned_harness_source_available` |
| RSH-022 | [RUSTSEC-2025-0054](https://rustsec.org/advisories/RUSTSEC-2025-0054.html) / `array-queue` | `uninitialized_drop` | expected no direct effect | `pinned_harness_source_available` |
| RSH-023 | [RUSTSEC-2020-0032](https://rustsec.org/advisories/RUSTSEC-2020-0032.html) / `alpm-rs` | `invalid_free` | Type Isolation: no direct; UniAlloc boundary: conditional detection | `poc_source_available` |
| RSH-024 | [RUSTSEC-2020-0006](https://rustsec.org/advisories/RUSTSEC-2020-0006.html) / `bumpalo` | `out_of_bounds_read` | expected no direct effect | `pinned_harness_source_available` |
| RSH-025 | [RUSTSEC-2021-0003](https://rustsec.org/advisories/RUSTSEC-2021-0003.html) / `smallvec` | `out_of_bounds_write` | expected no direct effect | `poc_source_available` |
| RSH-026 | [RUSTSEC-2021-0015](https://rustsec.org/advisories/RUSTSEC-2021-0015.html) / `calamine` | `out_of_bounds_write` | expected no direct effect | `pinned_harness_source_available` |
| RSH-027 | [RUSTSEC-2021-0050](https://rustsec.org/advisories/RUSTSEC-2021-0050.html) / `reorder` | `out_of_bounds_write` | expected no direct effect | `poc_source_available` |
| RSH-028 | [RUSTSEC-2021-0119](https://rustsec.org/advisories/RUSTSEC-2021-0119.html) / `nix` | `out_of_bounds_write` | expected no direct effect | `pinned_harness_source_available` |
| RSH-029 | [RUSTSEC-2022-0063](https://rustsec.org/advisories/RUSTSEC-2022-0063.html) / `linked_list_allocator` | `out_of_bounds_write` | expected no direct effect | `pinned_harness_source_available` |
| RSH-030 | [RUSTSEC-2022-0094](https://rustsec.org/advisories/RUSTSEC-2022-0094.html) / `mimalloc` | `misaligned_allocation` | expected no direct effect | `pinned_harness_source_available` |
| RSH-031 | [RUSTSEC-2023-0017](https://rustsec.org/advisories/RUSTSEC-2023-0017.html) / `maligned` | `invalid_free` | Type Isolation: no direct; UniAlloc boundary: conditional detection | `pinned_harness_source_available` |
| RSH-032 | [RUSTSEC-2023-0080](https://rustsec.org/advisories/RUSTSEC-2023-0080.html) / `transpose` | `out_of_bounds_write` | expected no direct effect | `pinned_harness_source_available` |
| RSH-033 | [RUSTSEC-2025-0049](https://rustsec.org/advisories/RUSTSEC-2025-0049.html) / `scratchpad` | `out_of_bounds_write` | expected no direct effect | `pinned_harness_source_available` |
| RSH-034 | [RUSTSEC-2021-0094](https://rustsec.org/advisories/RUSTSEC-2021-0094.html) / `rdiff` | `out_of_bounds_read` | expected no direct effect | `poc_source_available` |
| RSH-035 | [RUSTSEC-2020-0123](https://rustsec.org/advisories/RUSTSEC-2020-0123.html) / `libp2p-deflate` | `uninitialized_read` | expected no direct effect | `poc_source_available` |
| RSH-036 | [RUSTSEC-2020-0033](https://rustsec.org/advisories/RUSTSEC-2020-0033.html) / `alg_ds` | `uninitialized_drop` | expected no direct effect | `poc_source_available` |
| RSH-037 | [RUSTSEC-2020-0023](https://rustsec.org/advisories/RUSTSEC-2020-0023.html) / `rulinalg` | `aliasing_violation` | expected no direct effect | `poc_source_available` |
| RSH-038 | [RUSTSEC-2019-0036](https://rustsec.org/advisories/RUSTSEC-2019-0036.html) / `failure` | `type_confusion` | expected no direct effect | `poc_source_available` |
| RSH-039 | [RUSTSEC-2020-0050](https://rustsec.org/advisories/RUSTSEC-2020-0050.html) / `dync` | `misaligned_reference` | expected no direct effect | `poc_source_available` |
| RSH-040 | [RUSTSEC-2025-0105](https://rustsec.org/advisories/RUSTSEC-2025-0105.html) / `direct_ring_buffer` | `uninitialized_read` | expected no direct effect | `pinned_harness_source_available` |

The JSON manifest is the authoritative source for version ranges, replay pins,
source hashes, Rudra analyzer classes, secondary primitives, and detailed
mechanism boundaries.
