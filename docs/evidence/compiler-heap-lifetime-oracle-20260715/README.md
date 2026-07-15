# Compiler Heap-Lifetime Placement Oracle

This package preserves the controlled fixture experiment for the bounded compiler Long oracle and the lifetime-aware ordinary/THP placement mechanisms.

- `summary.json`: paired effects and per-measured-block THP gates.
- `samples.jsonl`: raw measured samples.
- `manifest.json`: workload, toolchain, host, and geometry provenance.
- `compiler-audits/`: compiler decisions for the baseline and inferred builds.

The `hints-ordinary` arm reduced median paired wall time by 10.70%, allocation time by 26.16%, workload time by 28.70%, and peak RSS by 55.17% relative to the fixture default. These are controlled mechanism-oracle results. They do not establish automatic-classifier gains on general software.

All five `hints-thp` measured samples had zero observed `AnonHugePages`. The THP performance comparison therefore remains unavailable; successful `madvise` calls alone carry no physical-backing claim.
