//! Matched source for openssl 0.10.70's corrected lifetime signature.

#![forbid(unsafe_code)]

use openssl::ssl;

fn main() {
    let server = vec![2, b'h', b'2'];
    let selected = ssl::select_next_proto(&server, b"\x02h2").expect("matching protocol");
    std::hint::black_box(selected[0]);
}
