//! Safe same-identity control for the RUSTSEC-2023-0054 reduction.
//!
//! A stack value records the patched insertion result without polluting the
//! four-byte heap class. The measured pair then allocates, reclaims, and
//! reallocates a four-byte Vec through the same indirect generic path and exact
//! manual identity.

use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4865_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4865_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4865_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4865_0000_0004;
const REPLACEMENT_ALLOC_CALLSITE: u64 = 0x5253_4865_0000_0005;
const REPLACEMENT_RECLAIM_CALLSITE: u64 = 0x5253_4865_0000_0006;
const INITIAL_CAPACITY: usize = 4;

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
fn materialize_target<T: Clone, const N: usize>(seed: &[T; N]) -> Vec<T> {
    seed.to_vec()
}

#[inline(never)]
fn reclaim_target<T>(value: T) {
    drop(value);
}

fn main() {
    black_box(*b"abXcd");
    let seed = *b"abcd";
    let materialize: fn(&[u8; INITIAL_CAPACITY]) -> Vec<u8> = materialize_target;
    let victim = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&seed),
    );
    assert_eq!(victim.capacity(), INITIAL_CAPACITY);
    let original_address = victim.as_ptr() as usize;
    let reclaim: fn(Vec<u8>) = reclaim_target;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || black_box(reclaim)(victim),
    );

    let replacement_seed = *b"WXYZ";
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

    let reclaim_replacement: fn(Vec<u8>) = reclaim_target;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_RECLAIM_CALLSITE,
        || black_box(reclaim_replacement)(replacement),
    );
}
