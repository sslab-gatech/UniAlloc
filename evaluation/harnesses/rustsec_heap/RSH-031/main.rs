//! RUSTSEC-2023-0017 / maligned 0.2.1.
//!
//! `align_first` allocates with A's alignment and returns `Vec<T>`.  Vec later
//! deallocates with T's smaller alignment. Miri, UniAlloc's exact layout/tag
//! record, or an allocator shim that compares allocation/deallocation layouts
//! supplies the oracle.

use maligned::{align_first, A256};

fn main() {
    let values: Vec<u8> = align_first::<u8, A256>(1009);
    assert_eq!((values.as_ptr() as usize) % 256, 0);
    assert_eq!(values.capacity(), 1009);
    drop(values);
}
