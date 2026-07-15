//! Mechanical payload-strengthening adapter for Rudra-PoC 0157.
//!
//! Origin: Rudra-PoC commit 6226dd030fffbed5601099cb0e24f73e4150a7f5,
//! `poc/0157-id-map.rs` (RUSTSEC-2021-0052). This selects the published
//! `clone_from` panic edge and augments `DropDetector` with a `Box<u64>` so the
//! stale destination entry produces an allocator-visible duplicate reclaim.

#![forbid(unsafe_code)]

use id_map::IdMap;

struct DropDetector(u32, #[allow(dead_code)] Box<u64>);

impl Drop for DropDetector {
    fn drop(&mut self) {
        eprintln!("Dropping {}", self.0);
    }
}

impl Clone for DropDetector {
    fn clone(&self) -> Self {
        panic!("Panic on clone!");
    }
}

fn main() {
    let mut source = IdMap::new();
    source.insert(DropDetector(1, Box::new(1)));

    let mut destination = IdMap::new();
    destination.insert(DropDetector(2, Box::new(2)));
    destination.clone_from(&source);
}
