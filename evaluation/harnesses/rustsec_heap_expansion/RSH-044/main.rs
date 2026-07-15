//! Mechanical payload-strengthening adapter for Rudra-PoC 0123.
//!
//! Origin: Rudra-PoC commit 6226dd030fffbed5601099cb0e24f73e4150a7f5,
//! `poc/0123-qwutils.rs` (RUSTSEC-2021-0018). The original `u32` payload
//! makes the duplicate drop visible only through logging. Adding a `Box<u64>`
//! preserves the panic path and gives AddressSanitizer an allocator-visible
//! double-free oracle.

#![forbid(unsafe_code)]

use qwutils::*;

struct DropDetector(u32, #[allow(dead_code)] Box<u64>);

impl Drop for DropDetector {
    fn drop(&mut self) {
        eprintln!("Dropping {}", self.0);
    }
}

impl Clone for DropDetector {
    fn clone(&self) -> Self {
        panic!("DropDetector {} panic on clone()", self.0);
    }
}

fn main() {
    let mut destination = vec![DropDetector(1, Box::new(11)), DropDetector(2, Box::new(22))];
    let source = [DropDetector(3, Box::new(33))];
    destination.insert_slice_clone(0, &source);
}
