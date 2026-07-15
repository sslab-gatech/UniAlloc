//! Safe matched control for the derived RUSTSEC-2021-0130 reuse edge.
//!
//! The patched crate ties iterator items to the cache borrow. This control
//! performs the same 48-byte LruEntry allocation and reclaim, then requests a
//! second LruEntry with the same manually attributed identity. It retains no
//! stale reference and controls for ordinary same-identity cache reuse.

use lru::LruCache;
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4808_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4808_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4808_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4808_0000_0004;
const REPLACEMENT_ALLOC_CALLSITE: u64 = 0x5253_4808_0000_0005;

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
    let mut cache = LruCache::new(1);
    let value = String::from("sixteen-byte-msg");
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || cache.put(1u32, value),
    );
    let original_address = cache.peek(&1).expect("victim must exist") as *const String as usize;

    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || cache.pop(&1),
    );

    let replacement_value = String::from("matched-safe-msg");
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_ALLOC_CALLSITE,
        || cache.put(2u32, replacement_value),
    );
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address =
        cache.peek(&2).expect("replacement must exist") as *const String as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(replacement_address);
}
