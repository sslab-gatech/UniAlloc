//! Safe same-identity control for the derived RUSTSEC-2020-0145 reuse edge.
//!
//! heapless 0.6.1 clones only the unconsumed iterator tail. This control
//! allocates and reclaims the same one-word Vec payload, exercises the patched
//! clone, and requests another one-word Vec under the exact same manual
//! identity. It retains no stale pointer and controls for ordinary reuse.

use heapless::consts::U16;
use heapless::Vec as HeaplessVec;
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4837_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4837_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4837_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4837_0000_0004;
const REPLACEMENT_ALLOC_CALLSITE: u64 = 0x5253_4837_0000_0005;
const REPLACEMENT_RECLAIM_CALLSITE: u64 = 0x5253_4837_0000_0006;

pub(crate) fn with_vulnerability_edge_identity<R>(
    _type_id: u64,
    _module_id: u64,
    _callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    f()
}

pub(crate) fn report_vulnerability_edge_reuse_denial() {}

#[derive(Clone, Debug)]
struct DropDetector {
    payload: std::vec::Vec<u64>,
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

    let cloned = iter.clone();
    black_box(cloned);
    drop(iter);

    let replacement_seed = [0x4242_4242_4242_4242u64];
    let replacement = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_ALLOC_CALLSITE,
        || black_box(materialize)(&replacement_seed),
    );
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = replacement.as_ptr() as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement[0]);

    let reclaim_replacement: fn(std::vec::Vec<u64>) = reclaim_payload;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_RECLAIM_CALLSITE,
        || black_box(reclaim_replacement)(replacement),
    );
}
