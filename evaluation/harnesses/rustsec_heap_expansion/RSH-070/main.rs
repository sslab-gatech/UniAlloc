//! Allocator-visible adapter for crossbeam PR #972's published race reproducer.
//!
//! The scenario patch restores the upstream two-second scheduling window. The
//! message changes from `u64` to `Box<u64>` so the RUSTSEC-2025-0024 duplicate
//! block reclamation has an unambiguous AddressSanitizer double-free oracle.

#![forbid(unsafe_code)]

use crossbeam_channel::unbounded;
use std::{thread, time::Duration};

fn main() {
    let (sender1, receiver) = unbounded::<Box<u64>>();
    let sender2 = sender1.clone();

    let thread1 = thread::spawn(move || assert!(sender1.send(Box::new(42)).is_err()));
    thread::sleep(Duration::from_millis(100));
    sender2.send(Box::new(42)).unwrap();
    drop(receiver);
    thread1.join().unwrap();
}
