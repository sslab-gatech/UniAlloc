//! Exact executable witness from Rudra-PoC 0140.
//!
//! Origin: Rudra-PoC commit 6226dd030fffbed5601099cb0e24f73e4150a7f5,
//! `poc/0140-through.rs` (RUSTSEC-2021-0049). `String` already carries an
//! allocator-visible heap allocation, so the witness requires no payload adapter.

#![forbid(unsafe_code)]

use through::through;

fn main() {
    let mut hello = String::from("Hello");
    through(&mut hello, |mut value| {
        value.push_str(" World!");
        panic!("Unexpected panic");
        #[allow(unreachable_code)]
        value
    });
}
