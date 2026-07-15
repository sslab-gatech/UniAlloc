# RSH-059 execution blocker

- Advisory: RUSTSEC-2021-0022 (`yottadb` 1.1.0; fixed in 1.2.0).
- Stable blocker code: `yottadb_native_runtime_missing`.
- `origin.rs` retains the exact advisory witness from RustSec advisory-db
  commit `9f3e138091487e69144f536d36976e427a7a3307`.
- The witness requires a configured YottaDB database because the stale pointer
  arises during a retry of `ydb_subscript_next_st` after the destination
  buffer is resized.
- Both pinned archive variants stop in their build scripts because
  `pkg-config --libs --cflags yottadb` cannot find `yottadb.pc` on this host.
  The host also lacks an initialized YottaDB runtime for the database setup in
  the published witness.
- The source and lockfiles are retained for a native-runtime worker. This case
  is excluded from executable baseline counts on the current host.
