//! Safe same-identity control for the derived RUSTSEC-2025-0016 reuse edge.
//!
//! pared 0.4.0 keeps a projection tied to the `Parc`-owned `String`. This
//! control reads the projection while its owner is live, reclaims the same
//! 64-byte String buffer with the last `Parc`, and requests another 64-byte
//! String buffer under the exact same manually attributed identity. It retains
//! no external stale projection and controls for same-identity cache reuse.

#![forbid(unsafe_code)]

use pared::sync::Parc;
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4844_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4844_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4844_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4844_0000_0004;
const REPLACEMENT_ALLOC_CALLSITE: u64 = 0x5253_4844_0000_0005;
const REPLACEMENT_RECLAIM_CALLSITE: u64 = 0x5253_4844_0000_0006;
const PAYLOAD_SIZE: usize = 64;
const PAYLOAD: &[u8] = b"Hello World!";

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
fn materialize_value(seed: &[u8; PAYLOAD_SIZE]) -> Vec<u8> {
    seed.to_vec()
}

#[inline(never)]
fn reclaim_value<T>(value: T) {
    drop(value);
}

fn main() {
    let mut seed = [b'A'; PAYLOAD_SIZE];
    seed[..PAYLOAD.len()].copy_from_slice(PAYLOAD);
    let materialize: fn(&[u8; PAYLOAD_SIZE]) -> Vec<u8> = materialize_value;
    let victim_bytes = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&seed),
    );
    let victim = String::from_utf8(victim_bytes).expect("ASCII victim payload");
    assert_eq!(victim.capacity(), PAYLOAD_SIZE);

    let owner = Parc::new(victim);
    let projection = owner.project(|owned| owned.as_str());
    drop(owner);
    let original_address = projection.as_ptr() as usize;
    assert_eq!(projection.as_bytes(), &seed);
    black_box(projection.as_bytes()[0]);

    let reclaim: fn(Parc<str>) = reclaim_value;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || black_box(reclaim)(projection),
    );

    let replacement_bytes = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_ALLOC_CALLSITE,
        || black_box(materialize)(&seed),
    );
    let replacement = String::from_utf8(replacement_bytes).expect("ASCII replacement payload");
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = replacement.as_ptr() as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement);

    let reclaim_replacement: fn(String) = reclaim_value;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_RECLAIM_CALLSITE,
        || black_box(reclaim_replacement)(replacement),
    );
}
