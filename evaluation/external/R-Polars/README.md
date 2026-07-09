# external-R-Polars paper workload adapter

This directory is a strict adapter scaffold for an external paper macro workload.
It is not performance evidence by itself.

## Contract

- `run_paper_workload.py` delegates to `evaluation/scripts/paper_external_workload_adapter.py`.
- The adapter refuses to emit timing unless `workload_config.json` or `workload_config.local.json` exists and sets `configured=true`.
- A real config must point at a real benchmark checkout and a command that selects dataset, benchmark, allocator, variant, and run index.
- The command/env/cwd template must keep selector placeholders for dataset, benchmark, allocator, variant, and run index so each paper cell is actually selected.
- The benchmark command must print a JSON object with a finite timing field such as `seconds`; set `benchmark_owned_json=true` only when that JSON comes from the benchmark harness rather than a generic wall-clock wrapper.

Benchmarks matched by the current bundle rule: `R-Polars`.

## Current readiness audit

`audit-rpolars-claim-matrix` is the current target-fragment-aware gate for this adapter. It reads the local workload config, the Polars checkout, the latest R-Polars sample audit, and any repeated `--samples <jsonl>` additions, then credits a paper repetition only when one unique `run_index` covers every discovered R-Polars cargo bench fragment for that `(dataset, allocator)` cell.

The latest local artifact is `evaluation/results/rpolars_claim_matrix_audit.json`, refreshed by `rpolars-claim-matrix-after-pac-authentication-all-allocators-runs1-6-20260616a`. It now merges 220 successful diagnostic records from 49 sample sources. The matrix has all 35 active R-Polars raw-timing / surface-complete cells: all seven allocator cells for `default_performance`, `type_isolation`, `hugepage_metadata`, `metadata_segregation`, and `pac_authentication` (`unialloc`, `jemalloc`, `mimalloc`, `tcmalloc`, `snmalloc`, Darwin-fallback `ptmalloc`, and Scudo-System-fallback `scudo`). Every covered cell has target-complete run indexes 1, 2, 3, 4, 5, and 6 with no missing surface run indexes. The matrix still has 0 claim-usable cells because the bridge remains `claim_grade=false` and the remaining blockers are paper-grade provenance/usable-sample gates rather than raw/surface repetition gaps.

The newest R-Polars shards close the `pac_authentication` raw/surface coverage: `evaluation/raw/rpolars-paper-plan-pac-authentication-snmalloc-runs1-6-20260615a/paper-performance.samples.jsonl`, `evaluation/raw/rpolars-paper-plan-pac-authentication-ptmalloc-runs1-6-20260615a/paper-performance.samples.jsonl`, and `evaluation/raw/rpolars-paper-plan-pac-authentication-scudo-runs1-6-20260615a/paper-performance.samples.jsonl`, with diagnostic audits under `evaluation/results/rpolars_paper_plan_pac_authentication_{snmalloc,ptmalloc,scudo}_runs1_6_shard_audit_20260615a.json`. Each shard contains six successful finite records, zero failed records, and 43 observed benchmark leaves. The measured wall times were about 400-412s for `snmalloc`, 377-386s for Darwin-fallback `ptmalloc`, and 380-383s for Scudo-System-fallback `scudo`. They remain diagnostic-only because the adapter bridge is non-claim-grade, `ptmalloc`/`scudo` use host fallback semantics on Darwin, the variant-routing evidence is still an adapter-level selector rather than paper-grade provenance, and paper-grade sample gates are still absent. The earlier `metadata_segregation/*`, `hugepage_metadata/*`, and `type_isolation/*` shards remain at their corresponding `evaluation/raw/rpolars-paper-plan-*-runs1-6-20260615a/` paths with published shard audits under `evaluation/results/`.

`generate-rpolars-paper-plan --dry-run-probe` is the execution schedule gate before any long R-Polars timing run. The latest local artifact pair is `evaluation/results/rpolars_paper_plan.json` and `evaluation/results/rpolars_paper_plan_audit.json`: it creates 35 workloads for 35 active cells, plans 210 repeated runs, dry-run probes all 35 adapter invocations successfully, and reports structural execution readiness while still blocking claim-grade import on adapter/sample provenance.

Earlier filtered runner checks still provide the complete `default_performance` surface for all seven allocators: UniAlloc run indexes 1 through 6, jemalloc/mimalloc run indexes 1 through 6 after focused success-only shards, tcmalloc and snmalloc run indexes 1 through 6, Darwin-fallback ptmalloc run indexes 1 through 6, and Scudo-System-fallback scudo run indexes 1 through 6. Those artifacts are diagnostic-only because the adapter bridge is non-claim and the generated `polars_groupby_compat.csv` fixture is used only to keep the historical `groupby` Criterion route executable when the checkout's `small.csv` lacks the required columns.

That audit is intentionally stricter than counting JSONL records. Multi-target smoke records can prove route readiness, but they are not paper-equivalent timing evidence until repeated runs, raw provenance, allocator semantics, and claim-grade metadata all pass for every required cell.

## Paper source hint

- Project: `Polars`
- Paper version: `v0.13.0`
- Upstream URL: `https://github.com/pola-rs/polars`

Start by copying `workload_config.template.json` to `workload_config.json`, filling in the real checkout path and command, then run the bundle audit with dry-run probing.
