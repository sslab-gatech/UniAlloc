# RSH-069 Type-Isolation Reuse-Edge Evidence

This bundle evaluates a synthetic reduction of RSH-069 /
RUSTSEC-2025-0022 (`openssl`). The vulnerable wrapper consumes an
`Option<CString>` while deriving the pointer passed to FFI, reclaiming the
allocation before the FFI read. The adapter preserves that consume-free-read
sequence and grooms a semantically distinct equal-layout 4,096-byte
`Replacement` at the stale-pointer edge.

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
- Vulnerable adapter: `evaluation/harnesses/rustsec_heap_expansion/RSH-069/derived_reuse.rs`
  (`59156d8f175c07124388b9dbf4befd91e6a66af79ccd5318137fdef92a7129a5`).
- Patched control: `evaluation/harnesses/rustsec_heap_expansion/RSH-069/derived_reuse_patched.rs`
  (`447cf102407be6753715fa2f7cabce26b82dc8631d099072709e0d44f17c1f01`).

This matrix is part of the 12-case frozen isolated WIP evidence snapshot with
UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current shared-session working tree and current HEAD have separate live
provenance.

RSH-069 is a synthetically groomed, manually annotated root-cause reduction.
Its execution scope is the 4,096-byte CString edge and excludes the complete
OpenSSL FFI path. Automatic compiler victim-site coverage is `0`, validated
complete source-level vulnerability detection is `0`, and `claim_grade`
remains `false`. The positive claim covers the measured 4,096-byte
cross-identity reuse decision.
