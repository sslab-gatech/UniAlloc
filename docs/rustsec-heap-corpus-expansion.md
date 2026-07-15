# RustSec Heap-Security Corpus Expansion

## Final terminal update

The expanded review initially screened **53 candidates**. Post-source
inspection reclassified RSH-006 as a stack-lifetime case: the
vulnerability-relevant dangling target/lifetime path has no
`GlobalAlloc`-mediated allocation, reclaim, or reuse edge. The corrected scope
retains **52 heap-relevant candidates**, comprising **48 executable**
witness/control integrations and **4 audit-only** exclusions (RSH-054,
RSH-056, RSH-059, and RSH-073). The executable partition contains 31
other-feature-only cases, 11 TypeIso measured-reuse-edge-only cases, 1 measured
reuse edge plus other feature case, 3 matched no-signal cases, and 2
mechanism-boundary cases.

Allocator mechanisms provide qualified coverage for **43/48** executable
candidates: 31 exact duplicate-reclaim detections, 1 exact recovery-layout
validation, and 12 compiler-bound causal mitigations of measured cross-identity
reuse edges, with RSH-002 counted once. Eleven of the Type Isolation rows are
TypeIso measured-reuse-edge-only; RSH-002 overlaps with `reclaim_checks`. This ratio describes
the integrated allocator mechanisms and witnesses. Type Isolation automatic
source-level vulnerability detections remain **0/12**. The retained UAF scope
contains **18 candidates**, including **17 executable** cases; allocator
mechanisms cover **14/17** executable UAF cases.

The 12 Type Isolation experiments comprise two
`derived_vulnerability_edge` scopes (RSH-008 and RSH-041) and ten
`source_shaped_derived` scopes. None has `published_source` coverage. Each
vulnerable system and `typed_plain` arm collides at the witness's reclaimed
address in all three repetitions. Type Isolation withholds that address in all
three repetitions. The stats record's `last_wrong_identity_retained_ptr` must
equal the harness's parsed `original` or `stale_value` pointer, which binds the
withheld cache object to the measured witness edge.

The compiler contract binds both preregistered sides of the decision. The
requested and victim sites must each select one unique applied compiler-audit
candidate, and the runtime requested/retained identities, modules, requested
callsite, size, and alignment must join those candidates. The automatic probe
turns the historical manual metadata helper into a no-op. Every strict row
records `compiler_automatic_victim_coverage=true`,
`manual_victim_identity_annotation=false`,
`source_vulnerability_detection_validated=false`,
`vulnerability_specific_detection_signal=false`, and `claim_grade=false`.

Coverage comes from generic `Box<T>` runtime identity, monomorphized runtime
`TypeId` for exact `Global` `Vec::with_capacity<T>`, authenticated primitive
`alloc::slice::to_vec`, and exact source-shaped `String` sites/transfers.
RSH-052 proves an exact `String` allocation origin followed by an audited
pointer-preserving `String -> Box<str>` ownership transfer. RSH-066 moves its
exact `String` owner into `AtomicStr`. RSH-068 and RSH-069 retain identities
from their allocation origins through the relevant `String` and `CString`
ownership chains. The pass fails closed for custom allocators, unresolved or
ambiguous generic owners, lookalike methods, and element types with user-defined
`Clone` or drop behavior.

All patched system, `typed_plain`, and `typeiso` arms exercise safe
same-identity functionality and complete without a matching attribution event.
Allocator unit controls separately preserve same-identity reuse and withhold an
eligible cross-identity cache entry. RSH-065 retains a synthetic reserve-
relocation reduction, and RSH-069 retains synthetic grooming of the FFI-read
edge. RSH-064's real Node/V8 source witness remains a complete ownership-chain
coverage gap while its separate derived edge supplies bounded causal evidence.

