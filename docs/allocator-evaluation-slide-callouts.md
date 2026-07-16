# Allocator evaluation slide callouts

These lines accompany the paired-panel allocator figures. Every measured Micro
comparison uses its matched same-session UniAlloc anchor. Execution-cost ratios
below `1x` favor the subject. Micro peak RSS includes process startup and
adaptive libtest work, so every Micro RSS statement is diagnostic.

## External allocator one-line summaries

| Allocator | Slide-ready summary |
|---|---|
| UniAlloc | **Performance:** reference at `1.000x`. **Peak RSS:** reference at `1.000x`. |
| UniAlloc + Type Isolation | **Performance:** `0.35%` higher execution cost than UniAlloc. **Peak RSS:** matches UniAlloc in the startup-inclusive Micro diagnostic. |
| ptmalloc | **Performance:** `0.72%` higher execution cost than UniAlloc. **Peak RSS:** matches UniAlloc at the current diagnostic resolution. |
| jemalloc | **Performance:** `0.24%` higher execution cost than UniAlloc. **Peak RSS:** matches UniAlloc at the current diagnostic resolution. |
| mimalloc | **Performance:** `0.05%` higher execution cost than UniAlloc. **Peak RSS:** matches UniAlloc at the current diagnostic resolution. |
| mimalloc, THP disabled | **Performance:** `0.22%` lower execution cost than UniAlloc. **Peak RSS:** matches UniAlloc at the current diagnostic resolution. |
| Google TCMalloc, modern | **Performance:** `1.50%` higher execution cost than UniAlloc. **Peak RSS:** `203.98%` higher in the startup-inclusive diagnostic. |
| snmalloc | **Performance:** `0.29%` lower execution cost than UniAlloc. **Peak RSS:** `37.10%` higher in the startup-inclusive diagnostic. |
| Scudo | **Performance:** `3.13%` higher execution cost than UniAlloc. **Peak RSS:** matches UniAlloc at the current diagnostic resolution. |

The two external-allocator measurement sessions differ by `0.237%` in their
common-236 UniAlloc performance anchors and match at the current RSS resolution.
Type Isolation retains its registered feature-campaign anchor. Differences
below `0.3%` deserve an effectively-even interpretation in the presentation.

## Illustrative real-world one-line summaries

> **ILLUSTRATIVE MODEL - NOT MEASURED:** The values in this section are
> presentation placeholders across Collections, Oxipng, redb, Polars, SWC,
> RustPython, and Actix Web.

| Configuration | Slide-ready summary |
|---|---|
| UniAlloc | **Performance:** reference at `1.000x`. **Peak RSS:** reference at `1.000x`. |
| UniAlloc + Type Isolation | **Performance:** modeled execution cost is `27.07%` higher. **Peak RSS:** modeled growth is `9.96%`. |
| ptmalloc | **Performance:** modeled execution cost is `2.97%` higher. **Peak RSS:** modeled growth is `11.67%`. |
| jemalloc | **Performance:** modeled execution cost is `4.88%` lower. **Peak RSS:** modeled growth is `5.26%`. |
| mimalloc | **Performance:** modeled execution cost is `18.81%` lower. **Peak RSS:** modeled growth is `36.49%`. |
| mimalloc, THP disabled | **Performance:** modeled execution cost is `10.19%` lower. **Peak RSS:** modeled growth is `9.12%`. |
| Google TCMalloc, modern | **Performance:** modeled execution cost is `1.31%` lower. **Peak RSS:** modeled growth is `15.10%`. |
| snmalloc | **Performance:** modeled execution cost is `5.74%` lower. **Peak RSS:** modeled growth is `7.83%`. |
| Scudo | **Performance:** modeled execution cost is `18.08%` higher. **Peak RSS:** modeled growth is `24.36%`. |

The standalone Macro plot carries the required estimate badge. These lines
must retain the same badge until measured cells replace the illustrative data.

## Type Isolation measured result

> **Measured frozen ce8:** Micro execution cost increases `0.35%`, Macro
> execution cost increases `35.09%`, and fixed-work Macro peak RSS increases
> `1.43%`.

This sentence describes the frozen `ce8af7b` campaign and remains the measured
result used by the canonical feature figures.

## Type Isolation development projection

> **STATIC MODEL ESTIMATE - NOT MEASURED:** About `87%` type coverage, `76%`
> effective isolation-cache coverage, `25-30%` expected execution-cost
> overhead, and `5-15%` expected peak-RSS growth.

Supporting planning ranges:

- Type coverage: `80-90%`, with an `87%` central estimate.
- Effective isolation-cache coverage: `72-80%`, with a `76%` central estimate.
- Execution-cost overhead: `20-35%` planning range, `25-30%` presentation
  range, and a design target of at most `30%`.
- Normal-program peak RSS growth: `5-15%`, with a `10%` central estimate.
- Modeled fixed metadata: about `1.28 KiB` TLS per thread.
- Modeled extreme retained-cache ceiling: about `2 MiB` per thread.

The machine-readable projection is
`benchmark-results/type-isolation-static-model-projection-20260716.json`.
It has no frozen revision or implementation digest and cannot enter a measured
comparison. Replace it after the current implementation completes its
registered three-round campaign.
