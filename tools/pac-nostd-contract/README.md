# PAC no_std build contract

This standalone package compiles the real `pac_metadata_probe_nostd` binary
with UniAlloc as a normal dependency.  Keeping it outside the main workspace is
intentional: UniAlloc's benchmark/test-only dependencies (`rand`, `bencher`,
`iai`, and related crates) are not part of this target graph.
The committed lockfile preserves the allocator's pinned dependency graph even
when a pinned registry release has been yanked upstream.

On a nightly that exposes `arm64e-apple-darwin` and has `rust-src`, run:

```sh
CARGO_TARGET_DIR="$(mktemp -d)" \
  cargo +"$(cat rust-toolchain)" run \
  --locked \
  -Z build-std=core,alloc,panic_abort \
  --manifest-path tools/pac-nostd-contract/Cargo.toml \
  --target arm64e-apple-darwin \
  --features pac,stats
```

A passing JSON event proves that the allocator metadata-auth and typed
side-cache reuse path ran in a no_std arm64e process.  It does not measure PAC
cost and must not be used as a performance claim.  On a non-arm64e target, the
same binary can validate allocation and typed side-cache mechanics; PAC backend
validation remains gated on an arm64e run.
