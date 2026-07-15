//! RUSTSEC-2022-0094 / mimalloc 0.1.32.
//!
//! This is the standalone form of the upstream attached project. The affected
//! wrapper routes a 4096-aligned request through the ordinary mimalloc API.
//! The failing iteration is environment-specific; the oracle records the
//! first 4096-alignment violation in raw output.

use mimalloc::MiMalloc;

#[global_allocator]
static GLOBAL: MiMalloc = MiMalloc;

fn main() {
    for iteration in 0..100_000 {
        let size = 4096;
        let alignment = 1 << (iteration % 16);
        let layout = std::alloc::Layout::from_size_align(size, alignment).expect("valid layout");
        let pointer = unsafe { std::alloc::alloc(layout) };
        assert!(!pointer.is_null(), "allocation failed");
        assert_eq!(
            (pointer as usize) % alignment,
            0,
            "iteration={iteration} alignment={alignment}"
        );
    }
}
