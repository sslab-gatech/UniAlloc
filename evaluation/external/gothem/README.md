# external-gothem paper workload adapter

This directory is a strict adapter scaffold for an external paper macro workload.
It is not performance evidence by itself.

## Contract

- `run_paper_workload.py` delegates to `evaluation/scripts/paper_external_workload_adapter.py`.
- The adapter refuses to emit timing unless `workload_config.json` or `workload_config.local.json` exists and sets `configured=true`.
- A real config must point at a real benchmark checkout and a command that selects dataset, benchmark, allocator, variant, and run index.
- The command/env/cwd template must keep selector placeholders for dataset, benchmark, allocator, variant, and run index so each paper cell is actually selected.
- The benchmark command must print a JSON object with a finite timing field such as `seconds`; set `benchmark_owned_json=true` only when that JSON comes from the benchmark harness rather than a generic wall-clock wrapper.

Benchmarks matched by the current bundle rule: `gothem`.


Start by copying `workload_config.template.json` to `workload_config.json`, filling in the real checkout path and command, then run the bundle audit with dry-run probing.
