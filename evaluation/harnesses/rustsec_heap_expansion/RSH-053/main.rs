//! Mechanical payload-strengthening adapter for Rudra-PoC 0012.
//!
//! Origin: Rudra-PoC commit 6226dd030fffbed5601099cb0e24f73e4150a7f5,
//! `poc/0012-ordnung.rs` (RUSTSEC-2020-0038). The original logging-only
//! `DropDetector` is augmented with a `Box<u64>` so the published invalid-index
//! panic makes the duplicate reclaim visible to AddressSanitizer.

#![forbid(unsafe_code)]

use ordnung::compact::Vec as CompactVec;

struct DropDetector(u32, #[allow(dead_code)] Box<u64>);

impl Drop for DropDetector {
    fn drop(&mut self) {
        eprintln!("Dropping {}", self.0);
    }
}

fn main() {
    let mut compact_vec = CompactVec::with_capacity(32);
    compact_vec.push(DropDetector(123, Box::new(123)));
    compact_vec.push(DropDetector(456, Box::new(456)));
    compact_vec.remove(123);
}