The attribution fields are `stats`-gated evaluation telemetry. The retained-
pointer field uses one companion atomic and leaves cache layouts and the public
semantic-stats snapshot unchanged. Production enforcement remains the ordinary
exact-identity cache decision. The completed paired
`typeiso_perf / typed_plain` ratio-of-ratios screen passed its merge gates:
Oxipng wall time changed **-1.6649%** and peak RSS changed **+0.583%**; the long
Ripgrep workload changed **+0.8107%** in wall time (95% bootstrap CI
**[-0.180%, +1.399%]**) and retained the same **7168 KiB** maximum RSS. The
gates require wall-time regression at or below 2%, Oxipng RSS regression at or
below 1%, and unchanged Ripgrep maximum RSS. Aggregate semantic-rewrite counts
were unchanged in these selected macros, so active new-site evidence comes
from the 12 RustSec matrices and compiler regression tests. Evidence is under
`docs/evidence/rustsec-typeiso-automatic-20260715/performance-screen/`.

RSH-002's bounded denial precedes a later `type-cache pointer already retained`
duplicate-reclaim abort. Its Type Isolation row covers the earlier
cross-identity reuse decision; its distinct `reclaim_checks` row covers the
later exact duplicate reclaim. RSH-031 contributes the separate policy-
independent recovery-layout validation.

The frozen manually annotated matrices remain historical calibration under
UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The automatic compiler-bound provenance epoch is
`docs/evidence/rustsec-typeiso-automatic-20260715/`; its per-case records and
`summary.json` remain separate from the historical manual and replay-arm
artifacts.

The scope freezes RustSec advisory-db at
`9f3e138091487e69144f536d36976e427a7a3307` and Rudra-PoC at
`6226dd030fffbed5601099cb0e24f73e4150a7f5`. The corrected 52-case heap scope
contains 30 double-free, 18 use-after-free, 2 uninitialized-drop, 1
invalid-free, and 1 out-of-bounds-read advisory. The initial 53-row screen adds
the subsequently excluded stack-lifetime RSH-006 row.

The feature-attribution campaign evaluates 42 reclaim scenarios with the six
logical arms `vulnerable,patched x system,reclaim_plain,reclaim_checks` and
three requested repetitions. The original direct portion contains 168 arms,
492 runtime executions, and 4 expected matched compile-rejection arms. Those
direct arms bind to implementation digest
`36bc040455f8c5fa6142a91b2321bc9d018aad08764f7d8c8e5554b3e4460847`;
the deterministic RSH-013 refresh binds all six arms to digest
`b9bd5442c557b3d39c34cf391e8c32388ba84ea643e6adc91ea4987d85adbc5a`.
Cargo metadata resolves `reclaim_plain` to `stats` and `reclaim_checks` to
`reclaim_checks,stats`. The direct observations include 29 vulnerable check-arm
signals, zero plain-arm signals, and zero patched signals. Supplemental RSH-001
and RSH-060 experiments raise the final reclaim-detection count to 31. RSH-013
uses deterministic terminal `drop(values)`, and RSH-020 uses a fail-closed
double-panic parser. The frozen pre-correction merged ledger retains 12 reclaim
no-signal rows, the historical evidence-inconclusive RSH-006 reclaim row, and 5
inconclusive Type Isolation source rows. Corrected case-level reconciliation
excludes RSH-006, then applies the distinct derived Type Isolation and
recovery-layout evidence before assigning one outcome per case.

The campaign manifest and derived records are retained as
`docs/evidence/rustsec-reclaim-checks-20260714/feature-matched-campaign.json`,
`summary-feature-matched-*.json`, and
`mechanism-results-feature-matched-*.json`.

The authoritative corrected result, commands, claim boundary, and figures are in
[`rustsec-security-scope-evaluation.md`](rustsec-security-scope-evaluation.md).
Editable set figures and exact case membership are under
[`figures/rustsec-security-sets-20260715/`](figures/rustsec-security-sets-20260715/).
The sections below preserve the discovery and staged-integration rationale.

## Discovery and initial expansion record

The 40-case corpus is a reproducibility-oriented pilot, not the RustSec research
universe. A complete scan of the pinned RustSec advisory database identifies a
substantially larger allocator-security program:

