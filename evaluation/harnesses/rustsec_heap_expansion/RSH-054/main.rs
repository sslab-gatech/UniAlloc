//! Mechanical payload-strengthening adapter for Rudra-PoC 0078.
//!
//! Origin: Rudra-PoC commit 6226dd030fffbed5601099cb0e24f73e4150a7f5,
//! `poc/0078-abi_stable.rs` (RUSTSEC-2020-0105). Rudra recorded the
//! `DrainFilter` root cause without a runnable PoC. This adapter instantiates
//! the cited rust-lang/rust#60977 panic sequence through `RVec::retain` and
//! gives each element an allocator-visible `Box<u64>` payload.

#![forbid(unsafe_code)]

use abi_stable::std_types::RVec;

struct DropDetector {
    id: u8,
    #[allow(dead_code)]
    payload: Box<u64>,
}

impl Drop for DropDetector {
    fn drop(&mut self) {
        eprintln!("Dropping {}", self.id);
    }
}

fn main() {
    let mut values: RVec<_> = [0, 1, 4, 5, 6]
        .iter()
        .map(|&id| DropDetector {
            id,
            payload: Box::new(id as u64),
        })
        .collect::<Vec<_>>()
        .into();

    values.retain(|value| {
        if value.id == 4 {
            panic!("published drain-filter panic point");
        }
        value.id >= 4
    });
}
