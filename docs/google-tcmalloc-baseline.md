# Modern Google TCMalloc baseline

## Active identity

`bench_tcmalloc` selects the modern `google/tcmalloc` implementation with its
HugePage-Aware Allocator (HPAA/Temeraire page heap). The explicit
`bench_gperftools_legacy` feature selects the crates.io wrapper around
gperftools `tc_memalign` and `tc_free` symbols.

The reproducible compatibility pin is:

- upstream: `https://github.com/google/tcmalloc.git`
- revision: `12f255231938d30493186b0a037feedd70f5a1c1` (2025-09-26)
- Bazel: `8.4.2`
- Bazel module lock: `evaluation/config/google_tcmalloc_MODULE.bazel.lock`

This revision is the tested compatibility pin. Upstream-current tracking runs
as a separate maintenance task. Google TCMalloc documents source/Bazel
integration and excludes compiled ABI stability from its compatibility
contract, so the repository builds a revision-bound shared link artifact.

Build it with:

```bash
uv run python evaluation/scripts/build_google_tcmalloc.py \
  --bazel /path/to/bazelisk-or-bazel
export UNIALLOC_GOOGLE_TCMALLOC_PREFIX="$PWD/evaluation/deps/tcmalloc"
uv run python evaluation/scripts/run_std_bench_allocator_variants.py \
  --tcmalloc-lib-dir "$UNIALLOC_GOOGLE_TCMALLOC_PREFIX/lib"
```

The builder checks the exact source tree, Bazel version, locked module graph,
exported dynamic symbols, revision-specific symbol, and a live
`MallocExtension::GetStats()` probe. The probe requires both `HugePageAware:`
and `PARAMETER hpaa_subrelease 1`, establishing the HPAA page heap with adaptive
subrelease. Before publishing the artifact, the builder stages it in a disposable
prefix and runs the required live integration gate described below. A gate
failure leaves any previously published artifact untouched. The Rust benchmark
constructor repeats the revision, HPAA, and malloc-provider checks before a
measurement can run. The shared artifact's own constructor performs the
HPAA/provider check and emits one identity marker in every linked or preloaded
target process; eval runners require that marker before accepting timing.

The active std-bench runner requires the uniquely named
`libunialloc_google_tcmalloc.so`, its hash-bound
`google-tcmalloc-provenance.json`, the revision symbol, HPAA stats, and the
active-provider proof. Generic `libtcmalloc.so` files follow the explicit
legacy path.

## Live integration gate

The builder runs this gate automatically before publication. Run it again
against the exact published prefix before a formal or nightly evaluation:

```bash
UNIALLOC_GOOGLE_TCMALLOC_PREFIX="$PWD/evaluation/deps/tcmalloc" \
UNIALLOC_REQUIRE_LIVE_GOOGLE_TCMALLOC=1 \
uv run python -m unittest \
  evaluation/scripts/test_google_tcmalloc_live_integration.py
```

The gate authenticates the provenance hash and dynamic symbols, then starts a
real `LD_PRELOAD` process. The process must map the exact DSO, resolve `malloc`
from it, report the pinned revision, confirm HPAA and allocator-provider state,
and emit exactly one runtime identity marker.
An explicitly configured prefix always fails closed. The test skips only when
no prefix or required-mode variable is configured and a clean checkout has no
default artifact. `UNIALLOC_REQUIRE_LIVE_GOOGLE_TCMALLOC` accepts exactly `1`.
The prefix must contain neither whitespace nor `:` because the Linux loader
uses those characters as `LD_PRELOAD` separators.

`UNIALLOC_TCMALLOC_LIB_DIR` remains a compatibility input for older runner
commands. New commands should use `UNIALLOC_GOOGLE_TCMALLOC_PREFIX`.

## Historical gperftools identity

`bench_gperftools_legacy` preserves the old crates.io wrapper for reproducing
historical gperftools evidence. Existing tracked measurements, figures, and
2026-07-14 documents that identify gperftools 2.18.1 retain their historical
allocator identity.

New performance or memory claims require rerunning their campaigns under the
modern artifact and recording the new provenance, mapped library, HPAA stats,
wall time, RSS/PSS, page faults, and hugepage backing.
