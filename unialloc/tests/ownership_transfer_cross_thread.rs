#![cfg(all(feature = "stats", feature = "type_isolation"))]

#[cfg(feature = "fixed_heap")]
#[path = "../src/bin_support/fixed_heap_probe_global.rs"]
mod fixed_heap_probe_global;

use std::thread;

use unialloc::alloc_api::type_isolation::__unialloc_semantic_string_into_bytes;
use unialloc::alloc_api::PLACEMENT_HINT_CROSS_THREAD_RECOVERY;
use unialloc::{
    semantic_auto_metadata_disable, semantic_metadata_validation_snapshot,
    semantic_ownership_transfer_snapshot, semantic_stats_recording_disable, semantic_stats_reset,
    with_semantic_metadata, AllocationMetadata, FLAG_TYPE_ISOLATED,
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

fn string_with_metadata(metadata: AllocationMetadata, seed: u8) -> String {
    with_semantic_metadata(metadata, || {
        let mut value = String::with_capacity(CAPACITY);
        for index in 0..CAPACITY {
            value.push((b'a' + seed.wrapping_add(index as u8) % 26) as char);
        }
        value
    })
}

fn vec_with_metadata(metadata: AllocationMetadata) -> Vec<u8> {
    with_semantic_metadata(metadata, || Vec::with_capacity(CAPACITY))
}

#[test]
fn string_into_bytes_transfer_survives_cross_thread_drop_without_type_aliasing() {
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();

    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let string_metadata = AllocationMetadata::for_type(0x57A1_C701)
        .with_module(0xC0DE_C701)
        .with_callsite(0xA110_C701)
        .with_flags(FLAG_TYPE_ISOLATED)
        .with_placement_hint(PLACEMENT_HINT_CROSS_THREAD_RECOVERY | 0x71);
    let vec_metadata = AllocationMetadata {
        type_id: 0x0EC0_C701,
        callsite: 0xA110_C702,
        ..string_metadata
    };

    let value = string_with_metadata(string_metadata, 3);
    let transferred_ptr = value.as_ptr() as usize;
    let transfer_before = semantic_ownership_transfer_snapshot();
    let validation_before = semantic_metadata_validation_snapshot();
    let bytes =
        __unialloc_semantic_string_into_bytes(value, string_metadata.type_id, vec_metadata.type_id);
    let transfer_after = semantic_ownership_transfer_snapshot();

    assert_eq!(bytes.as_ptr() as usize, transferred_ptr);
    assert_eq!(bytes.capacity(), CAPACITY);
    assert_eq!(transfer_after.attempted, transfer_before.attempted + 1);
    assert_eq!(transfer_after.applied, transfer_before.applied + 1);
    assert_eq!(transfer_after.rejected, transfer_before.rejected);

    thread::spawn(move || {
        for (index, byte) in bytes.iter().copied().enumerate() {
            assert_eq!(byte, b'a' + 3u8.wrapping_add(index as u8) % 26);
        }
        drop(bytes);

        let wrong_source = string_with_metadata(string_metadata, 5);
        assert_ne!(
            wrong_source.as_ptr() as usize,
            transferred_ptr,
            "cross-thread drop must not return Vec-owned storage to the old String identity"
        );
        drop(wrong_source);

        let same_target = vec_with_metadata(vec_metadata);
        assert_eq!(
            same_target.as_ptr() as usize,
            transferred_ptr,
            "cross-thread drop must cache transferred storage under the rebound Vec identity"
        );
        drop(same_target);
    })
    .join()
    .expect("cross-thread ownership-transfer isolation regression");
    assert_eq!(
        semantic_metadata_validation_snapshot()
            .recovery_identity_mismatches
            .saturating_sub(validation_before.recovery_identity_mismatches),
        0,
        "cross-thread Drop and both reuse probes must preserve the rebound identity"
    );
    semantic_stats_recording_disable();
}
