//! RUSTSEC-2022-0078 / bumpalo 3.11.0.
//!
//! This mechanical standalone adapter adds arena churn and a value assertion
//! to the advisory witness. The iterator lifetime can outlive the arena, and
//! subsequent arenas encourage reuse before the stale reads.

use bumpalo::{collections::Vec, Bump};
fn main() {
    let bump = Bump::new();
    let mut vec = Vec::new_in(&bump);
    vec.extend([0x01u8; 32]);
    let into_iter = vec.into_iter();
    drop(bump);

    for _ in 0..100 {
        let reuse_bump = Bump::new();
        let _reuse_alloc = reuse_bump.alloc([0x41u8; 10]);
    }

    let observed: std::vec::Vec<u8> = into_iter.collect();
    eprintln!("observed={observed:02x?}");
    assert_eq!(
        observed,
        vec![0x01; 32],
        "arena storage was reused after free"
    );
}
