# Standalone lifetime-pass regression gate

This gate compares the compatibility MIR driver with the extracted standalone
lifetime driver while holding the workload source, allocator rlib, release
profile, fixed work, and runtime policy constant.

## Contract

Before timing, each target must pass three equivalence checks:

1. both rebuilt outputs match the immutable pre-extraction four-arm target
   binary SHA and canonical lifetime audit;
2. compatibility and standalone canonical lifetime audit rows match by candidate identity,
   lifetime hint/basis/confidence, runtime-layout gate, rewrite status, and ABI
   symbol; path-like rustc arguments are excluded;
3. the compatibility and standalone target executables have identical `.text`
   bytes, and extracting `.text` leaves their full binary SHA unchanged.

The measurement then runs three `AB` plus `BA` pairs per target. Every cell is a
new process, `warmup_iterations` is zero, fixed-work correctness digests must
match, and `taskset` pins all cells to one allowed CPU when available.
Operation time and operation-window peak RSS have independent gates:

- median standalone slowdown at most 2%;
- median peak-RSS increase at most `max(2% of compatibility RSS, 1024 KiB)`.

The runner creates `raw/<run-id>` with `exist_ok=false`. It never discovers,
loads, or reuses a prior `run.json`.

## Inputs

The default pinned workload input is the immutable four-arm Bedrock/Convex raw
tree produced on 2026-07-16:

```text
/home/hanqing/alloc/UniAlloc-compiler-directed-swc-run-20260716/
  evaluation/raw/four-arm-other-targets-20260716
```

It supplies the exact upstream source checkouts, runner manifests, force-load
wrapper, and allocator-source replacement wrapper used by the five-target
evidence. The replacement wrapper contains pinned absolute source paths, so the
default exact-host location is part of this regression contract.

## Run

After the standalone entry point exists, run:

```sh
python3 evaluation/lifetime-pass-regression-20260716/run_regression.py \
  --run-id lifetime-pass-final-$(date +%Y%m%d-%H%M%S) \
  --standalone-source \
    tools/unialloc-rustc-pass/unialloc-rustc-lifetime-aware.rs \
  --targets bedrock convex \
  --pairs 3 \
  --cpu auto
```

Both pass entry points are compiled with the pinned toolchain using:

```sh
RUSTC_BOOTSTRAP=1 rustc +$(cat rust-toolchain) \
  --cfg unialloc_rustc_current PASS_SOURCE.rs -O -o PASS_BINARY
```

`build/passes.json` records a source-closure hash over each thin entry point
plus `unialloc-rustc-driver-engine.rs` and `lifetime_aware.rs`; the entry-point
hash alone is never treated as implementation provenance. The closure digest
uses stable repository-relative paths and fixed file roles; absolute checkout
paths remain descriptive provenance outside the digest material.

For each target, the runner bootstraps the exact allocator once, then cleans and
rebuilds the selected target crate first through the compatibility pass and
then through the standalone pass. This preserves one allocator rlib and one
source path across the comparison.

Key outputs are:

- `raw/<run-id>/build/targets/<target>/parity.json`;
- `raw/<run-id>/results/{bedrock,convex}.json`;
- `raw/<run-id>/summary.json`.

Run the dependency-free harness tests with:

```sh
python3 -m unittest -v \
  evaluation/lifetime-pass-regression-20260716/test_run_regression.py
```
