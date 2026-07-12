#![cfg(all(feature = "stats", feature = "type_isolation"))]

#[cfg(feature = "fixed_heap")]
#[path = "../src/bin_support/fixed_heap_probe_global.rs"]
mod fixed_heap_probe_global;

use unialloc::alloc_api::type_isolation::__unialloc_semantic_string_into_bytes;
use unialloc::alloc_api::FLAG_MEMORY_TAGGING;
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
fn string_into_bytes_rebinds_exact_identity_and_rejects_untrusted_records() {
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();

    semantic_auto_metadata_disable();
    semantic_stats_reset();
    let transfer_before = semantic_ownership_transfer_snapshot();
    let validation_before = semantic_metadata_validation_snapshot();

    let string_metadata = AllocationMetadata::for_type(0x57A1_0001)
        .with_module(0xC0DE_7001)
        .with_callsite(0xA110_7001)
        .with_flags(FLAG_TYPE_ISOLATED);
    let vec_metadata = AllocationMetadata {
        type_id: 0x0EC0_7001,
        ..string_metadata
    };
    let value = string_with_metadata(string_metadata, 3);
    let expected_payload = value.as_bytes().to_vec();
    let source_ptr = value.as_ptr();
    let bytes =
        __unialloc_semantic_string_into_bytes(value, string_metadata.type_id, vec_metadata.type_id);
    assert_eq!(bytes.as_ptr(), source_ptr);
    assert_eq!(bytes.capacity(), CAPACITY);
    assert_eq!(bytes, expected_payload);
    drop(bytes);

    let wrong_string = string_with_metadata(string_metadata, 5);
    assert_ne!(
        wrong_string.as_ptr(),
        source_ptr,
        "the old String identity must not reuse Vec-owned storage"
    );
    drop(wrong_string);
    let same_vec = vec_with_metadata(vec_metadata);
    assert_eq!(
        same_vec.as_ptr(),
        source_ptr,
        "the rebound Vec identity should recover the transferred storage"
    );
    drop(same_vec);

    let rejected_string_metadata = AllocationMetadata::for_type(0x57A1_0002)
        .with_module(0xC0DE_7002)
        .with_callsite(0xA110_7002)
        .with_flags(FLAG_TYPE_ISOLATED);
    let rejected_vec_metadata = AllocationMetadata {
        type_id: 0x0EC0_7002,
        ..rejected_string_metadata
    };
    let rejected = string_with_metadata(rejected_string_metadata, 7);
    let rejected_ptr = rejected.as_ptr();
    let rejected_bytes = __unialloc_semantic_string_into_bytes(
        rejected,
        rejected_string_metadata.type_id ^ 1,
        rejected_vec_metadata.type_id,
    );
    assert_eq!(rejected_bytes.as_ptr(), rejected_ptr);
    drop(rejected_bytes);
    let rejected_target = vec_with_metadata(rejected_vec_metadata);
    assert_ne!(rejected_target.as_ptr(), rejected_ptr);
    drop(rejected_target);
    let retained_source = string_with_metadata(rejected_string_metadata, 9);
    assert_eq!(
        retained_source.as_ptr(),
        rejected_ptr,
        "a wrong expected identity must retain the String cache owner"
    );
    drop(retained_source);

    let tagged_string_metadata = AllocationMetadata::for_type(0x57A1_0003)
        .with_module(0xC0DE_7003)
        .with_callsite(0xA110_7003)
        .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
    let tagged_vec_metadata = AllocationMetadata {
        type_id: 0x0EC0_7003,
        ..tagged_string_metadata
    };
    let tagged = string_with_metadata(tagged_string_metadata, 11);
    let tagged_ptr = tagged.as_ptr();
    let tagged_bytes = __unialloc_semantic_string_into_bytes(
        tagged,
        tagged_string_metadata.type_id,
        tagged_vec_metadata.type_id,
    );
    assert_eq!(tagged_bytes.as_ptr(), tagged_ptr);
    drop(tagged_bytes);
    let retained_tagged_source = string_with_metadata(tagged_string_metadata, 13);
    assert_ne!(
        retained_tagged_source.as_ptr(),
        tagged_ptr,
        "a tagged transfer must not return Vec-owned storage to the old String identity"
    );
    drop(retained_tagged_source);
    let tagged_target = vec_with_metadata(tagged_vec_metadata);
    assert_eq!(
        tagged_target.as_ptr(),
        tagged_ptr,
        "the rebound tagged Vec identity should recover transferred storage"
    );
    drop(tagged_target);

    let missing = String::from("missing-record");
    let missing_ptr = missing.as_ptr();
    let missing_bytes = __unialloc_semantic_string_into_bytes(missing, 0x57A1_0004, 0x0EC0_7004);
    assert_eq!(missing_bytes.as_ptr(), missing_ptr);
    assert_eq!(missing_bytes, b"missing-record");
    drop(missing_bytes);

    let transfer_after = semantic_ownership_transfer_snapshot();
    assert_eq!(
        transfer_after
            .attempted
            .saturating_sub(transfer_before.attempted),
        4
    );
    assert_eq!(
        transfer_after
            .applied
            .saturating_sub(transfer_before.applied),
        2
    );
    assert_eq!(
        transfer_after
            .rejected
            .saturating_sub(transfer_before.rejected),
        2
    );
    let validation_after = semantic_metadata_validation_snapshot();
    assert_eq!(
        validation_after
            .recovery_identity_mismatches
            .saturating_sub(validation_before.recovery_identity_mismatches),
        0,
        "fail-closed transfer probes must not corrupt deallocation identity"
    );
    semantic_stats_recording_disable();
}
