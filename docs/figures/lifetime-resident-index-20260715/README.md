# Lifetime-guided THP backup slide

Canonical tracked source:

- `docs/evidence/lifetime-resident-index-20260715/mixed-filler-fastpath-swc-quick-summary.json`
- compact summary SHA-256:
  `0543c6d01bd20bbd15e7de3ca288caeed1aaa982bcdb0cd117439b7886b520c5`
- raw source-worktree artifact SHA-256:
  `d757723b9561ed1362c56bb5fd59939cf340aa3d3dcf5bcc3204d4c6f0c83c2e`

Assets:

- `mixed-filler-fastpath-evidence-slide.svg`: editable 1600x900 B13 slide;
- `mixed-filler-fastpath-evidence-slide.png`: 3200x1800 8-bit RGB copy for
  direct presentation import.

Regenerate the SVG and PNG:

```bash
python3 evaluation/scripts/render_lifetime_mixed_fastpath_slide.py
convert -background white -density 192 \
  docs/figures/lifetime-resident-index-20260715/mixed-filler-fastpath-evidence-slide.svg \
  -resize 3200x1800\! -alpha remove -alpha off -depth 8 -strip PNG24:\
  docs/figures/lifetime-resident-index-20260715/mixed-filler-fastpath-evidence-slide.png
python3 evaluation/scripts/append_lifetime_fastpath_backup_slide.py
```

The append step is idempotent and manages physical slide 32, labeled B13, in
`docs/UniAlloc-Qualifier-Core-Deck.pptx`. It fails closed when slide 32 already
belongs to another source.

The figure deliberately carries its boundary inside the slide: SWC fixed work,
N=8 paired blocks, 32 fresh processes, a dirty working-tree snapshot, and both
performance and presentation claim eligibility set to false. It supports the
packing, cache, scan, and physical-backing mechanism. The clean committed rerun
is the next provenance gate; a larger preregistered campaign remains the stable
performance-claim gate.
