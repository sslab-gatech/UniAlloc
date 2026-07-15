//! Matched source for rust-i18n-support 3.0.1's guarded `AtomicStr` API.

#![forbid(unsafe_code)]

use rust_i18n_support::AtomicStr;

fn main() {
    let original = "A".repeat(4096);
    let current = AtomicStr::new(&original);
    let guarded = current.as_str();
    current.replace("B".repeat(4096));
    assert_eq!(guarded.as_bytes()[0], b'A');
}
