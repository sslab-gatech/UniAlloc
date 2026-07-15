# RustSec heap-security expansion evidence (2026-07-14)

This directory freezes the first executable expansion beyond the original
40-advisory pilot. It contains five new advisory IDs, five published or upstream
witness scenarios, and two separately derived cross-type reuse scenarios.
Published witnesses and derived policy probes use separate denominators.

## Reconciled terminal corpus context

The final 49-case efficacy scope contains 31 other-feature-only cases, 11
Type-Isolation-only cases, 1 multiple-mechanism case, 3 matched no-signal cases
(RSH-049, RSH-050, and RSH-075), and 3 inconclusive/unresolved cases (RSH-003,
RSH-006, and RSH-019). The strict positive count is **43/49**. The generated
case report records 32 `detected`, 11 `mitigated`, 3 `no_signal`, and 3
`inconclusive` cases. The mechanism ledger contains 62 rows: 32 `detected`, 12
`mitigated`, 12 `no_signal`, and 6 `inconclusive`. Its 44 positive rows cover
43 unique cases: 31 reclaim checks, 1 recovery-layout validation, and 12 Type
Isolation edges. RSH-002 is the sole overlap, and 13 cases retain multiple
rows.

This historical expansion bundle contributes the RSH-041 and RSH-042 Type
Isolation-positive derived edges. The reconciled corpus adds ten similarly
bounded edges: RSH-002, RSH-008, RSH-052, RSH-055, RSH-064, and RSH-065
through RSH-069. All 12 use manual
victim-to-replacement edge attribution and count as causal true positives only
for the measured cross-identity reuse decision. Each strict row validates the
matching treatment denial and report binding. All 12 positive rows record
`compiler_automatic_victim_coverage=false` and
`source_vulnerability_detection_validated=false`; the full automatic
source-vulnerability Type Isolation true-positive count is **0**.
RSH-065 is a manually modeled four-byte reserve-relocation reduction, and
RSH-069 is a synthetically groomed 4096-byte FFI-read reduction.

The final 12 strict Type Isolation matrices, including this bundle's RSH-041
and RSH-042 edges, bind to one frozen isolated WIP evidence snapshot with
UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current shared-session working tree and current HEAD have separate live
provenance.

## Environment and pins

- RustSec advisory database: commit
  `9f3e138091487e69144f536d36976e427a7a3307`.
- Rudra-PoC: commit `6226dd030fffbed5601099cb0e24f73e4150a7f5`.
- Experiment catalog:
  `evaluation/config/rustsec_heap_expansion_harnesses.json`. The retained
  machine-readable summaries and bundle manifest bind the catalog and input
  bytes used by each recorded experiment.
- Compiler toolchain: `nightly-2026-06-11`, target
  `x86_64-unknown-linux-gnu`.
- Repetitions: three per archive/allocator arm.
- Every result remains `claim_grade=false` and `mitigation_inferred=false`.

## Published-witness results

| Case | Advisory / crate | System ground truth | Patched control | `typed_plain` | `typeiso` | Attribution |
|---|---|---|---|---|---|---|
| RSH-041 | RUSTSEC-2026-0152 / `oneringbuf` | ASan heap UAF 3/3 | clean 3/3 | no allocator signal; all observed allocations fell back | no allocator signal; all observed allocations fell back | direct Type Isolation effect absent; critical-site coverage gap |
| RSH-042 | RUSTSEC-2026-0128 / `emap` | ASan heap UAF 3/3 | clean 3/3 | no allocator signal | no allocator signal | published witness has no measured A-to-B replacement edge |
| RSH-043 | RUSTSEC-2026-0131 / `bitchomp` | ASan double free 3/3 | clean 3/3 | pointer-already-released 3/3 | pointer-already-released 3/3 | common UniAlloc tracked-reclaim signal |
| RSH-044 | RUSTSEC-2021-0018 / `qwutils` | ASan double free 3/3 | expected safe Rust panic 3/3 | pointer-already-released 3/3 | pointer-already-released 3/3 | common UniAlloc tracked-reclaim signal |
| RSH-045 | RUSTSEC-2021-0033 / `stack_dst` | ASan double free 3/3 | expected safe Rust panic 3/3 | pointer-already-released 3/3 | pointer-already-released 3/3 | common UniAlloc tracked-reclaim signal |

