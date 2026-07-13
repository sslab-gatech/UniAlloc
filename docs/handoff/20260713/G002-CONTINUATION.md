# G002 continuation handoff — 2026-07-13

## Pull target

```bash
git fetch origin
git switch --track origin/g002-functional-correctness-20260713
# If the branch already exists locally:
# git switch g002-functional-correctness-20260713 && git pull --ff-only
```

Remote: `git@github.com:sslab-gatech/UniAlloc.git`

## Goal state

- `G001-users-hqzhao-downloads-rust-alloc-pa` remains `in_progress` but is
  explicitly `superseded`; it must never be reported as complete.
- `G002-unialloc-functional-correctness-and` is the active implementation-first
  goal.
- Full paper-performance reproduction and the 168-cell/1008-run campaign are
  intentionally deferred. Reduced runs are diagnostic only.
- The exact OMX goal snapshot and the append-only G001 stop record are copied
  beside this file. The local `.omx/` directory is not committed because it is
  762 MiB of runtime state and reproducible/raw artifacts.

## Important commits on this branch

- `f8966ca` — validated hidden `slice::IterMut<u8>.zip(Vec<u8>)` ownership
  transfer and fail-closed Drop provenance; its commit hook observed allocator
  `701/701` and finite compiler inventory `430/430`.
- `c820338` — **WIP checkpoint only**, type-isolation address lifecycle and
  reclaim hardening. This commit was deliberately made with `--no-verify` so
  the incomplete security repair can be continued from another machine.
- `6b73a55` — bounded diagnostic evaluation tooling and source-binding tests.
  Python verification after the final edits: `430/430` unit tests, `py_compile`,
  JSON parse, and `git diff --check` passed. It was committed with `--no-verify`
  only because the preceding P0 WIP makes the repository-wide Cargo hook fail.
- `9072fb3` — fail-first regression proving that cold `Released` lifecycle
  entries exhaust reclaim admission when more than the bounded capacity is
  consumed without exact-address reuse. The test currently fails with
  `Released history must not exhaust reclaim admission: Full`; this is the next
  P0 implementation target, not a passing correctness claim.

## P0 WIP: do not claim complete

Independent final review rejected `c820338` for two concrete gaps:

1. Terminal release leaves cold `Released` entries in the bounded registry until
   exact address reuse. More than capacity distinct terminal releases without
   reuse can still exhaust the table; the current >2x-capacity test immediately
   reuses each synthetic address and does not cover this case.
2. Recovery identity may be read before durable reclaim admission. A release +
   exact reuse between recovery lookup and admission can let a stale
   grow/realloc/dealloc act on the replacement object. Admission must bind the
   exact observation made before recovery lookup, or ownership must be acquired
   before lookup with a safe rollback.

The repository-wide pre-commit `cargo test` on `c820338` ran 706 allocator tests
and failed 11 (695 passed). Known failures:

- `delayed_to_type_handoff_rejects_forced_reclaim_entrypoint_race`
- `foreign_thread_raw_reclaim_of_real_retained_entry_fails_stop`
- `global_dealloc_corrupt_tag_preserves_exact_recovery_for_retry`
- `memory_tagging_global_rejected_dealloc_preserves_record_for_correct_retry`
- `memory_tagging_rejected_layout_dealloc_preserves_record_for_correct_retry`
- `memory_tagging_rejected_type_dealloc_preserves_record_for_correct_retry`
- `pending_cross_thread_quarantine_ownership_blocks_foreign_dealloc_before_tls_publish`
- `retained_type_cache_ownership_rejects_raw_reclaim_and_preserves_exact_reuse`
- `type_cache_owned_pointer_globalalloc_entrypoints_fail_stop`
- `type_cache_owned_pointer_rejects_raw_reclaim_without_mutation`
- `cache::tests::allocator_alignment_change_consumes_one_unknown_compiler_stream_entry`

Do not mask these by weakening expected panic reasons or deleting recovery retry
checks. Fix the lifecycle transaction and then make the original regressions pass.

## Fast next sequence

1. Add a fail-first test with more than 2x table capacity **distinct terminal
   releases before any reuse**; implement bounded safe retirement.
2. Add a test pause after recovery lookup but before admission for alignment-
   changing grow and one semantic deallocation/realloc path; bind a pre-lookup
   observation/token through mutation, rollback, and terminal release.
3. Run targeted hosted and fixed-heap tests, then repository-wide `cargo test`.
4. Only after a clean source commit, run exactly once:

```bash
python3 tools/unialloc-rustc-pass/test_mir_realistic_multimodule_type_isolation.py
```

5. Then run one pinned Oxipng v4.0.3 actual-wrapper functional one-shot; no
   timing loop or retry campaign. Finally run the finite 430-name gate once.

## Existing bounded real-Rust evidence

The last clean source-bound Oxipng artifact before this WIP is documented in
`docs/presentation-outline.md` as
`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-head-8e74f72-20260713-one-shot/`.
It built and ran once, matched the output hash, executed typed allocation paths,
and passed the injected wrong-type non-reuse/exact-type reuse oracle. It also
retained unresolved rows and `whole_program_compiler_coverage=false`; it is
functional evidence only and must not be rebound to `c820338`.

## Local-only exclusions

These were deliberately not pushed because they are not necessary source:

- root `api`: generated Mach-O arm64 executable;
- `evaluation/external/raw/`: older reproducible Oxipng raw logs/rewrite JSON;
- local deletion of `docs/diagram.png`: unclassified, so the tracked image is
  preserved on the remote branch;
- `.omx/`: runtime state/raw evidence; only the goal and stop snapshots needed
  for continuation are copied here.

No credentials, `target/`, Swoop campaign data, or formal performance matrix
records are included.

## Additional remote preservation branches

These branches are intentionally separate from the G002 continuation branch:

- `origin/g001-source-freeze-235549b2` at `0df377b224d660cc938f024654eb7d1e52649eaa`
  preserves the clean G001 historical source-freeze line. Do not merge it into
  G002 or reinterpret its evidence as current-source evidence.
- `origin/wip/pac-nostd-runner-20260713` at
  `5598ee78cea9eb0709fc6c478b338e8ea5368e61` preserves the only useful
  unmerged dirty-worktree change found by the cross-machine audit: a standalone
  no_std PAC probe runner that avoids package dev-target dependencies. It is a
  WIP candidate and is not part of G002 until reviewed/cherry-picked.

The remaining local branch names and temporary worktrees were not pushed:
their commits are already present or patch-equivalent on G002, are superseded
G001 candidates, or contain generated/raw diagnostic files rather than needed
source. No unique reviewed G002 source change was left only in those worktrees.
