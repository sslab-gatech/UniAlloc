# UniAlloc evaluation framework

This directory turns the paper claims in a sibling `../rust-alloc-paper` checkout (or `$UNIALLOC_RUST_ALLOC_PAPER`) into reproducible checks for this repository.

## What is tracked

- `config/paper_claims.json`: benchmark sets, allocator baselines, paper-data locations, and claim thresholds.
- `config/type_isolation_primary_suite.json`: current-version target and harness membership, comparison families, eligibility gates, and hierarchical aggregation.
- `config/rustsec_heap_security_corpus.json`: pinned RustSec/Rudra heap-safety classifications and preregistered UniAlloc mechanism hypotheses.
- `config/rustsec_heap_candidate_inventory.json`: generated full-database RustSec screening inventory plus pinned Rudra-PoC coverage.
- `config/rustsec_temporal_reclaim_review.json`: manual allocator-ownership review of temporal/reclaim candidates and the first expansion wave.
- `config/rustsec_heap_harnesses.json`: byte-pinned repository materializations, controls, tools, and oracles for 27 scenarios.
- `config/rustsec_heap_expansion_harnesses.json`: five-case executable expansion catalog with five published/upstream witnesses and two derived reuse probes.
- `config/rustsec_heap_strong_batch_{a..f}_harnesses.json`: terminal integration catalogs for the 53 reviewed strong candidates.
- `config/rustsec_heap_neon_node_harnesses.json`: Node/V8-hosted N-API catalog for the RSH-064 Neon witness.
- `config/rustsec_heap_complete_scope.json`: frozen pre-correction 49-row execution ledger plus four audit-only exclusions with retained reasons.
- `config/rustsec_heap_posthoc_scope_corrections.json`: hash-bound source-audit correction that excludes stack-lifetime RSH-006 from heap allocator efficacy accounting.
- `config/rustsec_heap_mechanism_results.json`: frozen 62-row mechanism-result ledger covering the 49 pre-correction executable cases.
- `config/rustsec_heap_mechanism_amendments.json`: reviewed post-integration scenario/mechanism amendments keyed to exact case and scenario identities.
- `config/rustsec_heap_primitive_overrides.json`: reviewed terminal-witness primitive labels with evidence provenance.
- `scripts/inventory_rustsec_heap_candidates.py`: commit-checked full RustSec and Rudra-PoC inventory generator.
- `scripts/run_rustsec_heap_harness.py`: contained materialization/build/native-run path.
- `scripts/run_rustsec_heap_experiment.py`: matched system/UniAlloc/typed-policy and feature-matched reclaim matrix orchestrator with Cargo-metadata provenance attestation.
- `scripts/run_rustsec_heap_sweep.py`: provenance-bound full-catalog preflight and execution wrapper.
- `scripts/run_rsh064_neon_witness.py`: pinned Neon addon runner using a real Node/V8 host.
- `scripts/run_rustsec_complete_scope.py`: historical all-53 replay, blocker-reattempt, and attempts-audit verifier.
- `scripts/summarize_rustsec_heap_expansion.py`: fail-closed merger for split system/typed expansion matrices.
- `scripts/export_rustsec_mechanism_results.py`: strict repeated reclaim and derived-reuse result exporter.
- `scripts/export_rustsec_typeiso_source_results.py`: coverage-gated Type Isolation source-sweep result exporter.
- `scripts/build_rustsec_security_scope.py`: fail-closed builder for the frozen pre-correction 49-case execution ledger, with reviewed exclusions preserved separately.
- `scripts/plot_rustsec_security_scope.py`: dependency-free exporter for the historical 49-case presentation.
- `scripts/plot_rustsec_security_sets.py`: hash-bound exporter for the corrected 48-case full-scope and 17-case executable-UAF set figures.
- `scripts/evaluate.py`: dependency doctor, paper-data importer, local runner, semantic coverage collector, result summarizer, and claim checker.
- `scripts/realworld_type_isolation_matrix.py`: source-pinned Rust application allocator, Type Isolation, feature-closure, and mimalloc THP matrix.
- `scripts/rsedis_thp_matrix.py`: fresh-server mimalloc THP on/off and gperftools TCMalloc comparison with process-level RSS and huge-page counters.
- `scripts/plot_allocator_feature_thp_slides.py`: deterministic Matplotlib exporter for three measured-target allocator/THP appendix figures, long-form plotted data, SVG/PNG/PDF assets, and a hash-bound manifest.
- `scripts/plot_type_isolation_primary_suite.py`: fail-closed Matplotlib exporter for the complete current-version seven-target overview. It emits title-free horizontal median bars only after every target, harness, source pin, variant, gate, and paired round passes validation.
- `scripts/plot_two_tier_allocator_evaluation.py`: canonical title-free microbenchmark and macrobenchmark presentation exporter with hierarchical aggregation and explicit RSS work-model boundaries.
- `paper-targets/`: generated reference summaries from the paper `.dat` files.
- `raw/`: local benchmark JSONL/CSV artifacts (ignored by git).
- `results/`: normalized summaries and claim-check JSON (ignored by git).
- `reports/`: Markdown/LaTeX summaries (ignored by git by default; copy selected final artifacts elsewhere before committing if needed).

## Allocator evaluation structure

The primary committee-facing evaluation follows one stable two-tier rule:

1. **Microbenchmarks** use Rust `std_bench` and report performance plus
   process-observed peak RSS. Fine-grained leaves are reduced within benchmark
   families before the eight families receive equal headline weight.
2. **Macrobenchmarks** use pinned real-world Rust programs and report
   performance plus peak RSS. Harnesses are reduced within each target before
   targets receive equal headline weight.
3. Type Isolation is a UniAlloc variant. `typed_plain` remains the matched
   compiler-route control, and `typeiso_perf` is UniAlloc + Type Isolation.

The canonical method, results, and source boundaries are in
`../docs/allocator-evaluation.md`. Regenerate the title-free presentation
figures with:

```bash
uv run evaluation/scripts/plot_two_tier_allocator_evaluation.py
```

The command writes the microbenchmark and macrobenchmark SVG/PNG figures,
uncapped long-form CSV data, compact presentation data, and a hash-bound
manifest under `../docs/figures/allocator-evaluation-20260714/`.

The full 468-leaf std-bench matrix and complete seven-target Type Isolation
suite remain detailed audit evidence. Collections belongs to the
microbenchmark section. Oxipng, redb, Polars, SWC, RustPython, and Actix Web
form the six-target macrobenchmark section. The older ripgrep/fd/Oxipng matrix
uses a different implementation digest and remains historical diagnostic
evidence outside the current primary aggregate.

The allocator/THP appendix pack under
`../docs/figures/allocator-feature-thp-20260714/` contains three measured-target
detail figures. It remains outside the primary two-tier presentation path.

## RustSec/Rudra heap-security corpus

The review initially screened **53 candidates**. Post-source inspection
reclassified RSH-006 as a stack-lifetime case: the vulnerability-relevant
dangling target/lifetime path has no `GlobalAlloc`-mediated allocation, reclaim,
or reuse edge. The corrected heap scope therefore retains **52 candidates**:
**48 executable** vulnerable-oracle/control integrations plus **4 audit-only**
exclusions (RSH-054, RSH-056, RSH-059, and RSH-073).

The corrected 48-case partition contains 31 other-feature-only cases, 11
Type-Isolation-only measured reuse edges, 1 overlap, 3 matched no-signal cases,
and 2 mechanism-boundary cases. The no-signal set is RSH-049, RSH-050, and
RSH-075; RSH-003 and RSH-019 exercise same-object or pre-reuse concurrency
outside the evaluated allocator contracts. Allocator mechanisms provide
qualified coverage for **43/48** executable candidates: 31 exact
`reclaim_checks` detections, 1 recovery-layout validation, and 12 bounded Type
Isolation reuse-edge mitigations, with RSH-002 counted once. The retained UAF
scope contains **18 candidates**, including **17 executable** cases; mechanisms
cover **14/17** executable UAF cases.

The 62-row mechanism ledger and generated 49-case report remain frozen
pre-correction execution provenance. They preserve the RSH-006 inconclusive row
and their historical **43/49** count. The authoritative corrected report is in
`../docs/rustsec-security-scope-evaluation.md`; editable set figures, exact
memberships, and PNG/PDF fallbacks are in
`../docs/figures/rustsec-security-sets-20260715/`. The historical all-53 attempt
ledger remains in `../docs/rustsec-all-53-live-evaluation.md`.

All 12 current Type Isolation-positive cases are compiler-bound derived or
source-shaped cross-identity reuse edges. Their strict true-positive scope is
the tested allocator decision: `typed_plain` permits the witness address reuse,
while Type Isolation withholds that exact retained pointer and emits the bound
denial. Automatic source-level vulnerability detection remains **0/12**; every
row records `source_vulnerability_detection_validated=false` and
`vulnerability_specific_detection_signal=false`.

RSH-064 is executable through a pinned Neon N-API addon loaded by Node. The
vulnerable source reproduces the stale external-buffer read across all five
allocator variants; the patched crate rejects the source through its added
`'static` bound. Its source Type Isolation row remains inconclusive because the
compiler audit records zero applied critical rewrites, and its supporting
`reclaim_checks` arm has no signal because the witness performs one free followed
by a stale read. A separate compiler-bound derived cross-identity reuse
experiment supplies the bounded Type Isolation mitigation row used by the
case-level partition. The source witness retains its independent inconclusive
automatic-detection classification.

RSH-031 supplies the additional policy-independent exact diagnostic. Its
recovery-layout matrix reports the allocation/deallocation layout mismatch in
every vulnerable typed arm and in zero patched arms, with a pinned upstream
Miri baseline. It contributes zero Type Isolation credit. RSH-065 and RSH-069
carry explicit synthetic-reduction caveats within the 12 bounded Type Isolation
edges.

The frozen manually annotated Type Isolation calibration matrices bind to an
isolated WIP evidence snapshot with UniAlloc implementation digest
`ce653fd5c35e2d6b912b7f8111e947cce6af29a78284cd57ea45d9bc347ab9c2`.
The current compiler-bound provenance epoch is
`../docs/evidence/rustsec-typeiso-automatic-20260715/`. The complete report
keeps
`replay_arm_implementation_digest_counts` separate from
`strict_typeiso_evidence_implementation_digest_counts` so historical replay
provenance and Type Isolation snapshot provenance remain distinct.

The scope freezes RustSec advisory-db at
`9f3e138091487e69144f536d36976e427a7a3307` and Rudra-PoC at
`6226dd030fffbed5601099cb0e24f73e4150a7f5`. The corrected 52-case heap scope
contains 30 double free, 18 use after free, 2 uninitialized drop, 1 invalid
free, and 1 out-of-bounds read witness. Every result remains exploratory with
`claim_grade=false`.

The feature-matched reclaim comparison uses six logical arms per scenario:
`vulnerable,patched x system,reclaim_plain,reclaim_checks`, with three
requested repetitions. The original direct campaign covers 42 scenarios, 168
arms, 492 runtime executions, and 4 expected matched compile-rejection arms. Cargo
metadata resolves `reclaim_plain` to `['stats']` and `reclaim_checks` to
`['reclaim_checks', 'stats']`; those direct arms use implementation digest
`36bc040455f8c5fa6142a91b2321bc9d018aad08764f7d8c8e5554b3e4460847`.
The deterministic RSH-013 refresh uses implementation digest
`b9bd5442c557b3d39c34cf391e8c32388ba84ea643e6adc91ea4987d85adbc5a`.
The original direct exporter accepts all 29 vulnerable check-arm exact signals;
the exact signal remains absent from every `reclaim_plain` and patched arm.
Supplemental strict RSH-001 and RSH-060 experiments raise the final
reclaim-positive case count to 31.
RSH-013 now uses a deterministic terminal `drop(values)` so corrupted string
formatting cannot preempt the allocator lifecycle. RSH-020 uses a fail-closed
double-panic parser: it accepts repeated identical source checkpoints followed
by the standard destructor-cleanup abort, while distinct checkpoints remain
ambiguous. The frozen pre-correction mechanism ledger retains 12 no-signal rows
and 6 inconclusive rows: 5 Type Isolation source rows behind compiler-coverage
gates and the historical RSH-006 reclaim row. The corrected heap allocator
efficacy view excludes RSH-006 because its vulnerability-relevant dangling
target/lifetime path has no `GlobalAlloc`-mediated allocation, reclaim, or
reuse edge.

The feature campaign is retained in
`../docs/evidence/rustsec-reclaim-checks-20260714/feature-matched-campaign.json`.
Its strict merged inputs and output fragments are
`summary-feature-matched-*.json` and
`mechanism-results-feature-matched-*.json` in the same directory.

Rebuild the frozen pre-correction execution ledger with every reviewed
amendment included:

```bash
args=(
  --catalog evaluation/config/rustsec_heap_harnesses.json
  --catalog evaluation/config/rustsec_heap_expansion_harnesses.json
  --catalog evaluation/config/rustsec_heap_neon_node_harnesses.json
)
for batch in a b c d e f; do
  args+=(--catalog "evaluation/config/rustsec_heap_strong_batch_${batch}_harnesses.json")
  args+=(--status "evaluation/config/rustsec_heap_strong_batch_${batch}_status.json")
done

uv run python evaluation/scripts/build_rustsec_security_scope.py \
  "${args[@]}" \
  --primitive-overrides evaluation/config/rustsec_heap_primitive_overrides.json \
  --mechanism-amendments evaluation/config/rustsec_heap_mechanism_amendments.json \
  --mechanism-results evaluation/config/rustsec_heap_mechanism_results.json \
  --output evaluation/config/rustsec_heap_complete_scope.json \
  --require-terminal-integration
```

Verify the historical live-attempt inputs and regenerate the frozen
pre-correction 49-case view plus its separate four-case exclusion audit. These
files preserve execution provenance; current presentation accounting applies
`config/rustsec_heap_posthoc_scope_corrections.json` and uses the corrected
figures under `../docs/figures/rustsec-security-sets-20260715/`.

```bash
uv run python evaluation/scripts/run_rustsec_complete_scope.py \
  --scope evaluation/config/rustsec_heap_complete_scope.json \
  --base-scope docs/evidence/rustsec-all-53-live-20260714/scope-before-rsh064.json \
  --executable-replay docs/evidence/rustsec-all-53-live-20260714/executable-replay-48.json \
  --blocked-attempts docs/evidence/rustsec-all-53-live-20260714/blocked-attempts/summary.json \
  --supplemental-executable docs/evidence/rustsec-all-53-live-20260714/rsh064/experiment.json \
  --output-json docs/evidence/rustsec-all-53-live-20260714/evaluable-49-report.json \
  --output-csv docs/evidence/rustsec-all-53-live-20260714/evaluable-49-cases.csv
```

The 40-case classification corpus is a purposive, source-ready pilot. The
complete pinned database contains 1,140 advisory records. The expanded screen
initially identified 53 candidates, including 36 absent from the pilot; the
post-source audit retains 52 heap-relevant candidates. The complete funnel,
manual classification, exclusion reasons, and 19-case first expansion wave are in
`../docs/rustsec-heap-corpus-expansion.md`.

Regenerate the full screening inventory before changing the curated corpus:

```bash
uv run python evaluation/scripts/inventory_rustsec_heap_candidates.py \
  --rustsec-db /path/to/advisory-db \
  --rudra-poc /path/to/Rudra-PoC \
  --output evaluation/config/rustsec_heap_candidate_inventory.json
uv run python -m unittest -v \
  evaluation/scripts/test_inventory_rustsec_heap_candidates.py
```

The frozen classification pilot contains 40 advisories. Its source-pinned
inventory contains 45 scenarios across all 40 distinct advisories, or 100%
source availability at the advisory level:

