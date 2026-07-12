#![cfg(all(feature = "stats", feature = "type_isolation"))]

#[cfg(feature = "fixed_heap")]
#[path = "../src/bin_support/fixed_heap_probe_global.rs"]
mod fixed_heap_probe_global;

use unialloc::{
    semantic_auto_metadata_disable, semantic_metadata_validation_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    type_isolation_side_cache_snapshot, with_semantic_metadata, AllocationMetadata,
    FLAG_TYPE_ISOLATED,
};

#[cfg(not(feature = "fixed_heap"))]
use unialloc::UniAlloc;

#[cfg(feature = "fixed_heap")]
#[global_allocator]
static ALLOCATOR: fixed_heap_probe_global::FixedHeapProbeAllocator =
    fixed_heap_probe_global::FixedHeapProbeAllocator;

#[cfg(not(feature = "fixed_heap"))]
#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const CAPACITY: usize = 256;

fn vec_with_metadata(metadata: AllocationMetadata) -> Vec<u8> {
    with_semantic_metadata(metadata, || Vec::<u8>::with_capacity(CAPACITY))
}

#[test]
fn lifetime_hint_is_a_type_cache_isolation_boundary() {
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();

    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let short_lived = AllocationMetadata::for_type(0x571F_E001)
        .with_module(0xC0DE_E001)
        .with_callsite(0xA110_E001)
        .with_flags(FLAG_TYPE_ISOLATED)
        .with_lifetime_hint(0x11);
    let long_lived = AllocationMetadata {
        lifetime_hint: 0x22,
        callsite: 0xA110_E002,
        ..short_lived
    };

    let first_short = vec_with_metadata(short_lived);
    let first_short_ptr = first_short.as_ptr() as usize;
    assert_eq!(first_short.capacity(), CAPACITY);
    drop(first_short);

    let long_probe = vec_with_metadata(long_lived);
    let long_probe_ptr = long_probe.as_ptr() as usize;
    assert_ne!(
        long_probe_ptr, first_short_ptr,
        "different lifetime hints must not alias the same type-cache entry"
    );
    drop(long_probe);

    let recovered_short = vec_with_metadata(short_lived);
    let recovered_short_ptr = recovered_short.as_ptr() as usize;
    assert_eq!(
        recovered_short_ptr, first_short_ptr,
        "the original lifetime-hint bucket must retain and recover its own entry"
    );
    drop(recovered_short);

    let stats = semantic_stats_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    assert_eq!(stats.fallback_allocations, 0, "{stats:?}");
    assert_eq!(stats.fallback_deallocations, 0, "{stats:?}");
    assert_eq!(validation.recovery_identity_mismatches, 0, "{validation:?}");
    assert_eq!(side_cache.corrupt_slots, 0, "{side_cache:?}");
    semantic_stats_recording_disable();
}
