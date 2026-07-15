# RSH-060 partial-yield panic evidence

This directory records a three-repetition, feature-matched reclaim experiment
for the advisory-derived `RSH-060-partial-panic` scenario. The vulnerable
`toodee` 0.2.1 arm reproduces an AddressSanitizer double-free after the input
iterator yields one owned element and then panics. UniAlloc's `reclaim_checks`
arm reports `unialloc_pointer_already_released_check` on that same unwind edge.
The feature-matched `reclaim_plain` arm has no allocator signal, and all
`toodee` 0.3.0 controls preserve the intentional Rust panic without an
allocator or sanitizer finding.

## Matrix result

| Archive | Allocator | Repetitions | Observation |
|---|---|---:|---|
| vulnerable | system + ASan | 3 | `asan_double_free` in 3/3 |
| vulnerable | `reclaim_plain` | 3 | stable intentional Rust panic; allocator signal absent |
| vulnerable | `reclaim_checks` | 3 | `unialloc_pointer_already_released_check` in 3/3 |
| patched | system + ASan | 3 | matched safe Rust panic; sanitizer finding absent |
| patched | `reclaim_plain` | 3 | matched safe Rust panic; allocator signal absent |
| patched | `reclaim_checks` | 3 | matched safe Rust panic; allocator signal absent |

`mechanism-results.json` therefore classifies this scenario as `detected` with
reason `feature_matched_exact_allocator_diagnostic`. This attribution covers
the duplicate-reclaim edge exercised by this witness. It does not claim that
reclaim checks detect every `insert_row` memory-safety path in the advisory.

## Reproduction

```sh
uv run python evaluation/scripts/run_rustsec_heap_experiment.py \
  --catalog evaluation/config/rustsec_heap_strong_batch_c_harnesses.json \
  --action run \
  --scenario RSH-060-partial-panic \
  --variants system,reclaim_plain,reclaim_checks \
  --archive-variants vulnerable,patched \
  --output-dir docs/evidence/rustsec-rsh060-partial-panic-20260714 \
  --cache /tmp/strong-batch-c-cache \
  --execute-unsafe --jobs 1 --repetitions 3 \
  --build-timeout 600 --run-timeout 60
```

## Retained artifact hashes

```text
08d4b231fb70bce2d54b18c81fe7c4f29f595eab137a0b3ae3b0390dc3947cc9  evaluation/config/rustsec_heap_strong_batch_c_harnesses.json
fc6cf4ffb3d629850a7ef11cfb30d3deb9449f0b3435cab50f8333d0fe4cd4c5  evaluation/harnesses/rustsec_heap_expansion/RSH-060/partial_panic.rs
130cc5c8cf87d23134c9e83b2b4d0b9f1ddb7a67c51050d49733638986cf6d9a  experiment.json
42676629f497cfb40dd2f8f357478019c699a0ebe9a86d66dba80f2bcf69d69f  summary.json
8690da167d08ef751508ca42b3c75e51628298fdd315c54112ed80fad1d8dcc0  mechanism-results.json
0c207bf98f4ae31c20a72bb5807435910ad9c18be9160b20a4396aa39eedabe2  preflight.json
```
