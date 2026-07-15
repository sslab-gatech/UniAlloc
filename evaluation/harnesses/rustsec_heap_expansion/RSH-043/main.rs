//! RUSTSEC-2026-0131 / bitchomp 0.3.1.
//!
//! This is the upstream issue witness with explicit drop order so the duplicate
//! ownership created by `Chomp::inner` reaches the allocator deterministically.

use bitchomp::Chomp;

fn main() {
    let original = Box::new(123_i32);
    let chomp = Chomp::new(&original);

    // Vulnerable bitchomp moves the same Box ownership out through a raw pointer.
    let duplicate_owner = chomp.inner();

    drop(duplicate_owner);
    drop(original);
}