| Screening stage | Count |
|---|---:|
| RustSec advisory records at the pinned commit | 1,140 |
| Active records | 875 |
| `memory-corruption` category | 253 |
| Original exact category/keyword screen | 276 (275 active) |
| Expanded title/keyword screen | 348 (347 active) |
| Active high-recall manual-review queue | 439 |
| Direct temporal/reclaim advisory records | 84 |
| Independent temporal/reclaim review units after one duplicate alias merge | 83 |
| Clear Rust-global allocator candidates from the strict screen | 50 |
| Additional strong candidates exposed by pinned Rudra source/PoCs | 3 |
| Initially screened candidates | **53** |
| Strong candidates already present in the 40-case corpus | 17 |
| Strong candidates missing from the 40-case corpus | **36** |
| Post-source-audit stack-lifetime exclusion | **1** |
| Retained heap-relevant candidates | **52** |
| Final evaluable heap efficacy scope | **48** |
| Audit-only exclusions with retained reasons | **4** |
| Strong candidates still pending integration | **0** |

The source snapshot is RustSec advisory-db commit
`9f3e138091487e69144f536d36976e427a7a3307`, dated 2026-07-13. It is both the
commit pinned by the existing corpus and the repository HEAD used for this
inventory.

The previous `3/40` number described three preregistered cross-identity
`A -> free -> B` research hypotheses in the frozen 40-advisory pilot. Two of
those hypotheses had repository-backed reuse-derived adapters. This number has
no detection-rate or mitigation-rate meaning. It also leaves the available
RustSec advisory pool, tracked-reclaim coverage, and additional controlled UAF
grooming experiments unmeasured.

## Reproducible inventory

The generated inventory is
`evaluation/config/rustsec_heap_candidate_inventory.json`. It records:

- every advisory admitted by the original metadata screen or the expanded
  title/keyword screen;
- the pinned source path and SHA-256 for every admitted advisory;
- explicit UAF, double-free, invalid-deallocation, OOB, and uninitialized-memory
  screening signals;
- the current 40-case membership, mechanism-review queues, and claim boundary;
- the pinned Rudra-PoC inventory and its RustSec mappings.

Regenerate it with:

```bash
uv run python evaluation/scripts/inventory_rustsec_heap_candidates.py \
  --rustsec-db /path/to/advisory-db \
  --rudra-poc /path/to/Rudra-PoC \
  --output evaluation/config/rustsec_heap_candidate_inventory.json
```

The command fails closed when either checkout differs from the commits pinned
in `evaluation/config/rustsec_heap_security_corpus.json`.

## Why the original metadata screen was incomplete

The original screen accepts `category = memory-corruption` or eight exact
hyphenated keywords. RustSec metadata is heterogeneous. Some advisories use
space-separated terms such as `use after free`; some state the primitive only
in the title or body; some use `informational = unsound` without a matching
category.

The expanded title/keyword screen adds 72 records. Sixteen direct
temporal/reclaim records were absent from the original 276-record screen,
including:

- `RUSTSEC-2018-0003` and `RUSTSEC-2019-0009` (`smallvec`);
- `RUSTSEC-2018-0009` (`crossbeam`);
- `RUSTSEC-2019-0023` (`string-interner`);
- `RUSTSEC-2026-0172` (`diesel`);
- `RUSTSEC-2026-0205` (`scc`).

Automated matching remains a review queue. A title or keyword cannot establish
which allocator owns the failing bytes, whether the witness is reproducible,
or whether the critical site carries exact compiler metadata.

## Temporal and reclaim manual review

The durable manual classification is
`evaluation/config/rustsec_temporal_reclaim_review.json`.

The 84 direct temporal/reclaim records contain two Wasmtime records for the
same upstream advisory, `GHSA-gwc9-348x-qwv2`. Merging that alias pair leaves 83
independent review units:

| Review outcome | Count | Meaning |
|---|---:|---|
| Clear Rust-global candidate | 50 | The advisory identifies Rust-global storage or a heap-owning Rust payload |
| Conditional candidate | 7 | Source/PoC inspection must still prove allocator ownership |
| Evidence-backed exclusion | 26 | Foreign/custom/arena ownership, stack-only storage, or no allocator transition |

