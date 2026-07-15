# Cross-Allocator Large-Page Evaluation

This package preserves the corrected modern baseline campaign.

## Google TCMalloc identity

- Repository: `https://github.com/google/tcmalloc.git`
- Commit: `12f255231938d30493186b0a037feedd70f5a1c1`
- BCR module: `0.0.0-20250927-12f2552`
- Allocator target: `@com_google_tcmalloc//tcmalloc`
- Integration: Bazel `cc_binary` `malloc` attribute at link time
- Hugepage allocator: Temeraire / HugePageAwareAllocator (HPAA)
- Bazel: 9.2.0

`google-tcmalloc-provenance.json` records the pinned checkout, implementation-file hashes, exact build command, strong `malloc` symbol, HPAA symbols, smoke checksum, and binary hashes. `google-tcmalloc-bazel-actions.textproto` preserves the compile/link action graph. The runner fails closed when this verified identity is unavailable. It never falls back to gperftools.

## Claim boundary

The Google TCMalloc arm realized zero physical hugepage backing in all five measured samples. The package supports an implementation-identity claim. It provides no Temeraire/HPAA hugepage performance claim and no HPAA causal comparison because the campaign lacks backed samples and a matched HPAA-off control.

The optional gperftools 2.18.1 implementation is named `gperftools-legacy` throughout the runner and was excluded from this campaign.
