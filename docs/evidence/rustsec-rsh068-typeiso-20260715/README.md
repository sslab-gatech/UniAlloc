# RSH-068 Type-Isolation Reuse-Edge Evidence

This bundle evaluates the manually attributed reuse edge derived from
RSH-068 / RUSTSEC-2025-0016 (`pared`). The vulnerable `Parc::project` can
retain a projection into an external `String`; dropping that string leaves the
projection stale. The adapter requests a semantically distinct equal-layout
64-byte `Replacement` before the collision-gated stale read.

## Strict result

| Arm | Address reuse | Matching TypeIso denial |
| --- | ---: | ---: |
| vulnerable / system | 3/3 | 0/3 |
| vulnerable / `typed_plain` | 3/3 | 0/3 |
| vulnerable / `typeiso` | 0/3 | 3/3 |
| patched / system | 3/3 | 0/3 |
| patched / `typed_plain` | 3/3 | 0/3 |
| patched / `typeiso` | 3/3 | 0/3 |

The strict exporter classifies this as
`type_isolation / mitigated / causal_mitigation_true_positive`. The vulnerable
system and plain ablation arms collide in every repetition; TypeIso denies and
reports each cross-identity collision; the patched same-identity control
retains ordinary reuse without a denial.

## Evidence and boundary

- `preflight.json` records the pinned inputs and supported arm topology.
- `experiment.json` contains the raw six-arm, three-repetition matrix.
- `derived-reuse-summary.json` validates the bounded A-to-B edge.
- `mechanism-results.json` contains the strict mechanism classification.
- Vulnerable adapter: `evaluation/harnesses/rustsec_heap_expansion/RSH-068/derived_reuse.rs`
  (`fb05c176c600793f276abb0fc7cd662f078fef23690e4c49db250750826ce5bc`).
- Patched control: `evaluation/harnesses/rustsec_heap_expansion/RSH-068/derived_reuse_patched.rs`
  (`23e1e9b092c528545f996529fa0c3c8de80ac277ef43dfd78130bb1c4865fc42`).

This matrix is part of the 12-case frozen isolated WIP evidence snapshot with
UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current shared-session working tree and current HEAD have separate live
provenance.

This is one manually annotated, collision-gated, layout-bounded allocator
edge. Automatic compiler victim-site coverage is `0`, validated complete
source-level vulnerability detection is `0`, and `claim_grade` remains
`false`. The positive claim covers denial of the measured 64-byte
cross-identity reuse decision.
