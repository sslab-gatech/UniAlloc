# Runtime Lifetime Classifier Figure

`summary.svg` is a 1600 x 900 slide-ready rendering of the feature-parity
performance comparison, runtime classification coverage, and claim boundary.
It is generated directly from
`docs/evidence/runtime-lifetime-classifier-20260714/summary.json`:

```sh
uv run python evaluation/scripts/plot_runtime_lifetime_classifier.py \
  --summary \
    docs/evidence/runtime-lifetime-classifier-20260714/summary.json \
  --output \
    docs/figures/runtime-lifetime-classifier-20260714/summary.svg
```

The SVG SHA-256 is
`5efb346ba28205dfdfab80593b7db52801c911f7fc7c9de403d669e6e86d0a9f`.
