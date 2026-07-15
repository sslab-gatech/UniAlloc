//! Safe same-identity control for the derived RUSTSEC-2026-0128 reuse edge.
//!
//! The patched dependency keeps the map entry live, so its source path cannot
//! supply a reclaim/reuse control. This companion performs the same 64-byte
//! `Vec<u8>` allocation through `materialize_victim`, releases it without a
//! stale owner, and requests the same concrete victim type under the same
//! manual identity. It proves that Type Isolation continues to authorize the
//! safe same-identity lifecycle exercised by the treatment matrix.

use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_482A_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_482A_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_482A_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_482A_0000_0004;
const REPLACEMENT_ALLOC_CALLSITE: u64 = 0x5253_482A_0000_0005;
const REPLACEMENT_RECLAIM_CALLSITE: u64 = 0x5253_482A_0000_0006;
const PAYLOAD_SIZE: usize = 64;

pub(crate) fn with_vulnerability_edge_identity<R>(
    _type_id: u64,
    _module_id: u64,
    _callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    f()
}

pub(crate) fn report_vulnerability_edge_reuse_denial() {}

#[derive(Clone)]
struct Victim<T>(Vec<T>);

#[inline(never)]
fn materialize_victim<T: Clone, const N: usize>(seed: &[T; N]) -> Victim<T> {
    Victim(seed.to_vec())
}

#[inline(never)]
fn reclaim_victim<T>(victim: Victim<T>) {
    drop(victim);
}

fn main() {
    let victim_seed = [0x41u8; PAYLOAD_SIZE];
    let materialize: fn(&[u8; PAYLOAD_SIZE]) -> Victim<u8> = materialize_victim;
    let victim = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&victim_seed),
    );
    assert_eq!(victim.0.len(), PAYLOAD_SIZE);
    assert_eq!(victim.0.capacity(), PAYLOAD_SIZE);
    let original_address = victim.0.as_ptr() as usize;
    let reclaim: fn(Victim<u8>) = reclaim_victim;
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
    let replacement_address = replacement.0.as_ptr() as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement);

    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_RECLAIM_CALLSITE,
        || drop(replacement),
    );
}
