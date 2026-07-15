//! RUSTSEC-2021-0030 / scratchpad 1.3.0.
//!
//! Heap-owning payload adapter for Rudra-PoC 0141. The published closure panic
//! makes vulnerable `SliceMoveSource::move_elements` drop its moved value
//! twice. A Box payload turns the duplicate destructor into an ASan oracle;
//! scratchpad 1.3.1 contains the upstream unwind-safety fix.

use scratchpad::{SliceLike, SliceMoveSource};

#[derive(Clone, Debug)]
struct HeapOwner(Box<i32>);

impl Drop for HeapOwner {
    fn drop(&mut self) {
        eprintln!("dropping {}", self.0);
    }
}

fn main() {
    let mut values = [HeapOwner(Box::new(1234)); 1];
    values.move_elements(|_| {
        panic!("published move_elements panic");
    });
}
