# Type Isolation primary-suite evidence

## Frozen contracts

The final suite implementation is Git revision
`f5d0c19c1cc5b56fdac3282d69333dd8c85d4cf2`. The tracked
`canonical-implementation-snapshot-f5d0c19.json` records the exact 131-file,
4,426,670-byte canonical scope captured during the Collections and Oxipng
campaign. Its nested component audit covers those two targets. The final
assembler independently validates the same revision and canonical digest in
all seven target results. Both a direct Git-blob recomputation and the frozen
campaign snapshot produce SHA-256
`cab1e580c08e2b16308bae75501716049ba428b040fb04bf305269c9ba9eaf01`.

The canonical input includes Rust and TOML files under `unialloc/` and
`alloc_macros/`, plus
`tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs`. Paths
containing a `target` component and all other extensions, including
`Cargo.lock`, are excluded. Files are sorted by snapshot-relative POSIX path.
SHA-256 consumes each UTF-8 path, a NUL byte, its exact Git blob, and a final
NUL byte.

`pre-amendment-suite-manifest-96fa640.json` preserves the exact result-blind
campaign contract at SHA-256
`96fa64009550d52f89549dd2124e3b1a402a42b7f1fd1750888600dd67bb8a6f`.
The final analysis manifest is
`evaluation/config/type_isolation_primary_suite.json` at SHA-256
`ba386a779e45e0884e02cf8825c69985d1b5cf68d57366af78e7f77cfa8f6ccb`.
The amendment adds RSS work-model interpretation, performance-source metadata,
and presentation markers. Target membership, source pins, harnesses, variants,
benchmark definitions, and the one-warmup plus five-round budget are unchanged.

## Final campaign inventory

All seven pinned targets are data-eligible. The final result contains 34
harnesses, 102 digest-validated warmups, and 510 measured processes. Every
correctness, build, allocator-activation, actual-MIR, stats-disabled, and
source-audit gate passes.

| Target | Harnesses | Warmups | Measurements | Published target SHA-256 | Route pass |
| --- | ---: | ---: | ---: | --- | ---: |
| Collections | 5 | 15 | 75 | `580e68528378d1186e0d817b61155e11a7ba2991dac2f24975bf9e4281cdef91` | 2/5 |
| Oxipng | 5 | 15 | 75 | `781ac89cb2b41c7541da7dff44432dbec6d7e0b3050d82d2930f78cb31b8a392` | 5/5 |
| redb | 4 | 12 | 60 | `670c7d0921a470b85b734ac2754bc9016b93d983dac4e8c2b0bb39ad7a62de2d` | 3/4 |
| Polars | 5 | 15 | 75 | `e29ef91465d8a30f43201e3104b89e7ebad269616dea30e9e604670c5935cab8` | 1/5 |
| SWC | 5 | 15 | 75 | `d24ad9310ff420313082227e28761a30e8b434044c16152c1690bebf70b37e3d` | 0/5 |
| RustPython | 5 | 15 | 75 | `8bde666c45ce4a6cfac0f94b8eac2289c42ae6d636c13549dbf6315f5d94e210` | 1/5 |
| Actix Web | 5 | 15 | 75 | `a48fee905dcc7ba4d5081fb76b941c0b25067799691c6cd21cf5622e54b0e765` | 1/5 |

RustPython dynamically resolves `libpython3.13.so.1.0`. All three variants
resolve the same runtime object with SHA-256
`e965823f0b6016b832cda18b6620a8e01100b7e0fe5b70cabc84b2ada8d63cf2`.
The campaign retains digest-checked `ldd` records and successful runtime probes.

Compiler-route equivalence passes 13 of 34 harnesses. Every measurement remains
in the final result. Route failures carry an attribution-limit marker because
the typed compiler/runtime route dominates the end-to-end ratio in those
harnesses. Type Isolation versus default UniAlloc is the deployment contrast;
Type Isolation versus typed control remains the matched policy ablation.

## RSS interpretation

Oxipng, redb, and Polars execute fixed predefined work. Their 14 harnesses form
the equal-work RSS core. Collections, SWC, RustPython, and Actix Web use
workload-native adaptive iterations. Their 20 peak-RSS results remain visible
as process-volume diagnostics and stay outside fixed-work geometric means.

`actix-async-service-direct-adaptive-rss-audit.json` retains the detailed
Criterion iteration-count audit. Across its 18 process observations, peak RSS
tracks the variant-dependent measured iteration count with `R^2 = 0.999955`.
This supports the process-volume interpretation and prevents the low typed-path
RSS from being presented as an equal-work allocator-memory improvement.

## Final tracked outputs

- Self-contained result: `benchmark-results/type-isolation-primary-v1.json`
- Canonical evaluation report: `docs/allocator-evaluation.md`
- Title-free SVG: `docs/figures/type-isolation-primary-suite/type-isolation-primary-suite.svg`
- Title-free PNG: `docs/figures/type-isolation-primary-suite/type-isolation-primary-suite.png`
- Uncapped paired-ratio data: `docs/figures/type-isolation-primary-suite/type-isolation-primary-suite-data.csv`
- Figure/source manifest: `docs/figures/type-isolation-primary-suite/type-isolation-primary-suite-manifest.json`

The assembler embeds warmup attestations and compact build, binary, allocator,
MIR, source, and runtime provenance. Its tracked output contains only
repository-relative optional paths. Figure generation depends only on the
tracked suite manifest and assembled result; ignored raw campaign directories
are unnecessary for rendering.

## Preserved rejected and intermediate runs

Ignored raw evidence retains the byte-exact preregistration build results, an
aborted pre-freeze analysis run, a four-record SMT-overlap audit plus the
superseded SWC run, and the initial fail-closed RustPython loader attempt. The
final SWC result is a clean rerun collected after the overlapping SMT workload
was quiesced. The final RustPython result follows an explicit runtime dependency
proof. None of the rejected measurements enters the assembled result.

`collections-oxipng-implementation-snapshot-34fcd72.json` and the byte-identical
historical target records under
`evaluation/raw/type-isolation-primary-v1/historical-34fcd72/targets/` preserve
the earlier Collections and Oxipng allocator source. That implementation is
superseded by the exact `f5d0c19` campaign and is excluded from final aggregates.
