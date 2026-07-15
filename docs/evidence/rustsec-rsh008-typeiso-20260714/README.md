# RSH-008 Type Isolation reuse-edge evidence

This directory records the bounded RSH-008 / RUSTSEC-2021-0130 experiment for
`lru` 0.7.0. The vulnerable iterator can retain a `String` reference after the
cache entry that owns it is reclaimed. The derived witness isolates one
exploit-enabling allocator decision: reuse of the reclaimed 48-byte, align-8
`LruEntry<u32, String>` slot for an equal-layout `Replacement` with a different
semantic identity.

The recorded run executed three allocator variants across vulnerable and
patched archives for three repetitions. The strict Type Isolation exporter
uses the complete six-arm causal matrix:

| Arm | Clean executions | Address collisions | Bound reuse denials |
| --- | ---: | ---: | ---: |
| vulnerable / system | 3/3 | 3/3 | 0/3 |
| vulnerable / `typed_plain` | 3/3 | 3/3 | 0/3 |
| vulnerable / `typeiso` | 3/3 | 0/3 | 3/3 |
| patched / system | 3/3 | 3/3 | 0/3 |
| patched / `typed_plain` | 3/3 | 3/3 | 0/3 |
| patched / `typeiso` | 3/3 | 3/3 | 0/3 |

The Type Isolation treatment withheld the cross-identity reuse and emitted a
matching denial in every vulnerable repetition. Each denial is bound to the
unique compiler-audited `Box<Replacement>` allocation site. The patched
control performs safe same-identity grooming, reuses the address in every arm,
and emits no denial. The strict exporter therefore records `outcome=mitigated`,
`true_positive=true`, `causal_mitigation_true_positive`, zero failed checks,
and zero evidence gaps.

## Claim boundary

This true positive covers the manually attributed, layout-bounded
`LruEntry-to-Replacement` cross-identity reuse edge. The experiment supplies
the victim identity through exact manual victim identity scopes around the
`LruCache::put` allocation and `LruCache::pop` reclaim. Compiler targeting is
restricted to `rsh_008_harness`, which preserves those scopes while allowing
the compiler pass to identify the distinct replacement allocation.

Compiler-automatic victim coverage is `false`. The source-vulnerability detection
result is `false`. The result establishes allocator enforcement at the measured
reuse edge. It leaves the stale-reference source defect, access before reuse,
same-identity reuse, and general UAF detection outside the claim.

## Reproduction

```bash
CARGO_HOME="$HOME/.cache/unialloc/rustsec-heap/cargo" \
python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --action run \
  --scenario RSH-008-derived-reuse \
  --variants system,typed_plain,typeiso \
  --archive-variants vulnerable,patched \
  --repetitions 3 \
  --cache "$HOME/.cache/unialloc/rustsec-heap" \
  --allow-download \
  --output-dir evaluation/raw/rsh008-derived-routing-20260714 \
  --execute-unsafe \
  --jobs 4 \
  --build-timeout 1200 \
  --run-timeout 600
```

## Evidence and provenance

- `preflight.json` records the pinned inputs, requested variants, and supported
  arm topology.
- `experiment.json` contains the raw six-arm, three-repetition matrix and the
  combined UniAlloc implementation digest used for that run.
- `derived-reuse-summary.json` validates the bounded A-to-B edge and binds its
  experiment artifact.
- `mechanism-results.json` contains the strict mechanism classification.
- The vulnerable and patched adapters remain at
  `evaluation/harnesses/rustsec_heap/RSH-008/derived_reuse.rs` and
  `evaluation/harnesses/rustsec_heap/RSH-008/derived_reuse_patched.rs`.

This matrix is part of the 12-case frozen isolated WIP evidence snapshot with
UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current shared-session working tree and current HEAD have separate live
provenance.

The machine-readable records are the provenance authority. Live repository
files can advance after capture, so this README avoids duplicating their byte
counts or hashes. The aggregate evidence manifests bind the copied bundle
artifacts used by the final report.
