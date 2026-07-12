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
fn placement_hint_is_a_type_cache_isolation_boundary() {
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();

    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let first_placement = AllocationMetadata::for_type(0x571F_E002)
        .with_module(0xC0DE_E002)
        .with_callsite(0xA110_E011)
        .with_flags(FLAG_TYPE_ISOLATED)
        .with_placement_hint(0x21);
    let second_placement = AllocationMetadata {
        placement_hint: 0x22,
        callsite: 0xA110_E012,
        ..first_placement
    };

    let first_value = vec_with_metadata(first_placement);
    let first_value_ptr = first_value.as_ptr() as usize;
    assert_eq!(first_value.capacity(), CAPACITY);
    drop(first_value);

    let second_value = vec_with_metadata(second_placement);
    let second_value_ptr = second_value.as_ptr() as usize;
    assert_ne!(
        second_value_ptr, first_value_ptr,
        "different placement hints must not alias the same type-cache entry"
    );
    drop(second_value);

    let recovered_first = vec_with_metadata(first_placement);
    let recovered_first_ptr = recovered_first.as_ptr() as usize;
    assert_eq!(
        recovered_first_ptr, first_value_ptr,
        "the original placement-hint bucket must retain and recover its own entry"
    );
    drop(recovered_first);

    let stats = semantic_stats_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    assert_eq!(stats.fallback_allocations, 0, "{stats:?}");
    assert_eq!(stats.fallback_deallocations, 0, "{stats:?}");
    assert_eq!(validation.recovery_identity_mismatches, 0, "{validation:?}");
    assert_eq!(side_cache.corrupt_slots, 0, "{side_cache:?}");
    semantic_stats_recording_disable();
}
