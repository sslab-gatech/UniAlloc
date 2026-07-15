# Cross-Allocator Large-Page Figures

Slide-ready PNG and editable SVG figures generated from `docs/evidence/cross-allocator-modern-tcmalloc-20260715/summary.json`:

- `incremental-effect`: matched on/off performance and resident-memory effects.
- `actual-backing`: observed physical large-page backing by endpoint.
- `endpoint-frontier`: endpoint timing and effective resident memory with matched-mode connectors; cross-family raw timing ranking is withheld.

The Google TCMalloc endpoint is identity-verified at the pinned commit. Its measured backing is zero, so the figures exclude a Temeraire/HPAA performance-effect claim.
