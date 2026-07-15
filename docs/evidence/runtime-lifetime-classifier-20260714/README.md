# Runtime Lifetime Classifier Evidence (2026-07-14)

This directory contains the final feature-parity quick evaluation for the
marker-free runtime lifetime classifier.

## Artifacts

| File | Purpose |
|---|---|
| `coverage-quick.json` | One telemetry-enabled run per application |
| `performance-off.json` | Three warmups and 15 measured runs with the policy disabled |
| `performance-adaptive.json` | Three warmups and 15 measured runs with the adaptive policy enabled |
| `summary.json` | Validated performance and classification aggregation |
| `thp-probe.json` | Host-gated learned-Long and realized-THP mechanism probe |

| File | SHA-256 |
|---|---|
| `coverage-quick.json` | `f60ea7d690f5d6ffe885f211d017fa233d67830b3ab964a5041f75e3bf49171b` |
| `performance-off.json` | `7349b7942c350cd020e992f63dc91c9e90207bfd595bfef43485b9bfc4c0b0cb` |
| `performance-adaptive.json` | `633658cc9ab213479bd5f61cd6d392d9242edfdcc4821ee0857f214b98ba35c8` |
| `summary.json` | `d24bca55a3b9bacc9d60aabc6637704b5675f70f959b06f21377109aa2329de6` |
| `thp-probe.json` | `55fe1e0fa26ae7719422336fb8dd395d627080510a381f6aa2c615e3679c32d9` |

The performance binaries in both modes use the exact feature set
`type_isolation,lifetime_hugepage`. The runtime initializer is the only policy
difference. Output hashes match across modes for every application.

## Reproduction

The original raw directories were outside the repository under
`/home/hanqing/runtime-lifetime-eval-tmp`. Equivalent commands from the
repository root are:

```sh
uv run python evaluation/scripts/runtime_lifetime_classifier_realapp.py \
  --runtime-lifetime-mode adaptive \
  --apps ripgrep,fd,oxipng \
  --variants typeiso_coverage \
  --quick --warmups 0 --repetitions 1 \
  --cpu-list 20 --numa-node 0 --jobs 32 \
  --checkout-root /home/hanqing/alloc/UniAlloc/evaluation/external/_checkouts \
  --raw-dir /home/hanqing/runtime-lifetime-eval-tmp/coverage-coldbackoff-final

uv run python evaluation/scripts/runtime_lifetime_classifier_realapp.py \
  --runtime-lifetime-mode off \
  --apps ripgrep,fd,oxipng \
  --variants typeiso_perf \
  --quick --warmups 3 --repetitions 15 \
  --cpu-list 20 --numa-node 0 --jobs 32 \
  --checkout-root /home/hanqing/alloc/UniAlloc/evaluation/external/_checkouts \
  --raw-dir /home/hanqing/runtime-lifetime-eval-tmp/perf-off-coldbackoff-final

uv run python evaluation/scripts/runtime_lifetime_classifier_realapp.py \
  --runtime-lifetime-mode adaptive \
  --apps ripgrep,fd,oxipng \
  --variants typeiso_perf \
  --quick --warmups 3 --repetitions 15 \
  --cpu-list 20 --numa-node 0 --jobs 32 \
  --checkout-root /home/hanqing/alloc/UniAlloc/evaluation/external/_checkouts \
  --raw-dir /home/hanqing/runtime-lifetime-eval-tmp/perf-adaptive-coldbackoff-final

cargo +nightly-2026-06-11 run --release -p unialloc \
  --example runtime_lifetime_classifier_thp_probe \
  --features lifetime_hugepage \
  > docs/evidence/runtime-lifetime-classifier-20260714/thp-probe.json

uv run python evaluation/scripts/summarize_runtime_lifetime_classifier.py \
  --performance-off \
    docs/evidence/runtime-lifetime-classifier-20260714/performance-off.json \
  --performance-adaptive \
    docs/evidence/runtime-lifetime-classifier-20260714/performance-adaptive.json \
  --coverage \
    docs/evidence/runtime-lifetime-classifier-20260714/coverage-quick.json \
  --output \
    docs/evidence/runtime-lifetime-classifier-20260714/summary.json

uv run python evaluation/scripts/plot_runtime_lifetime_classifier.py \
  --summary \
    docs/evidence/runtime-lifetime-classifier-20260714/summary.json \
  --output \
    docs/figures/runtime-lifetime-classifier-20260714/summary.svg
```

The off campaign ran before the adaptive campaign. The bootstrap intervals in
`summary.json` resample within each campaign; they exclude temporal drift
between the sequential campaigns.

## Pinned inputs and host

- toolchain: `nightly-2026-06-11`;
- rustc: `1.98.0-nightly (485ec3fbc 2026-06-10)`;
- Cargo: `1.98.0-nightly (0b1123a48 2026-06-01)`;
- host CPU: AMD EPYC 9354 32-Core Processor;
- kernel: Linux 6.8.0-111-generic;
- ripgrep: `4649aa9700619f94cf9c66876e9549d83420e16c`;
- fd: `b19136871310b01500b4f09eadd7387b8476be47`;
- Oxipng: `dea23211ae6259007e068c59ab16929798d00d96`.

The THP probe source is
`unialloc/examples/runtime_lifetime_classifier_thp_probe.rs`, SHA-256
`3696863baa229b88d13515ecda817f7c11087ceafcbd068025a2710e0dd6d6b7`.

## Integrity and interpretation

The JSON records carry implementation, wrapper, binary, audit, command,
workload, and output hashes. `summary.json` rejects failed runs, missing shared
applications, unequal allocator feature sets, and unequal output hashes.

This campaign observed 31 stable Short sites, zero stable Long sites, and zero
THP routes. It supports marker-free Short learning and the measured classifier
overhead. The separate synthetic probe observed one learned Long site, one exact
2 MiB VMA with `VmFlags: hg`, and `AnonHugePages: 2048 kB`. That result proves
realized backing on this host; real-application Long routing and application
benefit remain open.
