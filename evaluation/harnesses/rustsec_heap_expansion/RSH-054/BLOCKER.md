# RSH-054 execution blocker

- Advisory: RUSTSEC-2020-0105 (`abi_stable` 0.9.0; fixed in 0.9.1).
- Stable blocker code: `published_reproducer_absent_and_public_retain_does_not_duplicate_reclaim`.
- The pinned Rudra record explicitly says that it reports the copied
  `DrainFilter` implementation without a proof of concept. The exact record is
  retained in `origin.rs` at Rudra-PoC commit
  `6226dd030fffbed5601099cb0e24f73e4150a7f5`.
- `upstream_drain_filter.rs` retains the rust-lang/rust#60977 reproducer that
  motivated the report. Its direct `Vec::drain_filter` control flow cannot be
  expressed through `abi_stable` because `DrainFilter` is crate-private.
- `main.rs` exercises the reachable `RVec::retain` path with heap-owning
  elements and the same predicate panic sequence. Both archive variants build
  with the pinned locks on `nightly-2022-07-01`.
- With `MIRIFLAGS="-Zmiri-ignore-leaks -Zmiri-disable-stacked-borrows"`, the
  0.9.0 arm reaches the intentional panic and reports no Miri undefined
  behavior or duplicate reclaim. The 0.9.1 arm has the same safety outcome.
  Default stacked-borrows diagnostics occur in both versions and therefore do
  not distinguish the advisory flaw.
- This case remains source evidence and is excluded from executable baseline
  counts until a public-API reproducer with a root-cause-specific oracle is
  available.