- 18 external source PoCs from the pinned Rudra-PoC snapshot;
- 27 scenarios in 22 repository bundles: 24 mechanically adapted
  (`mechanical_adapter`) scenarios grounded in published, upstream, or advisory
  sources and 3 derived (`derived_adapter`) scenarios. Two derived scenarios
  exercise cross-identity reuse and one is a minimal calamine CFB adapter.

Scenarios and advisories use separate denominators. Multi-scenario cases and
separately derived adapters raise the source-pinned scenario count above the 40
source-covered advisories. All 40 advisories now have a source-pinned path, and
the five extra scenarios bring the source-pinned scenario count to 45. The corpus
preregisters three reuse hypotheses in total; two have repository reuse-derived
sources. The third derived adapter in the pilot is a calamine CFB fixture.
Published witnesses establish source-vulnerability baselines and
allocator-visible diagnostics. Exact-type reuse efficacy uses a separate,
coverage-qualified derived denominator.

The mutable live harness catalog adds the supplemental
`RSH-031-derived-layout-validation` adapter outside the frozen sweep. Its
current inventory is 46 source-pinned scenarios: 18 Rudra source PoCs plus 28
repository scenarios in 22 bundles, comprising 24 mechanical and 4 derived
adapters. The historical sweep denominator remains 27 repository scenarios.

The executable expansion adds 5 advisory IDs and 7 repository scenarios: 5
published/upstream witnesses and 2 derived reuse probes. Across the current
pilot catalog and expansion catalog, the repository records 45 advisory IDs,
53 source-pinned scenarios, and 35 repository scenarios in 27 bundles. The
frozen sweep, supplemental layout-validation adapter, and expansion retain
separate experiment matrices and outcome denominators.

The pilot harness catalog remains `claim_grade=false`. The frozen final sweep
reproduced the 27 repository-materializable baselines and patched controls and
retained their matched-arm artifacts. Exact critical-site compiler/runtime
coverage remains unvalidated, and the 18 Rudra-PoC-only programs remain
source-only inventory.

Audit the manifests and repository bundle:

```bash
python3 evaluation/scripts/audit_rustsec_heap_corpus.py
python3 evaluation/scripts/audit_rustsec_heap_corpus.py \
  --rustsec-db /path/to/advisory-db \
  --rudra-poc /path/to/Rudra-PoC \
  --output evaluation/results/rustsec_heap_corpus_audit.json
python3 -m unittest -v evaluation/scripts/test_rustsec_heap_corpus.py
```

List, materialize, or compile-check a repository scenario:

```bash
python3 evaluation/scripts/run_rustsec_heap_harness.py --action list
python3 evaluation/scripts/run_rustsec_heap_harness.py \
  --action materialize --scenario RSH-002-published --variant vulnerable \
  --allow-download --work-dir /tmp/unialloc-rsh-002-vulnerable
python3 evaluation/scripts/run_rustsec_heap_harness.py \
  --action check --scenario RSH-002-published --variant vulnerable \
  --allow-download
```

Native execution requires explicit unsafe opt-in and uses a network-disabled,
read-only, capability-dropped, resource-limited run container:

```bash
python3 evaluation/scripts/run_rustsec_heap_harness.py \
  --action run-native --scenario RSH-016-advisory --variant vulnerable \
  --allow-download --execute-unsafe --timeout 30
```

Run the matched allocator matrix through the separate experiment orchestrator.
`list` is the default read-only action. `preflight` materializes and validates
every selected arm without compiling or executing the witness:

```bash
python3 evaluation/scripts/run_rustsec_heap_experiment.py --action list
python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --action preflight \
  --scenario RSH-002-derived-reuse \
  --variants system,unialloc,typed_plain,typeiso \
  --archive-variants vulnerable,patched \
  --repetitions 2 \
  --cache "${XDG_CACHE_HOME:-$HOME/.cache}/unialloc/rustsec-heap" \
  --allow-download \
  --output-dir evaluation/raw/rustsec-heap-preflight
```

`run` requires the explicit unsafe-execution opt-in. It records each build and
run command, allowlisted environment, source/lock/tool hashes, per-repetition
logs, compiler audits, runtime statistics, and result classification under one
dedicated output directory:

```bash
python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --action run \
  --scenario RSH-002-derived-reuse \
  --variants system,unialloc,typed_plain,typeiso \
  --archive-variants vulnerable,patched \
  --repetitions 10 \
  --cache "${XDG_CACHE_HOME:-$HOME/.cache}/unialloc/rustsec-heap" \
  --allow-download \
  --output-dir evaluation/raw/rustsec-heap-rsh002 \
  --execute-unsafe
```

`RSH-002-derived-reuse` carries an explicit manual identity annotation for the
victim allocation and reclaim. The treatment wrapper emits
`UNIALLOC_SECURITY_REUSE_DENIAL` immediately after the distinct replacement
allocation, before the stale object is dropped. The matrix validator binds the
reported requested `(type_id, module_id, callsite)` to the unique compiler audit
row for `Box<Replacement>` and records the retained victim identity plus exact
layout. This row evaluates the allocator's cross-identity reuse policy;
automatic victim-site compiler coverage remains a separate gate. The patched
control also exercises the safe version of the same reuse decision, allowing
the result to distinguish reuse-edge enforcement from vulnerability-specific
detection.

Run the five-case expansion through the same orchestrator with its explicit
catalog:

```bash
python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --catalog evaluation/config/rustsec_heap_expansion_harnesses.json \
  --action list

python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --catalog evaluation/config/rustsec_heap_expansion_harnesses.json \
  --action run --scenario RSH-041-advisory \
  --variants system --archive-variants vulnerable,patched \
  --output-dir /tmp/rustsec-expansion-rsh041-system \
  --allow-download --execute-unsafe --jobs 4 --repetitions 3

python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --catalog evaluation/config/rustsec_heap_expansion_harnesses.json \
  --action run --scenario RSH-041-advisory \
  --variants typed_plain,typeiso --archive-variants vulnerable,patched \
  --output-dir /tmp/rustsec-expansion-rsh041-typed \
  --allow-download --execute-unsafe --jobs 4 --repetitions 3
```

The completed expansion reproduced all five vulnerable system baselines and
all five matched patched controls. Three double-reclaim witnesses emitted the
same pointer-already-released signal under `typed_plain` and `typeiso`, assigning
that observation to common UniAlloc tracking. The two published UAF witnesses
produced no Type-Isolation-specific signal. The separately derived RSH-041 and
RSH-042 probes both observed `typed_plain` address reuse 3/3 and `typeiso`
non-reuse plus a matching denial 3/3. Both derived probes use manual victim
identity attribution, so automatic compiler coverage and source-level
vulnerability detection remain outside their result.

The complete matrices and compact summaries are retained at
`../docs/evidence/rustsec-security-expansion-20260714/`. Summarize split
published matrices with repeated `CASE=path` inputs:

```bash
python3 evaluation/scripts/summarize_rustsec_heap_expansion.py \
  --catalog evaluation/config/rustsec_heap_expansion_harnesses.json \
  --experiment RSH-041=/path/to/system/experiment.json \
  --experiment RSH-041=/path/to/typed/experiment.json \
  --output /tmp/rustsec-expansion-summary.json
```

Run all 27 frozen-pilot repository scenarios through the hardened sweep
wrapper. The wrapper validates the complete Cartesian arm matrix, removes stale
result manifests on fresh runs, binds successful resume state to result and
input hashes, isolates worker failures, and invalidates the summary if the
catalog, runner, sweep script, or UniAlloc implementation changes during the
run:

```bash
CARGO_HOME="$HOME/.cache/unialloc/rustsec-heap/cargo" \
uv run python evaluation/scripts/run_rustsec_heap_sweep.py \
  --action preflight \
  --cache "$HOME/.cache/unialloc/rustsec-heap" \
  --output-dir evaluation/raw/rustsec-heap-full-preflight \
  --scenario-jobs 1 \
  --jobs 4 \
  --repetitions 2 \
  --build-timeout 1200 \
  --run-timeout 600

CARGO_HOME="$HOME/.cache/unialloc/rustsec-heap/cargo" \
uv run python evaluation/scripts/run_rustsec_heap_sweep.py \
  --action run \
  --cache "$HOME/.cache/unialloc/rustsec-heap" \
  --output-dir evaluation/raw/rustsec-heap-full-sweep \
  --scenario-jobs 1 \
  --jobs 4 \
  --repetitions 2 \
  --build-timeout 1200 \
  --run-timeout 600 \
  --execute-unsafe
```

`scenario-jobs=1` is the provenance-clean default and prevents scenario-level
cache competition. `--allow-download` requires that sequential setting. The
2026-07-14 final run completed all 27 scenarios and all 216 terminal arms, with
352 runtime executions, 32 expected compile-rejection arms, 8 preregistered
topology exclusions, and 0 unexpected arms. All 27 vulnerable system oracles
and all 27 patched system controls reproduced. See
`docs/evidence/rustsec-security-evaluation-20260714/` for the result hashes and
the non-claim-grade interpretation boundary.

The system arm runs the cataloged ASan or Miri oracle. Allocator arms run as
native diagnostics so sanitizer/interpreter behavior cannot be mistaken for an
allocator effect. `typed_plain` and `typeiso` use one force-loaded UniAlloc
rlib across both the subject crate and harness; the runner rejects a mixed
direct-dependency/force-loaded identity. The runner also detects subject-owned
`#[global_allocator]` topology. The `system` arm preserves that allocator;
allocator-substitution arms are marked as expected unsupported when adding a
second allocator would make ownership ambiguous. This is the intended RSH-030
shape because the fixed subject owns MiMalloc. RSH-028 separately excludes only
the direct raw-UniAlloc route because its exact historical libc pin conflicts
with UniAlloc's exact libc dependency; both force-loaded policy arms remain
supported. Historical-Miri system arms preserve Rust's default allocator, and
the runner records any compatibility flags removed for the pinned old compiler.
Each Type Isolation arm build starts from a reset target so compiler audits
regenerate instead of reusing stale audit files. Clean allocator runs must
include valid UniAlloc runtime statistics; abnormal early exits record
statistics as unavailable. Historical crate build scripts and memory-unsafe
witnesses execute on the host in this path. Run the command inside a disposable
VM for the claim-grade containment boundary.

The audit verifies upstream snapshot pins, RustSec/Rudra metadata, vulnerable
version ranges, archive metadata and cross-references, repository
source/lock/patch hashes, cross-manifest case identity, frozen taxonomy floors,
and claim boundaries. Experiment compiler audits validate target-crate presence
and force-load topology. Critical-site coverage is a separate efficacy gate.
All arms in the frozen 27-scenario pilot sweep and all ten input matrices for
the five published expansion scenarios stay `efficacy_eligible=false`. The two derived expansion probes report
manually attributed policy-edge evidence and retain `claim_grade=false`. The
runner verifies the byte count and SHA-256 of each actual cached or
downloaded crate archive before extraction. The native runner does not provide
ASan or Miri; those scenarios require the cataloged dedicated environment and
remain separate ground-truth arms because instrumentation can change allocator
behavior. See
`harnesses/rustsec_heap/README.md` for the integrity and containment contract,
and `docs/type-isolation-security-evaluation.md` for the mechanism matrix and
matched-arm protocol.

## Quick start

```bash
python3 evaluation/scripts/evaluate.py doctor
python3 evaluation/scripts/evaluate.py import-paper --paper-dir "${UNIALLOC_RUST_ALLOC_PAPER:-../rust-alloc-paper}"
python3 evaluation/scripts/evaluate.py claim-check --source paper
python3 evaluation/scripts/evaluate.py collect-coverage --run-id semantic-smoke
python3 evaluation/scripts/evaluate.py collect-abi-coverage --run-id compiler-abi-smoke
python3 evaluation/scripts/evaluate.py collect-scoped-std-coverage --run-id scoped-std-smoke
python3 evaluation/scripts/evaluate.py collect-semantic-std-bench-coverage --run-id semantic-std-bench-smoke
python3 evaluation/scripts/evaluate.py collect-std-bench-auto-coverage --run-id std-bench-auto-smoke --bench-filter semantic_auto_metadata
python3 evaluation/scripts/evaluate.py collect-std-bench-auto-coverage --run-id std-bench-compiler-site-replay-full --timeout 2400 --compiler-site-replay-type-mapping evaluation/raw/rustc-mir-actual-std-bench-target-20260612/rustc-mir-type-mapping.json --no-import-results
python3 evaluation/scripts/evaluate.py audit-compiler-proto-instrumentation --run-id compiler-proto-instrumentation-audit
python3 evaluation/scripts/evaluate.py collect-compiler-proto-std-bench-coverage --run-id compiler-proto-smoke --only-with-sentinels binary_heap::bench_push
python3 evaluation/scripts/evaluate.py generate-compiler-coverage-manifest-template --manifest-out evaluation/config/compiler_coverage_manifest.template.json --run-id compiler-coverage-template
python3 evaluation/scripts/evaluate.py package-current-compiler-coverage-manifest --run-id compiler-coverage-current-prototype
python3 evaluation/scripts/evaluate.py package-std-bench-compiler-site-replay --run-id std-bench-compiler-site-replay-full-packaged --replay-summary evaluation/raw/std-bench-compiler-site-replay-full/std-bench-auto-summary.json
python3 evaluation/scripts/evaluate.py audit-compiler-coverage-evidence --manifest /path/to/compiler-coverage-manifest.json --run-id compiler-coverage-audit
python3 evaluation/scripts/evaluate.py import-compiler-coverage --manifest /path/to/compiler-coverage-manifest.json --run-id compiler-coverage-import
python3 evaluation/scripts/evaluate.py audit-paper-performance-targets --run-id paper-performance-targets
python3 evaluation/scripts/evaluate.py generate-paper-performance-wrapper-contracts --run-id paper-wrapper-contracts
python3 evaluation/scripts/evaluate.py audit-paper-collections-allocator-preflight --run-id paper-allocator-preflight
python3 evaluation/scripts/evaluate.py generate-paper-external-workload-bundle-template --run-id paper-external-bundle-template
python3 evaluation/scripts/evaluate.py audit-paper-external-workload-bundle --bundle evaluation/config/paper_external_workloads.template.json --run-id paper-external-bundle-audit
python3 evaluation/scripts/evaluate.py generate-paper-workload-wrapper-manifest-template --run-id paper-wrapper-manifest-template
python3 evaluation/scripts/evaluate.py audit-paper-workload-wrapper-manifest --manifest evaluation/config/paper_workload_wrappers.template.json --run-id paper-wrapper-manifest-audit
python3 evaluation/scripts/evaluate.py generate-paper-performance-plan --dataset default_performance --run-id paper-plan-skeleton-default
python3 evaluation/scripts/evaluate.py generate-paper-performance-plan --dataset default_performance --local-collections-only --driver-bench-filter binary_heap::bench_push --run-id paper-plan-local-collections-smoke
python3 evaluation/scripts/evaluate.py generate-paper-performance-plan --dataset default_performance --collections-driver --driver-bench-filter binary_heap::bench_push --run-id paper-plan-collections-smoke
python3 evaluation/scripts/evaluate.py audit-paper-performance-plan --plan /path/to/paper-plan.json --run-id paper-plan-audit --write-missing-template /tmp/paper-plan-missing.json
python3 evaluation/scripts/evaluate.py run-paper-performance-plan --plan /path/to/paper-plan.json --run-id paper-plan-run --keep-going --resume-successful
python3 evaluation/scripts/evaluate.py import-paper-performance-samples --samples /path/to/paper-run-samples.jsonl --run-id paper-run-import --import-results
python3 evaluation/scripts/evaluate.py export-current-datasets --run-dir evaluation/raw/local-variant-smoke --paper-shape --import-results
python3 evaluation/scripts/evaluate.py merge-current-datasets --data-dir evaluation/raw/local-variant-smoke/data --data-dir evaluation/raw/paper-run-import/data --run-id current-merged --import-results
python3 evaluation/scripts/evaluate.py claim-check --source current
python3 evaluation/scripts/evaluate.py overclaim-worklist --source current --refresh-claim-check
```

