# RSH-064 Neon Node/V8 witness

This directory contains the executable witness for RUSTSEC-2022-0028
(`neon` 0.8.0 through 0.10.0). The original standalone-runner blocker was
resolved by compiling the witness as an N-API addon and loading it from Node,
which supplies the required Neon `FunctionContext`.

## Pinned inputs

- `published_witness.rs` retains the advisory-shaped Rust function.
- `addon.rs` adds only the `#[neon::main]` export required by the Node host.
- `invoke.js` calls the addon, reads the external `ArrayBuffer`, creates
  allocation pressure, and reads the buffer again.
- `derived_reuse.rs` isolates the advisory's released `Vec<u8>` backing-store
  reuse edge with a four-byte distinct `Replacement` allocation.
- `derived_reuse_patched.rs` is the safe same-identity reuse control and keeps
  no external view after the first `Vec<u8>` is released.
- `Cargo.lock.vulnerable` and `Cargo.lock.patched` are feature-appropriate
  N-API locks for pinned neon 0.10.0 and 0.10.1 archives.
- `Cargo.lock.reclaim` pins the direct-UniAlloc diagnostic arms.
- `Cargo.lock.legacy-vulnerable` and `Cargo.lock.legacy-patched` preserve the
  earlier legacy-runtime locks that accompanied the blocked source record.

The archive URLs, byte lengths, SHA-256 digests, upstream revisions, source
digests, and lockfile digests are recorded in
`evaluation/config/rustsec_heap_neon_node_harnesses.json`.

## Oracle

The vulnerable source creates an external `ArrayBuffer` from a stack-borrowed
view of a `Vec`, drops the `Vec`, and returns the buffer to JavaScript. The
expected payload is `0,1,2,3`; any different four-byte value observed by the
Node driver is a root use-after-free symptom. neon 0.10.1 adds a `'static`
bound and rejects the same source with Rust errors E0597 and E0505.

The direct `reclaim_plain` and `reclaim_checks` arms retain the exact witness
and add only the selected global allocator. This vulnerability performs one
valid free followed by a stale V8 read, so absence of a duplicate-reclaim
diagnostic is an expected mechanism result.

The `typed_plain` and `typeiso` arms use the existing compiler wrappers and
retain their compiler audits. Both currently expose the corrupt bytes. The
critical `Vec` allocation and Neon external-buffer transfer receive no
supported allocation rewrite, so the Type Isolation result is classified as
compiler-coverage inconclusive rather than as a mitigation failure.

## Derived Type Isolation edge

`RSH-064-derived-reuse` is a separate, manually attributed experiment tied to
the advisory's stale Rust backing-store mechanism. The vulnerable arm exposes
the victim address through an `ExternalView`, releases its `Vec<u8>` owner,
requests an equal-layout distinct Rust `Replacement`, and performs the stale
read only when the addresses collide. The patched arm requests a second
`Vec<u8>` under the same identity and retains no stale view.

A validated six-arm matrix establishes one bounded result: exact-identity
cache routing blocks and reports the exploit-enabling cross-type reuse edge.
The manual victim annotation and the remaining source-level stale-pointer
behavior stay outside the automatic-coverage claim.

## Reproduction

List or validate the pinned inputs without executing the witness:

```console
uv run python evaluation/scripts/run_rsh064_neon_witness.py --action list
uv run python evaluation/scripts/run_rsh064_neon_witness.py \
  --action preflight --allow-download --output-dir /tmp/rsh064-preflight
```

Execute the memory-unsafe vulnerable arms only with the explicit opt-in:

```console
uv run python evaluation/scripts/run_rsh064_neon_witness.py \
  --action run --allow-download --execute-unsafe --repetitions 3 \
  --output-dir /tmp/rsh064-run
```

The dedicated runner records build output, Node output, exact input hashes,
the UniAlloc implementation digest, and the expected patched compile
rejection. Results remain non-claim-grade until the complete-scope ledger is
regenerated and independently verified.

Run the derived six-arm matrix through its pinned entry point:

```console
uv run python evaluation/scripts/run_rsh064_derived_reuse.py \
  --action preflight --allow-download --repetitions 3 \
  --variants system,typed_plain,typeiso \
  --archive-variants vulnerable,patched \
  --output-dir /tmp/rsh064-derived-preflight

uv run python evaluation/scripts/run_rsh064_derived_reuse.py \
  --action run --allow-download --execute-unsafe --repetitions 3 \
  --variants system,typed_plain,typeiso \
  --archive-variants vulnerable,patched \
  --output-dir /tmp/rsh064-derived-run
```
