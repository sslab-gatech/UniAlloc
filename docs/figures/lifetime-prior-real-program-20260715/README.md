# Real-Program Lifetime-Prior Figures

These figures are rendered from the source-digest-bound compact package in
[`docs/evidence/lifetime-prior-real-program-20260715/`](../../evidence/lifetime-prior-real-program-20260715/).

## Files

- `stage-a-opportunity.svg` and `.csv` compare the completed requested-byte
  lifetime mix across six pinned real programs. The lower panel reports exact
  executed Long-prior precision and recall only for the four current
  exact-layout-gated rows. Oxipng and redb remain labeled pre-gate triage.
- `polars-stage-b-ablation.svg` and `.csv` retain the fixed-work five-arm Polars
  comparison. The two ordinary-placement comparisons remain claim eligible.
  Every THP comparison is visibly blocked because neither selective arm routed
  Long allocations, created a THP extent, issued THP advice, or acquired
  resident `AnonHugePages`.
- The PNG companions are presentation conveniences generated from the
  canonical SVG files with the locally installed ImageMagick version.

The SWC production-clock Stage-B result remains in the compact evidence and
the main evaluation document. It is a two-repeat Criterion diagnostic: the THP
arms acquired timed-phase backing, while dynamic work and the small repeat
count keep its timing and RSS values outside presentation-grade performance
claims.

## Regenerate

```sh
uv run python evaluation/scripts/render_lifetime_prior_real_program_charts.py \
  --repo-root . \
  --summary docs/evidence/lifetime-prior-real-program-20260715/summary.json \
  --output-dir docs/figures/lifetime-prior-real-program-20260715
```

The renderer verifies all referenced raw-source SHA-256 digests before writing
any output.