`--source paper` validates that the framework can parse and audit the paper reference data. It does **not** prove current UniAlloc performance. Current-run claims require `run-local`/imported raw artifacts under `evaluation/raw/` followed by `summarize --source current`.

`overclaim-worklist` aggregates `claim-check --source current` with the latest coverage, paper-performance, wrapper-contract, Collections allocator preflight, external-workload-bundle, wrapper-manifest, and platform preflight artifacts. It writes `evaluation/results/overclaim_worklist.json`, `evaluation/reports/overclaim_worklist.md`, and `evaluation/raw/<run-id>/overclaim-worklist.json`. The command returns nonzero while required claims still need current evidence, making it a concise next-action dashboard rather than another way to pass an overclaim gate.

`collect-coverage` runs the in-tree `semantic_coverage` example and imports its JSONL allocation events into `results/coverage_summary.json`. This is a smoke test for semantic/fallback accounting, type-cache reuse, delayed-free policy accounting, and typed/fallback byte accounting; it is intentionally not treated as the full compiler-instrumented Rust `alloc` benchmark coverage result claimed by the paper.

`collect-abi-coverage` runs the in-tree `compiler_abi_coverage` example. That harness calls the exported C ABI hooks from standard-library-shaped proxy patterns (`Box`/`Vec`/collection-node/string-map-like allocations), records typed/fallback object and byte counters, and exercises cache/delayed-free policy accounting through the ABI surface. It is stronger integration evidence than the direct Rust smoke harness, but it is still a proxy: it is not a rustc/LLVM pass and remains `claim_grade=false`.

`collect-scoped-std-coverage` runs the in-tree `scoped_std_coverage` example. That harness executes real standard collection operations while a thread-local semantic metadata scope is active, so ordinary `GlobalAlloc` calls inherit typed metadata without calling the semantic allocator API directly. This validates the runtime mechanism a compiler pass would need, but it is still source-scoped prototype evidence rather than an automatic rustc/LLVM rewrite over the complete standard `alloc` benchmark suite.

`collect-semantic-std-bench-coverage` runs the `semantic_std` libtest benchmark target through `cargo bench --bench semantic_std -- --nocapture`. The benchmark uses the same scoped metadata runtime path inside real `Bencher::iter` loops and emits JSON coverage events alongside libtest timing output. It now samples the main in-tree standard allocation categories (`vec`, `string`, `vec_deque`, `binary_heap`, `btree_map`, `btree_set`, `linked_list`, `slice`, and a mixed multi-type compiler-scope surface) and records those labels in `event_workload_categories`. Use `--bench-filter <substring>` for focused collection while developing a specific scoped workload. This is closer to the paper's standard `alloc` benchmark evidence surface than examples, but remains non-claim-grade until an automatic compiler pass instruments the complete benchmark suite.

`collect-std-bench-auto-coverage` runs the existing `std_bench` target with process-wide runtime layout auto-metadata enabled by the bench harness. This path requires no source wrappers around each collection benchmark, records both libtest timing rows and the final coverage counters, and is useful for unmodified-benchmark integration smoke coverage. Use repeated `--skip <filter>` for known-crashing libtest benchmarks while preserving the sentinel/report benchmarks; skipped filters are recorded in the raw summary and keep the run non-claim-grade. For focused crash isolation, prefer `--only-with-sentinels <substring[,substring...]>`: it lists all benchmarks, skips every non-matching benchmark, and keeps the auto-metadata enable/report sentinels in the run. Failed runs also record `bench_diagnostics` with FAILED benchmark names, the last completed/seen benchmark, and stderr signal/panic/assertion tails so the next run can resume from concrete evidence. It is intentionally not claim-grade: without `--compiler-site-replay-type-mapping`, the auto IDs are derived from `(size, align)`, not compiler-assigned Rust type IDs.

`collect-std-bench-auto-coverage --compiler-site-replay-type-mapping <rustc-mir-type-mapping.json>` feeds the audited MIR allocation-site `type_id` rows into the runtime auto-metadata ABI through `UNIALLOC_COMPILER_SITE_TYPE_IDS`. This validates that ordinary `std_bench` `GlobalAlloc` calls can carry compiler-assigned allocation-site object type IDs at runtime. `--compiler-site-id-mode cyclic-replay` preserves the full-surface high-coverage bridge used by the current packaged evidence; `--compiler-site-id-mode consuming-stream` consumes each id once and leaves later allocations untyped so short or misaligned runtime streams are visible instead of silently wrapping. `package-std-bench-compiler-site-replay` then combines that runtime summary with `evaluation/results/rustc_mir_allocation_site_audit.json` and refreshes `evaluation/results/std_bench_compiler_site_replay_audit.json` plus `evaluation/results/compiler_coverage_evidence_audit.json`. The current bridge evidence is explicit preflight evidence only: both cyclic replay and finite bench-harness streams are not exact dynamic allocation-site attribution. The full 2026-06-12 run observes all 430 expected non-synthetic `std_bench` names, which removes the benchmark-surface gap for this bridge while keeping the evidence non-claim-grade until exact compiler-instrumented attribution and paper-equivalent status exist.

`collect-compiler-proto-std-bench-coverage` generates a temporary source-to-source benchmark target from the existing `std_bench` modules. The generator wraps literal and macro-template `#[bench]` functions in semantic metadata scopes, then runs the generated target and imports the final counters as `std_bench_compiler_proto` events. For modules with unambiguous collection allocation types, the wrapper now uses Rust type-derived metadata via `with_rust_type_metadata_at::<T>()`; audited string traversal benchmarks that operate only on literals/slices are marked `known-no-heap`; and the allocation-bearing slice macro templates (`sort!`, `sort_strings!`, `rotate!`, and `sort_lexicographic!`) are split into value-inferred scopes so setup, clone/reference collection, string-key, cached-key-buffer, and size-probe allocations inherit Rust type metadata after macro substitution. Current generated manifests can reach zero benchmark-scope fallback IDs. Each run saves the generated source tree and `instrumentation-manifest.json` under `evaluation/raw/<run-id>/generated-source/` so the rewrite, each inferred type, each value-inferred scope, each no-heap classification, and any fallback is auditable. This is stronger than layout-auto smoke because IDs are derived from source-inferred or value-inferred Rust collection types rather than `(size, align)`, and it exercises an automatic rewrite over the existing benchmark source. It still remains `claim_grade=false`: it is not a rustc/LLVM/MIR pass, inferred types are module/function/value-wrapper heuristics rather than exact allocation-site object types, and source/value-inferred wrapper scopes are not compiler allocation-site type IDs.

`audit-compiler-proto-instrumentation` runs the same source-to-source generator without executing libtest. It writes `evaluation/raw/<run-id>/compiler-proto-instrumentation-audit.json` plus the generated source/manifest, then removes the temporary bench target unless `--keep-generated` is used. Use this fast audit before long coverage runs to inspect `rust_type_inferred_count`, `known_no_heap_scope_count`, `typed_or_known_no_heap_scope_count`, `scope_id_fallback_count`, `allocation_semantics_counts`, per-module inferred types, audited no-heap scopes, macro template names, and the exact fallback function list. The slice macro audit currently types `reverse!` through its `$ty` parameter, `sort_expensive!` through its monomorphic `gen_random` expansion, and value-infers `sort!`, `sort_strings!`, `rotate!`, and `sort_lexicographic!`; the latest audit has no remaining allocation-bearing benchmark-scope fallback entries. Add `--fail-on-fallback` when using it as a CI gate for eliminating allocation-bearing benchmark-scope fallback IDs.

`audit-paper-performance-targets` writes a machine-readable run-plan/gap audit for the C001/C003-C006 paper performance datasets. It parses the paper `.dat` reference rows/columns, maps each row to the current in-tree runner capability, maps each allocator column to `run-local` feature support, records the six-run paper methodology, and snapshots whether any imported current dataset is already claim-grade. The default output is `evaluation/raw/<run-id>/paper-performance-targets.json` plus a Markdown report; `evaluation/results/paper_performance_targets.json` is refreshed for the latest audit. This is the preflight that tells a complete runner or imported artifact exactly which rows (`Collections`, `RJS-Compiler`, `RPython`, `R-Polars`, `R-Oxipng*`, `RRedis*`) and columns (`jemalloc`, `ptmalloc`, `mimalloc`, `tcmalloc`, `snmalloc`, `scudo`) must exist before performance claims can pass. Add `--write-sample-template /tmp/paper-samples.jsonl` to emit one fill-in JSONL record for every required dataset/row/allocator/run sample, including the `unialloc` denominator cells and evidence SHA-256 placeholders expected by `import-paper-performance-samples`. Template rows carry `template_only=true`, which is an explicit claim-grade blocker until removed after real timing and raw-evidence provenance are filled.

`generate-paper-performance-wrapper-contracts` writes `evaluation/results/paper_performance_wrapper_contracts.json`, a per-cell wrapper contract and capability matrix for the five C001/C003-C006 performance datasets. The contract separates locally runnable `Collections` cells from external macro rows, local host blockers such as Darwin `ptmalloc`, optional local dependency cells such as `tcmalloc` when either `UNIALLOC_TCMALLOC_LIB_DIR` points at a usable `libtcmalloc` or repo-local auto-discovery finds `evaluation/deps/tcmalloc/lib` / `evaluation/raw/local-tcmalloc-dep-build-*/prefix/lib`, and toolchain/runtime-blocked cells such as `scudo`; it also records the required command shape, timing field, sample fields, provenance roles, and evidence digests for every required `(dataset, benchmark, allocator)` cell. This artifact is not timing evidence. It is the handoff checklist for converting placeholder plan entries into real Linux/default/type/meta/hugepage/PAC workload wrappers before `run-paper-performance-plan`.

`audit-paper-collections-allocator-preflight` writes `evaluation/results/paper_collections_allocator_preflight_audit.json`, a host/library/link feasibility audit for the paper `Collections` allocator cells. It records each allocator's required cell count, Cargo feature mapping, declared-feature status, host/Libc probe, package/library probes, configured local dependency paths, and optional narrow `cargo bench --test` link probes via `--link-probe-allocator <allocator>`. The current Darwin/aarch64 preflight can treat `tcmalloc` as locally feasible when `UNIALLOC_TCMALLOC_LIB_DIR` / `UNIALLOC_CMAKE_BIN` are set or when repo-local auto-discovery finds `evaluation/raw/local-tcmalloc-dep-build-*/prefix/lib` and `evaluation/raw/local-cmake-pip-*/bin/cmake`; the focused auto-discovery refresh reports 35 `Collections` cells across seven allocators, 25 locally feasible cells (`unialloc`, `jemalloc`, `mimalloc`, `tcmalloc`, `snmalloc`), a passing narrow `tcmalloc` link probe, and 10 cells still blocked by Linux/glibc-only `ptmalloc` plus current-toolchain `scudo` sanitizer/runtime support. The audit is not timing evidence; it keeps host/dependency failures explicit before wrapper or plan artifacts can treat those cells as runnable.

`generate-paper-external-workload-bundle-template --all-paper-data` writes `evaluation/config/paper_external_workloads.template.json`, a 19-rule bundle for every paper row that is not locally runnable through the in-tree `Collections` driver. The current all-paper-data template spans nine paper datasets and 338 external cells: the original macro families (`RJS-Compiler`, `RPython`, `R-Polars`, `R-Oxipng*`, and `RRedis*`) plus web/platform rows such as `actix`, `rocket`, `tokio`, and `warp`. The template is grouped by benchmark family rather than by every allocator cell, so each rule remains a checkout/command slot rather than a synthetic timing source. `generate-paper-external-workload-adapters` then creates strict per-family scaffolds under `evaluation/external/<family>/`: `run_paper_workload.py` delegates to `evaluation/scripts/paper_external_workload_adapter.py`, `workload_config.template.json` documents the real checkout/command contract, and the adapter refuses to emit timing unless a local config is explicitly marked `configured=true` and the real benchmark command prints finite JSON timing. The generated templates include paper-source hints from `rust-alloc-paper` (`SWC v1.2.51`, `RustPython v0.1.2`, `Polars v0.13.0`, `Oxipng v4.0.3`, and `Rsedis`) plus upstream URLs. `audit-paper-external-workload-source-hints --paper-repo "${UNIALLOC_RUST_ALLOC_PAPER:-../rust-alloc-paper}" --run-id paper-source-provenance-scan-20260612b` writes `evaluation/results/paper_external_workload_source_hints.json` and makes missing authoritative upstream/ref mappings explicit before checkout fetch or command drafting; the current audit records 19/19 fetch-ready source-hinted families, 19/19 concrete `rust-alloc-paper` row/table matches, 4 paper-version-evidenced exact-ref families, 13 crate-metadata-backed families, 19/19 rules with embedded `paper_source` metadata in the bundle artifact, 58 scanned paper files, and 15 HEAD-only mappings that are now gap-audited but still need exact paper version/ref provenance. `fetch-paper-external-workload-checkouts` resolves those source hints through `git ls-remote`, optionally fetches the selected refs under the ignored `evaluation/external/_checkouts/` root, and writes `evaluation/results/paper_external_workload_checkouts.json`; fetched checkouts are source-readiness artifacts, not timing evidence. The current fetch artifact resolves and checks out 19/19 source families under `evaluation/external/_checkouts/`: exact refs for SWC `v1.2.51`, Polars `py-0.13.0` as the paper `v0.13.0` source, and Oxipng `v4.0.3`, plus Rsedis `HEAD`, 14 crate-registry-derived web/platform `HEAD` candidates, and RustPython fallback tag `0.1.0` because upstream does not expose the paper's `v0.1.2` / `0.1.2` tag. The RustPython fallback and fetched web/platform rows remain non-exact paper provenance and are not timing evidence. `discover-paper-external-workload-checkouts` scans local search roots for likely checkouts of those upstream repositories and writes `evaluation/results/paper_external_workload_checkout_discovery.json`; the current `_checkouts` discovery finds candidate project directories for 19/19 families and selects 19/19 families for adapter config after preferring high-confidence project roots in monorepos. Discovery is only a readiness aid, not timing evidence. `audit-paper-external-workload-source-surfaces` scans fetched checkouts without executing them, records root/workspace Cargo packages, binary targets, bench files, package scripts, README command hints, and candidate build/run/bench command surfaces in `evaluation/results/paper_external_workload_source_surfaces.json`; the latest artifact scans 19/19 fetched families and finds 273 command-surface hints across 19 checkout-ready families. FAF is scanned through the sparse TechEmpower `frameworks/Rust/faf` subdir with `faf-ex` binary command hints, and RustPython fallback `0.1.0` exposes the `rustpython` binary plus `bench` target as non-exact, non-claim command surfaces. The historical `draft-paper-external-workload-command-configs` artifact (`paper-external-command-config-drafts-rpython-fallback-safe-20260612`) preserved the 18 bridge configs that existed at that time and wrote a protected RustPython fallback draft; the active state has since promoted RustPython into a configured cargo-bench JSON adapter, so command drafting is now a source-surface/history aid rather than the active adapter state.

