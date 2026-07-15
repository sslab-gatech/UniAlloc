//! Advisory-derived panic-safety witness for RUSTSEC-2021-0028
//! (`toodee` 0.2.1).
//!
//! Vulnerable `insert_row` shifts the existing row with `ptr::copy` before it
//! consumes the caller's iterator.  At row zero, that shift leaves duplicate
//! owners inside the vector's published length.  This iterator yields one
//! element and then panics, so unwinding drops both copies of an existing
//! `Box` owner.  `toodee` 0.3.0 publishes a shortened length before the shift,
//! making the same intentional panic a safe patched control.

#![forbid(unsafe_code)]

use toodee::TooDee;

struct OwnedValue {
    id: u32,
    #[allow(dead_code)]
    owner: Box<u64>,
}

impl OwnedValue {
    fn new(id: u32) -> Self {
        Self {
            id,
            owner: Box::new(u64::from(id)),
        }
    }
}

impl Drop for OwnedValue {
    fn drop(&mut self) {
        eprintln!("dropping owned value {}", self.id);
    }
}

struct PartialThenPanic {
    yielded: bool,
}

impl Iterator for PartialThenPanic {
    type Item = OwnedValue;

    fn next(&mut self) -> Option<Self::Item> {
        if self.yielded {
            panic!("intentional iterator panic after one yielded item");
        }
        self.yielded = true;
        Some(OwnedValue::new(99))
    }
}

impl ExactSizeIterator for PartialThenPanic {
    fn len(&self) -> usize {
        2
    }
}

fn main() {
    let values = vec![
        OwnedValue::new(1),
        OwnedValue::new(2),
        OwnedValue::new(3),
        OwnedValue::new(4),
    ];
    let mut matrix: TooDee<_> = TooDee::from_vec(2, 2, values);
    matrix.insert_row(0, PartialThenPanic { yielded: false });
}
