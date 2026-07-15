//! RUSTSEC-2020-0007 / bitvec 0.17.3.
//!
//! `into_boxed_bitslice` saves the pre-shrink address, converts the backing
//! vector into a boxed slice, and then rebuilds the BitBox from the stale
//! address.  A moving shrink/reallocation turns the final access or drop into
//! a use-after-free/double-free.  Run this witness with Miri, ASan, or a
//! deterministic moving-realloc allocator.

use bitvec::prelude::*;
fn main() {
    let length = 8065;
    let mut bits = BitVec::with_capacity(length);
    bits.push(false);
    let _: BitBox = bits.into();
}
