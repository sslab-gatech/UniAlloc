# RSH-066 Type-Isolation Reuse-Edge Evidence

This bundle evaluates the manually attributed reuse edge derived from
RSH-066 / RUSTSEC-2024-0007 (`rust-i18n-support`). The vulnerable `AtomicStr`
returns an unguarded `&str`; replacing the value and dropping the returned old
`Arc<String>` reclaims the backing string while that reference remains stale.
The adapter requests a distinct equal-layout 4,096-byte `Replacement` before
the collision-gated stale read.

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
- Vulnerable adapter: `evaluation/harnesses/rustsec_heap_expansion/RSH-066/derived_reuse.rs`
  (`5d8a5b15070bf275c16acb3a03badb43b773fb9ae9a1a08bfe5cb03d26888115`).
- Patched control: `evaluation/harnesses/rustsec_heap_expansion/RSH-066/derived_reuse_patched.rs`
  (`aaab7486d537889e1df49db364ff0d0f9a358339fcb1ca7cbb50d77d77f74151`).

This matrix is part of the 12-case frozen isolated WIP evidence snapshot with
UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current shared-session working tree and current HEAD have separate live
provenance.

This is one manually annotated, collision-gated, layout-bounded allocator
edge. Automatic compiler victim-site coverage is `0`, validated complete
source-level vulnerability detection is `0`, and `claim_grade` remains
`false`. The positive claim covers denial of the measured 4,096-byte
cross-identity reuse decision.
