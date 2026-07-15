# RSH-031 Recovery-Layout Validation Evidence

This bundle evaluates the RSH-031 / RUSTSEC-2023-0017 (`maligned`)
allocation-layout edge. The pinned upstream witness allocates 1,009 bytes with
alignment 256; the vulnerable `Vec<u8>` Drop submits the same size with
alignment 1. The derived pair isolates UniAlloc's same-pointer recovery-record
validation at that deallocation boundary.

## Result

| Arm | Exact layout diagnostics | Expected recorded/requested layout |
| --- | ---: | --- |
| vulnerable / `typed_plain` | 3/3 | `1009/256 -> 1009/1` |
| vulnerable / `typeiso` | 3/3 | `1009/256 -> 1009/1` |
| patched / `typed_plain` | 0/3 | `1009/256 -> 1009/256` |
| patched / `typeiso` | 0/3 | `1009/256 -> 1009/256` |

Every arm completed all three repetitions. Each vulnerable arm emitted one
`unialloc_recovery_deallocation_layout_mismatch` signal per repetition and
preserved the authoritative recovery record for exact cleanup. Every patched
control remained signal-free. The policy-independent result is classified as
`recovery_layout_validation / detected / exact_diagnostic_true_positive` under
`other_allocator_feature`. This bundle contributes zero TypeIso positives.

The copied upstream experiment supplies source-level ground truth: its system
Miri arm reports `miri_undefined_behavior` in 3/3 vulnerable repetitions and
completes cleanly in 3/3 patched repetitions. The derived matrix supplies the
allocator-mechanism treatment and matched controls. This bounded pairing keeps
`claim_grade=false`; the positive scope is the exact same-pointer
allocation/deallocation layout-mismatch edge on the RSH-031 `Vec` Drop path.

## Reproduction

```bash
python3 evaluation/scripts/run_rsh031_layout_mismatch.py \
  --repetitions 3 \
  --target-dir /tmp/unialloc-rsh031-evidence-target \
  --ground-truth \
    docs/evidence/rustsec-rsh031-layout-20260715/upstream-miri-experiment.json \
  --output \
    docs/evidence/rustsec-rsh031-layout-20260715/experiment.json \
  --mechanism-output \
    docs/evidence/rustsec-rsh031-layout-20260715/mechanism-results.json
```

## Evidence and stable inputs

- `experiment.json` contains the four-arm derived validation matrix and the
  exact allocator implementation digest used for that run.
- `mechanism-results.json` contains the strict mechanism classification.
- `upstream-miri-experiment.json` is the copied ground-truth matrix
  (`0ef1b51e968a67d35f34eaf27e38a21f8a4a8a5606e5879668dfdd30012db8fe`).
- Vulnerable derived witness:
  `evaluation/harnesses/rustsec_heap/RSH-031/derived_layout_vulnerable.rs`
  (`be587469c964a2495246af4396eef461a79802d495c24348e1f86ec4968a0dd8`).
- Patched derived control:
  `evaluation/harnesses/rustsec_heap/RSH-031/derived_layout_patched.rs`
  (`edac5b700fdaa7abfb03bc5aae62eea88733d4b3799934f6623b0c11534a753e`).
