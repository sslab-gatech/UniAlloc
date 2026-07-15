# Complete RustSec Strong-Candidate Integration Evidence

The reconciled review contains 53 strong candidates. The final efficacy scope
contains 49 executable cases; RSH-054, RSH-056, RSH-059, and RSH-073 are
audit-only exclusions with retained evidence.

The repository catalogs are:

```text
evaluation/config/rustsec_heap_harnesses.json
evaluation/config/rustsec_heap_expansion_harnesses.json
evaluation/config/rustsec_heap_strong_batch_a_harnesses.json
evaluation/config/rustsec_heap_strong_batch_b_harnesses.json
evaluation/config/rustsec_heap_strong_batch_c_harnesses.json
evaluation/config/rustsec_heap_strong_batch_d_harnesses.json
evaluation/config/rustsec_heap_strong_batch_e_harnesses.json
evaluation/config/rustsec_heap_strong_batch_f_harnesses.json
evaluation/config/rustsec_heap_neon_node_harnesses.json
```

Batch status catalogs preserve all failed integration attempts and their source
evidence. `evaluation/config/rustsec_heap_complete_scope.json` is the generated
49-case efficacy ledger; its `excluded_cases` array retains the four audit-only
reasons. `docs/figures/rustsec-security-scope-20260714/` preserves the
current reviewed-candidate presentation bundle.

The exclusive case-level partition contains 31 other-feature-only cases, 11
Type-Isolation-only cases, 1 multiple-mechanism case, 3 matched no-signal
cases (RSH-049, RSH-050, and RSH-075), and 3 inconclusive/unresolved cases
(RSH-003, RSH-006, and RSH-019), with a strict positive count of **43/49**.
The generated case report records 32 `detected`, 11 `mitigated`, 3
`no_signal`, and 3 `inconclusive` cases. The underlying mechanism ledger
contains 62 rows: 32 `detected`, 12 `mitigated`, 12 `no_signal`, and 6
`inconclusive`. Its 44 positive mechanism rows cover 43 unique cases: 31
reclaim checks, 1 recovery-layout validation, and 12 Type Isolation edges.
RSH-002 is the sole overlap. Thirteen cases retain two rows, so source,
derived, and supplemental mechanism evidence remain auditable while each
advisory receives one case-level disposition. The four
audit-only exclusions remain in the 53-candidate attempts audit and stay
outside every efficacy numerator and denominator. The complete matched campaign inventory is
`docs/evidence/rustsec-reclaim-checks-20260714/feature-matched-campaign.json`.

All 12 Type Isolation-positive cases are manually attributed derived
cross-identity reuse edges. Their causal true-positive scope is the bounded
allocator decision demonstrated by matched address reuse under `typed_plain`
and matching reuse denial under Type Isolation. Each strict row validates the
matching treatment denial and report binding. All 12 rows record
`compiler_automatic_victim_coverage=false` and
`source_vulnerability_detection_validated=false`; the full automatic
source-vulnerability Type Isolation true-positive count is **0**.
RSH-065 is a manually modeled four-byte reserve-relocation reduction, and
RSH-069 is a synthetically groomed 4096-byte FFI-read reduction.

All 12 strict Type Isolation matrices bind to one frozen isolated WIP evidence
snapshot with UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current shared-session working tree and current HEAD have separate live
provenance. `evaluable-49-report.json` records
`strict_typeiso_evidence_implementation_digest_counts` separately from
`replay_arm_implementation_digest_counts`.
`typeiso-frozen-snapshot-summary.json` binds the 12 matrix hashes and case IDs.
`typeiso-frozen-implementation-ce653fd5.tar.gz` preserves the exact 136
implementation inputs, and its adjacent manifest recomputes the same digest
from sorted repository-relative paths and bytes.

The original direct campaign supplies 29 reclaim detections. Supplemental
strict experiments for RSH-001 and RSH-060 raise the final reclaim-positive
case count to 31. RSH-013 reaches `detected` through its deterministic terminal
`drop(values)` witness. RSH-020 reaches `detected` through a fail-closed
double-panic parser that accepts repeated identical source checkpoints followed
by the standard destructor-cleanup abort and rejects distinct checkpoints as
ambiguous.

RSH-064 supplies the additional executable source row. Its source Type
Isolation result remains inconclusive, while its separately derived,
manually attributed reuse edge is `mitigated` and supplies the bounded
case-level Type Isolation-positive result. Its real Node/V8-hosted evidence is
`docs/evidence/rustsec-all-53-live-20260714/rsh064/experiment.json`. The full
terminal-attempt bundle and per-case ledger are documented in
`docs/evidence/rustsec-all-53-live-20260714/README.md` and
`docs/rustsec-all-53-live-evaluation.md`.

RSH-031 supplies the policy-independent recovery-layout positive. Its pinned
upstream Miri baseline establishes the allocation/deallocation mismatch, and
the derived native matrix emits the exact recovery-layout diagnostic only in
the vulnerable typed arms. This row contributes zero Type Isolation credit.

The scope gate is:

```bash
args=(
  --catalog evaluation/config/rustsec_heap_harnesses.json
  --catalog evaluation/config/rustsec_heap_expansion_harnesses.json
  --catalog evaluation/config/rustsec_heap_neon_node_harnesses.json
)
for batch in a b c d e f; do
  args+=(--catalog "evaluation/config/rustsec_heap_strong_batch_${batch}_harnesses.json")
  args+=(--status "evaluation/config/rustsec_heap_strong_batch_${batch}_status.json")
done

uv run python evaluation/scripts/build_rustsec_security_scope.py \
  "${args[@]}" \
  --primitive-overrides evaluation/config/rustsec_heap_primitive_overrides.json \
  --mechanism-amendments evaluation/config/rustsec_heap_mechanism_amendments.json \
  --mechanism-results evaluation/config/rustsec_heap_mechanism_results.json \
  --output evaluation/config/rustsec_heap_complete_scope.json \
  --require-terminal-integration
```

The terminal gate rejects any pending or source-only efficacy row. A reviewed
candidate may remain outside the efficacy scope only through an explicit
`scope_exclusion` reason plus retained evidence paths.

`terminal-artifact-hashes.json` pins the final catalogs, status ledgers,
mechanism amendments and results, feature-matched campaign artifacts,
generated scope, figure bundle, and overhead summary.
