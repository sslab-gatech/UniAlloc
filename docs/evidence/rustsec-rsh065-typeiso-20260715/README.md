# RSH-065 Type-Isolation Reuse-Edge Evidence

This bundle evaluates a manually modeled reduction of RSH-065 /
RUSTSEC-2023-0054 (`mail-internals`). The vulnerable operation computes an
insertion pointer before `Vec::reserve`; relocation can reclaim the old buffer
before a copy through that pointer. The adapter models relocation explicitly
and places an equal-layout foreign `Replacement` after the four-byte victim
buffer is reclaimed.

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
- Vulnerable adapter: `evaluation/harnesses/rustsec_heap_expansion/RSH-065/derived_reuse.rs`
  (`8123e1c70af07184d3896e1220b1de242319f98845a13a2f0c27098a4dcb1a53`).
- Patched control: `evaluation/harnesses/rustsec_heap_expansion/RSH-065/derived_reuse_patched.rs`
  (`2cbc7f945b9961f6d022897ac54191aacfc239c3dea77969f252ba326270070f`).

This matrix is part of the 12-case frozen isolated WIP evidence snapshot with
UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current shared-session working tree and current HEAD have separate live
provenance.

RSH-065 is a synthetic, manually modeled reserve-relocation reduction. Its
execution scope is the four-byte root-cause edge and excludes the complete
vulnerable source path. Automatic compiler victim-site coverage is `0`,
validated complete source-level vulnerability detection is `0`, and
`claim_grade` remains `false`. The positive claim covers the measured
four-byte cross-identity reuse decision.