The measured published-witness totals are:

- vulnerable baseline reproduced: **5/5 advisory IDs**, 15/15 system runs;
- matched patched oracle reproduced: **5/5 advisory IDs**, 15/15 system runs;
- allocator-boundary signal shared by `typed_plain` and `typeiso`: **3/5**;
- Type-Isolation-specific signal in the published witness: **0/5**;
- mitigation inferred from clean execution: **0/5**.

`published-summary.json` is the machine-readable aggregation. The bundle
manifest binds its bytes, and the ten input matrices remain under
`raw/published/`.

## Derived cross-type reuse results

The two derived scenarios isolate an exact `A -> free -> B` allocator decision
that the corresponding UAF path can make. Both use manual victim identity
attribution and retain matched `typed_plain` controls.

RSH-041 explicitly excludes `oneringbuf` from compiler rewriting because the
bounded experiment assigns the vulnerable `LocalHeapRB` slot its reviewed
manual identity. The harness replacement allocation remains compiler-audited,
so every treatment denial is bound to the measured B-side decision without
overriding the A-side identity contract.

| Scenario | Layout | Vulnerable system / `typed_plain` | Vulnerable `typeiso` | Patched `typeiso` | Bounded result |
|---|---|---|---|---|---|
| RSH-041 derived | 40 bytes, align 8 | exact address reuse 3/3 | reuse 0/3; matching, compiler-bound denial 3/3 | safe address reuse 3/3; denial 0/3 | cross-identity boxed `LocalHeapRB`-to-`Replacement` edge withheld and reported |
| RSH-042 derived | 64 bytes, align 1 | exact address reuse 3/3 | reuse 0/3; matching, compiler-bound denial 3/3 | safe same-identity reuse 3/3; denial 0/3 | cross-identity `Vec<u8>`-buffer-to-`Replacement` edge withheld and reported |

The derived denominator supports **2/2 manually attributed reuse-edge policy
observations**. Automatic compiler victim coverage is **0/2** and validated
source-vulnerability detection is **0/2**. The source stale handle survives;
same-identity reuse against a stale handle and access before reuse remain
outside this result.
The raw runner field `vulnerability_specific_detection_signal` is scoped to
the derived scenario's matched reuse decision. The paper-facing source result
uses `source_vulnerability_detection_validated=false`.

`derived-reuse-summary.json` records the bounded evaluations and raw-artifact
hashes. The complete matrices remain under `raw/derived/`, and the bundle
manifest supplies their current byte lengths and digests.

## Reproduction

A published scenario is run in separate system and typed matrices so the ASan
ground truth and UniAlloc compiler instrumentation keep their intended
configurations:

```bash
python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --catalog evaluation/config/rustsec_heap_expansion_harnesses.json \
  --action run --scenario RSH-041-advisory \
  --variants system --archive-variants vulnerable,patched \
  --output-dir /tmp/rustsec-expansion-rsh041-system \
  --allow-download --execute-unsafe --jobs 4 --repetitions 3

python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --catalog evaluation/config/rustsec_heap_expansion_harnesses.json \
  --action run --scenario RSH-041-advisory \
  --variants typed_plain,typeiso --archive-variants vulnerable,patched \
  --output-dir /tmp/rustsec-expansion-rsh041-typed \
  --allow-download --execute-unsafe --jobs 4 --repetitions 3
```

A derived reuse matrix uses the same runner and explicit unsafe-execution gate:

```bash
python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --catalog evaluation/config/rustsec_heap_expansion_harnesses.json \
  --action run --scenario RSH-041-derived-reuse \
  --variants system,typed_plain,typeiso \
  --archive-variants vulnerable,patched \
  --output-dir /tmp/rustsec-expansion-rsh041-derived \
  --allow-download --execute-unsafe --jobs 4 --repetitions 3
```

The claim boundary is the allocator decision that was directly attributed and
audited. General UAF mitigation requires automatic critical-site coverage,
matched vulnerable/patched controls, and source-level exploit outcomes.
