//! Allocator-visible adapter for the upstream RUSTSEC-2020-0145 PoC.
//!
//! Origin: rust-embedded/heapless issue #181. The published witness uses a
//! logging payload. Adding a `Box<u64>` makes cloning the already-consumed
//! iterator slot produce an AddressSanitizer heap-use-after-free finding.

#![forbid(unsafe_code)]

use heapless::consts::U16;
use heapless::Vec;

#[derive(Debug)]
struct DropDetector {
    id: u32,
    payload: Box<u64>,
}

impl Clone for DropDetector {
    fn clone(&self) -> Self {
        eprintln!("Cloning {}", self.id);
        Self {
            id: self.id,
            payload: self.payload.clone(),
        }
    }
}

impl Drop for DropDetector {
    fn drop(&mut self) {
        eprintln!("Dropping {}", self.id);
    }
}

fn main() {
    let mut values: Vec<DropDetector, U16> = Vec::new();
    values
        .push(DropDetector {
            id: 1,
            payload: Box::new(1),
        })
        .unwrap();
    values
        .push(DropDetector {
            id: 2,
            payload: Box::new(2),
        })
        .unwrap();
    values
        .push(DropDetector {
            id: 3,
            payload: Box::new(3),
        })
        .unwrap();

    let mut iter = values.into_iter();
    drop(iter.next());
    let _clone = iter.clone();
}