The 50 clear candidates decompose into:

- **24** cross-type-reuse/UAF candidates;
- **33** tracked-reclaim or invalid-deallocation candidates;
- **7** candidates belonging to both groups.

Pinned Rudra source evidence adds three double-drop candidates whose advisory
titles are too generic for the strict screen:

- `RUSTSEC-2020-0038` (`ordnung`);
- `RUSTSEC-2020-0105` (`abi_stable`);
- `RUSTSEC-2021-0009` (`basic_dsp_matrix`).

The review initially admitted **53 research candidates**. Post-source audit
removes stack-lifetime RSH-006 from heap allocator efficacy accounting, leaving
**52 heap-relevant candidates**. Population-level detection and mitigation
numerators remain unmeasured. The completed campaign provides a **48-case**
executable heap efficacy denominator and retains four non-evaluable candidates
as audit-only exclusions.

## Rudra-PoC expansion opportunity

The pinned Rudra-PoC checkout contains:

| Metric | Count |
|---|---:|
| Rust PoC files | 166 |
| Parseable PoC files | 165 |
| RustSec-mapped PoCs / unique RustSec IDs | 129 / 129 |
| IDs selected by the frozen 40-advisory pilot | 19 |
| Mapped IDs missing from the frozen pilot | **110** |
| RustSec-mapped PanicSafety IDs | 24 |
| PanicSafety IDs missing from the frozen pilot | **18** |

This source is the fastest expansion path because it already supplies pinned
vulnerable versions and Rust trigger programs. Repository materializers,
patched controls, and matched allocator arms are still required before an ID
enters the executable denominator.

The executable expansion adds two Rudra-mapped IDs, RSH-044 and RSH-045. The
combined pilot-plus-expansion selection therefore covers 21 of the 129 mapped
IDs and 8 of the 24 PanicSafety IDs, leaving 108 and 16 respectively outside
the selected set.

## First expansion wave (historical construction record)

The first wave contains 19 advisories chosen for existing pure-Rust PoCs,
inline triggers, or small deterministic upstream witnesses.

### Pinned Rudra-PoC panic-safety cases

```text
RUSTSEC-2020-0038  ordnung
RUSTSEC-2020-0105  abi_stable
RUSTSEC-2021-0005  glsl-layout
RUSTSEC-2021-0009  basic_dsp_matrix
RUSTSEC-2021-0010  containers
RUSTSEC-2021-0018  qwutils
RUSTSEC-2021-0028  toodee
RUSTSEC-2021-0033  stack_dst
RUSTSEC-2021-0039  endian_trait
RUSTSEC-2021-0040  arenavec
RUSTSEC-2021-0042  insert_many
```

`RUSTSEC-2021-0011` (`fil-ocl`) has a relevant PoC and remains deferred behind
an OpenCL runtime requirement.

### Inline or compact upstream witnesses

```text
RUSTSEC-2018-0003  smallvec
RUSTSEC-2019-0009  smallvec
RUSTSEC-2025-0044  slice-ring-buffer
RUSTSEC-2026-0122  rkyv
RUSTSEC-2026-0128  emap
RUSTSEC-2026-0131  bitchomp
RUSTSEC-2026-0152  oneringbuf
RUSTSEC-2026-0205  scc
```

These cases emphasize duplicate-drop/reclaim behavior and controlled UAF
grooming. The initial five published cases reused the existing
vulnerable/patched/system/UniAlloc matrix: `oneringbuf`, `emap`, `bitchomp`,
`qwutils`, and `stack_dst`. Later batches completed the terminal integration
program recorded above. The expansion also contains two derived reuse scenarios
for `oneringbuf` and `emap`; these exercise allocator policy and retain a
separate denominator from the advisory witnesses.

## Executable first-wave status (historical five-case slice)

The executable expansion catalog is
`evaluation/config/rustsec_heap_expansion_harnesses.json`. It contains five new
advisory IDs and seven repository scenarios: five published/upstream witnesses
and two derived reuse probes. The retained evidence is in
`docs/evidence/rustsec-security-expansion-20260714/`.

