# Reclaim-Checks Evidence Bundle

This bundle retains the feature-matched RustSec allocator experiments used by
`docs/rustsec-security-scope-evaluation.md`. All results are exploratory
(`claim_grade=false`) and bounded to the integrated witnesses.

## Final strict results

The authoritative merged ledger is
`evaluation/config/rustsec_heap_mechanism_results.json`:

- `reclaim_checks`: 31 `detected`, 12 `no_signal`, and 1 `inconclusive`
  (RSH-006);
- `recovery_layout_validation`: 1 `detected` (RSH-031);
- Type Isolation derived edges: 12 `mitigated`;
- Type Isolation source experiments: 5 `inconclusive`, including the separate
  RSH-064 Node/V8 experiment.

The mechanism ledger contains 62 rows across 49 executable cases: 32
`detected`, 12 `mitigated`, 12 `no_signal`, and 6 `inconclusive`. Thirteen
cases retain two mechanism or scenario rows: RSH-001, RSH-002, RSH-008,
RSH-031, RSH-052, RSH-055, RSH-060, RSH-064, RSH-065, RSH-066, RSH-067,
RSH-068, and RSH-069. Its 44 positive rows cover 43 unique cases: 31 reclaim
checks, 1 recovery-layout validation, and 12 Type Isolation edges, with
RSH-002 as the sole overlap.

The exclusive case-level partition is 31 other-feature-only, 11 Type-Isolation-
only, 1 multiple-mechanism, 3 no-signal (RSH-049, RSH-050, and RSH-075), and 3
inconclusive/unresolved (RSH-003, RSH-006, and RSH-019). The strict positive
count is **43/49**. The generated case report records 32 `detected`, 11
`mitigated`, 3 `no_signal`, and 3 `inconclusive` cases. The historical review
contains 53 candidates; RSH-054, RSH-056, RSH-059, and RSH-073 retain
audit-only exclusion reasons outside the efficacy denominator.

The 12 Type Isolation mitigations are manually attributed derived
cross-identity reuse edges. They are causal true positives for the bounded
allocator decision represented by each experiment. Published/source rows remain
separate, including an inconclusive source row alongside a positive derived row
for RSH-008 and RSH-064. All 12 positive rows record
`compiler_automatic_victim_coverage=false` and
`source_vulnerability_detection_validated=false`; the full automatic
source-vulnerability Type Isolation true-positive count is **0**.
RSH-065 is a manually modeled four-byte reserve-relocation reduction, and
RSH-069 is a synthetically groomed 4096-byte FFI-read reduction.

All 12 strict Type Isolation matrices bind to one frozen isolated WIP evidence
snapshot with UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current shared-session working tree and current HEAD have separate live
provenance. The complete report keeps historical replay-arm digest counts and
strict Type Isolation evidence digest counts in separate fields.

Each reclaim result requires six matched arms and three requested repetitions:

```text
vulnerable/system
vulnerable/reclaim_plain
vulnerable/reclaim_checks
patched/system
patched/reclaim_plain
patched/reclaim_checks
```

`reclaim_plain` resolves exactly `stats`; `reclaim_checks` resolves exactly
`stats,reclaim_checks`. Both disable default features. The exporter also
requires one canonical repository package identity, one UniAlloc
implementation digest across the four direct arms, identical subject Cargo
configuration within each plain/check pair, reproduced system oracles, and
matched patched controls.

The original 42-scenario direct campaign contains 168 arms, 492 runtime
executions, and 4 matched compile-rejection arms. Those direct arms record
implementation digest
`36bc040455f8c5fa6142a91b2321bc9d018aad08764f7d8c8e5554b3e4460847`.
RSH-013 also has a current-source deterministic six-arm refresh bound to
implementation digest
`b9bd5442c557b3d39c34cf391e8c32388ba84ea643e6adc91ea4987d85adbc5a`.
The exact release diagnostic appeared in 29 vulnerable `reclaim_checks`
scenarios in the original direct campaign and in zero `reclaim_plain` or
patched scenarios. Supplemental strict RSH-001 and RSH-060 experiments raise
the final reclaim-positive case count to 31. RSH-013 replaces the formatted
post-corruption observation with deterministic `drop(values)` and completes all
six arms in 3/3 repetitions. RSH-020 is revalidated from retained raw output by a fail-closed
double-panic parser: repeated identical source checkpoints followed by the
standard destructor-cleanup abort form one stable outcome; distinct checkpoints
remain ambiguous.

RSH-006 remains evidence-inconclusive. Its matched SIGSEGV lacks a normalized
source-bound fault/checkpoint fingerprint. Source analysis indicates a
stack-lifetime boundary, while empirical no-signal attribution remains
withheld.

## Files

- `feature-matched-campaign.json`: campaign inventory, hashes, selected
  allocator arms, feature contracts, and implementation digest;
- `raw/catalog-snapshots/rustsec_heap_strong_batch_e_harnesses-d0442a40.json`:
  immutable catalog for the historical RSH-010, RSH-011, RSH-012, and RSH-014
  runs;
- `summary-feature-matched-*.json`: normalized per-catalog six-arm summaries;
- `mechanism-results-feature-matched-*.json`: strict exporter fragments;
- `mechanism-results-typeiso-source-sweep.json`: compiler-coverage-gated
  Type Isolation source results;
- `mechanism-results.json`: merged 62-row ledger covering 49 executable cases;
- `raw/feature-matched/*experiment.json`: complete direct-arm runner records;
- `raw/feature-matched/*preflight.json`: planned topology and source pins;
- `raw/*experiment.json` and `raw/*preflight.json`: retained system and earlier
  campaign records referenced by the feature-matched summaries;
- `overhead-summary.json` and `raw/overhead-*.json`: bounded wall-time and
  peak-RSS measurements.

RSH-075 uses a cataloged allocator-specific Cargo feature selection. The system
arm keeps Diesel's `sqlite,__with_asan_tests` configuration for the ASan ground
truth. Both direct arms use the mirrored `sqlite` subject configuration, and
the plain/check pair differs only by UniAlloc's `reclaim_checks` feature.

## Boundary

`detected` requires the exact `unialloc_pointer_already_released_check` in every
vulnerable treatment repetition, a signal-free feature-matched plain arm with a
complete outcome, and valid controls. The diagnostic covers duplicate reclaim
before intervening address reuse. Stale access, spatial corruption,
uninitialized reads, and source-level UAF paths that never reach duplicate
reclaim remain separate mechanism classes.
