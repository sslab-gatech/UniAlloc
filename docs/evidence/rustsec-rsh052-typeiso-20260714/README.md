# RSH-052 Type Isolation reuse-edge evidence

This directory records the bounded RSH-052 / RUSTSEC-2019-0023 experiment.
The vulnerable `string-interner` clone retains raw map references into the old
interner. Dropping the old interner reclaims its 64-byte `Box<str>` payload.
The derived witness then requests an equal-layout `Replacement` with a
different semantic identity.

The six-arm, three-repetition matrix validates a causal Type Isolation effect
on that A-to-B reuse edge:

| Arm | Address collisions | Bound reuse denials |
| --- | ---: | ---: |
| vulnerable / system | 3/3 | 0/3 |
| vulnerable / typed_plain | 3/3 | 0/3 |
| vulnerable / typeiso | 0/3 | 3/3 |
| patched / system | 3/3 | 0/3 |
| patched / typed_plain | 3/3 | 0/3 |
| patched / typeiso | 3/3 | 0/3 |

The patched Type Isolation arm uses the same `Box<str>` allocation path and
the same exact identity on both sides of the reclaim. Its 3/3 address
collisions and zero denials establish that ordinary same-identity reuse remains
available. The exporter classifies the derived edge as `mitigated`, with
`true_positive=true`, `causal_mitigation_true_positive`, and zero failed
checks.

The claim covers the manually attributed exploit-enabling cross-identity reuse
edge. The source-level stale pointer remains present, and compiler-automatic
victim coverage remains outside this result.

## Reproduction

```bash
python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --catalog evaluation/config/rustsec_heap_strong_batch_a_harnesses.json \
  --action run \
  --scenario RSH-052-derived-reuse \
  --variants system,typed_plain,typeiso \
  --archive-variants vulnerable,patched \
  --output-dir /tmp/rsh052-derived-final-frozen \
  --cache /tmp/strong-batch-a-cache \
  --repetitions 3 \
  --execute-unsafe \
  --jobs 4 \
  --build-timeout 1800 \
  --run-timeout 120
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
  `evaluation/harnesses/rustsec_heap_expansion/RSH-052/derived_reuse.rs` and
  `evaluation/harnesses/rustsec_heap_expansion/RSH-052/derived_reuse_patched.rs`.

This matrix is part of the 12-case frozen isolated WIP evidence snapshot with
UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current shared-session working tree and current HEAD have separate live
provenance.

The machine-readable records are the provenance authority. Live repository
files can advance after capture, so this README avoids duplicating their byte
counts or hashes. The aggregate evidence manifests bind the copied bundle
artifacts used by the final report.