| Case | Published baseline and patched control | Published allocator result | Derived Type Isolation result |
|---|---|---|---|
| RSH-041 / `oneringbuf` | ASan UAF 3/3; patched clean 3/3 | no allocator signal; observed allocations used fallback | historical manually attributed 40-byte A-to-B edge: `typed_plain` reused 3/3, `typeiso` reused 0/3 with a historical stats attribution event 3/3 |
| RSH-042 / `emap` | ASan UAF 3/3; patched clean 3/3 | no allocator signal; published witness contains no measured replacement edge | historical manually attributed 64-byte A-to-B edge: `typed_plain` reused 3/3, `typeiso` reused 0/3 with a historical stats attribution event 3/3 |
| RSH-043 / `bitchomp` | ASan double free 3/3; patched clean 3/3 | `typed_plain` and `typeiso` both reported pointer-already-released 3/3 | not applicable |
| RSH-044 / `qwutils` | ASan double free 3/3; patched safe panic 3/3 | `typed_plain` and `typeiso` both reported pointer-already-released 3/3 | not applicable |
| RSH-045 / `stack_dst` | ASan double free 3/3; patched safe panic 3/3 | `typed_plain` and `typeiso` both reported pointer-already-released 3/3 | not applicable |

The published-witness denominator yields five reproduced baselines, five
matched patched controls, three common UniAlloc tracked-reclaim signals, and
zero Type-Isolation-specific published signals. The derived denominator yields
two validated, historically manually attributed cross-identity reuse-policy
observations. That first-wave snapshot recorded zero automatic compiler victim
coverage. The terminal automatic probe now covers both derived edges without manual
metadata and binds the withheld pointer to each harness witness. Source-level
vulnerability detection remains zero, and both rows remain bounded allocator
reuse-edge mitigations.

## Evaluation denominators

The evaluation should report the following funnel while retaining every
reviewed attempt:

```text
1,140 RustSec records
  -> 875 active records
  -> 439 high-recall manual-review rows
  -> heap-participating and allocator-owned units
  -> vulnerable source resolvable
  -> public PoC or materializer available
  -> vulnerable and patched controls build
  -> baseline oracle reproduces
  -> otherwise: audit-only exclusion with a reason code
  -> failing object reaches UniAlloc
  -> critical compiler metadata is covered
  -> mechanism eligible
  -> detected / no signal / inconclusive
```

Every exit receives a reason code such as `foreign_allocator`,
`stack_only`, `no_public_reproducer`, `patched_control_unavailable`,
`compiler_identity_unresolved`, or `mechanism_ineligible`.
Only rows that reach an executable vulnerable oracle and control enter the
48-case corrected heap efficacy denominator. The frozen 53-row attempts audit
and pre-correction 49-case execution files remain retained as historical
provenance.

Three complementary denominator units are required:

1. **Advisory IDs** measure database coverage.
2. **Independent root-cause families** avoid duplicate GHSA/upstream fixes.
3. **Executable witnesses** preserve multiple independent bugs contained in one
   advisory, such as the four witnesses reported by `slice-ring-buffer`.

## Mechanism attribution

Results must remain separated by mechanism:

- exact-type reuse routing: a distinct identity cannot consume an eligible
  retained object;
- tracked reclaim: a duplicate reclaim fails while the address remains in a
  retained, delayed, released, or bounded-history state;
- recovery and software-tag validation: known pointer/layout/identity mismatch
  is reported, corrected, or rejected according to the selected policy;
- guard pages and force initialization: separate spatial and initialization
  treatments.

The supported Type Isolation claim is a compiler-bound causal mitigation of a
measured derived or source-shaped cross-identity reuse edge. The supported
tracked-reclaim claim is rejection of a second allocator operation before
duplicate backend release. Type Isolation source-level vulnerability detection
and vulnerability-specific detector signals remain zero; source-level UAF
repair, ordinary stale-load detection, and general pointer provenance stay
outside these mechanism contracts.
