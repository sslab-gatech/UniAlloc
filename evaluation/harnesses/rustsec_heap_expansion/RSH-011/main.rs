//! Mechanical payload-strengthening adapter for Rudra-PoC 0001.
//!
//! Origin: Rudra-PoC commit 6226dd030fffbed5601099cb0e24f73e4150a7f5,
//! `poc/0001-http.rs` (RUSTSEC-2019-0034). The adapter isolates the published
//! forgotten-Drain edge and adds a `Box<u64>` to make duplicate reclaim visible
//! to AddressSanitizer. Dropping the first yielded pair preserves the PoC's
//! ownership transition before the `Drain` itself is forgotten.

#![forbid(unsafe_code)]

use http::header::HeaderMap;

struct DropDetector(u32, #[allow(dead_code)] Box<u64>);

impl Drop for DropDetector {
    fn drop(&mut self) {
        eprintln!("Dropping {}", self.0);
    }
}

fn main() {
    let mut map = HeaderMap::with_capacity(32);
    map.insert("1", DropDetector(1, Box::new(1)));
    map.insert("2", DropDetector(2, Box::new(2)));

    let mut drain = map.drain();
    let first = drain.next().expect("the map contains two entries");
    drop(first);
    std::mem::forget(drain);
}
