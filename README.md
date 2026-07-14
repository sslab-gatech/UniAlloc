# UniAlloc: A Retargetable Rust Memory Allocator

UniAlloc is a Rust memory allocator research prototype that carries optional
compiler-derived allocation semantics into reusable allocator policies and
platform backends.

## Repository Layout

- `unialloc/`: allocator library, tests, examples, and benchmarks.
- `alloc_macros/`: procedural macros used by the allocator.
- `tools/unialloc-rustc-pass/`: compiler integration and validation probes.
- `evaluation/`: reproducible evaluation drivers, workload adapters, and gates.
- `kernel/`: kernel integration and fixed root-filesystem fixtures.
- `docs/`: architecture, validation, evidence, and presentation material.

## Build

```bash
cargo build
```

The pinned toolchain is declared in `rust-toolchain`. Run the default test suite
directly:

```bash
cargo test
```

The private-rseq registration tests isolate themselves in child processes with
glibc registration disabled. Normal applications, the parent test process, and
the real-world evaluation retain libc-managed rseq. The production allocation
hot path currently makes no rseq call.

## Use UniAlloc as the Global Allocator

```rust
use unialloc::UniAlloc;

#[global_allocator]
static OURSELF: UniAlloc = UniAlloc;
```

Runnable examples are in `unialloc/examples/`.

## Evaluation and Benchmarks

```bash
cargo bench --bench std_bench
```

See `evaluation/README.md` for the evidence pipeline and
`tools/unialloc-rustc-pass/README.md` for compiler-assisted validation.

The current source-bound Type Isolation diagnostic covers pinned ripgrep, fd,
and Oxipng builds across native, jemalloc, mimalloc, UniAlloc, typed-without-
policy, Type Isolation, and coverage variants. Its per-application timing, RSS,
and allocation-event coverage results are documented in
`evaluation/README.md`; they are diagnostic evidence rather than a reproduction
of the paper's performance or 72.17% coverage result.
