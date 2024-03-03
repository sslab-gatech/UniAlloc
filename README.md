# UNIALLOC: A Retargetable Rust Memory Allocator

## Build

```bash
$ cargo build
```

## Test and Benchmarking

- 1. Disable system-wide restartable-sequence

```bash
$ export GLIBC_TUNABLES=glibc.pthread.rseq=0
```

- 2. Use unialloc as GlobalAllocator

```rust
use unialloc::UniAlloc;
#[global_allocator]
static OURSELF: UniAlloc = UniAlloc;

// more examples in unialloc/examples
```

- 3. Unit tests

```bash
$ cargo test
```

- 4. benchmarking

```bash
$ cargo bench --bench std_bench
```
