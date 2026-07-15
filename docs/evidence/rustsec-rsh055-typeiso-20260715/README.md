# RSH-055 Type-Isolation Reuse-Edge Evidence

This bundle evaluates the manually attributed reuse edge derived from
RSH-055 / RUSTSEC-2020-0145 (`heapless`). In `heapless` 0.6.0,
`IntoIter::clone` can clone a consumed inline slot after `next`; dropping that
element leaves a stale one-word `Vec<u64>` payload pointer. The adapter places
an equal-layout, semantically distinct eight-byte `Replacement` at that edge.

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
- Vulnerable adapter: `evaluation/harnesses/rustsec_heap_expansion/RSH-055/derived_reuse.rs`
  (`93ccd3eaa9e3f0accbbec1944b718b19e5230e1c0dd1bbd398d72fd396160d45`).
- Patched control: `evaluation/harnesses/rustsec_heap_expansion/RSH-055/derived_reuse_patched.rs`
  (`b2d790af6201f5ab517f85b8b37fd587e0c3e9da4aa9c58f5b27aa891926e79e`).

This matrix is part of the 12-case frozen isolated WIP evidence snapshot with
UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current shared-session working tree and current HEAD have separate live
provenance.

This is one manually annotated, collision-gated, layout-bounded allocator
edge. Automatic compiler victim-site coverage is `0`, validated complete
source-level vulnerability detection is `0`, and `claim_grade` remains
`false`. The positive claim covers the measured eight-byte cross-identity
reuse decision.
