//! Deterministic advisory-root-cause witness for RUSTSEC-2024-0007
//! (`rust-i18n-support` 3.0.0).
//!
//! `AtomicStr::as_str` returns an unguarded reference. Replacing the `Arc` and
//! dropping the returned old owner leaves that reference dangling.

#![forbid(unsafe_code)]

use rust_i18n_support::AtomicStr;

fn main() {
    let current = AtomicStr::new("A".repeat(4096));
    let stale = current.as_str();
    let old_owner = current.replace("B".repeat(4096));
    drop(old_owner);
    std::hint::black_box(stale.as_bytes()[0]);
}
