//! Safe same-identity control for the derived RUSTSEC-2025-0004 reuse edge.
//!
//! openssl 0.10.70 ties the selected slice to the server list. This control
//! consumes the selection before reclaiming the same 64-byte `Vec<u8>` victim,
//! then requests another 64-byte `Vec<u8>` under the exact same manually
//! attributed identity. It retains no stale slice and controls for ordinary
//! same-identity cache reuse.

#![forbid(unsafe_code)]

use openssl::ssl;
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4843_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4843_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4843_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4843_0000_0004;
const REPLACEMENT_ALLOC_CALLSITE: u64 = 0x5253_4843_0000_0005;
const REPLACEMENT_RECLAIM_CALLSITE: u64 = 0x5253_4843_0000_0006;
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

// Keep the allocation helper noinline so the compiler audit sees a distinct
// critical site with the concrete owner type used by this adapter.
#[inline(never)]
fn materialize_server(seed: &[u8; PAYLOAD_SIZE]) -> Vec<u8> {
    seed.to_vec()
}

#[inline(never)]
fn reclaim_server<T>(server: T) {
    drop(server);
}

fn main() {
    let mut seed = [b'x'; PAYLOAD_SIZE];
    seed[..3].copy_from_slice(b"\x02h2");
    seed[3] = 60;
    let materialize: fn(&[u8; PAYLOAD_SIZE]) -> Vec<u8> = materialize_server;
    let victim = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&seed),
    );
    assert_eq!(victim.capacity(), PAYLOAD_SIZE);
    let original_address = victim.as_ptr() as usize;

    {
        let selected = ssl::select_next_proto(&victim, b"\x02h2").expect("matching protocol");
        assert_eq!(selected, b"h2");
        black_box(selected[0]);
    }

    let reclaim: fn(Vec<u8>) = reclaim_server;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || black_box(reclaim)(victim),
    );

    let replacement = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_ALLOC_CALLSITE,
        || black_box(materialize)(&seed),
    );
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = replacement.as_ptr() as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement);

    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_RECLAIM_CALLSITE,
        || black_box(reclaim)(replacement),
    );
}
