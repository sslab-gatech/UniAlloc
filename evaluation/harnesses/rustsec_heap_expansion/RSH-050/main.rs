//! RUSTSEC-2019-0014 / image 0.21.2.
//!
//! Minimal truncated-input adapter for upstream issue 980 and the 0.21-line
//! fix. A valid one-pixel Radiance HDR header allocates the generic output
//! vector; the missing scanline returns an error before any Box is written.
//! Vulnerable image sets the vector length first and drops that uninitialized
//! Box, giving AddressSanitizer a heap-visible oracle.

use image::hdr::HDRDecoder;
use std::io::Cursor;

fn main() {
    let input = b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 1 +X 1\n";
    let decoder = HDRDecoder::new(Cursor::new(&input[..])).expect("valid HDR header");
    let result = decoder.read_image_transform(|_| Box::new(0x50_u64));
    assert!(result.is_err(), "the truncated scanline must fail");
}
