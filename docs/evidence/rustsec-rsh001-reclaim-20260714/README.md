# RSH-001 Feature-Matched Reclaim Evidence

This bundle evaluates the upstream `chttp` duplicate-ownership witness as an
exact duplicate-reclaim event. The matrix uses three repetitions of all six
logical arms:

```text
vulnerable,patched x system,reclaim_plain,reclaim_checks
```

## Result

- Vulnerable system/ASan reproduced `asan_double_free` in 3/3 runs.
- Vulnerable `reclaim_plain` completed without an allocator diagnostic in 3/3
  runs.
- Vulnerable `reclaim_checks` emitted the exact
  `unialloc_pointer_already_released_check` diagnostic in 3/3 runs.
- Patched system, `reclaim_plain`, and `reclaim_checks` controls completed
  without sanitizer or allocator signals in 3/3 runs each.
- Cargo feature and implementation provenance matched between the plain and
  treatment arms.

The strict exporter classifies this scenario as
`detected / exact_diagnostic_true_positive`. The positive scope is the second
reclaim of the same address in the integrated source witness. This evidence
attributes the result to `reclaim_checks`; it does not classify RSH-001 as a
Type Isolation mitigation.

## Reproduction

```bash
python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --action run --scenario RSH-001-upstream \
  --variants system,reclaim_plain,reclaim_checks \
  --archive-variants vulnerable,patched --repetitions 3 \
  --cache "$HOME/.cache/unialloc/rustsec-heap" --allow-download \
  --output-dir /tmp/unialloc-rsh001-reclaim-run \
  --execute-unsafe --jobs 4
```

The retained artifacts are `preflight.json`, `experiment.json`, `summary.json`,
and `mechanism-results.json`. Every result remains exploratory with
`claim_grade=false`.
