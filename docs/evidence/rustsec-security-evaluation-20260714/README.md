# RustSec heap-security evaluation evidence, 2026-07-14

> Historical frozen-pilot evidence. The terminal efficacy partition and claim
> boundary are in `../rustsec-security-complete-20260714/README.md` and
> `../rustsec-all-53-live-20260714/evaluable-49-report.json`.

## Result and claim boundary

The final sweep executed **all 27 repository-materializable scenarios** in the
pinned RustSec heap corpus. Each scenario planned vulnerable and patched
archives under system, raw UniAlloc, `typed_plain`, and `typeiso` arms. The
tracked corpus also contains 18 source-pinned Rudra-PoC programs that do not yet
have repository materializers, patched controls, or matched allocator arms;
they remain source-only inventory outside the executed denominator.

The final orchestration result is:

| Metric | Result |
|---|---:|
| Repository scenarios | 27 / 27 successful |
| Matrix arms | 216 / 216 terminal |
| Supported arms | 208 |
| Planned topology exclusions | 8 |
| Expected compile-rejection arms | 32 |
| Runtime witness executions | 352 / 352 |
| Unexpected arms | 0 |
| Input drift during the sweep | 0 |
| Vulnerable system oracles reproduced | 27 / 27 |
| Patched system controls reproduced | 27 / 27 |

Every artifact remains `claim_grade=false`. Every arm records
`mitigation_inferred=false`. The sweep establishes source integrity, oracle
reproduction, topology coverage, and native allocator diagnostics. It carries
zero mitigation, detection-coverage, or exploit-prevention claim.

For the 52 supported Type Isolation arms:

- 52 completed;
- 0 have validated critical-site coverage;
- 0 are efficacy-eligible;
- compiler audit routes are 35 semantic, 8 direct-only, and 9 with no observed
  rewrite; the two RSH-030 Type Isolation arms are topology-excluded.

A clean allocator run remains inconclusive. A native signal remains diagnostic
only. Type Isolation performs no ordinary load/store bounds check, so the
spatial cases provide expected-no-direct-effect controls rather than heap
buffer-overflow mitigation evidence.

### Annotated RSH-002 reuse-edge follow-up

`rsh002-annotated-reuse-edge.json` records a subsequent six-arm,
two-repetition experiment. It supplies an explicitly manual exact identity for
the BitVec victim allocation and reclaim while retaining the compiler-derived
identity for `Box<Replacement>`. The vulnerable system and `typed_plain` arms
reused the victim address in 2/2 repetitions. The `typeiso` arm reused it in
0/2 repetitions and emitted a matching wrong-identity denial in 2/2; each event
bound to the unique compiler audit row at `src/witness.rs:50:23-50:74`.

The patched `typeiso` control also emitted the event. The supported result is a
bounded cross-identity reuse-edge enforcement claim under manual victim
attribution. Vulnerability-specific detection and automatic critical-site
coverage remain pending.

## Evidence hierarchy

The retained evidence is ordered from compact tracked records to ignored raw
artifacts:

1. `source-audit-summary.json` records the pinned-source audit and full
   preflight digests.
2. `experiment-summary.json` records every scenario/result digest, compact arm
   classifications, aggregate counts, and claim blockers.
3. `scenario-results.md` indexes the vulnerable allocator observation and Type
   Isolation rewrite boundary for every executed scenario.
4. `rsh002-annotated-reuse-edge.json` records the later manual-attribution
   reuse-edge experiment and binds its ignored raw matrix by SHA-256.
5. `evaluation/raw/rustsec-heap-full-sweep-20260714/sweep-summary.json` is the
   canonical full-run orchestration artifact.
6. Each scenario directory below that raw root contains `preflight.json`,
   `experiment.json`, per-arm results, commands, environment records, compiler
   audits, binaries, and repetition logs.
7. The earlier RSH-002, RSH-029, and RSH-030 smoke runs are calibration history;
   they serve only as calibration history.

Raw artifacts are ignored by Git because the complete tree contains historical
build outputs. The tracked summaries bind the raw manifests by path and SHA-256.

## Input and artifact identities

