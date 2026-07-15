//! Standalone adapter for the regression test added by rust-openssl PR #2390.

#![forbid(unsafe_code)]

use openssl::md::Md;

fn main() {
    assert!(
        Md::fetch(None, "SHA-256", Some("provider=gibberish")).is_err(),
        "freed properties were treated as an empty query"
    );
}
