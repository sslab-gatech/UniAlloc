# RSH-064 Type Isolation reuse-edge evidence

This directory records the bounded RSH-064 / RUSTSEC-2022-0028 derived
experiment. The published Neon witness exposes a Rust `Vec<u8>` backing store
to JavaScript and releases the Rust owner while the external view remains.
The derived vulnerable arm retains that ownership sequence, then requests an
equal-layout four-byte `Replacement` with a distinct semantic identity before
using the external view on an address collision.

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

Every arm completed all three repetitions. The vulnerable system and
`typed_plain` arms reused the released address and observed replacement byte
`0x64` through the stale external view. The Type Isolation arm denied that
cross-identity reuse in 3/3 runs, bound every denial to the audited
`Box<witness::Replacement>` allocation, and therefore skipped the stale read.
The patched Type Isolation arm reused the address in 3/3 runs under the same
manual identity and emitted zero denials.

The fail-closed exporter classifies this edge as `mitigated`, with
`true_positive=true`, `result_semantics=causal_mitigation_true_positive`, and
zero failed checks. The claim covers the manually attributed
`Vec<u8>`-backing-store-to-`Replacement` reuse edge. Automatic compiler
coverage of the published Neon `Vec`/external-buffer path remains deferred.
The published Node/V8 witness remains the separate `RSH-064-node-addon`
scenario in the same pinned catalog.

## Reproduction

```bash
python3 evaluation/scripts/run_rsh064_derived_reuse.py \
  --action run \
  --variants system,typed_plain,typeiso \
  --archive-variants vulnerable,patched \
  --repetitions 3 \
  --cache "$HOME/.cache/unialloc/rustsec-heap" \
  --allow-download \
  --output-dir /tmp/unialloc-rsh064-derived-run \
  --execute-unsafe \
  --jobs 4
```

## Evidence and provenance

- `preflight.json` records the pinned inputs, requested variants, and supported
  arm topology for the derived edge.
- `experiment.json` contains the raw six-arm, three-repetition matrix and the
  combined UniAlloc implementation digest used for that run.
- `derived-reuse-summary.json` validates the bounded A-to-B edge and binds its
  experiment artifact.
- `mechanism-results.json` contains the strict mechanism classification.
- The vulnerable and patched adapters remain at
  `evaluation/harnesses/rustsec_heap_expansion/RSH-064/derived_reuse.rs` and
  `evaluation/harnesses/rustsec_heap_expansion/RSH-064/derived_reuse_patched.rs`.
- The source-level Node/V8 experiment remains separate at
  `docs/evidence/rustsec-all-53-live-20260714/rsh064/experiment.json`.

The derived matrix is part of the 12-case frozen isolated WIP evidence snapshot
with UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current shared-session working tree and current HEAD have separate live
provenance. The Node/V8 source experiment retains its own historical digest.

The machine-readable records are the provenance authority. Live repository
files can advance after capture, so this README avoids duplicating their byte
counts or hashes. The aggregate evidence manifests bind the copied bundle
artifacts used by the final report.
