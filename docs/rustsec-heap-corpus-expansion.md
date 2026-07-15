# RustSec Heap-Security Corpus Expansion

## Final terminal update

The expanded review is complete for 53 strong candidates. The final efficacy
scope contains 49 executable witness/control integrations. RSH-054, RSH-056,
RSH-059, and RSH-073 retain evidence-backed attempt reasons outside that
denominator. The exclusive case-level partition contains 31 other-feature-only,
11 Type-Isolation-only, 1 multiple-mechanism, 3 matched no-signal, and 3
inconclusive/unresolved cases. The no-signal set is RSH-049, RSH-050, and
RSH-075; the unresolved set is RSH-003, RSH-006, and RSH-019. The strict
case-level positive result is **43/49**. The generated case report records 32
`detected`, 11 `mitigated`, 3 `no_signal`, and 3 `inconclusive` cases. The
mechanism-row ledger contains 62
rows: 32 `detected`, 12 `mitigated`, 12 `no_signal`, and 6 `inconclusive`.
Its 44 positive mechanism rows cover 43 unique cases: 31 reclaim checks, 1
recovery-layout validation, and 12 Type Isolation edges. RSH-002 is the sole
overlap, and 13 cases retain multiple rows.

The 12 Type Isolation-positive cases are manually attributed derived
cross-identity reuse experiments. They count as causal true positives for the
bounded allocator decision because the matched control reuses the
exploit-enabling address while Type Isolation withholds and reports that reuse.
Each strict row validates the matching treatment denial and report binding.
All 12 rows record `compiler_automatic_victim_coverage=false` and
`source_vulnerability_detection_validated=false`. Published and upstream source
witnesses retain a separate denominator, and the full automatic
source-vulnerability Type Isolation true-positive count is **0**.

RSH-064 (`neon`) moved from blocked to executable after integration of a real
Node/V8-hosted N-API addon. Neon 0.10.0 reproduces the stale external-buffer
read, and Neon 0.10.1 rejects the identical source with its added `'static`
bound. The source Type Isolation row remains inconclusive because the compiler
audit records zero applied rewrites at the critical allocation path. A separate
manually attributed derived reuse experiment contributes RSH-064's bounded
Type Isolation-positive case-level result while preserving the source row and
its automatic-coverage boundary.

RSH-031 contributes the policy-independent recovery-layout detection. Its
derived native matrix binds an exact allocation/deallocation layout diagnostic
to the Vec Drop edge, while the pinned upstream Miri matrix supplies source
ground truth. RSH-065 and RSH-069 retain explicit synthetic-reduction caveats
inside the manual Type Isolation set.

All 12 strict Type Isolation matrices bind to one frozen isolated WIP evidence
snapshot with UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
This hash names the captured evidence snapshot. The current shared-session
working tree and current HEAD have separate live provenance. The complete report records Type Isolation
snapshot digests separately from historical replay-arm digests.

The scope freezes RustSec advisory-db at
`9f3e138091487e69144f536d36976e427a7a3307` and Rudra-PoC at
`6226dd030fffbed5601099cb0e24f73e4150a7f5`. Its primary witness taxonomy is 30
double-free, 19 use-after-free, 2 uninitialized-drop, 1 invalid-free, and 1
out-of-bounds-read advisory. Every result remains exploratory with
`claim_grade=false`.

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
signals, zero plain-arm signals, and zero patched signals. The original strict
exporter accepts all 29 direct detections; supplemental RSH-001 and RSH-060
experiments raise the final reclaim-positive case count to 31. RSH-013 uses a
deterministic terminal `drop(values)`, which prevents corrupted-string
formatting from preempting the duplicate reclaim. RSH-020 uses a fail-closed
double-panic parser that accepts repeated identical source checkpoints followed
by the standard destructor-cleanup abort and rejects distinct checkpoints as
ambiguous. The final merged ledger records 12 reclaim no-signal rows, 1
evidence-inconclusive reclaim row, and 5 inconclusive Type Isolation source
rows; case-level reconciliation uses the positive derived Type Isolation and
recovery-layout rows before assigning one outcome per case. RSH-006 supplies
the evidence-inconclusive reclaim row because its matched SIGSEGV lacks a
normalized source-bound fault/checkpoint fingerprint; source analysis still
indicates a stack-lifetime boundary.

The campaign manifest and derived records are retained as
`docs/evidence/rustsec-reclaim-checks-20260714/feature-matched-campaign.json`,
`summary-feature-matched-*.json`, and
`mechanism-results-feature-matched-*.json`.

The authoritative result, commands, claim boundary, and figure are in
[`rustsec-security-scope-evaluation.md`](rustsec-security-scope-evaluation.md).
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
| Reviewed strong candidates | **53** |
| Strong candidates already present in the 40-case corpus | 17 |
| Strong candidates missing from the 40-case corpus | **36** |
| Final evaluable efficacy scope | **49** |
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

The review supports **53 strong research candidates**. This is a candidate
count. Population-level detection and mitigation numerators remain unmeasured.
The completed campaign provides a 49-case executable efficacy denominator and
retains four non-evaluable candidates as audit-only exclusions.

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
| RSH-041 / `oneringbuf` | ASan UAF 3/3; patched clean 3/3 | no allocator signal; observed allocations used fallback | manually attributed 40-byte A-to-B edge: `typed_plain` reused 3/3, `typeiso` reused 0/3 and reported 3/3 |
| RSH-042 / `emap` | ASan UAF 3/3; patched clean 3/3 | no allocator signal; published witness contains no measured replacement edge | manually attributed 64-byte A-to-B edge: `typed_plain` reused 3/3, `typeiso` reused 0/3 and reported 3/3 |
| RSH-043 / `bitchomp` | ASan double free 3/3; patched clean 3/3 | `typed_plain` and `typeiso` both reported pointer-already-released 3/3 | not applicable |
| RSH-044 / `qwutils` | ASan double free 3/3; patched safe panic 3/3 | `typed_plain` and `typeiso` both reported pointer-already-released 3/3 | not applicable |
| RSH-045 / `stack_dst` | ASan double free 3/3; patched safe panic 3/3 | `typed_plain` and `typeiso` both reported pointer-already-released 3/3 | not applicable |

The published-witness denominator yields five reproduced baselines, five
matched patched controls, three common UniAlloc tracked-reclaim signals, and
zero Type-Isolation-specific published signals. The derived denominator yields
two validated, manually attributed cross-identity reuse-policy observations.
Automatic compiler victim coverage and source-vulnerability detection remain
zero for those two derived scenarios, so neither result is a general UAF
mitigation claim.

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
49-case efficacy denominator; earlier exits remain in the 53-candidate attempts
audit.

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

The supported Type Isolation claim is that a covered cross-identity reuse edge
is withheld. The supported tracked-reclaim claim is that a second allocator
operation is rejected before duplicate backend release. Source-level UAF repair,
ordinary stale-load detection, and general pointer provenance remain outside
those claims.
