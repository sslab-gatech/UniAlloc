//! Safe matched control for the derived RUSTSEC-2026-0152 reuse edge.
//!
//! oneringbuf 0.8.0 removed the obsolete safe `IntoRef::into_ref` entry
//! point. This control allocates and reclaims the same 40-byte
//! `LocalHeapRB<usize>` object, then requests a same-type replacement. It has
//! no stale handle and therefore completes a safe matched lifecycle while
//! controlling for ordinary same-identity cache reuse.

use oneringbuf::LocalHeapRB;
use std::hint::black_box;
use std::mem::{align_of, size_of};

const VICTIM_TYPE_ID: u64 = 0x5253_4841_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4841_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4841_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4841_0000_0004;
const REPLACEMENT_ALLOC_CALLSITE: u64 = 0x5253_4841_0000_0005;
const REPLACEMENT_RECLAIM_CALLSITE: u64 = 0x5253_4841_0000_0006;

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
    assert_eq!(size_of::<LocalHeapRB<usize>>(), 40);
    assert_eq!(align_of::<LocalHeapRB<usize>>(), 8);

    let rb = LocalHeapRB::<usize>::from(vec![1, 2, 3]);
    let victim = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || Box::new(rb),
    );
    let original_address = (&*victim as *const LocalHeapRB<usize>) as usize;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || drop(victim),
    );

    let replacement = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_ALLOC_CALLSITE,
        || Box::new(LocalHeapRB::<usize>::from(vec![4, 5, 6])),
    );
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = (&*replacement as *const LocalHeapRB<usize>) as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement);

    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_RECLAIM_CALLSITE,
        || drop(replacement),
    );
}
