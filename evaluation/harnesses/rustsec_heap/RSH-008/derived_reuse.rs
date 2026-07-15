//! Derived A -> free -> B adapter for RUSTSEC-2021-0130.
//!
//! `Replacement` matches the observed 48-byte LruEntry allocation on x86-64.
//! Exact address equality is the Type Isolation treatment oracle; any later
//! dangling access remains an independent source defect.

use lru::LruCache;
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4808_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4808_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4808_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4808_0000_0004;

// These hooks preserve standalone execution. The allocator experiment wrapper
// replaces them with an explicitly scoped victim identity and a denial report.
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
struct Replacement([usize; 6]);

fn main() {
    let mut cache = LruCache::new(1);
    let value = String::from("sixteen-byte-msg");
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || cache.put(1u32, value),
    );

    let (key, value) = cache.iter().next().expect("entry must exist");
    let stale_value_address = value as *const String as usize;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || cache.pop(key),
    );

    let replacement = Box::new(Replacement([0x4141_4141_4141_4141; 6]));
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = (&*replacement as *const Replacement) as usize;
    eprintln!("stale_value={stale_value_address:#x} replacement={replacement_address:#x}");
    black_box(stale_value_address);
    black_box(&replacement.0);
}
