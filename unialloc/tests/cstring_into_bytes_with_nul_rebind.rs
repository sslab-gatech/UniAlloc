#![cfg(all(feature = "stats", feature = "type_isolation"))]

#[cfg(feature = "fixed_heap")]
#[path = "../src/bin_support/fixed_heap_probe_global.rs"]
mod fixed_heap_probe_global;

use std::ffi::CString;
use unialloc::alloc_api::type_isolation::__unialloc_semantic_cstring_into_bytes_with_nul;
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

const PAYLOAD_LEN: usize = 257;
const ALLOCATION_LEN: usize = PAYLOAD_LEN + 1;

fn payload_byte(seed: u8, index: usize) -> u8 {
    1 + seed.wrapping_add(index as u8).wrapping_mul(13) % 254
}

fn cstring_with_metadata(metadata: AllocationMetadata, seed: u8) -> CString {
    let mut payload = [0_u8; PAYLOAD_LEN];
    for (index, byte) in payload.iter_mut().enumerate() {
        *byte = payload_byte(seed, index);
    }
    with_semantic_metadata(metadata, || {
        CString::new(&payload[..]).expect("generated payload has no nul bytes")
    })
}

fn cstring_without_metadata(seed: u8) -> CString {
    let mut payload = [0_u8; PAYLOAD_LEN];
    for (index, byte) in payload.iter_mut().enumerate() {
        *byte = payload_byte(seed, index);
    }
    CString::new(&payload[..]).expect("generated payload has no nul bytes")
}

fn vec_with_metadata(metadata: AllocationMetadata) -> Vec<u8> {
    with_semantic_metadata(metadata, || Vec::with_capacity(ALLOCATION_LEN))
}

fn payload_matches(bytes: &[u8], seed: u8) -> bool {
    bytes.len() == ALLOCATION_LEN
        && bytes[PAYLOAD_LEN] == 0
        && bytes[..PAYLOAD_LEN]
            .iter()
            .enumerate()
            .all(|(index, byte)| *byte == payload_byte(seed, index))
}

#[test]
fn cstring_into_bytes_with_nul_rebinds_exact_identity_and_fails_closed() {
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();

    semantic_auto_metadata_disable();
    semantic_stats_reset();
    let transfer_before = semantic_ownership_transfer_snapshot();
    let validation_before = semantic_metadata_validation_snapshot();

    let source = AllocationMetadata::for_type(0xC571_9201)
        .with_module(0xC0DE_9201)
        .with_callsite(0xA110_9201)
        .with_flags(FLAG_TYPE_ISOLATED);
    let target = AllocationMetadata {
        type_id: 0x0EC0_9201,
        ..source
    };
    let value = cstring_with_metadata(source, 3);
    let source_ptr = value.as_ptr() as *const u8;
    let bytes =
        __unialloc_semantic_cstring_into_bytes_with_nul(value, source.type_id, target.type_id);
    assert_eq!(bytes.as_ptr(), source_ptr);
    assert_eq!(bytes.capacity(), ALLOCATION_LEN);
    assert!(payload_matches(&bytes, 3));
    drop(bytes);

    let source_control = cstring_with_metadata(source, 5);
    assert_ne!(
        source_control.as_ptr() as *const u8,
        source_ptr,
        "the old CString identity must not reuse Vec-owned storage"
    );
    drop(source_control);
    let target_control = vec_with_metadata(target);
    assert_eq!(
        target_control.as_ptr(),
        source_ptr,
        "the rebound Vec identity should recover transferred storage"
    );
    drop(target_control);

    let rejected_source = AllocationMetadata::for_type(0xC571_9202)
        .with_module(0xC0DE_9202)
        .with_callsite(0xA110_9202)
        .with_flags(FLAG_TYPE_ISOLATED);
    let rejected_target = AllocationMetadata {
        type_id: 0x0EC0_9202,
        ..rejected_source
    };
    let rejected = cstring_with_metadata(rejected_source, 7);
    let rejected_ptr = rejected.as_ptr() as *const u8;
    let rejected_bytes = __unialloc_semantic_cstring_into_bytes_with_nul(
        rejected,
        rejected_source.type_id ^ 1,
        rejected_target.type_id,
    );
    assert_eq!(rejected_bytes.as_ptr(), rejected_ptr);
    assert!(payload_matches(&rejected_bytes, 7));
    drop(rejected_bytes);
    let rejected_target_control = vec_with_metadata(rejected_target);
    assert_ne!(rejected_target_control.as_ptr(), rejected_ptr);
    drop(rejected_target_control);
    let rejected_source_control = cstring_with_metadata(rejected_source, 11);
    assert_eq!(
        rejected_source_control.as_ptr() as *const u8,
        rejected_ptr,
        "a rejected transfer must retain the CString cache identity"
    );
    drop(rejected_source_control);

    let tagged_source = AllocationMetadata::for_type(0xC571_9203)
        .with_module(0xC0DE_9203)
        .with_callsite(0xA110_9203)
        .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
    let tagged_target = AllocationMetadata {
        type_id: 0x0EC0_9203,
        ..tagged_source
    };
    let tagged = cstring_with_metadata(tagged_source, 13);
    let tagged_ptr = tagged.as_ptr() as *const u8;
    let tagged_bytes = __unialloc_semantic_cstring_into_bytes_with_nul(
        tagged,
        tagged_source.type_id,
        tagged_target.type_id,
    );
    assert_eq!(tagged_bytes.as_ptr(), tagged_ptr);
    assert!(payload_matches(&tagged_bytes, 13));
    drop(tagged_bytes);
    let tagged_source_control = cstring_with_metadata(tagged_source, 17);
    assert_ne!(tagged_source_control.as_ptr() as *const u8, tagged_ptr);
    drop(tagged_source_control);
    let tagged_target_control = vec_with_metadata(tagged_target);
    assert_eq!(tagged_target_control.as_ptr(), tagged_ptr);
    drop(tagged_target_control);

    let missing = cstring_without_metadata(19);
    let missing_ptr = missing.as_ptr() as *const u8;
    let missing_bytes =
        __unialloc_semantic_cstring_into_bytes_with_nul(missing, 0xC571_9204, 0x0EC0_9204);
    assert_eq!(missing_bytes.as_ptr(), missing_ptr);
    assert!(payload_matches(&missing_bytes, 19));
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
    assert_eq!(
        semantic_metadata_validation_snapshot()
            .recovery_identity_mismatches
            .saturating_sub(validation_before.recovery_identity_mismatches),
        0,
        "accepted and fail-closed Vec drops must retain coherent recovery identity"
    );
    semantic_stats_recording_disable();
}
