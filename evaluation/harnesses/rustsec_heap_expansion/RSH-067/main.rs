//! Advisory-shape witness for RUSTSEC-2025-0004 (`openssl` 0.10.69).
//!
//! The vulnerable signature binds the returned protocol slice only to
//! `client`, allowing it to escape a shorter-lived heap-backed `server` list.

#![forbid(unsafe_code)]

use openssl::ssl;

fn dangling_selection<'a>(client: &'a [u8]) -> &'a [u8] {
    let server = vec![2, b'h', b'2'];
    ssl::select_next_proto(&server, client).expect("matching protocol")
}

fn main() {
    let selected = dangling_selection(b"\x02h2");
    std::hint::black_box(selected[0]);
}
