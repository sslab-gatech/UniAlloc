# Real-world Rust allocator evaluation (2026-07-14)

Lower wall time, total CPU, peak RSS, faults, and context switches are better. Higher throughput is better. Performance rows exclude the statistics-enabled coverage build.

## Interpretation boundaries

- These are warm-cache, CPU- and NUMA-pinned measurements on a shared host.
- Peak RSS is GNU time `%M`, covering the complete measured process.
- TCMalloc uses an identical Rust System binary plus gperftools `LD_PRELOAD`; mimalloc uses its native Rust `GlobalAlloc` wrapper and a statically linked core.
- The TCMalloc proof verifies that the exact shared library is mapped; exported allocator-symbol validation remains a separate manual check.
- The runner inherits its parent environment; same-host reruns should start from a clean allocator environment.
- Cross-application geometric means are directional summaries; per-application absolute time and RSS remain the primary evidence.

## Cross-application comparison

| Subject | Reference | Campaigns | Wall geomean | Throughput geomean | Total CPU geomean | Peak RSS geomean |
|---|---|---:|---:|---:|---:|---:|
| UniAlloc | mimalloc | 3 | 1.0652x | 0.9388x | 1.0707x | 0.4538x |
| UniAlloc | TCMalloc | 3 | 1.0544x | 0.9484x | 1.0519x | 0.5790x |
| Type Isolation | Typed plain | 3 | 1.0181x | 0.9822x | 1.0212x | 1.0003x |

## Headline

- UniAlloc's cross-workload wall-time geomean is +6.52% versus mimalloc and +5.44% versus TCMalloc.
- UniAlloc's peak-RSS geomean is -54.62% versus mimalloc and -42.10% versus TCMalloc.
- Type Isolation adds +1.81% wall time and +0.03% peak RSS over typed plain across these workloads.

## Same-host reproduction

These commands reuse the audited TCMalloc shared library and pinned toolchain at their recorded absolute paths on this host.

### ripgrep

```sh
python3 evaluation/scripts/realworld_type_isolation_matrix.py --apps ripgrep --variants system,jemalloc,mimalloc,tcmalloc,unialloc,typed_plain,typeiso_perf --full --warmups 1 --repetitions 7 --toolchain nightly-2026-06-11 --ripgrep-path-repetitions 32 --cpu-list 20 --numa-node 0 --jobs 32 --tcmalloc-library /home/hanqing/.local/state/unialloc/runs/g001-minimal-235549b2-20260711T155042Z/evidence/pilot-controller/repairs/tcmalloc-full-runtime-v1-20260711T1717Z-attempt2/lib/libtcmalloc.so.4.6.5 --discard-run-output --raw-dir /home/hanqing/alloc/UniAlloc/evaluation/raw/realworld-rust-allocator-20260714/ripgrep
```

### fd

```sh
python3 evaluation/scripts/realworld_type_isolation_matrix.py --apps fd --variants system,jemalloc,mimalloc,tcmalloc,unialloc,typed_plain,typeiso_perf --full --warmups 1 --repetitions 7 --toolchain nightly-2026-06-11 --fd-path-repetitions 12 --cpu-list 20 --numa-node 0 --jobs 32 --tcmalloc-library /home/hanqing/.local/state/unialloc/runs/g001-minimal-235549b2-20260711T155042Z/evidence/pilot-controller/repairs/tcmalloc-full-runtime-v1-20260711T1717Z-attempt2/lib/libtcmalloc.so.4.6.5 --discard-run-output --raw-dir /home/hanqing/alloc/UniAlloc/evaluation/raw/realworld-rust-allocator-20260714/fd
```

### oxipng

```sh
python3 evaluation/scripts/realworld_type_isolation_matrix.py --apps oxipng --variants system,jemalloc,mimalloc,tcmalloc,unialloc,typed_plain,typeiso_perf --quick --warmups 1 --repetitions 7 --toolchain nightly-2026-06-11 --cpu-list 20 --numa-node 0 --jobs 32 --tcmalloc-library /home/hanqing/.local/state/unialloc/runs/g001-minimal-235549b2-20260711T155042Z/evidence/pilot-controller/repairs/tcmalloc-full-runtime-v1-20260711T1717Z-attempt2/lib/libtcmalloc.so.4.6.5 --discard-run-output --raw-dir /home/hanqing/alloc/UniAlloc/evaluation/raw/realworld-rust-allocator-20260714/oxipng
```

## Profile priority

- The largest raw UniAlloc gap is `fd` versus TCMalloc: +14.94% wall time. UniAlloc records 19978 median minor faults versus 1949.
- The largest incremental Type Isolation gap is `fd`: +6.49% over typed plain. Typed plain is 3.411x raw UniAlloc in that workload, so the base typed compiler/runtime path dominates the policy-only increment.

