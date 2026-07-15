//! Safe same-identity control for the derived RUSTSEC-2020-0007 reuse edge.
//!
//! bitvec 0.17.4 provides the patched dependency. This control allocates and
//! reclaims the same 1016-byte `BitVec` victim, then requests another
//! 1016-byte `BitVec` under the exact same manually attributed identity. It
//! retains no stale pointer and controls for ordinary same-identity reuse.

use bitvec::prelude::*;
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4802_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4802_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4802_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4802_0000_0004;
const REPLACEMENT_ALLOC_CALLSITE: u64 = 0x5253_4802_0000_0005;
const REPLACEMENT_RECLAIM_CALLSITE: u64 = 0x5253_4802_0000_0006;

pub(crate) fn with_vulnerability_edge_identity<R>(
    _type_id: u64,
    _module_id: u64,
    _callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    f()
}

pub(crate) fn report_vulnerability_edge_reuse_denial() {}

fn main() {
    let mut victim: BitVec = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || BitVec::with_capacity(8065),
    );
    victim.push(false);
    let original_address = victim.as_slice().as_ptr() as *const () as usize;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || drop(victim),
    );

    let mut replacement: BitVec = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_ALLOC_CALLSITE,
        || BitVec::with_capacity(8065),
    );
    replacement.push(true);
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = replacement.as_slice().as_ptr() as *const () as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement);

    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_RECLAIM_CALLSITE,
        || drop(replacement),
    );
}
