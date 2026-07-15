# Compiler-pass performance and RSS screen

This directory records the merge gate for the automatic RustSec compiler-pass
coverage change. It compares the pre-change snapshot with the dev-based
candidate through `typeiso_perf / typed_plain` ratios, then compares those
ratios across snapshots. Each order uses three warmups and fifteen measured
runs on CPU 20 / NUMA node 0. `ripgrep` repeats its path 32 times per process to
move the measurement above the short-process timing floor.

`summary.json` is the compact result. `raw/` retains every source matrix. The
broad runs also authenticate modern Google TCMalloc revision
`12f255231938d30493186b0a037feedd70f5a1c1`, HPAA activation, self-provider
identity, exact per-target markers, stable output hashes, and stats-free
performance variants.

The gate passes. The candidate-to-baseline Type Isolation wall-time
ratio-of-ratios is -1.66% for Oxipng and +0.81% for the long Ripgrep workload.
Oxipng peak RSS changes by +0.58%; Ripgrep's maximum remains 7 MiB in both
snapshots. The 95% bootstrap interval for the long Ripgrep wall-time delta spans
zero and ends below +1.4%.

The macro builds retain the same aggregate compiler rewrite counts, so they
screen pass overhead without claiming activation of the new sites. The twelve
RustSec causal matrices and the compiler regression tests provide active
`Vec::with_capacity` and primitive `slice::to_vec` coverage.
