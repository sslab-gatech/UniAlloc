//! RUSTSEC-2020-0091 / arc-swap 1.0.0.
//!
//! The exact executable body from Rudra-PoC 0059. Vulnerable `access::Map`
//! returns a guard that points into a temporary stack value. Miri reports the
//! dangling reference when the loaded guard is constructed; arc-swap 1.1.0
//! owns the projection guard correctly and completes the same program.

use arc_swap::access::Map;
use arc_swap::access::{Access, Constant};

static CORRECT_ADDR: &str = "I'm pointing to the correct location!";

#[derive(Clone)]
struct MemoryChecker {
    message: &'static str,
}

impl MemoryChecker {
    fn new() -> Self {
        Self {
            message: CORRECT_ADDR,
        }
    }

    fn validate(&self) {
        println!(
            "Pointing to {:?}, len {}",
            self.message as *const _,
            self.message.len()
        );
        println!("Message: {}", self.message);
    }
}

fn overwrite() {
    let a = 123;
    let b = 456;
    println!("Overwriting stack content {} {}", a, b);
}

fn main() {
    let constant = Constant(MemoryChecker::new());
    constant.0.validate();

    let map = Map::new(constant, |checker: &MemoryChecker| checker);
    let loaded = map.load();

    overwrite();
    loaded.validate();
}
