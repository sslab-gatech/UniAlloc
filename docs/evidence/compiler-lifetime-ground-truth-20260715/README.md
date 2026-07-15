# Compiler Heap-Lifetime Ground Truth

This package preserves the quick real-program campaign used to join exact runtime pressure-relative lifetime outcomes with compiler MIR features.

## Contents

- `runtime/*.stderr.bin`: runtime exporter records for ripgrep, fd, and oxipng.
- `compiler-audits.tar.gz`: compiler audit rows. Extract it in this directory to create `audits/` before rerunning the join.
- `ground-truth-manifest.json`: relocatable join manifest.
- `ground-truth-summary.json`: machine-readable join, rankings, and heuristic-promotion decision.
- `ground-truth-analysis.md`: human-readable analysis.
- `results.json`: original campaign result index.

## Reproduce the join

From this directory:

```bash
tar -xzf compiler-audits.tar.gz
uv run python ../../../evaluation/scripts/compiler_heap_lifetime_ground_truth.py \
  --manifest ground-truth-manifest.json \
  --output /tmp/compiler-lifetime-ground-truth-summary.json \
  --markdown-output /tmp/compiler-lifetime-ground-truth-analysis.md \
  --required-apps ripgrep,fd,oxipng \
  --minimum-decisive-outcomes 3 \
  --minimum-long-byte-share 0.8 \
  --minimum-feature-sites 2
```

The campaign collected 74,507 allocations across 175 exact runtime sites with zero bypassed observations. The preregistered candidate gate found zero Long-dominant sites, so the recorded production decision is `ABSTAIN`. Compiler features remain observational inputs for future held-out validation.
