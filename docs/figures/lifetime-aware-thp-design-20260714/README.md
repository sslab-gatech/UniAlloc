# Lifetime-aware THP presentation figures

Canonical source:

- `docs/evidence/cross-allocator-large-page-final-20260714/unialloc-ablation-effects.csv`
- `docs/evidence/cross-allocator-large-page-final-20260714/summary.json`
- `docs/allocator-memory-and-mechanisms.md`

Assets:

- `lifetime-aware-thp-pipeline.svg/.png`: editable design pipeline.
- `lifetime-aware-feature-decomposition.svg/.png`: feature-on/off causal decomposition.
- `lifetime-aware-feature-decomposition.csv`: exact values copied from the formal
  ablation evidence; resident columns use signed change, where negative means a
  smaller process-residency metric.

PNG companions are rendered at 3200x1800 from the canonical 1600x900 SVGs:

```bash
convert -background white -density 192 input.svg -resize 3200x1800! output.png
```

The figures keep the synthetic-workload, physical-backing, and metric boundaries
inside the slide asset so they remain visible after copy/paste.
