# external-RRedis paper workload adapter

This directory is a strict adapter scaffold for an external paper macro workload.
It is not performance evidence by itself.

## Contract

- `run_paper_workload.py` delegates to `evaluation/scripts/paper_external_workload_adapter.py`.
- The adapter refuses to emit timing unless `workload_config.json` or `workload_config.local.json` exists and sets `configured=true`.
- A real config must point at a real benchmark checkout and a command that selects dataset, benchmark, allocator, variant, and run index.
- The command/env/cwd template must keep selector placeholders for dataset, benchmark, allocator, variant, and run index so each paper cell is actually selected.
- The benchmark command must print a JSON object with a finite timing field such as `seconds`; set `benchmark_owned_json=true` only when that JSON comes from the benchmark harness rather than a generic wall-clock wrapper.

Benchmarks matched by the current bundle rule: `RRedis*`.


## Paper source hint

- Project: `Rsedis`
- Paper version: `not specified`
- Upstream URL: `https://github.com/seppo0010/rsedis`

Start by copying `workload_config.template.json` to `workload_config.json`, filling in the real checkout path and command, then run the bundle audit with dry-run probing.


## Current local bridge status

- `workload_config.local.json` keeps the existing RESP SET/GET JSON bridge as the active diagnostic command so local matrix samples can still produce finite non-claim timing when `redis-benchmark` is absent.
- `redis_benchmark_json_wrapper` records the paper-client path through `evaluation/scripts/paper_external_redis_benchmark_json.py`; it launches the upstream Rsedis server, waits for RESP readiness, invokes a redis-benchmark-compatible client, parses text or CSV throughput, and emits `measurement_source=redis_benchmark_json`.
- `evaluation/scripts/prepare_redis_benchmark_tool.py` probes explicit `--redis-benchmark-bin`, `UNIALLOC_REDIS_BENCHMARK_BIN`, PATH, ignored repo-local locations such as `evaluation/external/_deps/redis/bin/redis-benchmark`, and Homebrew Redis paths without installing packages. `probe-redis-benchmark-tool --run-id redis-benchmark-tool-probe-20260612a` publishes the live result to `evaluation/results/redis_benchmark_tool_probe.json`.
- `validate-redis-benchmark-tool-probe --run-id redis-benchmark-tool-probe-validation-20260612a` verifies explicit-path, environment-variable, and missing-tool discovery with fake executables. `validate-paper-external-redis-benchmark-json-wrapper --run-id redis-benchmark-json-validation-after-tool-probe-20260612a` is the focused wrapper contract check after adding tool-probe metadata. These are validation plumbing, not paper timing evidence.
- Remaining RRedis paper blockers are intentionally explicit: no local redis-benchmark-compatible client in the current probe result, no exact paper command in the paper source, active diagnostic samples still use the RESP bridge, Darwin host parity is not Linux/glibc, and samples remain non-claim until repeated raw evidence and allocator/runtime equivalence are proven.
