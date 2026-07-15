# RSH-002 Type-Isolation Reuse-Edge Evidence

This bundle evaluates the manually attributed reuse edge derived from
RSH-002 / RUSTSEC-2020-0007 (`bitvec`). The vulnerable adapter preserves the
BitVec-to-BitBox ownership failure and places an equal-layout, semantically
distinct `Replacement` immediately after the 1,016-byte victim reclaim.

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
`type_isolation / mitigated / causal_mitigation_true_positive`: the system and
plain ablation arms reproduce the cross-identity collision, the TypeIso arm
denies and reports that collision in every vulnerable repetition, and the
same-identity patched control remains reusable without a denial.

## Evidence and boundary

- `preflight.json` records the pinned inputs and supported arm topology.
- `experiment.json` contains the raw six-arm, three-repetition matrix.
- `derived-reuse-summary.json` validates the bounded A-to-B edge.
- `mechanism-results.json` contains the strict mechanism classification.
- Vulnerable adapter: `evaluation/harnesses/rustsec_heap/RSH-002/derived_reuse.rs`
  (`d6ebecf4f322b65a5405f873d4cddfe160fb361c61f46940ba7e22808cd4f324`).
- Patched control: `evaluation/harnesses/rustsec_heap/RSH-002/derived_reuse_patched.rs`
  (`ce184d4b223762d935f6f3bf71800290206a3bd5d320be5e41b983059a687055`).

This matrix is part of the 12-case frozen isolated WIP evidence snapshot with
UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current shared-session working tree and current HEAD have separate live
provenance.

This is one manually annotated, layout-bounded allocator edge. Automatic
compiler coverage of the vulnerable victim sites is `0`, validated complete
source-level vulnerability detection is `0`, and `claim_grade` remains
`false`. The positive claim covers denial of the measured exploit-enabling
cross-identity reuse decision.
