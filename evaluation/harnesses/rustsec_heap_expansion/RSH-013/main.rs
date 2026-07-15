//! Lifecycle-stable executable adaptation of Rudra-PoC 0163.
//!
//! Origin: Rudra-PoC commit 6226dd030fffbed5601099cb0e24f73e4150a7f5,
//! `poc/0163-algorithmica.rs` (RUSTSEC-2021-0053). `String` already carries
//! allocator-visible ownership. The vulnerable sort call is unchanged; only
//! the terminal observation is made deterministic.

#![forbid(unsafe_code)]

fn main() {
    let mut values = vec![
        String::from("Hello"),
        String::from("World"),
        String::from("Rust"),
    ];

    algorithmica::sort::merge_sort::sort(&mut values);

    // Reclaim the corrupted owners directly. Inspecting their string contents
    // first makes the witness depend on whichever invalid UTF-8 bytes happen
    // to be observed, producing nondeterministic formatting panics before the
    // duplicate reclaim reaches the allocator boundary.
    drop(values);
}
