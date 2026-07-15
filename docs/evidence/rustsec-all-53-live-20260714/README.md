# All-53 Live RustSec Attempt Evidence

> **Frozen pre-correction provenance.** This bundle preserves the original
> 53-row screen, 49-case execution ledger, and **43/49** historical result.
> Post-source audit reclassifies RSH-006 as stack-lifetime and excludes it from
> heap allocator efficacy accounting. The current view is 52 retained
> heap-relevant candidates, 48 executable plus 4 audit-only, and **43/48**
> covered; the retained UAF view is 18 candidates, 17 executable, and **14/17**
> covered. See `docs/rustsec-security-scope-evaluation.md` and
> `docs/figures/rustsec-security-sets-20260715/`.

This historical bundle records terminal attempts for all 53 initially screened
candidates on 2026-07-14. RSH-006 remains present for replay provenance; its
vulnerability-relevant dangling target/lifetime path has no
`GlobalAlloc`-mediated allocation, reclaim, or reuse edge. The frozen
pre-correction efficacy denominator contains 49 executable cases.

- `executable-replay-48.json`: hash-verified execution of the 48 cases that were
  executable in the frozen scope, covering 98 live arms;
- `rsh064/experiment.json`: three-repetition Node/V8 execution of RSH-064 across
  `system`, `reclaim_plain`, `reclaim_checks`, `typed_plain`, and `typeiso`, plus
  the patched compile-rejection control; SHA-256
  `c3ecb6b1fd8abbf0990286e198f31d2c0b97c016a8921398317d45cae5a105ba`,
  using UniAlloc implementation digest
  `29ec9af086226428864e331e8a21fb9c3437c484e177a4973bb48312d8f9f8c7`;
- `rsh064/preflight.json`: pinned-input and host preflight for the Neon addon;
- `blocked-attempts/summary.json`: 37 captured diagnostic commands across the
  five cases that entered the reattempt stage;
- `blocked-attempts/REPORT.md`: human-readable reattempt findings;
- `scope-before-rsh064.json`: the 48-executable/5-blocked scope consumed by the
  frozen replay and reattempt artifact.
- `all-53-report.json`: reconciled machine-readable report joining the frozen
  replay, reattempts, and current RSH-064 experiment;
- `all-53-cases.csv`: one flattened row for each of the 53 candidates.
- `evaluable-49-report.json`: frozen pre-correction efficacy report with the four excluded
  rows and the RSH-064 historical transition kept in separate audit sections;
- `evaluable-49-cases.csv`: frozen pre-correction efficacy rows only.
- `artifact-hashes.json`: byte length and SHA-256 for every retained file in
  this bundle, including the RSH-064 compiler audits and blocker-probe logs.

RSH-064 became executable during the reattempt stage. The frozen source of
execution provenance is `evaluation/config/rustsec_heap_complete_scope.json`:
49 executable cases in the pre-correction efficacy scope and 4 audit-only
exclusions. The historical exclusive
case-level partition is 31 other-feature-only, 11 Type-Isolation-only, 1
multiple-mechanism, 3 matched no-signal (RSH-049, RSH-050, and RSH-075), and 3
inconclusive/unresolved (RSH-003, RSH-006, and RSH-019). The strict positive
count is **43/49**. The generated case report records 32 `detected`, 11
`mitigated`, 3 `no_signal`, and 3 `inconclusive` cases. The supporting
mechanism ledger has 62 rows: 32 `detected`, 12 `mitigated`, 12 `no_signal`,
and 6 `inconclusive`. Its 44 positive rows cover 43 unique cases: 31 reclaim
checks, 1 recovery-layout validation, and 12 Type Isolation edges, with
RSH-002 as the sole overlap. Thirteen cases retain two rows, preserving the
separate source, derived, or supplemental mechanism observations behind their
case-level disposition. RSH-054, RSH-056, RSH-059, and RSH-073 retain their
reattempt records and exclusion reasons here and stay outside every efficacy
numerator and denominator.

All 12 Type Isolation-positive cases are manually attributed derived
cross-identity reuse edges and count as causal true positives for the tested,
layout-bounded allocator decision. Source/published witnesses remain a separate
denominator. All 12 positive rows record
`compiler_automatic_victim_coverage=false` and
`source_vulnerability_detection_validated=false`; the full automatic
source-vulnerability Type Isolation true-positive count is **0**.
RSH-065 is a manually modeled four-byte reserve-relocation reduction, and
RSH-069 is a synthetically groomed 4096-byte FFI-read reduction. RSH-031's
separate recovery-layout validation contributes one policy-independent exact
diagnostic and zero Type Isolation credit.

The original direct campaign supplies 29 reclaim detections, including the
strict RSH-013 and RSH-020 refreshes. Supplemental RSH-001 and RSH-060
experiments raise the final reclaim-positive case count to 31. RSH-013 uses a
deterministic terminal `drop(values)` witness. RSH-020 uses a fail-closed parser
for its repeated identical source panic plus the standard destructor-cleanup
abort; distinct panic checkpoints remain ambiguous.

The 48-case replay artifact records binaries whose hashes were verified during
the original campaigns. The current local audit rehashes the 3 binaries still
present and marks 95 cleaned binaries as `binary_available=false` with their
recorded digests preserved as archived evidence. Every current strict-result
overlay remains bound to a present evidence file whose size and SHA-256 are
verified. Separately, all 12 strict Type Isolation matrices bind to one frozen
isolated WIP evidence snapshot with UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current shared-session working tree and current HEAD have separate live
provenance. `evaluable-49-report.json` keeps
`replay_arm_implementation_digest_counts` and
`strict_typeiso_evidence_implementation_digest_counts` separate. A unified
single-snapshot rebuild of all 49 evaluable cases remains future work.

The RSH-064 experiment records hashes for 17 ephemeral materializations: five
built addon binaries, six copied lockfiles, and six copied sources. The runner
removed its temporary build tree after completion. The repository retains the
pinned sources, lockfiles, runner, compiler audits, and stdout/stderr logs;
post-cleanup byte rehashing of those 17 materialized paths is unavailable.
Claim-grade release work should retain a complete raw build tree or publish a
content-addressed archive from one immutable source snapshot.

Seven older Batch-C status rows retain their original absolute
`/tmp/strong-batch-c-system-runs/.../experiment.json` provenance paths. Those
baseline JSON files are no longer available for post-hoc rehashing. Their
hash-verified frozen replay arms and strict mechanism evidence remain in the
current bundle; a claim-grade release should regenerate the seven baselines
inside a retained content-addressed output directory.

See `docs/rustsec-all-53-live-evaluation.md` for the complete 53-row ledger,
root-cause synopses, and claim boundaries.

Rebuild the frozen pre-correction 49-case view and separate exclusion audit:

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