## ripgrep

- Input evidence: `/home/hanqing/alloc/UniAlloc/evaluation/raw/realworld-rust-allocator-20260714/ripgrep/results.json`
- Input SHA-256: `62e59c2147ffb959c3adffb700e9de95c4263c105fa735647731452558a9a38c`
- Toolchain: `nightly-2026-06-11`
- Mode: `full`; warmups: `1`; repetitions: `7`
- glibc rseq mode: `libc_default`
- Implementation SHA-256: `d4ade5d634e4b7b03ca83611a22b596f5bac0498428c25058b5b9317f9ed29cf`

### Performance and memory

| Allocator | Samples | Wall median | Wall MAD | Throughput | Total CPU | Peak RSS median [range] | Major/minor faults | Involuntary/voluntary switches |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| jemalloc | 7 | 2382.167 ms | 17.845 ms | 3438.886 MiB/s | 2370.000 ms | 6.00 MiB [6.00 MiB to 7.00 MiB] | 0.0/490.0 | 9.0/1.0 |
| mimalloc | 7 | 2439.229 ms | 9.025 ms | 3358.438 MiB/s | 2430.000 ms | 11.54 MiB [11.54 MiB to 11.54 MiB] | 0.0/348.0 | 15.0/1.0 |
| System | 7 | 2407.574 ms | 8.638 ms | 3402.596 MiB/s | 2390.000 ms | 5.00 MiB [5.00 MiB to 5.00 MiB] | 0.0/413.0 | 13.0/1.0 |
| TCMalloc | 7 | 2449.183 ms | 8.860 ms | 3344.789 MiB/s | 2440.000 ms | 11.00 MiB [11.00 MiB to 11.00 MiB] | 0.0/1572.0 | 15.0/1.0 |
| Typed plain | 7 | 2693.908 ms | 10.372 ms | 3040.935 MiB/s | 2680.000 ms | 6.00 MiB [6.00 MiB to 6.00 MiB] | 0.0/740.0 | 12.0/1.0 |
| Type Isolation | 7 | 2699.314 ms | 17.760 ms | 3034.845 MiB/s | 2680.000 ms | 6.00 MiB [6.00 MiB to 6.00 MiB] | 0.0/741.0 | 18.0/1.0 |
| UniAlloc | 7 | 2444.788 ms | 8.876 ms | 3350.801 MiB/s | 2430.000 ms | 6.00 MiB [6.00 MiB to 6.00 MiB] | 0.0/460.0 | 15.0/1.0 |

### Direct comparisons

| Subject | Reference | Wall delta | Throughput delta | Total CPU ratio | Peak RSS delta | Ratio method |
|---|---|---:|---:|---:|---:|---|
| UniAlloc | mimalloc | +0.09% | -0.09% | 1.0000x | -48.00% | wall: paired median; throughput: inverse of paired wall median; CPU: ratio of medians; RSS: paired median |
| UniAlloc | TCMalloc | +0.04% | -0.04% | 0.9959x | -45.45% | wall: paired median; throughput: inverse of paired wall median; CPU: ratio of medians; RSS: paired median |
| Type Isolation | Typed plain | -0.46% | +0.46% | 1.0000x | +0.00% | wall: paired median; throughput: inverse of paired wall median; CPU: ratio of medians; RSS: paired median |

### Workload

```json
{
  "description": "single-thread regex search over repeated deterministic paths",
  "path_repetitions": 32,
  "work_amount": 8589934592,
  "work_unit": "bytes"
}
```

### Provenance and controls

- Host: `AMD EPYC 9354 32-Core Processor`; kernel `6.8.0-111-generic`; initial load average `{'fifteen_minutes': 6.23828125, 'five_minutes': 6.83251953125, 'one_minute': 5.7900390625}`.
- Affinity: physical CPU `20`; NUMA node `0`.
- TCMalloc: `gperftools-2.18.1-full-preload` via `system-api-ld-preload`; library SHA-256 `3e9994675a51a1a7f893e02236b36e3150a37b5e8ea740e0cc78f6650f7b1841`.
- mimalloc: wrapper `0.1.25`; sys crate `0.1.49`; core generation `v3` version number `30302`; secure mode `False`.
- Workload input SHA-256: `4a059105d877d838d7166cfd2d5fd7780487fe9020599bef8bf384b78ba3ca9b`.
- Output-equivalence SHA-256: `d9bd5723d23e35e03e773f87a8be1f77715118df0bbe65a0e36d48d2eeea0f47`.
- Full build records, loader proofs, commands, summaries, and every measurement row are preserved in the companion JSON evidence.

