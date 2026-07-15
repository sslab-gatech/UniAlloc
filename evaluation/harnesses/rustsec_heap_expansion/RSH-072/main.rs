//! Root-cause witness for rkyv's pre-0.8.16 panic-unsafe `InlineVec::clear`.
//!
//! A panicking destructor interrupts `clear` before the vulnerable release
//! resets its length. Dropping the vector then reclaims the same `Box` again.

#![forbid(unsafe_code)]

use rkyv::util::InlineVec;
use std::panic::{catch_unwind, AssertUnwindSafe};

struct PanicOnDrop {
    panic_once: bool,
    payload: Box<u64>,
}

impl Drop for PanicOnDrop {
    fn drop(&mut self) {
        if self.panic_once {
            self.panic_once = false;
            panic!("intentional panic from Drop");
        }
        eprintln!("payload={}", self.payload);
    }
}

fn main() {
    let mut values = InlineVec::<PanicOnDrop, 2>::new();
    values.push(PanicOnDrop {
        panic_once: true,
        payload: Box::new(7),
    });
    let result = catch_unwind(AssertUnwindSafe(|| values.clear()));
    assert!(result.is_err());
    drop(values);
}
