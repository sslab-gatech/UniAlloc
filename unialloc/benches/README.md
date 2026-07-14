# `std_bench` provenance

The collection benchmark modules used by the `std_bench` target are synchronized
from [`library/alloctests/benches` at rustc commit
`485ec3fbcc12fa14ef6596dabb125ad710499c9e`](https://github.com/rust-lang/rust/tree/485ec3fbcc12fa14ef6596dabb125ad710499c9e/library/alloctests/benches),
which is selected by `rust-toolchain`. The upstream `alloctests` package declares
`MIT OR Apache-2.0`.

UniAlloc keeps a thin local adaptation around that suite:

- allocator selection and semantic/statistics probes remain in `lib.rs`;
- random inputs use the repository's existing `rand 0.7` dependencies;
- benchmark-source API adaptations remain compatible with the paper-pinned
  `nightly-2022-07-01` compiler;
- local correctness fixes for benchmark setup and initialized vector lengths are
  retained.

The canonical leaf inventory lives in
`evaluation/config/compiler_coverage_manifest.template.json`. Its ordered names
must match `cargo bench --bench allocbenches -- --list` for the pinned rustc
source exactly.
