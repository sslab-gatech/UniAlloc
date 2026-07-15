//! Safe same-identity control for the RUSTSEC-2025-0022 reduction.
//!
//! The properties owner remains live through the simulated FFI read. After it
//! is safely reclaimed through the owner helper, a second CString is allocated
//! and reclaimed through the same concrete allocation path.

use std::ffi::{CStr, CString};
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4869_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4869_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4869_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4869_0000_0004;
const REPLACEMENT_ALLOC_CALLSITE: u64 = 0x5253_4869_0000_0005;
const REPLACEMENT_RECLAIM_CALLSITE: u64 = 0x5253_4869_0000_0006;
const PAYLOAD_SIZE: usize = 4096;

pub(crate) fn with_vulnerability_edge_identity<R>(
    _type_id: u64,
    _module_id: u64,
    _callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    f()
}

pub(crate) fn report_vulnerability_edge_reuse_denial() {}

// Keep the allocation helper noinline so the compiler audit sees a distinct
// critical site with the concrete owner type used by this adapter.
#[inline(never)]
fn materialize_bytes(seed: &[u8; PAYLOAD_SIZE]) -> Vec<u8> {
    seed.to_vec()
}

#[inline(never)]
fn reclaim_owner<T>(owner: T) {
    drop(owner);
}

fn main() {
    let mut victim_seed = [b'A'; PAYLOAD_SIZE];
    victim_seed[PAYLOAD_SIZE - 1] = 0;
    let materialize: fn(&[u8; PAYLOAD_SIZE]) -> Vec<u8> = materialize_bytes;
    let victim_bytes = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&victim_seed),
    );
    assert_eq!(victim_bytes.capacity(), PAYLOAD_SIZE);
    let victim_backing_address = victim_bytes.as_ptr() as usize;
    let victim = unsafe { CString::from_vec_with_nul_unchecked(victim_bytes) };
    let original_address = victim.as_ptr() as usize;
    assert_eq!(original_address, victim_backing_address);
    let observed = unsafe { CStr::from_ptr(victim.as_ptr()) };
    assert_eq!(observed.to_bytes()[0], b'A');
    black_box(observed);

    let reclaim: fn(CString) = reclaim_owner;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || black_box(reclaim)(victim),
    );

    let mut replacement_seed = [b'B'; PAYLOAD_SIZE];
    replacement_seed[PAYLOAD_SIZE - 1] = 0;
    let replacement_bytes = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_ALLOC_CALLSITE,
        || black_box(materialize)(&replacement_seed),
    );
    assert_eq!(replacement_bytes.capacity(), PAYLOAD_SIZE);
    let replacement_backing_address = replacement_bytes.as_ptr() as usize;
    let replacement = unsafe { CString::from_vec_with_nul_unchecked(replacement_bytes) };
    assert_eq!(replacement.as_ptr() as usize, replacement_backing_address);
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = replacement.as_ptr() as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement);

    let reclaim_replacement: fn(CString) = reclaim_owner;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_RECLAIM_CALLSITE,
        || black_box(reclaim_replacement)(replacement),
    );
}
