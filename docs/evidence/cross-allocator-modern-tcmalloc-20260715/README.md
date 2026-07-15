# Cross-Allocator Large-Page Evaluation

This package preserves the corrected modern baseline campaign.

## Google TCMalloc identity

- Repository: `https://github.com/google/tcmalloc.git`
- Commit: `12f255231938d30493186b0a037feedd70f5a1c1`
- Commit date: 2025-09-26
- Revision role: tested compatibility pin; upstream-current tracking is separate
- BCR module: `0.0.0-20250927-12f2552`
- Allocator target: `@com_google_tcmalloc//tcmalloc:tcmalloc`
- Integration: Bazel `cc_binary` `malloc` attribute at link time
- Hugepage allocator: Temeraire / HugePageAwareAllocator (HPAA)
- Bazel: 8.4.2

`google-tcmalloc-provenance.json` records the pinned checkout, implementation-file hashes, exact build command, strong `malloc` symbol, HPAA symbols, smoke checksum, and binary hashes. `google-tcmalloc-bazel-actions.textproto` preserves the compile/link action graph. The runner fails closed when this verified identity is unavailable. It never falls back to gperftools.

The repository's authenticated real-application DSO contract uses `libunialloc_google_tcmalloc.so` with SHA-256 `5f99dcf644a7e1e138439fed5d42216c5313ad28e2557f936a250643e0f42081` and additionally validates live HPAA stats, the active malloc provider, the mapped library, and a per-target marker. This neutral probe records an independent link-time identity proof at the same revision and Bazel version.

## Claim boundary

The Google TCMalloc arm realized zero physical hugepage backing in all five measured samples. The package supports an implementation-identity claim. It provides no Temeraire/HPAA hugepage performance claim and no HPAA causal comparison because the campaign lacks backed samples and a matched HPAA-off control.

The optional gperftools 2.18.1 implementation is named `gperftools-legacy` throughout the runner and was excluded from this campaign.
