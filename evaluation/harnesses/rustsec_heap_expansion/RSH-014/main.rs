//! Mechanical payload-strengthening adapter for Rudra-PoC 0145.
//!
//! Origin: Rudra-PoC commit 6226dd030fffbed5601099cb0e24f73e4150a7f5,
//! `poc/0145-slice-deque.rs` (RUSTSEC-2021-0047). The published logging-only
//! `DropDetector` gains a `Box<u64>` so the duplicate drop after the predicate
//! panic becomes an allocator-visible duplicate reclaim.

#![forbid(unsafe_code)]

use slice_deque::SliceDeque;

struct DropDetector(u32, #[allow(dead_code)] Box<u64>);

impl Drop for DropDetector {
    fn drop(&mut self) {
        eprintln!("Dropping {}", self.0);
    }
}

fn main() {
    let mut deque = SliceDeque::new();
    for value in 1..=3 {
        deque.push_back(DropDetector(value, Box::new(value as u64)));
    }

    let _drained = deque
        .drain_filter(|value| match value.0 {
            1 => true,
            2 => false,
            _ => panic!("predicate panicked!"),
        })
        .collect::<SliceDeque<_>>();
}
