//! Minimal upstream-root-cause witness for RUSTSEC-2023-0054
//! (`mail-internals` 0.2.3).
//!
//! `vec_insert_bytes` computes `insertion_point` before `Vec::reserve`; this
//! input forces reallocation and then makes the function copy through the stale
//! pointer.

#![forbid(unsafe_code)]

use mail_internals::utils::vec_insert_bytes;

fn main() {
    let mut target = Vec::with_capacity(4);
    target.extend_from_slice(b"abcd");
    let inserted = vec![b'X'; 4096];
    vec_insert_bytes(&mut target, 2, &inserted);
    std::hint::black_box(target);
}
