# Compiler Heap-Lifetime Ground-Truth Analysis

## Status

Joined 175 exact runtime sites across fd, oxipng, ripgrep.

## Application coverage

| Application | Runtime sites | Static-feature matches | Exact layout matches | Matched requested bytes | Long outcomes | Live censored |
|---|---:|---:|---:|---:|---:|---:|
| fd | 37 | 37 | 25 | 100.0% | 151 | 0 |
| oxipng | 21 | 21 | 4 | 100.0% | 14 | 0 |
| ripgrep | 117 | 117 | 29 | 100.0% | 0 | 0 |

## Highest long-outcome byte volume

| Application | Function | Requested size | Allocations | Long outcomes | Long byte share | Requested-byte hotness |
|---|---|---:|---:|---:|---:|---:|
| fd | `walk::DirEntryRaw::from_entry_os` | 12 | 20736 | 132 | 0.6% | 13.3% |
| fd | `dir::Ignore::add_child` | 536 | 257 | 1 | 0.4% | 7.4% |
| fd | `dir::IgnoreBuilder::build` | 536 | 1 | 1 | 100.0% | 0.0% |
| oxipng | `reduction::alpha::reduced_alpha_channel` | 280 | 1 | 1 | 100.0% | 0.0% |
| oxipng | `png::PngData::from_slice` | 176 | 1 | 1 | 100.0% | 0.0% |
| oxipng | `perform_reductions` | 176 | 1 | 1 | 100.0% | 0.0% |
| fd | `dir::IgnoreBuilder::new` | 136 | 1 | 1 | 100.0% | 0.0% |
| fd | `dir::IgnoreBuilder::overrides` | 120 | 1 | 1 | 100.0% | 0.0% |
| fd | `dir::IgnoreBuilder::build` | 120 | 1 | 1 | 100.0% | 0.0% |
| oxipng | `optimize_png` | 112 | 1 | 1 | 100.0% | 0.0% |
| oxipng | `<OutFile as std::clone::Clone>::clone` | 96 | 1 | 1 | 100.0% | 0.0% |
| oxipng | `reduction::alpha::reduced_alpha_channel` | 88 | 1 | 1 | 100.0% | 0.0% |
| oxipng | `optimize_png` | 88 | 1 | 1 | 100.0% | 0.0% |
| fd | `dir::IgnoreBuilder::build` | 80 | 1 | 1 | 100.0% | 0.0% |
| fd | `gitignore::GitignoreBuilder::build` | 64 | 1 | 1 | 100.0% | 0.0% |
| fd | `types::Types::empty` | 64 | 1 | 1 | 100.0% | 0.0% |
| fd | `walk::WorkerState::spawn_senders::{closure#0}` | 56 | 2 | 1 | 50.0% | 0.0% |
| oxipng | `optimize` | 56 | 1 | 1 | 100.0% | 0.0% |
| oxipng | `optimize_png` | 52 | 1 | 1 | 100.0% | 0.0% |
| oxipng | `optimize_png` | 48 | 1 | 1 | 100.0% | 0.0% |

## Strongest feature associations

| Feature | Present sites | Decisive bytes | Present long share | Absent long share | Difference |
|---|---:|---:|---:|---:|---:|
| `owner_place_basis=destination_owner_place_unresolved` | 13 | 2661 | 25.6% | 0.0% | 25.6% |
| `reachable_cleanup_blocks_count=1` | 8 | 576 | 20.8% | 0.0% | 20.8% |
| `reachable_cleanup_blocks_count=2-3` | 42 | 4935 | 7.7% | 0.0% | 7.7% |
| `reachable_cleanup_blocks_count=4+` | 43 | 15479 | 7.7% | 0.0% | 7.7% |
| `callee_family=box_new` | 58 | 164184 | 1.5% | 0.0% | 1.4% |
| `destination_family=box` | 58 | 164184 | 1.5% | 0.0% | 1.4% |
| `requested_layout_basis=exact_box_new_payload_layout` | 58 | 164184 | 1.5% | 0.0% | 1.4% |
| `cleanup_successor_count=1` | 119 | 201008 | 1.4% | 0.0% | 1.4% |
| `destination_family=vec` | 65 | 49427 | 1.0% | 0.0% | 1.0% |
| `reachable_normal_blocks_count=2-3` | 57 | 159095 | 0.7% | 0.0% | 0.7% |
| `escape_sink_blocks_count=0` | 166 | 1944153 | 0.2% | 0.0% | 0.2% |
| `owner_place_basis=semantic_destination_owner` | 151 | 1941388 | 0.2% | 0.0% | 0.2% |
| `store_sink_blocks_count=1` | 151 | 1941388 | 0.2% | 0.0% | 0.2% |
| `store_sink` | 152 | 1947148 | 0.2% | 0.0% | 0.2% |
| `reachable_normal_blocks_count=1` | 63 | 1767792 | 0.1% | 0.0% | 0.1% |
| `callee_family=other` | 41 | 1730438 | 0.1% | 0.0% | 0.1% |
| `cleanup_drop_blocks_count=0` | 156 | 25050340 | 0.0% | 0.0% | 0.0% |
| `escape_sink_blocks_count=2-3` | 2 | 99 | 0.0% | 0.0% | -0.0% |
| `allocation_in_natural_loop` | 3 | 216 | 0.0% | 0.0% | -0.0% |
| `escape_sink_blocks_count=1` | 3 | 216 | 0.0% | 0.0% | -0.0% |

## Evidence boundaries

- The compiler join uses exact `(callsite, type_id, module_id)` identity; every runtime size/alignment row remains separate.
- Concrete compiler layout claims receive an exact five-field check. Dynamic layouts remain explicit abstentions.
- Completed indeterminate outcomes, live right-censored objects, and bypassed allocations stay outside Short/Long denominators.
- Feature correlations are observational. They rank hypotheses for a held-out classifier and carry no causal claim.
- When any allocation is bypassed, outcome shares and feature associations are conditioned on the runtime-selected tracked stream. Population-prevalence claims require force-track-all evidence or valid inclusion-probability weighting.
- Exact Drop establishes a scoped owner path and carries no Short-duration claim. `mem::forget`/`Box::leak` remain bounded oracle cases.
- Allocation count, requested bytes, and pressure span are allocation-hotness proxies. Access hotness is unavailable.