## fd

- Input evidence: `/home/hanqing/alloc/UniAlloc/evaluation/raw/realworld-rust-allocator-20260714/fd/results.json`
- Input SHA-256: `4fb23c3698fa43fcdc03c05528a50f6a9fe850b06863d13410a9439e856af04d`
- Toolchain: `nightly-2026-06-11`
- Mode: `full`; warmups: `1`; repetitions: `7`
- glibc rseq mode: `libc_default`
- Implementation SHA-256: `d4ade5d634e4b7b03ca83611a22b596f5bac0498428c25058b5b9317f9ed29cf`

### Performance and memory

| Allocator | Samples | Wall median | Wall MAD | Throughput | Total CPU | Peak RSS median [range] | Major/minor faults | Involuntary/voluntary switches |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| jemalloc | 7 | 1555.132 ms | 5.989 ms | 1580316.301 files/s | 1500.000 ms | 6.00 MiB [6.00 MiB to 6.00 MiB] | 0.0/892.0 | 9608.0/3.0 |
| mimalloc | 7 | 1479.592 ms | 15.860 ms | 1660998.646 files/s | 1400.000 ms | 20.43 MiB [20.42 MiB to 20.43 MiB] | 0.0/306.0 | 9608.0/3.0 |
| System | 7 | 1514.859 ms | 15.617 ms | 1622329.285 files/s | 1460.000 ms | 5.00 MiB [5.00 MiB to 5.00 MiB] | 0.0/874.0 | 9607.0/3.0 |
| TCMalloc | 7 | 1471.971 ms | 10.432 ms | 1669597.796 files/s | 1410.000 ms | 13.00 MiB [12.00 MiB to 13.00 MiB] | 0.0/1949.0 | 9608.0/3.0 |
| Typed plain | 7 | 5745.994 ms | 34.973 ms | 427706.711 files/s | 5700.000 ms | 6.00 MiB [6.00 MiB to 6.00 MiB] | 0.0/22644.0 | 9632.0/3.0 |
| Type Isolation | 7 | 6118.931 ms | 39.650 ms | 401638.782 files/s | 6070.000 ms | 6.00 MiB [6.00 MiB to 7.00 MiB] | 0.0/22235.0 | 9637.0/3.0 |
| UniAlloc | 7 | 1684.633 ms | 8.119 ms | 1458833.891 files/s | 1610.000 ms | 6.00 MiB [6.00 MiB to 6.00 MiB] | 0.0/19978.0 | 9609.0/3.0 |

### Direct comparisons

| Subject | Reference | Wall delta | Throughput delta | Total CPU ratio | Peak RSS delta | Ratio method |
|---|---|---:|---:|---:|---:|---|
| UniAlloc | mimalloc | +13.31% | -11.75% | 1.1500x | -70.63% | wall: paired median; throughput: inverse of paired wall median; CPU: ratio of medians; RSS: paired median |
| UniAlloc | TCMalloc | +14.94% | -13.00% | 1.1418x | -53.85% | wall: paired median; throughput: inverse of paired wall median; CPU: ratio of medians; RSS: paired median |
| Type Isolation | Typed plain | +6.49% | -6.09% | 1.0649x | +0.00% | wall: paired median; throughput: inverse of paired wall median; CPU: ratio of medians; RSS: paired median |

### Workload

```json
{
  "description": "single-thread metadata traversal over repeated deterministic paths",
  "path_repetitions": 12,
  "work_amount": 2457600,
  "work_unit": "files"
}
```

### Provenance and controls

- Host: `AMD EPYC 9354 32-Core Processor`; kernel `6.8.0-111-generic`; initial load average `{'fifteen_minutes': 6.43408203125, 'five_minutes': 7.09619140625, 'one_minute': 8.89697265625}`.
- Affinity: physical CPU `20`; NUMA node `0`.
- TCMalloc: `gperftools-2.18.1-full-preload` via `system-api-ld-preload`; library SHA-256 `3e9994675a51a1a7f893e02236b36e3150a37b5e8ea740e0cc78f6650f7b1841`.
- mimalloc: wrapper `0.1.25`; sys crate `0.1.49`; core generation `v3` version number `30302`; secure mode `False`.
- Workload input SHA-256: `835f96aaa48205f8c6decd7683c594f8fbabc1d00bb0a673cf3adb03fd125c8d`.
- Output-equivalence SHA-256: `c8e3f5f5753d414b6d409d296860f77898980f3ca80d482241fbdbd73df25d03`.
- Full build records, loader proofs, commands, summaries, and every measurement row are preserved in the companion JSON evidence.

## oxipng

