# Per-scenario full-sweep results

> Historical 27-scenario frozen-pilot table. The terminal efficacy partition is
> in `../rustsec-security-complete-20260714/README.md` and
> `../rustsec-all-53-live-20260714/evaluable-49-report.json`.

This table is a compact index of the 27 repository-materializable scenarios. All vulnerable and patched system oracles reproduced. Allocator columns report vulnerable-arm observations only; native outcomes are diagnostic and carry zero mitigation inference. Exact arm records and hashes are in `experiment-summary.json`.

| Scenario | Primitive | Tool | Raw UniAlloc | `typed_plain` | `typeiso` | TypeISO vulnerable route | Boundary |
|---|---|---|---|---|---|---|---|
| `RSH-001-upstream` | `use_after_free` | `asan` | native clean; inconclusive | native clean; inconclusive | native clean; inconclusive | `semantic` | critical-site coverage blocked |
| `RSH-002-derived-reuse` | `use_after_free` | `asan` | native diagnostic | native clean; inconclusive | native clean; inconclusive | `semantic` | critical-site coverage blocked |
| `RSH-002-published` | `use_after_free` | `asan` | native diagnostic | native diagnostic | native diagnostic | `semantic` | critical-site coverage blocked |
| `RSH-003-upstream` | `use_after_free` | `asan` | native diagnostic | native diagnostic | native diagnostic | `semantic` | critical-site coverage blocked |
| `RSH-004-published` | `use_after_free` | `miri` | native diagnostic | native diagnostic | native diagnostic | `semantic` | critical-site coverage blocked |
| `RSH-008-derived-reuse` | `use_after_free` | `native_address_trace` | catalog finding | catalog finding | catalog finding | `semantic` | critical-site coverage blocked |
| `RSH-008-published` | `use_after_free` | `asan` | native clean; inconclusive | native clean; inconclusive | native clean; inconclusive | `semantic` | critical-site coverage blocked |
| `RSH-015-published` | `double_free` | `asan` | native clean; inconclusive | native diagnostic | native abnormal; inconclusive | `semantic` | critical-site coverage blocked |
| `RSH-016-advisory` | `use_after_free` | `native_value_oracle` | catalog finding | catalog finding | catalog finding | `semantic` | critical-site coverage blocked |
| `RSH-017-invalid-free` | `invalid_free` | `asan` | native clean; inconclusive | native clean; inconclusive | native clean; inconclusive | `direct_only` | critical-site coverage blocked |
| `RSH-017-uaf` | `invalid_free` | `asan_c_and_rust` | native diagnostic | native diagnostic | native clean; inconclusive | `semantic` | critical-site coverage blocked |
| `RSH-018-reserve-overflow` | `double_free` | `asan` | native clean; inconclusive | native clean; inconclusive | native clean; inconclusive | `semantic` | critical-site coverage blocked |
| `RSH-018-split-off` | `double_free` | `asan` | native clean; inconclusive | native diagnostic | native diagnostic | `semantic` | critical-site coverage blocked |
| `RSH-018-unsafe-alloc-handle` | `double_free` | `asan` | native abnormal; inconclusive | native abnormal; inconclusive | native abnormal; inconclusive | `semantic` | critical-site coverage blocked |
| `RSH-019-upstream` | `use_after_free` | `miri` | native clean; inconclusive | native clean; inconclusive | native clean; inconclusive | `none` | no compiler rewrite; coverage blocked |
| `RSH-020-advisory` | `double_free` | `asan` | native diagnostic | native diagnostic | native diagnostic | `direct_only` | critical-site coverage blocked |
| `RSH-021-advisory` | `double_free` | `asan` | native clean; inconclusive | native clean; inconclusive | native clean; inconclusive | `semantic` | critical-site coverage blocked |
| `RSH-022-upstream` | `uninitialized_drop` | `asan` | native diagnostic | native diagnostic | native diagnostic | `semantic` | critical-site coverage blocked |
| `RSH-024-upstream` | `out_of_bounds_read` | `asan` | native clean; inconclusive | native clean; inconclusive | native clean; inconclusive | `direct_only` | critical-site coverage blocked |
| `RSH-026-derived-cfb` | `out_of_bounds_write` | `asan` | native clean; inconclusive | native clean; inconclusive | native clean; inconclusive | `semantic` | critical-site coverage blocked |
| `RSH-028-upstream` | `out_of_bounds_write` | `asan` | unsupported topology | native diagnostic | native diagnostic | `semantic` | critical-site coverage blocked |
| `RSH-029-advisory` | `out_of_bounds_write` | `asan` | native clean; inconclusive | native clean; inconclusive | native clean; inconclusive | `semantic` | critical-site coverage blocked |
| `RSH-030-upstream` | `misaligned_allocation` | `native_alignment` | unsupported topology | unsupported topology | unsupported topology | `unsupported` | topology excluded |
| `RSH-031-upstream` | `invalid_free` | `miri` | native clean; inconclusive | native clean; inconclusive | native clean; inconclusive | `semantic` | critical-site coverage blocked |
| `RSH-032-upstream` | `out_of_bounds_write` | `asan` | native abnormal; inconclusive | native abnormal; inconclusive | native abnormal; inconclusive | `none` | no compiler rewrite; coverage blocked |
| `RSH-033-upstream` | `out_of_bounds_write` | `asan` | native clean; inconclusive | native clean; inconclusive | native clean; inconclusive | `semantic` | critical-site coverage blocked |
| `RSH-040-upstream` | `uninitialized_read` | `miri` | native clean; inconclusive | native clean; inconclusive | native clean; inconclusive | `none` | no compiler rewrite; coverage blocked |

The table records the automatic-coverage full sweep. The later manually
attributed RSH-002 follow-up is stored in
`rsh002-annotated-reuse-edge.json`: vulnerable `typed_plain` reused the victim
address in 2/2 repetitions with zero denial events, while vulnerable `typeiso`
reused it in 0/2 and emitted 2/2 events bound to the compiler-audited
`Box<Replacement>` site. Automatic victim-site coverage remains pending.

Interpretation rules:

- `catalog finding` is a preregistered native address/value oracle; it is separate from an allocator-efficacy claim.
- `native diagnostic` records a signal, panic, or allocator message in the non-instrumented allocator arm.
- `native clean` and `native abnormal` remain inconclusive because the system arm owns the sanitizer or Miri ground truth.
- `semantic` and `direct_only` describe observed compiler rewrite routes. They do not validate the vulnerable critical site.
- `none` adds `no_compiler_rewrite_observed`; every supported Type Isolation arm also carries `critical_site_coverage_not_validated`.
