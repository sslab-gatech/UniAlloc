//! RUSTSEC-2019-0009 / smallvec 0.6.9.
//!
//! The standalone reproducer published in upstream issue 148. The final
//! diagnostic print also forces a read from the backing allocation that
//! `grow(4)` incorrectly freed before the vector's destructor frees it again.

use smallvec::SmallVec;

fn main() {
    let mut v: SmallVec<[u8; 2]> = SmallVec::new();
    v.push(0);
    v.push(1);
    v.push(2);
    assert_eq!(v.capacity(), 4);
    v.grow(4);
    eprintln!("{:?}", v);
}