`audit-paper-external-workload-configs --run-id paper-external-config-audit-runner-ready-split-20260612` is the current active adapter preflight: it reports 19 adapter scripts, 19 template configs, 19 active local configs, 19 configured benchmark-owned JSON adapter bridges, 19 benchmark-owned-timing metadata paths, 19 selector-complete configs, zero command-draft configs, zero missing/unconfigured families, 19 structurally runner-ready families, and zero claim-grade runner-ready families. In this audit, `runner_ready` means the adapter has a configured concrete command/cwd, real checkout, selector placeholders, and benchmark-owned JSON timing metadata; `claim_grade_runner_ready` additionally requires paper-grade provenance gates that are still absent. Adapter JSON output now carries `host` plus structured `claim_grade_blockers` for dry-run-only records, unconfigured configs, selector/timing contract failures, workload failures, and configured-but-non-claim bridges, so runner samples can preserve why an external timing bridge is not yet usable as paper evidence instead of only recording `claim_grade=false`; `workload-adapter-child-blockers-validation-20260612b` validates those positive and negative adapter contract paths with synthetic fixtures, including propagation of child wrapper blockers and allocator semantics into the adapter output. The active cargo-bench JSON routes now include `RPython` through RustPython fallback tag `0.1.0` (`cargo bench --package rustpython --bench bench`) alongside `actix`, `R-Oxipng*`, `R-Polars`, `RJS-Compiler`, `gothem`, `ntex`, `rocket`, `thruster`, `tide`, and `tokio`; the HTTP routes are `may-minihttp`, `warp`, `nickel`, `roa`, `saphir`, `salvo`, and TechEmpower `faf`; the Redis route is `RRedis*`. The cargo-bench JSON bridge now emits `allocator_semantics` and top-level blockers for host fallback selectors: on this Darwin host `bench_ptmalloc` is recorded as a `std::alloc::System` fallback rather than Linux/glibc ptmalloc paper evidence, and `bench_scudo` is recorded as a `std::alloc::System` fallback pending Scudo sanitizer/toolchain or external-wrapper evidence. `evaluation/raw/cargo-bench-allocator-semantics-smoke-20260612/semantic-check-summary.json` validates both direct child JSON and runner/sample-audit propagation. The Redis/RRedis JSON bridge now carries the same allocator-semantics contract for the `RRedis*` route; `validate-paper-external-redis-load-json-wrapper --run-id redis-load-allocator-semantics-validation-20260612d` refreshes `evaluation/results/paper_external_redis_load_json_validation.json` with passing positive Redis timing, host-aware `bench_ptmalloc` fallback metadata, explicit `bench_scudo` fallback blockers, and a synthetic Rsedis checkout fixture that verifies both old host-target dependency preparation and allocator overlay preparation before delegation. The sibling `paper_external_redis_benchmark_json.py` bridge now records the redis-benchmark-compatible client path for RRedis: `validate-paper-external-redis-benchmark-json-wrapper --run-id redis-benchmark-json-validation-after-tool-probe-20260612a` refreshes `evaluation/results/paper_external_redis_benchmark_json_validation.json` with passing text-output parsing, CSV-output parsing, missing-binary failure JSON, startup-refusal JSON, allocator-semantics propagation, and embedded tool-probe metadata. `validate-redis-benchmark-tool-probe --run-id redis-benchmark-tool-probe-validation-20260612a` validates explicit-path, environment-variable, and missing-tool discovery with hermetic fake executables; the live `probe-redis-benchmark-tool --run-id redis-benchmark-tool-probe-20260612a` publishes `evaluation/results/redis_benchmark_tool_probe.json` and currently records no usable client across PATH, `UNIALLOC_REDIS_BENCHMARK_BIN`, repo-local deps, or Homebrew candidates on this host. The active local RRedis command still uses the RESP bridge for finite diagnostic samples when `redis-benchmark` is absent; the command-contract audit records `redis_benchmark_json_wrapper_configured=true`, `uses_redis_benchmark=true`, `active_uses_redis_benchmark=false`, and a null `redis_benchmark_selected_source` until the binary and exact paper command provenance are available. `probe-paper-external-workload-command-drafts --run-id paper-external-command-probe-effective-wrapper-kind-20260612` now classifies active adapter wrappers by their effective JSON timing bridge rather than by stale source-surface build hints: it reports 19/19 checked families with existing cwd, available tools, non-build wrapper commands, configured adapters, and structural runner-ready configs. `audit-paper-external-workload-bundle --run-id paper-external-bundle-audit-rpython-bridge-20260612 --dry-run-probe --all-paper-data` proves 338/338 external queue cells are structurally covered and dry-run probed with zero probe failures and zero template-only cells. These bridges are still adapter-integration evidence only: exact provenance, allocator/variant routing, paper-parity load tools, repetitions, and raw evidence gates remain required before any paper-performance claim can use their timing.