- Input evidence: `/home/hanqing/alloc/UniAlloc/evaluation/raw/realworld-rust-allocator-20260714/oxipng/results.json`
- Input SHA-256: `7835b3f576906ecab96476816a321eca9e641d2ebedffe704c09fef72a88978d`
- Toolchain: `nightly-2026-06-11`
- Mode: `quick`; warmups: `1`; repetitions: `7`
- glibc rseq mode: `libc_default`
- Implementation SHA-256: `d4ade5d634e4b7b03ca83611a22b596f5bac0498428c25058b5b9317f9ed29cf`

### Performance and memory

| Allocator | Samples | Wall median | Wall MAD | Throughput | Total CPU | Peak RSS median [range] | Major/minor faults | Involuntary/voluntary switches |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| jemalloc | 7 | 1730.807 ms | 1.331 ms | 0.911 MiB/s | 1720.000 ms | 47.95 MiB [47.95 MiB to 48.95 MiB] | 0.0/23250.0 | 99.0/10.0 |
| mimalloc | 7 | 1639.835 ms | 0.344 ms | 0.962 MiB/s | 1630.000 ms | 70.60 MiB [70.60 MiB to 70.61 MiB] | 0.0/1326.0 | 85.0/10.0 |
| System | 7 | 1715.072 ms | 0.700 ms | 0.919 MiB/s | 1700.000 ms | 45.47 MiB [45.31 MiB to 45.47 MiB] | 0.0/14926.0 | 91.0/9.0 |
| TCMalloc | 7 | 1714.623 ms | 0.952 ms | 0.920 MiB/s | 1700.000 ms | 56.00 MiB [56.00 MiB to 56.00 MiB] | 0.0/13467.0 | 90.0/10.0 |
| Typed plain | 7 | 1767.980 ms | 1.390 ms | 0.892 MiB/s | 1750.000 ms | 43.45 MiB [43.42 MiB to 43.46 MiB] | 0.0/37502.0 | 101.0/10.0 |
| Type Isolation | 7 | 1760.384 ms | 0.787 ms | 0.896 MiB/s | 1750.000 ms | 43.49 MiB [43.49 MiB to 43.51 MiB] | 0.0/37601.0 | 102.0/10.0 |
| UniAlloc | 7 | 1748.057 ms | 0.848 ms | 0.902 MiB/s | 1740.000 ms | 43.19 MiB [43.18 MiB to 43.19 MiB] | 0.0/37377.0 | 99.0/10.0 |

### Direct comparisons

| Subject | Reference | Wall delta | Throughput delta | Total CPU ratio | Peak RSS delta | Ratio method |
|---|---|---:|---:|---:|---:|---|
| UniAlloc | mimalloc | +6.58% | -6.18% | 1.0675x | -38.83% | wall: paired median; throughput: inverse of paired wall median; CPU: ratio of medians; RSS: paired median |
| UniAlloc | TCMalloc | +1.95% | -1.91% | 1.0235x | -22.88% | wall: paired median; throughput: inverse of paired wall median; CPU: ratio of medians; RSS: paired median |
| Type Isolation | Typed plain | -0.44% | +0.44% | 1.0000x | +0.10% | wall: paired median; throughput: inverse of paired wall median; CPU: ratio of medians; RSS: paired median |

### Workload

```json
{
  "description": "single-thread lossless PNG optimization",
  "input": "/home/hanqing/alloc/UniAlloc/evaluation/external/_checkouts/realworld-oxipng/tests/files/issue-141.png",
  "input_sha256": "909418f09bfb42612575aa3d12c7d7b57546d361b382a9ebd8d3c83531948334",
  "work_amount": 1653611,
  "work_unit": "bytes"
}
```

### Provenance and controls

- Host: `AMD EPYC 9354 32-Core Processor`; kernel `6.8.0-111-generic`; initial load average `{'fifteen_minutes': 5.29248046875, 'five_minutes': 4.0947265625, 'one_minute': 2.4453125}`.
- Affinity: physical CPU `20`; NUMA node `0`.
- TCMalloc: `gperftools-2.18.1-full-preload` via `system-api-ld-preload`; library SHA-256 `3e9994675a51a1a7f893e02236b36e3150a37b5e8ea740e0cc78f6650f7b1841`.
- mimalloc: wrapper `0.1.25`; sys crate `0.1.49`; core generation `v3` version number `30302`; secure mode `False`.
- Workload input SHA-256: `909418f09bfb42612575aa3d12c7d7b57546d361b382a9ebd8d3c83531948334`.
- Output-equivalence SHA-256: `581b84d2185c6c10f9f3eec2cdbc75671136910428c630d347c43e7ec41f6743`.
- Full build records, loader proofs, commands, summaries, and every measurement row are preserved in the companion JSON evidence.