| Item | SHA-256 |
|---|---|
| Full-sweep harness catalog | `fc83229a1fe21901bb416aecc4dc6d433465301597b17c564dc2804bbf937c26` |
| Annotated-follow-up harness catalog | `58a5896f20eda7b4f113aafd1ca1083b9d969c9805efb116c634ea9e9cac592d` |
| Annotated RSH-002 follow-up summary | `ea21df35d28bc2df614776f68f447d367556d4663e0bfd5ad0d701f36ce27727` |
| Security corpus | `1441faa0e96188da7d18b2c4e2b95a930c30d492eed07dfa6e42115a16f7bce9` |
| Corpus audit runner | `e6373bbee15e620adb082df49903d02c44dab920ce9f9a484929da21b0fbf746` |
| Contained harness runner | `ea1de13aa79777a5d273b9e314850ae966d0ec036b9a99fe33d0c7232fe425a7` |
| Experiment runner | `13fcce760f3e49bc901527c6eddab199548a4a753d2b82f49a680477d7db36e2` |
| Sweep runner | `4d73135c76a18f12741ff33d949672b42207455d08e552fc32c821958b8387b4` |
| UniAlloc implementation digest | `159806869e12b58903f0f941b016ff60fed6e53a842cca35b5434f54a3d9da09` |
| Final preflight summary | `b2a82f65e7e82811af26de8d2351105bd6e7a4788bf89fd296208e97722d9e45` |
| Final launch metadata | `1acc9f2a47244f9bbef7d4ab8f0a566249b8e68259d18177e07269001c2639c5` |
| Final source audit | `3ebe211a8a61054d076c7ac5f93116e159a761f8d75f37ec5b48e95e996ef690` |
| Final sweep summary file | `2e38d8ad34f6de8bdedd4e099ae29fa34eba0f15e08f7d1652bb2398d73d11e9` |
| Tracked experiment summary | `4d17597cdcf6a75909cf54a4cc24b50ceaf9faf396b8186ace07387951dd1d01` |
| Tracked source-audit summary | `0866395a60d89022b722e95734ce32aaa8cde0c5b6de679318d09ad23fa34421` |
| Tracked per-scenario table | `d1bf5abdf7152d433bdecf30a94525dff8edcb93221f40d23cbff85b12963034` |

The sweep summary's internal canonical digest is
`0128bc41805078e1a264d23b2258b5d623cb60d2d24d488280f8b0447e1cb3c0`.
Start and end hashes match for the catalog, experiment runner, sweep runner, and
UniAlloc implementation.

## Planned exclusions and oracle-specific controls

The eight unsupported arms are part of the preregistered matrix:

- RSH-028: vulnerable and patched raw-`unialloc` arms are excluded because the
  pinned nix harness requires exact `libc=0.2.99` while direct UniAlloc resolves
  exact `libc=0.2.183`. The force-loaded `typed_plain` and `typeiso` arms remain
  supported and completed.
- RSH-030: the subject intentionally owns MiMalloc through
  `#[global_allocator]`. Raw UniAlloc, `typed_plain`, and `typeiso` substitutions
  are excluded for both archives; the two system arms preserve MiMalloc and
  reproduce the alignment oracle and patched control.

RSH-031 uses `nightly-2022-07-01` Miri for its two system oracle arms because
the current Miri toolchain does not report the historical deallocation-layout
mismatch. The runner removes two current-only lint allowances for that pinned
historical compiler and preserves Rust's default allocator. It records the
exact flag policy and allocator topology. The vulnerable control reports the
1009-byte, alignment-256 allocation deallocated with alignment 1; the patched
control exits cleanly. Allocator diagnostic arms remain on
`nightly-2026-06-11`.

RSH-032 uses `-Copt-level=0 -Coverflow-checks=off -Zub-checks=no` before adding
the ASan flags. This prevents the current optimizer from eliding the witness.
The vulnerable system arm reports the cataloged high-address ASan deadly fault
in `transpose_small`; the patched system arm reaches its safe panic control.
Native allocator exits remain diagnostic and inconclusive.

## Reproduction

The final preflight command was:

```bash
CARGO_HOME="$HOME/.cache/unialloc/rustsec-heap/cargo" \
uv run python evaluation/scripts/run_rustsec_heap_sweep.py \
  --action preflight \
  --cache "$HOME/.cache/unialloc/rustsec-heap" \
  --output-dir evaluation/raw/rustsec-heap-full-preflight-final-20260714 \
  --scenario-jobs 1 \
  --jobs 4 \
  --repetitions 2 \
  --build-timeout 1200 \
  --run-timeout 600
```

The final run command was:

```bash
CARGO_HOME="$HOME/.cache/unialloc/rustsec-heap/cargo" \
uv run python evaluation/scripts/run_rustsec_heap_sweep.py \
  --action run \
  --cache "$HOME/.cache/unialloc/rustsec-heap" \
  --output-dir evaluation/raw/rustsec-heap-full-sweep-20260714 \
  --scenario-jobs 1 \
  --jobs 4 \
  --repetitions 2 \
  --build-timeout 1200 \
  --run-timeout 600 \
  --execute-unsafe
```

`scenario-jobs=1` keeps scenario provenance sequential and avoids shared-cache
competition. The commands execute historical crate build logic and unsafe
witnesses on the host. A disposable VM remains the stronger containment
boundary for a claim-grade rerun.

## Remaining research work

- Materialize the 18 source-pinned Rudra-PoC-only programs with vulnerable and
  patched controls before adding them to the matched allocator denominator.
- Validate the exact allocation, reclaim, and replacement sites for every
  Type Isolation arm; structural target-crate audit presence is insufficient.
- Add claim-grade origin-content hashes for the mechanical adapters that
  currently rely on human-reviewed origin attribution.
- Rerun in a disposable VM with preregistered repetition counts before making
  statistical or efficacy claims.
