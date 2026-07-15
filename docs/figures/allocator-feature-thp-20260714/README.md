# Target-centric allocator evaluation figures

This pack contains **three** presentation-eligible 16:9 measured-target detail
figures. They show paired performance deltas and absolute peak RSS in MiB using
campaign-local references. Separate Oxipng and Rsedis campaigns remain in
labeled, unpooled sections.

| Figure | Target | Campaigns shown |
|---|---|---|
| `01-ripgrep-configurations` | ripgrep | 7-configuration real-world matrix, n=7 |
| `02-oxipng-configurations` | Oxipng | 1-thread real-world matrix, n=7; 4-thread 15-configuration feature matrix, n=5 |
| `03-rsedis-configurations` | Rsedis | 5-allocator matrix, n=5; separate 3-configuration THP mechanism matrix, n=5 |

The plotted scope contains **3 unique targets, 37 target-configuration cells,
213 measured runs, and 37 warmups**. Type Isolation rows are subsets of these
campaigns. Bars show medians; filled dots show every retained measured run.
Same-round deltas are computed before their medians. Each detail retains its
original diagnostic source and campaign labels.

## Formats

- SVG: three editable vector figures with live text.
- PNG: three 2,400 x 1,350 slide-ready exports.
- PDF: one three-page figure pack.
- CSV: three target tables and one HugeTLB accounting table.
- JSON: source hashes, chart contracts, campaign boundaries, and asset hashes.

## Source artifacts

- `benchmark-results/realworld-rust-allocator-20260714.json`
  SHA-256: `7598e2b06143b7bd3a73d8c599402fd3c8dd6bdddb60706d46f987b3b4fd2741`
- `benchmark-results/rust-alloc-paper-evaluation-20260714.json`
  SHA-256: `163a2897ce92b80a758949c614e525ef9fe37380aa44af566d40f898a1407609`
- `benchmark-results/allocator-feature-thp-evaluation-20260714.json`
  SHA-256: `293dbfd0e1b6966b4bb2e1884fd498994299e065fc5c240a337b18e43d9fd490`

## Regenerate

```bash
uv run evaluation/scripts/plot_allocator_feature_thp_slides.py
```

The script pins Matplotlib 3.11.0 through PEP 723 metadata. The figures are
current-host descriptive evidence. Raw-point variation and median bars are not
confidence intervals. The 1-thread and 4-thread Oxipng campaigns are not
pooled. The Rsedis allocator and THP campaigns are not pooled. TCMalloc in the
THP mechanism campaign uses the host default; UniAlloc is outside that
mechanism campaign. `VmHWM` and `HugetlbPages` maxima in the supporting table
were sampled independently and are not stacked.