`audit-rpolars-paper-parity` is the focused readiness audit for the active R-Polars bridge. It reads `evaluation/external/R-Polars/workload_config.local.json`, scans the fetched Polars checkout under `evaluation/external/_checkouts/`, parses libtest and Criterion bench surfaces, compares the configured cargo bench target/filter selection against the paper-side R-Polars rows, and optionally folds in the latest R-Polars sample audit. When a sample audit names a JSONL sample file, the command now reads those records directly and recognizes adapter-wrapped metadata in `metric_metadata.child_record`, direct cargo-bench wrapper metadata in `metric_metadata`, direct adapter output with a top-level `child_record`, multi-target `cargo_bench_targets`, per-row `cargo_bench_target`, and `target_records` before reporting sample-observed libtest leaves, Criterion leaves, cargo bench targets, filters, row counts, allocators, allocator×target and allocator×function sample matrices, and repetition scope. The active R-Polars config now routes all discovered Polars cargo bench targets through the cargo-bench JSON bridge (`bench,csv,groupby,collect,take,sort`) with no bench-name filter and sets `CSV_SRC` to the checkout `py-polars/tests/files/small.csv` fixture so the historical `csv` Criterion target has a deterministic smoke input; when the `groupby` target is selected and that CSV lacks Polars' historical `id1`-`id6`/`v1`-`v3` schema, the wrapper writes and selects the generated compatibility fixture `evaluation/raw/rpolars-generated-fixtures/polars_groupby_compat.csv` while marking that path as non-claim fixture evidence. The latest audit `rpolars-paper-parity-after-rpolars-combined-plus-six-allocators-system-fallback-fixture-20260612` refreshes `evaluation/results/rpolars_paper_parity_audit.json` and records configured structural coverage of 6/6 cargo bench targets, 6/6 libtest leaves, and 37/37 Criterion leaves after expanding the finite `sort` Criterion format loop into its 12 concrete leaf names. The combined non-claim sample audit `evaluation/results/paper_performance_rpolars_combined_libtest_csv_groupby_collect_take_sort_plus_six_allocators_system_fallback_fixture_smoke_samples_audit.json` now contributes 15 records, 6 raw covered cells, all 6 observed cargo bench targets, all 6 observed libtest leaves, all 5 observed Criterion cargo targets (`csv`, `collect`, `groupby`, `sort`, `take`), all 37 observed Criterion leaves, 6 observed allocators, 6 observed libtest allocators, 6 observed Criterion allocators, 36 allocator×cargo-target cells, 30 allocator×Criterion-target cells, 36 allocator×libtest-leaf cells, and 222 allocator×Criterion-leaf cells while still reporting zero usable claim cells and 42 missing usable cells. `audit-rpolars-claim-matrix --run-id rpolars-claim-matrix-after-target-fragment-audit-20260612a` now publishes `evaluation/results/rpolars_claim_matrix_audit.json`, grouping target-fragment samples by unique `run_index` before crediting a paper repetition; the current matrix covers all thirty-five active R-Polars cells (all seven allocator cells for `default_performance`, `type_isolation`, `hugepage_metadata`, `metadata_segregation`, and `pac_authentication`), finds 35 raw-timing cells, records thirty-five target-complete cells plus zero partial-surface cells, records every focused R-Polars cell at 6/6 target repetitions where present, has no remaining raw/surface repetition gaps, and leaves all 35 required cells with no claim-usable run. Both `audit-rpolars-claim-matrix` and `audit-rpolars-paper-parity` now accept repeated `--samples <jsonl>` inputs in addition to the sample audit's primary JSONL, so focused runner shards can be folded into the same matrix without hand-merging files; `rpolars-claim-matrix-with-plan-filter-unialloc-runs1-6-jemalloc-runs2-6-mimalloc-runs2-6-tcmalloc-runs2-6-snmalloc-runs2-6-ptmalloc-runs2-6-scudo-runs1-6-success-20260613a` and `rpolars-paper-parity-with-plan-filter-unialloc-runs1-6-jemalloc-runs2-6-mimalloc-runs2-6-tcmalloc-runs2-6-snmalloc-runs2-6-ptmalloc-runs2-6-scudo-runs1-6-success-20260613a` merge the 15-record combined smoke surface with `rpolars-paper-plan-run-filter-unialloc-run1-20260612b`, `rpolars-paper-plan-filter-unialloc-run2-success-20260612a`, `rpolars-paper-plan-filter-unialloc-run3-success-20260613a`, `rpolars-paper-plan-filter-unialloc-run4-success-20260613a`, `rpolars-paper-plan-filter-unialloc-run5-success-20260613a`, `rpolars-paper-plan-filter-unialloc-run6-success-20260613a`, `rpolars-paper-plan-filter-jemalloc-run2-success-20260613a`, `rpolars-paper-plan-filter-jemalloc-run3-success-20260613a`, `rpolars-paper-plan-filter-jemalloc-run4-success-20260613a`, `rpolars-paper-plan-filter-jemalloc-run5-success-20260613a`, `rpolars-paper-plan-filter-jemalloc-run6-success-20260613a`, `rpolars-paper-plan-filter-mimalloc-run2-success-20260613a`, `rpolars-paper-plan-filter-mimalloc-run3-success-20260613a`, `rpolars-paper-plan-filter-mimalloc-run4-success-20260613a`, `rpolars-paper-plan-filter-mimalloc-run5-success-20260613a`, `rpolars-paper-plan-filter-mimalloc-run6-success-20260613a`, and `rpolars-paper-plan-filter-tcmalloc-runs2-6-20260613a`, `rpolars-paper-plan-filter-snmalloc-runs2-6-20260613a`, `rpolars-paper-plan-filter-ptmalloc-runs2-6-20260613a`, and `rpolars-paper-plan-filter-scudo-runs1-6-20260613a`, producing 220 successful R-Polars records from forty-nine sample sources after adding `rpolars-paper-plan-type-isolation-unialloc-runs1-6-20260615a`, `rpolars-paper-plan-type-isolation-jemalloc-runs1-6-20260615a`, `rpolars-paper-plan-type-isolation-mimalloc-runs1-6-20260615a`, `rpolars-paper-plan-type-isolation-tcmalloc-runs1-6-20260615a`, `rpolars-paper-plan-type-isolation-snmalloc-runs1-6-20260615a`, `rpolars-paper-plan-type-isolation-ptmalloc-runs1-6-20260615a`, `rpolars-paper-plan-type-isolation-scudo-runs1-6-20260615a`, `rpolars-paper-plan-hugepage-metadata-unialloc-runs1-6-20260615a`, `rpolars-paper-plan-hugepage-metadata-jemalloc-runs1-6-20260615a`, `rpolars-paper-plan-hugepage-metadata-mimalloc-runs1-6-20260615a`, `rpolars-paper-plan-hugepage-metadata-tcmalloc-runs1-6-20260615a`, `rpolars-paper-plan-hugepage-metadata-snmalloc-runs1-6-20260615a`, `rpolars-paper-plan-hugepage-metadata-ptmalloc-runs1-6-20260615a`, `rpolars-paper-plan-hugepage-metadata-scudo-runs1-6-20260615a`, `rpolars-paper-plan-metadata-segregation-unialloc-runs1-6-20260615a`, `rpolars-paper-plan-metadata-segregation-jemalloc-runs1-6-20260615a`, `rpolars-paper-plan-metadata-segregation-mimalloc-runs1-6-20260615a`, `rpolars-paper-plan-metadata-segregation-tcmalloc-runs1-6-20260615a`, `rpolars-paper-plan-metadata-segregation-snmalloc-runs1-6-20260615a`, `rpolars-paper-plan-metadata-segregation-ptmalloc-runs1-6-20260615a`, `rpolars-paper-plan-metadata-segregation-scudo-runs1-6-20260615a`, `rpolars-paper-plan-pac-authentication-unialloc-runs1-6-20260615a`, `rpolars-paper-plan-pac-authentication-jemalloc-runs1-6-20260615a`, `rpolars-paper-plan-pac-authentication-mimalloc-runs1-6-20260615a`, `rpolars-paper-plan-pac-authentication-tcmalloc-runs1-6-20260615a`, `rpolars-paper-plan-pac-authentication-snmalloc-runs1-6-20260615a`, `rpolars-paper-plan-pac-authentication-ptmalloc-runs1-6-20260615a`, and `rpolars-paper-plan-pac-authentication-scudo-runs1-6-20260615a`, while preserving 0 claim-usable cells; `default_performance/unialloc` now has target-complete run indexes 1, 2, 3, 4, 5, and 6 with no missing surface run indexes, `default_performance/jemalloc` now also has target-complete run indexes 1, 2, 3, 4, 5, and 6 with no missing surface run indexes, and `default_performance/mimalloc`, `default_performance/tcmalloc`, `default_performance/snmalloc`, Darwin-fallback `default_performance/ptmalloc`, and Scudo-System-fallback `default_performance/scudo` now have target-complete/surface run indexes 1, 2, 3, 4, 5, and 6 with no missing surface run indexes. The 2026-06-15 refresh `rpolars-claim-matrix-after-hugepage-metadata-tcmalloc-runs1-6-20260615a` added `hugepage_metadata/tcmalloc` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-hugepage-metadata-tcmalloc-runs1-6-20260615a/paper-performance.samples.jsonl` on top of `hugepage_metadata/unialloc`, `hugepage_metadata/jemalloc`, `hugepage_metadata/mimalloc`, and the preceding `type_isolation/*` shards, raising the matrix to 118 records, 32 sample sources, 18 raw-timing cells, and 18 surface-complete cells while still leaving 0 claim-usable cells and 17 cells without raw/surface evidence. The 2026-06-15 refresh `rpolars-claim-matrix-after-hugepage-metadata-snmalloc-runs1-6-20260615a` added `hugepage_metadata/snmalloc` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-hugepage-metadata-snmalloc-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 124 records, 33 sample sources, 19 raw-timing cells, and 19 surface-complete cells while still leaving 0 claim-usable cells and 16 cells without raw/surface evidence. The 2026-06-15 refresh `rpolars-claim-matrix-after-hugepage-metadata-ptmalloc-runs1-6-20260615a` added Darwin-fallback `hugepage_metadata/ptmalloc` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-hugepage-metadata-ptmalloc-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 130 records, 34 sample sources, 20 raw-timing cells, and 20 surface-complete cells while still leaving 0 claim-usable cells and 15 cells without raw/surface evidence. The 2026-06-15 refresh `rpolars-claim-matrix-after-hugepage-metadata-scudo-runs1-6-20260615a` added Scudo-System-fallback `hugepage_metadata/scudo` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-hugepage-metadata-scudo-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 136 records, 35 sample sources, 21 raw-timing cells, and 21 surface-complete cells while still leaving 0 claim-usable cells and 14 cells without raw/surface evidence. The preceding refresh `rpolars-claim-matrix-after-metadata-segregation-unialloc-runs1-6-20260615a` added `metadata_segregation/unialloc` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-metadata-segregation-unialloc-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 142 records, 36 sample sources, 22 raw-timing cells, and 22 surface-complete cells while still leaving 0 claim-usable cells and 13 cells without raw/surface evidence. The preceding refresh `rpolars-claim-matrix-after-metadata-segregation-jemalloc-runs1-6-20260615a` added `metadata_segregation/jemalloc` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-metadata-segregation-jemalloc-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 148 records, 37 sample sources, 23 raw-timing cells, and 23 surface-complete cells while still leaving 0 claim-usable cells and 12 cells without raw/surface evidence. The preceding refresh `rpolars-claim-matrix-after-metadata-segregation-mimalloc-runs1-6-20260615a` added `metadata_segregation/mimalloc` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-metadata-segregation-mimalloc-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 154 records, 38 sample sources, 24 raw-timing cells, and 24 surface-complete cells while still leaving 0 claim-usable cells and 11 cells without raw/surface evidence. The preceding refresh `rpolars-claim-matrix-after-metadata-segregation-tcmalloc-runs1-6-20260615a` added `metadata_segregation/tcmalloc` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-metadata-segregation-tcmalloc-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 160 records, 39 sample sources, 25 raw-timing cells, and 25 surface-complete cells while still leaving 0 claim-usable cells and 10 cells without raw/surface evidence. The preceding refresh `rpolars-claim-matrix-after-metadata-segregation-snmalloc-runs1-6-20260615a` added `metadata_segregation/snmalloc` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-metadata-segregation-snmalloc-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 166 records, 40 sample sources, 26 raw-timing cells, and 26 surface-complete cells while still leaving 0 claim-usable cells and 9 cells without raw/surface evidence. The preceding refresh `rpolars-claim-matrix-after-metadata-segregation-ptmalloc-runs1-6-20260615a` added Darwin-fallback `metadata_segregation/ptmalloc` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-metadata-segregation-ptmalloc-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 172 records, 41 sample sources, 27 raw-timing cells, and 27 surface-complete cells while still leaving 0 claim-usable cells and 8 cells without raw/surface evidence. The preceding refresh `rpolars-claim-matrix-after-metadata-segregation-scudo-runs1-6-20260615a` added Scudo-System-fallback `metadata_segregation/scudo` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-metadata-segregation-scudo-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 178 records, 42 sample sources, 28 raw-timing cells, and 28 surface-complete cells while still leaving 0 claim-usable cells and 7 cells without raw/surface evidence. The preceding refresh `rpolars-claim-matrix-after-pac-authentication-unialloc-runs1-6-20260615a` added `pac_authentication/unialloc` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-pac-authentication-unialloc-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 184 records, 43 sample sources, 29 raw-timing cells, and 29 surface-complete cells while still leaving 0 claim-usable cells and 6 cells without raw/surface evidence. The preceding refresh `rpolars-claim-matrix-after-pac-authentication-jemalloc-runs1-6-20260615a` added `pac_authentication/jemalloc` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-pac-authentication-jemalloc-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 190 records, 44 sample sources, 30 raw-timing cells, and 30 surface-complete cells while still leaving 0 claim-usable cells and 5 cells without raw/surface evidence. The preceding refresh `rpolars-claim-matrix-after-pac-authentication-mimalloc-runs1-6-20260615a` added `pac_authentication/mimalloc` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-pac-authentication-mimalloc-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 196 records, 45 sample sources, 31 raw-timing cells, and 31 surface-complete cells while still leaving 0 claim-usable cells and 4 cells without raw/surface evidence. The preceding refresh `rpolars-claim-matrix-after-pac-authentication-tcmalloc-runs1-6-20260615a` added `pac_authentication/tcmalloc` run indexes 1, 2, 3, 4, 5, and 6 from `evaluation/raw/rpolars-paper-plan-pac-authentication-tcmalloc-runs1-6-20260615a/paper-performance.samples.jsonl`, raising the matrix to 202 records, 46 sample sources, 32 raw-timing cells, and 32 surface-complete cells while still leaving 0 claim-usable cells and 3 cells without raw/surface evidence. The current refresh `rpolars-claim-matrix-after-pac-authentication-all-allocators-runs1-6-20260616a` adds `pac_authentication/snmalloc`, Darwin-fallback `pac_authentication/ptmalloc`, and Scudo-System-fallback `pac_authentication/scudo` run indexes 1, 2, 3, 4, 5, and 6 from their `evaluation/raw/rpolars-paper-plan-pac-authentication-*-runs1-6-20260615a/paper-performance.samples.jsonl` shards, raising the matrix to 220 records, 49 sample sources, 35 raw-timing cells, and 35 surface-complete cells while still leaving 0 claim-usable cells and no raw/surface gaps. The refreshed parity audit `rpolars-paper-parity-after-pac-authentication-all-allocators-runs1-6-20260616a` embeds the same 35-surface-cell matrix so record count can no longer be confused with methodology repetitions. Focused real-command smoke artifacts now validate `csv`, `collect`, `groupby`, `take`, and `sort` routes for `unialloc`, plus `jemalloc`, `mimalloc`, `snmalloc`, and `tcmalloc` all-Criterion-target comparator routes in `evaluation/raw/rpolars-criterion-all-targets-jemalloc-fixture-smoke-20260612/paper-performance.samples.jsonl`, `evaluation/raw/rpolars-criterion-all-targets-mimalloc-snmalloc-fixture-smoke-20260612/paper-performance.samples.jsonl`, and `evaluation/raw/rpolars-criterion-all-targets-tcmalloc-fixture-smoke-20260612/paper-performance.samples.jsonl` with 148 finite comparator Criterion rows, plus `evaluation/raw/rpolars-bench-libtest-tcmalloc-fixture-smoke-20260612/paper-performance.samples.jsonl` with 6 finite `bench` libtest rows for the tcmalloc route, and `evaluation/raw/rpolars-all-targets-ptmalloc-system-fallback-fixture-smoke-20260612/paper-performance.samples.jsonl` with 43 finite rows for the Darwin `bench_ptmalloc` selector/system-allocator fallback route; `evaluation/raw/rpolars-groupby-criterion-fixture-unialloc-smoke-20260612/summary.json` records 10 finite `groupby` Criterion rows with a synthetic small groupby fixture and structured routing evidence that the wrapper adds Polars' crate-native `bench` feature for the lazy API, while `evaluation/raw/rpolars-sort-criterion-fixture-unialloc-smoke-20260612/summary.json` records 12 finite `sort` rows after the wrapper repairs the removed `Int32Chunked::init_rand` helper path. These artifacts are readiness/provenance evidence only: structural route coverage and fixture smoke timing do not become paper timing evidence until repeated multi-allocator samples with raw provenance and paper-equivalent inputs satisfy the paper methodology.

`audit-roxipng-claim-matrix` is the conservative matrix audit for the active R-Oxipng bridge. It scans the fetched Oxipng checkout root (`benches/*.rs` plus `Cargo.toml` bench declarations or inferred bench files), folds in repeated `--samples` JSONL shards, groups records by `(dataset, R-Oxipng*, allocator, run_index)`, and requires every discovered cargo bench target plus every libtest bench leaf before a run can be credited as full-surface evidence for a paper cell. The current refresh `roxipng-claim-matrix-after-hugepage-metadata-scudo-run2-full-surface-20260620a` writes `evaluation/results/roxipng_claim_matrix_audit.json`: the exact Oxipng v4.0.3 checkout exposes 6 cargo bench targets and 97 libtest leaves, while the diagnostic sample set now contributes 810 R-Oxipng records from 156 sample sources, 14 raw-timing cells, all 6 observed cargo bench targets, and all 97 observed libtest leaves across the five-dataset/35-cell paper matrix. `hugepage_metadata/scudo` now has surface-complete run indexes 1 and 2 with missing surface run indexes 3, 4, 5, and 6; Default/default-performance `unialloc`, `jemalloc`, `mimalloc`, `tcmalloc`, `snmalloc`, Darwin-fallback `ptmalloc`, and Scudo-System-fallback `scudo` each have full-surface run indexes 1, 2, 3, 4, 5, and 6 with no missing target/leaf records for those repetitions; `hugepage_metadata/unialloc`, `hugepage_metadata/jemalloc`, `hugepage_metadata/mimalloc`, and `hugepage_metadata/tcmalloc` also have full-surface run indexes 1, 2, 3, 4, 5, and 6 with no missing surface run indexes, and `hugepage_metadata/snmalloc` has full-surface run indexes 1, 2, 3, 4, 5, and 6 with no missing surface run indexes, and `hugepage_metadata/ptmalloc` now has surface-complete run indexes 1, 2, 3, 4, 5, and 6 with no missing surface run indexes, and `hugepage_metadata/scudo` now has surface-complete run indexes 1 and 2 with missing surface run indexes 3, 4, 5, and 6. The latest hugepage metadata comparator run is `evaluation/raw/roxipng-hugepage-metadata-scudo-run2-full-surface-20260620a/paper-performance.samples.jsonl`, with shard audit `evaluation/results/roxipng_hugepage_metadata_scudo_run2_full_surface_shard_audit_20260620a.json`; it recorded ten successful finite diagnostic records with zero failed records: the fast target groups measured about 69.35s/47.53s/29.79s/69.35s/47.02s for `deflate`/`filters`/`interlacing`/`libdeflater`/`reductions`, and the zopfli leaves measured about 80.70s/290.77s/860.07s/1678.31s/3064.80s for `zopfli_1`/`zopfli_2`/`zopfli_4`/`zopfli_8`/`zopfli_16`. The preceding hugepage metadata ptmalloc run6 remains isolated at `evaluation/raw/roxipng-hugepage-metadata-ptmalloc-run6-full-surface-20260620a/paper-performance.samples.jsonl` with shard audit `evaluation/results/roxipng_hugepage_metadata_ptmalloc_run6_full_surface_shard_audit_20260620a.json`; run5 remains isolated at `evaluation/raw/roxipng-hugepage-metadata-ptmalloc-run5-full-surface-20260620a/paper-performance.samples.jsonl` with shard audit `evaluation/results/roxipng_hugepage_metadata_ptmalloc_run5_full_surface_shard_audit_20260620a.json`; run4 remains isolated at `evaluation/raw/roxipng-hugepage-metadata-ptmalloc-run4-full-surface-20260619a/paper-performance.samples.jsonl` with shard audit `evaluation/results/roxipng_hugepage_metadata_ptmalloc_run4_full_surface_shard_audit_20260619a.json`; run3 remains isolated at `evaluation/raw/roxipng-hugepage-metadata-ptmalloc-run3-full-surface-20260619a/paper-performance.samples.jsonl` with shard audit `evaluation/results/roxipng_hugepage_metadata_ptmalloc_run3_full_surface_shard_audit_20260619a.json`; run2 remains isolated at `evaluation/raw/roxipng-hugepage-metadata-ptmalloc-run2-full-surface-20260619a/paper-performance.samples.jsonl` with shard audit `evaluation/results/roxipng_hugepage_metadata_ptmalloc_run2_full_surface_shard_audit_20260619a.json`; run1 remains isolated at `evaluation/raw/roxipng-hugepage-metadata-ptmalloc-run1-full-surface-20260619a/paper-performance.samples.jsonl` with shard audit `evaluation/results/roxipng_hugepage_metadata_ptmalloc_run1_full_surface_shard_audit_20260619a.json`. The preceding hugepage metadata snmalloc run6 remains isolated at `evaluation/raw/roxipng-hugepage-metadata-snmalloc-run6-full-surface-20260619a/paper-performance.samples.jsonl` with shard audit `evaluation/results/roxipng_hugepage_metadata_snmalloc_run6_full_surface_shard_audit_20260619a.json`. The preceding default-performance allocator shard audits, hugepage metadata unialloc run1/run2/run3/run4/run5/run6 audits, hugepage metadata jemalloc run1/run2/run3/run4/run5/run6 audits, hugepage metadata mimalloc run1/run2/run3/run4/run5/run6 audits, hugepage metadata tcmalloc run1/run2/run3/run4/run5/run6 audits, and hugepage metadata snmalloc run1/run2/run3/run4/run5/run6 audits and hugepage metadata ptmalloc run1/run2/run3/run4/run5/run6 audits remain diagnostic-only with zero failed records. Matrix status is now 97/97 observed leaves overall, 14 raw-timing cells, 13/35 cell-level surface-complete cells, 1 surface-partial cell, 21 cells with no surface evidence, and 0 claim-usable cells because the paper methodology also requires claim-grade provenance, true Scudo sanitizer/toolchain or external-wrapper evidence, and the remaining repetitions/allocator/dataset cells. Target/leaf-split records keep `plan_fragment_id`, `split_cargo_bench_target`, `split_libtest_bench_function`, `cargo_bench_targets`, `bench_rows`, and `target_records`, and the runner can select future shards by stable fragment id (including the leaf function suffix) rather than unstable workload indexes. These artifacts are execution/readiness and diagnostic timing evidence, not paper claim evidence.

`run-paper-performance-plan` executes an explicit JSON command plan for external paper workloads and writes `paper-performance.samples.jsonl`, per-run stdout/stderr logs, and `paper-performance-plan-run-summary.json` under `evaluation/raw/<run-id>/`. Each workload must name the paper `dataset`, exact paper `benchmark` row, `allocator`, and `command`; relative `cwd` values are resolved from the repository root. Before running, it also writes `paper-performance-plan-preflight-audit.json` using the same row/column/repetition checks as `audit-paper-performance-plan`. Add `--preflight-dry-run-probe` when the runner should perform the supported delegated/local `--dry-run` probes as part of that preflight, not only during the standalone audit; `dry_run_probe_failure_count` is surfaced in `plan_preflight_blockers`. The focused validation artifact `evaluation/raw/plan-run-preflight-dry-run-probe-validation-20260611/preflight-dry-run-probe-validation.json` proves that a positive local-driver probe records one successful preflight probe and skips before execution under `--claim-grade`, while a mismatched local-driver variant records `dry_run_probe_failure_count=1` and also skips before execution. The full mixed local/external skeleton was then exercised through the same runner gate at `evaluation/raw/paper-performance-plan-full-mixed-run-preflight-probe-20260611/full-mixed-run-preflight-probe-validation.json`: all 20 supported local Collections dry-run probes passed, all 210 workloads were stopped before execution in claim-grade mode, and the remaining blockers are the 190 template wrapper commands plus insufficient repetitions. Non-claim smoke plans may still run with preflight blockers recorded, but `--claim-grade` refuses to start unless that preflight is ready: no fail-safe placeholders, no bench-filtered subset cells, no dry-run probe failures, no missing cells, and enough repeated runs for the paper methodology. By default the sample timing is command wall time, but a workload can set `time_field` / `time_fields` with `measurement: "stdout_json"` when the command prints a JSON timing object. Runner-produced samples include stdout/stderr evidence entries with SHA-256 digests plus per-sample host/provenance metadata, so a future full external run can enter the same raw-evidence gate without hand-authored host fields; if the child JSON is an external adapter record, its structured claim-grade blockers are copied into the sample and surfaced by `audit-paper-performance-samples`. Plan-level `claim_grade`, `claim_grade_scope`, `claim_grade_note`, and `claim_grade_blockers` are now also propagated onto the emitted sample, so a successful command that is only a Darwin `ptmalloc`/System-fallback or fixture smoke remains visibly non-claim even when the metric JSON itself is finite. The validation artifact `evaluation/raw/plan-claim-grade-metadata-propagation-smoke-20260612/` proves the sample audit reports the plan scope plus the Darwin/System-fallback blocker. Per-run selector placeholders in plan `env` values are rendered at execution time; `runtime-run-index-env-smoke-20260612` verifies `UNIALLOC_PAPER_RUN_INDEX` becomes `1` and `2` across a two-run workload instead of staying as a static template string. The focused bridge smoke `evaluation/raw/paper-performance-plan-runner-provenance-smoke-20260612/` ran a JSON-timed sample whose child output omitted host metadata; the generated sample still passed sample-record validity (`invalid_sample_count=0`) because the runner supplied host/provenance and verified log digests. The adapter blocker smoke `evaluation/raw/paper-external-adapter-blockers-plan-smoke-20260612/` used a configured synthetic adapter with finite benchmark-owned JSON timing but `claim_grade=false`; the runner recorded the finite sample and copied `adapter config remains claim_grade=false`, while the sample audit reported one raw-covered cell, zero usable cells, and one invalid sample. Failed commands, timeouts, and missing timing metrics remain in the raw JSONL for auditability but are filtered out by the sample importer, so they cannot silently become finite performance cells. Execution filters `--dataset`, `--benchmark`, `--allocator`, `--workload-index`, and `--run-index` restrict the actual work queue before commands launch and are copied into `run_filters` plus `selected_work_item_count` in the run summary; `plan-filter-empty-20260612a` records `selected_work_item_count=0` for an out-of-range workload filter, while `rpolars-paper-plan-run-filter-unialloc-run1-20260612b` records a single selected/executed R-Polars work item. Add `--resume-successful` with the same `--output-dir` to archive the previous JSONL, keep only successful finite samples matching the current plan cell/run keys, and skip those commands during a retry; failed or stale samples are not carried forward into the active JSONL. Add `--import-results` only when the generated samples should be imported into the active current summary.

When the local `paper_workload_driver.py` fails after printing partial libtest rows, the driver now includes `parsed_bench_rows`, `benchmarks_before_failure`, and `last_bench_rows` in its stderr JSON. `run-paper-performance-plan` copies those fields into the unsuccessful sample under `driver_error` plus top-level diagnostics, while still keeping the raw stdout/stderr evidence digests. The focused crash artifact `evaluation/raw/local-collections-crash-isolation-20260611/crash-isolation-summary.json` records both the original `Collections/unialloc` `slice::` debug failure and the post-fix state: the original runner sample exited 101 after eight parsed slice rows with last completed row `slice::mut_iterator`; after the `BumpAlloc` over-window fix, the focused `slice::push` driver exits 0 and the full filtered `slice::` diagnostic subset exits 0 with all 74 parsed rows in a 240s window. These filtered artifacts remain non-claim-grade diagnostic evidence only.

For semantic `Collections/unialloc` local timing (`type_isolation`, `metadata_segregation`, `hugepage_metadata`, or `pac_authentication`), `paper_workload_driver.py` can replay compiler-site type IDs from a real MIR `type_mapping` artifact:

```bash
python3 evaluation/scripts/paper_workload_driver.py \
  --dataset metadata_segregation \
  --benchmark Collections \
  --allocator unialloc \
  --bench-filter vec::bench_with_capacity_1000 \
  --compiler-site-replay-type-mapping evaluation/raw/<rustc-run>/rustc-driver-direct-allocator-mir-probe-type-mapping.json \
  --compiler-site-id-mode cyclic-replay \
  --compiler-site-recovery-scope thread-local
```

The driver feeds the mapping's non-zero `type_id` rows to the bench harness through `UNIALLOC_COMPILER_SITE_TYPE_IDS` and records only count/sample/SHA-256 in `subprocess_env_delta`, not the whole stream. A successful run should report `semantic_policy.ready=true` with `type_id_basis=compiler-assigned-...`; filtered rows still remain non-claim-grade until the full paper row, repetitions, and raw-evidence gates are satisfied. Use `--compiler-site-id-mode consuming-stream` when testing finite-stream exhaustion behavior rather than high-coverage cyclic replay. The local driver defaults compiler-site replay to `--compiler-site-recovery-scope thread-local`, which keeps single-thread `std_bench` replay on UniAlloc's TLS fast recovery table instead of charging every allocation for the conservative global cross-thread table; pass `--compiler-site-recovery-scope global` when the workload can transfer allocation ownership across threads without exact deallocation metadata.

`paper-performance-local-collections-slice-plan-after-bump-fix-20260611` and `paper-performance-local-collections-slice-run-after-bump-fix-20260611` connect that fix back through the paper-performance plan runner. The generated local-only plan contains the four host-runnable `Collections` allocator cells (`unialloc`, `jemalloc`, `mimalloc`, `snmalloc`) filtered to `slice::`; the runner executed all four with exit code 0 and 74 parsed rows each. Its preflight intentionally remains not claim-grade (`missing_cell_count=38`, `invalid_workload_count=4`, `insufficient_cell_count=4`, `non_claim_grade_subset_workload_count=4`) because the plan omits macro rows/blocked allocators, uses one run instead of the paper methodology, and is a filtered subset.

`audit-paper-performance-shards` summarizes those local/filtered runner JSONL shards without importing them as paper-matrix evidence. For `paper-performance-local-collections-slice-shard-audit-20260611`, the audit records four successful finite samples, 74 observed `slice::` leaf benchmarks, zero allocator parity gaps across `unialloc`/`jemalloc`/`mimalloc`/`snmalloc`, and the expected diagnostic blockers (`claim_grade=false`, `collections-subset`, and `bench_filter=slice::`). It writes raw output under `evaluation/raw/<run-id>/paper-performance-shard-audit.json` and can publish a diagnostic summary such as `evaluation/results/paper_performance_local_shard_audit.json`; the summary is intentionally `ready_for_claim_grade_import=false`.

`import-paper-performance-samples` normalizes external paper workload timing samples into current `.dat` datasets plus a provenance manifest. It accepts CSV or JSONL records with `dataset`, `benchmark`, `allocator`, `run_index`, and a timing field such as `seconds`, `wall_seconds`, `ns_per_iter`, or `time_ns`. Each dataset row needs `allocator=unialloc` samples as the denominator plus samples for each paper allocator column. The importer discards the configured warmup count, uses the geometric mean of the remaining samples, writes ratios as `allocator_time / UniAlloc_time`, and refuses to mark a dataset claim-grade unless every paper row/column has finite values, verified evidence, matching SHA-256 digests for raw evidence, the configured repeated sample count, and no sample-level non-claim metadata. Every import now writes `paper-performance-samples-preflight-audit.json` and includes that path plus the audit summary in the manifest/summary. With `--claim-grade`, a failed preflight stops before writing `.dat` or a manifest, so templates, partial smoke runs, or unverified logs cannot become current evidence. Use `--claim-grade` only for real full-run artifacts; the importer records blockers such as missing baselines, missing allocator cells, insufficient repeated samples, missing/mismatched evidence digests, or driver-emitted `claim_grade=false` / subset-scope metadata, and returns nonzero when a requested claim-grade import is downgraded by blockers.

The current local Darwin/arm64 full-suite smoke command is:

```bash
python3 evaluation/scripts/evaluate.py collect-std-bench-auto-coverage \
  --run-id std-bench-auto-full-after-page-size-radix-fix-20260611 \
  --timeout 1800
```

That run completed all 432 parsed `std_bench` timing rows and reported 100% layout-auto typed object/byte coverage, but still remains `claim_grade=false` because layout-derived IDs are not paper-equivalent compiler type IDs.

### Fast std_bench development loop

Do not use the calibrated 430-case Collections timing row as an edit-loop test. `run-std-bench-dev-loop` builds the real `std_bench` test target once through Cargo, checks that the resulting binary still exposes the exact canonical 468-name surface synchronized from rustc `485ec3fbcc12fa14ef6596dabb125ad710499c9e`, and then reuses that binary for one bounded process per selected leaf. The consolidated `evaluation/config/std_bench_dev_profiles.json` catalog is explicitly non-claim-grade and defaults to the three-leaf `quick-e2e` preset. Prefer this short real E2E check during implementation; stop on the first failure and rerun only that exact case plus one nearby control before broadening to `family-smoke`.

Known high-cost sentinels remain separate singleton batches inside the same catalog; select `--preset pathology --batch-index N`. A timeout or clearly pathological slowdown is sufficient diagnostic evidence for the development iteration: the summary retains the benchmark name, elapsed time, bounded log tails, and process-group cleanup result, then stops. Pathology probes have a five-second observation budget instead of waiting for completion; `quick-e2e` uses 12 seconds per leaf and `family-smoke` uses 20. The one Cargo build has its own budget. No preset replaces the final 468-name claim checkpoint.

For `run-paper-performance-plan`, pressing Ctrl-C now preserves that diagnostic boundary without converting the interrupted workload into a timing sample. The runner writes bounded per-workload stdout/stderr logs, `paper-performance-plan-interruption.json`, `paper-performance.interruptions.jsonl`, and an interrupted run summary, then re-raises the original interrupt. When the workload uses `paper_collections_docker_driver.py`, the wrapper also uses the recorded container CID to run a bounded `kill` / `rm -f` / `inspect` cleanup and nests the absence proof in the interruption artifact. Interrupted records are diagnostic-only, non-claim-grade, and never imported.

```bash
# Default: build once, then run three real E2E leaves in separate fail-fast processes.
python3 evaluation/scripts/evaluate.py \
  run-std-bench-dev-loop \
  --run-id std-bench-quick-e2e

# Broaden only after quick-e2e passes: all 10 cross-family leaves.
python3 evaluation/scripts/evaluate.py \
  run-std-bench-dev-loop \
  --run-id std-bench-family-smoke \
  --preset family-smoke

# One isolated high-cost probe; timeout/interrupt evidence is enough to stop.
python3 evaluation/scripts/evaluate.py \
  run-std-bench-dev-loop \
  --run-id std-bench-pathology-probe-0 \
  --preset pathology \
  --batch-index 0

# After a failure, rerun only the leaf and one nearby control with the same binary build.
python3 evaluation/scripts/evaluate.py \
  run-std-bench-dev-loop \
  --benchmark slice::random_inserts \
  --benchmark vec::bench_with_capacity_1000
```

This path deliberately does not build the rustc-driver MIR pass, purge Cargo artifacts, calibrate benchmark timing, or write `evaluation/results`. Use `collect-rustc-driver-mir-semantic-scope-std-bench-runtime-smoke` only when the compiler-rewrite/runtime integration itself changed; use the full calibrated plan only at the final claim checkpoint.

For compiler/runtime integration, UniAlloc exports C ABI hooks named `__unialloc_alloc_with_metadata`, `__unialloc_dealloc_with_metadata`, `__unialloc_realloc_with_metadata`, `__unialloc_semantic_stats_snapshot`, and `__unialloc_semantic_stats_reset`. Size-negotiated consumers should prefer `__unialloc_semantic_stats_snapshot_size`, `__unialloc_semantic_stats_snapshot_abi_version`, and `__unialloc_semantic_stats_snapshot_checked` before copying `SemanticStatsSnapshot`, because the stats snapshot may grow as the evidence contract gains counters. It also exports `__unialloc_semantic_scope_enter` / `__unialloc_semantic_scope_exit` so instrumentation can make ordinary global allocation calls inherit semantic metadata. Runtime-only evaluation harnesses can enable layout-derived fallback metadata through `__unialloc_semantic_auto_metadata_enable` / `__unialloc_semantic_auto_metadata_disable`, but that mode is only an unmodified-benchmark upper-bound/prototype. Rust-side helpers include `semantic_type_id<T>()`, `semantic_layout_id(size, align)`, `AllocationMetadata::for_rust_type<T>()`, `with_rust_type_metadata_at<T>()`, and `semantic_auto_metadata_enable(...)`. A claim-grade coverage run should reset counters, have an instrumentation pass call these hooks from standard Rust allocation sites across the full benchmark suite, snapshot the counters/events, and then import the emitted/current coverage evidence.

Coverage imports default to `claim_grade=false`, including `collect-coverage`, `collect-abi-coverage`, `collect-scoped-std-coverage`, `collect-semantic-std-bench-coverage`, and `collect-std-bench-auto-coverage`. Use `audit-compiler-coverage-evidence` and then `import-compiler-coverage` for a full paper-equivalent compiler-instrumented run; otherwise `claim-check --source current` will report the observed percentage but keep the coverage overclaim gate as `missing`. The generic `import-coverage` command remains available for smoke/manual artifacts, but C002 worklist items now expect the audited compiler manifest path.

`generate-compiler-coverage-manifest-template` writes a fill-in C002 manifest and immediately audits it as non-ready. Use `--manifest-out evaluation/config/compiler_coverage_manifest.template.json` when you want the template to be visible from the repository config tree and from `overclaim-worklist`. The template now includes the explicit benchmark-surface fields that a full C002 run must replace: the paper-equivalent expected benchmark names, the observed benchmark names, and the without-source-changes marker. When a successful unfiltered `std_bench` summary is available, the template seeds the expected surface from it. The checked-in template currently tracks all 468 names from rustc `485ec3fbcc12fa14ef6596dabb125ad710499c9e`; `std-bench-auto-full-after-page-size-radix-fix-20260611` remains the historical 430-name runtime evidence after excluding the `aaa_`/`zzz_` helper benches.

`package-current-compiler-coverage-manifest` packages the current `results/coverage_summary.json` and its verified source-to-source prototype JSONL into a concrete C002 manifest plus self-audit under `evaluation/raw/<run-id>/`. It is deliberately a non-claim-grade command: the emitted manifest records real `coverage_events`, `compiler_pass_log`, `benchmark_run_summary`, and `type_mapping` evidence paths so the worklist no longer relies on template placeholder paths, while the audit still reports the remaining blockers for the paper claim: no rustc/MIR/LLVM pass, no compiler-assigned allocation-site object IDs, and no without-source-changes full benchmark-suite marker.

`audit-rustc-mir-allocation-sites --cargo-std-bench-mir --run-id rustc-mir-actual-std-bench-target-20260612` is the current rustc-backed allocation-site type-mapping preflight. It runs `cargo +nightly-2022-07-01 rustc -p unialloc --bench std_bench --features bench_ourself,stats -- -Zunpretty=mir` against the original `std_bench` target, parses MIR allocation calls, and writes `evaluation/results/rustc_mir_allocation_site_audit.json`. That dated actual-target preflight compiles successfully, extracts 274 valid compiler-assigned allocation-site object-type rows, pairs the observed target with its 2026-06-12 430-name non-synthetic `std_bench` surface, and its embedded static MIR audit passes that historical benchmark-name surface with `missing_expected_benchmarks=[]`. The older `--std-bench-surface-fixture` mode remains useful as a parser stress preflight: `rustc-mir-std-bench-surface-preflight-20260612` generated one representative function per known benchmark name and extracted 1232 mapping rows, but its observed surface was intentionally a generated fixture. The actual-target MIR evidence is stronger because it compiles the unmodified benchmark target, but it is still non-claim-grade: it is a static compiler-site map, not dynamic allocation-event attribution.

`package-std-bench-mir-runtime-correlation --run-id std-bench-mir-runtime-correlation-20260612` packages the latest successful original `std_bench` layout-auto runtime events together with the actual-target rustc MIR type map. It writes `evaluation/results/std_bench_mir_runtime_correlation_audit.json` and refreshes `evaluation/results/compiler_coverage_evidence_audit.json` with the stronger combined preflight: runtime events are valid, object/byte coverage is 100.0%, 430 non-synthetic benchmark names are observed, and the MIR type-mapping audit still passes with 274 valid rows. The combined audit intentionally remains non-claim-grade because the runtime event basis is inferred as `layout-derived-size-align`, the observed benchmark source is not a compiler-instrumented C002 run, and a future compiler/runtime pass must emit compiler-assigned allocation-site IDs during benchmark execution.

`package-std-bench-compiler-site-replay --run-id std-bench-compiler-site-replay-full-packaged-20260612 --replay-summary evaluation/raw/std-bench-compiler-site-replay-full-20260612/std-bench-auto-summary.json` packages the full compiler-site replay bridge. The packaged audit records valid runtime events with `type_id_basis=compiler-assigned-allocation-site-object-type-id-cyclic-replay`, 99.9999997692% object coverage, 99.9999999231% byte coverage, two replay-tagged coverage events, 115 unique replayed MIR type IDs, all 430 expected non-synthetic `std_bench` names observed, and the same 274-row actual-target MIR map. This closes the previous “runtime events are only layout-derived” preflight gap and the focused-run benchmark-surface gap for the bridge path, but it deliberately remains non-claim-grade: the replay sequence is cyclic rather than exact per-allocation dynamic attribution, the observed benchmark source is still the bridge/reference runtime rather than the final compiler-instrumented C002 run, and the run is not marked paper-equivalent.

`collect-std-bench-auto-coverage --run-id std-bench-compiler-site-consuming-stream-smoke-20260612 --only-with-sentinels binary_heap::bench_push --compiler-site-replay-type-mapping evaluation/raw/rustc-mir-actual-std-bench-target-20260612/rustc-mir-type-mapping.json --compiler-site-id-mode consuming-stream --no-import-results` is the focused finite-stream preflight. It emits `type_id_basis=compiler-assigned-allocation-site-object-type-id-consuming-stream`, records 58 typed allocations and 410 fallback allocations, and packages as `std-bench-compiler-site-consuming-stream-smoke-package-20260612` with 12.3931623932% object coverage. This validates non-wrapping runtime id consumption and stream-exhaustion reporting; it intentionally does not replace the full-surface cyclic bridge as the strongest current C002 runtime-basis evidence.

`audit-compiler-dynamic-attribution-gap --run-id compiler-dynamic-attribution-gap-consuming-stream-capable-20260612` writes `evaluation/results/compiler_dynamic_attribution_gap_audit.json` so the worklist can distinguish solved bridge preflight gates from remaining C002 claim gaps. The current audit records valid replay runtime events, all 430/430 expected non-synthetic `std_bench` names observed, 115 unique replayed MIR type IDs, and 274 valid MIR type-mapping rows. It also records four remaining required gaps: cyclic replay instead of exact per-allocation dynamic IDs, bridge/reference observed benchmark source, benchmark suite not declared complete for the claim, and manifest not requesting a claim-grade import. A separate no-update smoke audit (`compiler-dynamic-attribution-gap-consuming-stream-smoke-20260612`) verifies that finite stream runs are reported as `finite_runtime_allocation_site_id_stream_not_exact_dynamic_attribution` rather than being confused with cyclic replay.

`audit-compiler-coverage-evidence` validates a JSON manifest before any full C002 import. The manifest must name `events`/`coverage_events`, set `claim_grade` or `complete_for_claim`, use a compiler-assigned allocation-site object `type_id_basis`, include metadata fields `compiler_pass`, `toolchain`, `benchmark_suite`, and `benchmark_command`, and attach verified evidence objects with roles `coverage_events`, `compiler_pass_log`, `benchmark_run_summary`, and `type_mapping`. The `type_mapping` artifact is now parsed as JSON/JSONL, not only presence-checked: every claim-grade row must include an allocation-site id, type id, object/Rust type, source span or MIR location, and compiler-pass provenance. Source-inferred/prototype mapping manifests therefore remain useful preflight evidence but cannot satisfy the compiler-assisted claim. The audit now also records event-basis provenance from the runtime JSONL: when events omit `type_id_basis`, it infers the effective basis from event sources such as `std_bench_auto_metadata`, so a layout-auto runtime artifact cannot be mistaken for compiler-assigned allocation-site runtime IDs. The `benchmark_suite` metadata is audited as a full benchmark-surface contract: it must explicitly say the run is without source changes and complete for the paper claim, name the paper-equivalent `std_bench` target, list expected benchmark names, and list matching observed benchmark names or provide them in `benchmark_run_summary` evidence. The observed benchmark source must also be the compiler-instrumented C002 run; reference/layout-auto surfaces may establish the expected name set but remain blockers when used as observed claim evidence. Prototype markers such as source-to-source, layout-derived, manual, FFI/proxy, fixture, provided-source, smoke, partial, or placeholder evidence paths keep the audit not ready. `import-compiler-coverage` reruns that audit and refuses to refresh `results/coverage_summary.json` unless the manifest passes all gates and the observed object coverage meets the C002 threshold.

`results/coverage_summary.json` now records provenance fields that explain that gate explicitly:

- `type_id_basis`: how the observed type IDs were produced, such as `manual-semantic-api-type-id`, `ffi-abi-proxy-rust-type-id`, `source-scoped-rust-type-id`, `source-to-source-prototype-source-inferred-rust-type-id-with-scope-fallback`, `source-to-source-prototype-bench-scope-id`, or `layout-derived-size-align`.
- `claim_grade_blockers`: concrete reasons the current artifact cannot satisfy the paper's compiler-assisted, without-source-changes coverage claim.
- `event_sources` / `event_type_id_bases` / `event_workload_categories`: raw event labels used to infer provenance and sampled benchmark surfaces when they are not supplied manually.

For manual imports, `import-coverage` also accepts `--type-id-basis` and repeated `--claim-grade-blocker` so external raw artifacts can carry the same audit metadata instead of silently passing or failing the claim gate.

## Real-world Type Isolation diagnostic

`scripts/realworld_type_isolation_matrix.py` builds and runs pinned ripgrep,
fd, and Oxipng sources through seven allocator/compiler routes. This is a
source-bound diagnostic over exact application revisions and inputs. The
paper's original toolchain, workload matrix, aggregation, and 72.17% coverage
reproduction remain deferred.

The routes are:

- `native`: the application's original allocator route;
- `jemalloc`: `jemallocator 0.5.4` with
  `jemalloc-sys 0.5.4+5.3.0-patched` and embedded jemalloc `5.3.0-patched`;
- `mimalloc`: `mimalloc 0.1.25` with resolved
  `libmimalloc-sys 0.1.49` and embedded mimalloc `3.3.2`; wrapper default
  features, including secure mode, are disabled;
- `unialloc`: UniAlloc without compiler semantic rewriting;
- `typed_plain`: the actual MIR rewrite with type metadata and lowering policy
  flags set to zero;
- `typeiso_perf`: the same actual MIR rewrite with Type Isolation enabled and
  runtime statistics disabled;
- `typeiso_coverage`: Type Isolation with runtime statistics enabled.

The fd source already selects jemalloc `0.5.4` in its native configuration.
For fd, `native` and explicit `jemalloc` exercise the original source route and
serve as route controls. The primary incremental comparison is
`typeiso_perf / typed_plain`, because those binaries share the compiler rewrite
and allocator while differing in the Type Isolation policy flag. The
`typeiso_perf / native` ratio is an end-to-end comparison that also includes the
allocator, compiler rewrite, recovery bookkeeping, and policy.

These exact lockfile identities form a mixed-current set. As of 2026-07-14, the
mimalloc core is current and its thin Rust wrapper has a newer `0.1.52` release;
the jemallocator wrapper is current in its crate line and upstream jemalloc has
a newer `5.3.1` release. See
`../docs/allocator-memory-and-mechanisms.md` for layered provenance, memory
methodology, mimalloc purge/THP sensitivity, and safe claim wording.

The final runs used `nightly-2026-06-11`, physical CPU 6, NUMA node 0, and
libc-managed rseq. ripgrep and fd used full inputs with two warmups and 9 and 7
measured repetitions, respectively. Oxipng used its quick input with one warmup
and 5 measured repetitions. Performance routes ran with runtime statistics
disabled. `typeiso_coverage` timing is performance-ineligible; it exists only
to collect counters. All seven variants produced the same output SHA-256 within
each application.

| Application | Variant | Median wall time (s) | Median peak RSS (KiB) | Performance eligible |
|---|---|---:|---:|---|
| ripgrep | `native` | `0.086309096` | `5120` | yes |
| ripgrep | `jemalloc` | `0.086702833` | `6144` | yes |
| ripgrep | `mimalloc` | `0.088868793` | `11812` | yes |
| ripgrep | `unialloc` | `0.090066579` | `6144` | yes |
| ripgrep | `typed_plain` | `0.092411192` | `6144` | yes |
| ripgrep | `typeiso_perf` | `0.092586986` | `6144` | yes |
| ripgrep | `typeiso_coverage` | `0.094015251` | `6144` | no |
| fd | `native` | `0.136296730` | `6144` | yes |
| fd | `jemalloc` | `0.136304143` | `6144` | yes |
| fd | `mimalloc` | `0.129672706` | `18872` | yes |
| fd | `unialloc` | `0.167541836` | `6144` | yes |
| fd | `typed_plain` | `0.464682945` | `6144` | yes |
| fd | `typeiso_perf` | `0.465833677` | `6144` | yes |
| fd | `typeiso_coverage` | `0.491091350` | `6144` | no |
| Oxipng | `native` | `1.712608439` | `46400` | yes |
| Oxipng | `jemalloc` | `1.727841580` | `49088` | yes |
| Oxipng | `mimalloc` | `1.634668906` | `71180` | yes |
| Oxipng | `unialloc` | `1.736975509` | `44260` | yes |
| Oxipng | `typed_plain` | `1.766903750` | `44508` | yes |
| Oxipng | `typeiso_perf` | `1.754456338` | `44524` | yes |
| Oxipng | `typeiso_coverage` | `1.754579386` | `44640` | no |

| Application | `typeiso_perf / typed_plain` | Incremental time | `typeiso_perf / native` | End-to-end time | Typed allocation events | Total allocation events | Event coverage |
|---|---:|---:|---:|---:|---:|---:|---:|
| ripgrep | `1.001902302050` | `+0.190230%` | `1.072737292950` | `+7.273729%` | `2266` | `22725` | `9.97%` (`997` bp) |
| fd | `1.002476380966` | `+0.247638%` | `3.417790558878` | `+241.779056%` | `616262` | `831549` | `74.11%` (`7411` bp) |
| Oxipng | `0.992955240488` | `-0.704476%` | `1.024435182057` | `+2.443518%` | `9661` | `10368` | `93.18%` (`9318` bp) |

| Application | Type Isolation RSS (KiB) | Typed-plain RSS (KiB) | RSS ratio vs typed plain | Native RSS (KiB) | RSS ratio vs native |
|---|---:|---:|---:|---:|---:|
| ripgrep | `6144` | `6144` | `1.000000000000` | `5120` | `1.200000000000` |
| fd | `6144` | `6144` | `1.000000000000` | `6144` | `1.000000000000` |
| Oxipng | `44524` | `44508` | `1.000359485935` | `46400` | `0.959568965517` |

Event coverage is `typed_allocations / total_allocations` for one exact
application revision and input. It is reported per application; aggregation
would erase workload-specific compiler and allocator behavior. This metric has
no source-line, Rust-type, byte, or universal program denominator, and it uses a
different denominator from the paper's 72.17% result.

The measured artifact source binding is:

- measured implementation-bundle SHA-256:
  `7e98e63ce2fbeccc361ea57bd26773ccdb02664b83d772f0475161c980c55929`;
- MIR pass source SHA-256:
  `ae7dd0da2368c670323287647c94ce5a90069b6f2e4a3d51b298d48cb9a5ac63`;
- ripgrep source: `4649aa9700619f94cf9c66876e9549d83420e16c`;
- fd source: `b19136871310b01500b4f09eadd7387b8476be47`;
- Oxipng source: `dea23211ae6259007e068c59ab16929798d00d96`.

The current matrix-runner implementation-bundle SHA-256 is
`94ede1223c7b348639a7041a40a5b1840a6840cc18b4dbcf7d0f7a6ef8bb2cf4`.
It differs because binary-cache reuse validation was hardened after the
measurements. The allocator and MIR pass sources used by the measured binaries
are unchanged.

### Optimization trajectory

The fd full-input run provides the clearest before/after signal for the two
runtime optimizations. Under the same CPU 6 / NUMA 0 pinning and default
libc-managed rseq contract, the pre-optimization and final medians were:

| Route | Pre-optimization wall (s) | Final wall (s) | Change | Pre/final RSS (KiB) |
|---|---:|---:|---:|---:|
| `typed_plain` | `0.611635718` | `0.464682945` | `-24.026192%` | `6144 / 6144` |
| `typeiso_perf` | `0.573481469` | `0.465833677` | `-18.770928%` | `6144 / 6144` |

The native route moved by `-0.218373%` in the same comparison. Event coverage
remained `74.11%` (`616259/831530` before and `616262/831549` final). These are
separate source-bound diagnostic states, so the deltas establish a directional
optimization result for this fd input rather than a stable cross-workload
speedup.

The authoritative results are:

- `raw/realworld-nightly-20260713/type-isolation-ripgrep-final/results-pinned-final.json`;
- `raw/realworld-nightly-20260713/type-isolation-fd-final/results-pinned-final.json`;
- `raw/realworld-nightly-20260713/type-isolation-oxipng-final/results-pinned-final.json`.

The final runtime optimizations preserve these lifecycle constraints:

1. Hosted automatic-allocation records remain in their pointer-derived home
   shard and home overflow. A sticky legacy-state marker enables the exhaustive
   fallback only when old or test-only non-home inline state can exist.
   `fixed_heap` retains its bounded cross-shard inline behavior. The active-shard
   mask changes only on live-count transitions from zero to one and one to zero.
2. A generic raw allocation miss publishes the authoritative strict lifecycle
   record once, then completes semantic admission through an after-publication
   path. Cache hits and guarded mappings retain semantic publication, and raw
   backend alias rejection remains at the strict publication boundary.

Ordinary compiler semantic scopes request conservative cross-thread recovery;
explicit `_local` scopes retain local placement. Exact history observations pin
their selected `Absent` entry until admission completes, eviction skips pinned
entries, and dropping the observation releases its lease. The history remains
an eight-way bounded window; simultaneous leases on all eight ways use the
fail-stop availability path.

Linux rseq itself allows libc-managed registration. UniAlloc's production
allocation hot path currently makes no rseq call. The matrix therefore uses the
default libc registration (`glibc_rseq_mode=libc_default`, no glibc tunable).
Only the allocator's private self-registration tests require process startup
with `GLIBC_TUNABLES=glibc.pthread.rseq=0`; the optional
`--disable-glibc-rseq` matrix flag exists for that diagnostic boundary.

## Platform evidence flow

`generate-platform-matrix-template` writes a fill-in matrix with the required artifact roles for every C007 platform. `collect-platform-smoke` runs local retargeting smoke checks and writes a generated matrix under `evaluation/raw/<run-id>/platform-matrix.generated.json`, then imports it into `evaluation/results/platform_matrix.json` unless `--no-import` is used. `audit-platform-matrix` can be run on any hand-authored or collected matrix before import; `import-platform-matrix` always writes `platform-matrix-preflight-audit.json` next to the source matrix and refreshes `evaluation/results/platform_matrix_audit.json`.

```bash
python3 evaluation/scripts/evaluate.py collect-platform-smoke \
  --run-id platform-smoke-all-20260611 \
  --platforms macos windows rust-for-linux blogos redox
python3 evaluation/scripts/evaluate.py generate-platform-matrix-template \
  --run-id platform-matrix-template-20260611 \
  --matrix-out /tmp/platform-matrix.to-fill.json
python3 evaluation/scripts/evaluate.py audit-platform-matrix \
  --matrix evaluation/raw/platform-smoke-all-20260611/platform-matrix.generated.json
python3 evaluation/scripts/evaluate.py claim-check --source current
```

The collector records:

- macOS host feature build smoke: `cargo check -p unialloc --lib --features type_isolation,metadata_segregation,hugepage,pac,force_initialize`.
  On Darwin hosts this now writes explicit artifact roles for the matrix audit:
  `macos-build.log` (`build_log`), `macos-run-summary.json`
  (`run_summary`), and `macos-performance-data.json`
  (`performance_data`). The performance artifact is smoke timing metadata only;
  it is deliberately marked non-claim-grade so it cannot satisfy the paper
  macOS performance claim without a real paper-shaped run.
- Windows target check for `x86_64-pc-windows-gnu` by default. Use
  `--windows-rust-target` to point the collector at a different installed Rust
  target, `--windows-linker` or `UNIALLOC_WINDOWS_LINKER` to provide the real
  linker used for executable build evidence, and `--windows-runtime-runner` or
  `UNIALLOC_WINDOWS_RUNTIME_RUNNER` to provide a real Windows userspace runner
  when `wine64`/`wine` is not on `PATH`. The collector records the selected
  linker/runner and cargo environment overrides in the raw artifacts so Windows
  C007 evidence is reproducible rather than an implicit host assumption. The
  collector always emits
  the required C007 artifact roles: `windows-build.log` (`build_log`),
  `windows-run-summary.json` (`run_summary`), and
  `windows-performance-data.json` (`performance_data`). When the Rust target is
  not installed, these are explicit blocker artifacts documenting that no
  Windows build/run/performance sample was produced; when it is installed, the
  build log records the cross `cargo check` but the run and performance files
  still remain non-claim-grade until real Windows execution evidence exists.
- Rust-for-Linux source-presence smoke for the bundled kernel-module benchmark files.
  This now emits explicit blocker artifacts for the required roles:
  `rust-for-linux-kernel-build.log` (`kernel_build_log`) and
  `rust-for-linux-cycle-counts.json` (`cycle_counts`). These artifacts document
  that only the repository `kernel/` subtree was inspected; no external
  Rust-for-Linux kernel tree, kernel build, module execution, or cycle-count run
  was performed, so the entry remains non-claim-grade.
- BlogOS/Redox-style constrained heap smoke: `cargo check -p unialloc --lib --no-default-features --features fixed_heap,allow_mem_leak`.
  These constrained checks now emit `*-build.log` (`build_log`) and
  `*-boot-cycles.json` (`boot_cycles`) artifacts. The boot-cycle artifact is a
  blocker record, not a synthetic boot: it states that no BlogOS/Redox target
  image or emulator run was performed, so the entries remain non-claim-grade.

These are intentionally **not** claim-grade by default. Platform matrix entries must set `claim_grade` or `complete_for_claim` to `true`, pass the per-platform preflight, and include the required artifact classes before C007 can pass; otherwise the checker treats them as useful smoke evidence but reports the platform as `missing, unverified, or non-claim-grade`. The preflight rejects placeholder paths, missing files, smoke-only notes/scope, failed platform runs, missing target/build metadata fields, missing required evidence kinds, and JSON evidence artifacts that explicitly declare non-claim-grade status, failed execution, empty samples, smoke/blocker scope, or claim-grade blockers:

- Windows/macOS: `build_log`, `run_summary`, and `performance_data`.
- Rust-for-Linux: `kernel_build_log` and `cycle_counts`.
- BlogOS/Redox: `build_log` and `boot_cycles`.

Claim-grade platform entries may use evidence objects such as `{"path": "windows/build.log", "kind": "build_log"}` so the importer can verify both file existence and artifact role, not just a path string. The repository template at `evaluation/config/platform_matrix.template.json` intentionally contains `path/to/...` placeholders and audits as not-ready until every path is replaced with real evidence. Metadata is all-field, not any-field: Windows/macOS entries must include host, target triple, Rust target, platform target, OS version, and target metadata; Rust-for-Linux must include kernel version/tree/config, target triple, platform target, and target metadata; BlogOS/Redox must include image, target triple, platform target, emulator, boot config, and target metadata.

## Local benchmark flow

1. Install/activate the Rust nightly toolchain required by `rust-toolchain`.
2. Keep the default libc-managed rseq environment for application benchmarks.
   Set the following only when the run intentionally exercises UniAlloc's
   private rseq self-registration tests:
   ```bash
   export GLIBC_TUNABLES=glibc.pthread.rseq=0
   ```
3. Run the local harness:
   ```bash
   python3 evaluation/scripts/evaluate.py run-local --profile release --runs 6
   python3 evaluation/scripts/evaluate.py summarize --source current
   python3 evaluation/scripts/evaluate.py claim-check --source current
   python3 evaluation/scripts/evaluate.py overclaim-worklist --source current --refresh-claim-check
   ```

The runner records command, environment, wall time, exit status, stdout/stderr paths, host metadata, and parsed libtest `ns/iter` benchmark rows. `summarize --source current` converts matching `bench_ourself` and baseline feature rows into a local `default_performance` dataset.

`export-current-datasets` converts a `run-local` raw directory into `.dat` files plus `dataset-manifest.json` provenance. With `--paper-shape`, local `std_bench` rows are collapsed into the paper's `Collections` row so the generated files have the same first-row shape as `data/default-perf.dat`, `data/type-perf.dat`, `data/meta-perf.dat`, `data/hugepage-perf.dat`, and `data/pac-perf.dat`:

```bash
python3 evaluation/scripts/evaluate.py audit-paper-performance-targets \
  --run-id paper-performance-targets
python3 evaluation/scripts/evaluate.py export-current-datasets \
  --run-dir evaluation/raw/local-variant-smoke \
  --paper-shape \
  --import-results
```

The target audit is intentionally separate from export: it describes the full paper-shaped matrix that must be produced, while the exporter writes whatever the current local raw run actually supports. The exporter is still non-claim-grade by default. `merge-current-datasets` makes this composition reproducible when different current runs cover different datasets: pass multiple `--data-dir` values, choose `--prefer first|last`, and it writes a merged data directory plus `dataset-manifest.json` whose `merge_sources` preserve each source manifest as a provenance root. `import-current-datasets` now requires both (1) row/column coverage of the paper reference dataset and (2) a manifest entry whose raw provenance evidence verifies and explicitly sets `complete_for_claim` or `claim_grade`; use repeated `--dataset` to restrict an import to the active paper-claim datasets in a partial run. Evidence paths may be relative to the active manifest directory, the repository root, a merged source manifest, or a recorded `run_dir`, so safe merged smoke manifests keep their provenance without weakening the file-existence check. This prevents a paper-shaped smoke `.dat` file from passing C001/C003-C006 merely because it has familiar column names. Missing workload rows, missing allocator columns, missing provenance, or a manifest that does not opt into claim-grade completion are surfaced as `claim_grade_blockers`.

External full-run samples can enter the same gate without hand-writing `.dat` files:

```bash
python3 evaluation/scripts/evaluate.py generate-paper-performance-wrapper-contracts \
  --run-id paper-wrapper-contracts
python3 evaluation/scripts/evaluate.py generate-paper-external-workload-bundle-template \
  --run-id paper-external-bundle-template
python3 evaluation/scripts/evaluate.py audit-paper-external-workload-source-hints \
  --bundle evaluation/config/paper_external_workloads.template.json \
  --paper-repo "${UNIALLOC_RUST_ALLOC_PAPER:-../rust-alloc-paper}" \
  --run-id paper-source-provenance-scan
python3 evaluation/scripts/evaluate.py fetch-paper-external-workload-checkouts \
  --bundle evaluation/config/paper_external_workloads.template.json \
  --run-id paper-external-checkout-fetch \
  --execute
python3 evaluation/scripts/evaluate.py discover-paper-external-workload-checkouts \
  --bundle evaluation/config/paper_external_workloads.template.json \
  --search-root evaluation/external/_checkouts \
  --max-depth 2 \
  --run-id paper-external-checkout-discovery
python3 evaluation/scripts/evaluate.py draft-paper-external-workload-configs \
  --bundle evaluation/config/paper_external_workloads.template.json \
  --discovery evaluation/results/paper_external_workload_checkout_discovery.json \
  --run-id paper-external-config-drafts \
  --write
python3 evaluation/scripts/evaluate.py audit-paper-external-workload-configs \
  --bundle evaluation/config/paper_external_workloads.template.json \
  --run-id paper-external-config-audit
python3 evaluation/scripts/evaluate.py validate-paper-external-workload-adapter-contract \
  --run-id paper-external-adapter-contract-validation
python3 evaluation/scripts/evaluate.py audit-paper-external-workload-source-surfaces \
  --bundle evaluation/config/paper_external_workloads.template.json \
  --checkouts evaluation/results/paper_external_workload_checkouts.json \
  --run-id paper-external-source-surfaces
python3 evaluation/scripts/evaluate.py draft-paper-external-workload-command-configs \
  --bundle evaluation/config/paper_external_workloads.template.json \
  --source-surfaces evaluation/results/paper_external_workload_source_surfaces.json \
  --run-id paper-external-command-config-drafts \
  --write
python3 evaluation/scripts/evaluate.py probe-paper-external-workload-command-drafts \
  --bundle evaluation/config/paper_external_workloads.template.json \
  --command-config-drafts evaluation/results/paper_external_workload_command_config_drafts.json \
  --config-audit evaluation/results/paper_external_workload_config_audit.json \
  --run-id paper-external-command-probe
python3 evaluation/scripts/evaluate.py audit-paper-external-workload-bundle \
  --bundle evaluation/config/paper_external_workloads.template.json \
  --run-id paper-external-bundle-audit \
  --dry-run-probe
python3 evaluation/scripts/evaluate.py generate-paper-workload-wrapper-manifest-template \
  --run-id paper-wrapper-manifest-template
python3 evaluation/scripts/evaluate.py audit-paper-workload-wrapper-manifest \
  --manifest evaluation/config/paper_workload_wrappers.template.json \
  --run-id paper-wrapper-manifest-audit \
  --dry-run-probe
python3 evaluation/scripts/evaluate.py generate-paper-performance-plan \
  --run-id paper-plan-skeleton \
  --collections-driver \
  --wrapper-manifest evaluation/config/paper_workload_wrappers.template.json \
  --plan-out /tmp/paper-plan.json
python3 evaluation/scripts/evaluate.py audit-paper-performance-plan \
  --plan /tmp/paper-plan.json \
  --run-id paper-plan-audit \
  --dry-run-probe \
  --write-missing-template /tmp/paper-plan-missing.json
python3 evaluation/scripts/evaluate.py run-paper-performance-plan \
  --plan /tmp/paper-plan.json \
  --run-id paper-plan-run \
  --keep-going
python3 evaluation/scripts/evaluate.py import-paper-performance-samples \
  --samples evaluation/raw/paper-plan-run/paper-performance.samples.jsonl \
  --run-id paper-run-import \
  --claim-grade \
  --evidence evaluation/raw/paper-plan-run/paper-performance-plan-run-summary.json \
  --import-results
python3 evaluation/scripts/evaluate.py claim-check --source current
```

Minimal plan shape:

```json
{
  "schema_version": 1,
  "workloads": [
    {
      "dataset": "default_performance",
      "benchmark": "RJS-Compiler",
      "allocator": "unialloc",
      "command": ["./run-rjs-compiler.sh", "--allocator", "unialloc"],
      "cwd": "evaluation/external/rjs",
      "env": {"RUSTFLAGS": "-C target-cpu=native"},
      "timeout": 1800
    }
  ]
}
```

The sample file must include the paper dataset key (`default_performance`, `type_isolation`, `metadata_segregation`, `hugepage_metadata`, or `pac_authentication`), exact paper benchmark row name, allocator name, run index, and a timing value. A claim-grade import also requires command/invocation provenance, host or target provenance, and at least one verified raw evidence path or URI with a SHA-256 digest for every successful sample; otherwise the importer records sample-level blockers and downgrades the dataset. For example:

```json
{"dataset":"default_performance","benchmark":"RJS-Compiler","allocator":"unialloc","run_index":1,"seconds":10.25,"command":["./run-rjs-compiler.sh","--allocator","unialloc"],"host":{"system":"Linux","machine":"x86_64"},"evidence":[{"path":"logs/default_performance-RJS-Compiler-unialloc-run1.stdout.txt","sha256":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}]}
{"dataset":"default_performance","benchmark":"RJS-Compiler","allocator":"jemalloc","run_index":1,"seconds":10.91,"command":["./run-rjs-compiler.sh","--allocator","jemalloc"],"host":{"system":"Linux","machine":"x86_64"},"evidence":[{"path":"logs/default_performance-RJS-Compiler-jemalloc-run1.stdout.txt","sha256":"fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"}]}
```

Short local runs are useful for regression triage:

```bash
python3 evaluation/scripts/evaluate.py run-local \
  --run-id local-bench-smoke \
  --runs 1 \
  --features bench_ourself bench_mimalloc \
  --bench-filter binary_heap::bench_push
python3 evaluation/scripts/evaluate.py summarize --source current --run-dir evaluation/raw/local-bench-smoke
python3 evaluation/scripts/evaluate.py claim-check --source current
```

The same summarizer can generate smoke-only variant datasets when feature runs combine `bench_ourself` with a paper variant flag:

```bash
python3 evaluation/scripts/evaluate.py run-local \
  --run-id local-variant-smoke \
  --runs 1 \
  --features \
    bench_ourself \
    bench_mimalloc \
    bench_ourself,type_isolation \
    bench_ourself,metadata_segregation \
    bench_ourself,hugepage \
    bench_ourself,pac \
  --bench-filter binary_heap::bench_push
python3 evaluation/scripts/evaluate.py summarize --source current --run-dir evaluation/raw/local-variant-smoke
python3 evaluation/scripts/evaluate.py claim-check --source current
```

Variant rows use `baseline allocator ns_per_iter / variant UniAlloc ns_per_iter`; values greater than 1 mean the local variant was faster than that baseline allocator for that benchmark. These rows populate `type_isolation`, `metadata_segregation`, `hugepage_metadata`, and `pac_authentication`, but remain `claim_grade=false`.

Filtered or short `std_bench` datasets are marked `claim_grade=false`, so the checker reports their values but keeps paper overclaim gates as `missing`. Full claims require paper-shaped current datasets imported with `import-current-datasets` or a complete runner that covers the paper's Linux macro/web/platform workloads. Imported `.dat` files are compared against the paper reference row/column coverage before they can become claim-grade. The checker intentionally reports missing toolchains, missing benchmarks, non-claim-grade datasets, or missing platform artifacts as `missing` rather than passing claims by assumption.

Claim thresholds are interpreted as **paper-equivalent-or-better** gates rather than exact-value matches. Current evidence may beat the paper, but it must not be worse than the paper-facing bound: default-performance deltas may exceed the positive/faster side of the `+-2%` window but must not be slower than `-2%`; speedup claims use the lower bound as the pass threshold and accept higher speedups; slowdown-cost claims use the upper bound as the pass threshold and accept lower slowdowns; coverage claims accept higher coverage; and platform claims still require verified claim-grade evidence for every required platform. `claim-check` and `overclaim-worklist` include an `acceptance_rule` field so this directionality is visible in JSON and Markdown artifacts.

## Overclaim worklist dashboard

`overclaim-worklist --source current --refresh-claim-check` converts the current claim audit into a focused action list. It groups C002 under compiler coverage, C001/C003-C006 under paper performance, and C007 under platform retargeting. Each item carries extracted blockers, explicit missing requirements, compact observed evidence, links to the latest coverage/performance/wrapper-contract/external-workload-bundle/wrapper-manifest/platform artifacts, and concrete next actions. Performance items include per-dataset wrapper capability counts, external-workload-bundle audit counts, wrapper-manifest audit counts, sample-audit counts, R-Polars target-fragment claim-matrix counts/blockers when the parity audit embeds them, examples of external or blocked wrapper cells, examples of missing/template/invalid external bundle cells, examples of missing/template/invalid wrapper manifest cells, and examples of missing usable sample cells; platform items expand the per-platform missing artifact roles, missing metadata fields, evidence-content blocker counts/examples, and smoke/template blockers. The JSON form is written to both `evaluation/results/overclaim_worklist.json` and `evaluation/raw/<run-id>/overclaim-worklist.json`; the Markdown form is written to `evaluation/reports/overclaim_worklist.md`.

The worklist now also links the R-Polars repeated-run plan and prints its per-item diagnostics: 35 workloads, 210/210 planned runs, 35/35 covered cells, 35 dry-run probes, zero probe failures, structurally runnable schedule `true`, and claim-import readiness `false` while adapter/sample provenance gates remain open.

The command is intentionally non-passing while required current claims still need stronger evidence. Use `--allow-incomplete` only when a CI job or exploratory script needs the dashboard artifact without failing on the known remaining work.
