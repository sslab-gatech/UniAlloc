//! RUSTSEC-2019-0016 / chttp 0.1.2.
//!
//! The advisory's affected `From<Buffer> for Vec<u8>` conversion returns a
//! vector whose allocation was already freed by a temporary owner.  Dropping
//! that vector gives ASan a deterministic double-free oracle.  The Buffer
//! module was private in both releases, so the harness catalog applies the
//! same visibility-only source patch to 0.1.2 and the 0.1.3 control.

#![forbid(unsafe_code)]

use chttp::buffer::Buffer;

fn main() {
    let mut buffer = Buffer::new();
    buffer.push(b"unialloc-rustsec");
    let converted: Vec<u8> = buffer.into();
    std::hint::black_box(converted);
}
