//! Safe matched control for the derived RUSTSEC-2022-0028 reuse edge.
//!
//! This control allocates and releases the same four-byte `Vec<u8>` backing
//! store, then requests another `Vec<u8>` under the same manual identity. It
//! retains no external view and controls for ordinary same-identity reuse.

use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4840_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4840_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4840_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4840_0000_0004;
const REPLACEMENT_ALLOC_CALLSITE: u64 = 0x5253_4840_0000_0005;
const PAYLOAD_SIZE: usize = 4;

pub(crate) fn with_vulnerability_edge_identity<R>(
    _type_id: u64,
    _module_id: u64,
    _callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    f()
}

pub(crate) fn report_vulnerability_edge_reuse_denial() {}

#[inline(never)]
fn materialize_payload<T: Clone, const N: usize>(seed: &[T; N]) -> Vec<T> {
    seed.to_vec()
}

#[inline(never)]
fn reclaim_payload<T>(payload: T) {
    drop(payload);
}

fn main() {
    let victim_seed = [0x41u8; PAYLOAD_SIZE];
    let materialize: fn(&[u8; PAYLOAD_SIZE]) -> Vec<u8> = materialize_payload;
    let victim = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&victim_seed),
    );
    assert_eq!(victim.capacity(), PAYLOAD_SIZE);
    let original_address = victim.as_ptr() as usize;
    let reclaim: fn(Vec<u8>) = reclaim_payload;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || black_box(reclaim)(victim),
    );

    let replacement_seed = [0x42u8; PAYLOAD_SIZE];
    let replacement = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_ALLOC_CALLSITE,
        || black_box(materialize)(&replacement_seed),
    );
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = replacement.as_ptr() as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement);
}
