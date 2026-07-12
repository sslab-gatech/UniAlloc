# PAC no_std build contract

This standalone package compiles the real `pac_metadata_probe_nostd` binary
with UniAlloc as a normal dependency.  Keeping it outside the main workspace is
intentional: UniAlloc's benchmark/test-only dependencies (`rand`, `bencher`,
`iai`, and related crates) are not part of this target graph.

On a nightly that exposes `arm64e-apple-darwin` and has `rust-src`, run:

```sh
CARGO_TARGET_DIR="$(mktemp -d)" \
  cargo +"$(cat rust-toolchain)" run \
  -Z build-std=core,alloc,panic_abort \
  --manifest-path tools/pac-nostd-contract/Cargo.toml \
  --target arm64e-apple-darwin \
  --features pac,stats
```

A passing JSON event proves that the allocator metadata-auth and typed
side-cache reuse path ran in a no_std arm64e process.  It does not measure PAC
cost and must not be used as a performance claim.  On a non-arm64e target, the
same probe validates the fail-closed software authenticator rather than
hardware PAC.
