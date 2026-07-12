#![cfg(all(feature = "stats", feature = "type_isolation"))]

#[cfg(feature = "fixed_heap")]
#[path = "../src/bin_support/fixed_heap_probe_global.rs"]
mod fixed_heap_probe_global;

use unialloc::alloc_api::type_isolation::__unialloc_semantic_boxed_str_into_string;
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

const LEN: usize = 256;

fn payload_byte(seed: u8, index: usize) -> u8 {
    b'a' + ((seed.wrapping_add(index as u8).wrapping_mul(13)) % 26)
}

fn boxed_str_with_metadata(metadata: AllocationMetadata, seed: u8) -> Box<str> {
    let mut payload = [0_u8; LEN];
    for (index, byte) in payload.iter_mut().enumerate() {
        *byte = payload_byte(seed, index);
    }
    let payload = std::str::from_utf8(&payload).expect("ASCII payload is valid UTF-8");
    with_semantic_metadata(metadata, || Box::<str>::from(payload))
}

fn boxed_str_without_metadata(seed: u8) -> Box<str> {
    let mut payload = [0_u8; LEN];
    for (index, byte) in payload.iter_mut().enumerate() {
        *byte = payload_byte(seed, index);
    }
    let payload = std::str::from_utf8(&payload).expect("ASCII payload is valid UTF-8");
    Box::<str>::from(payload)
}

fn string_with_metadata(metadata: AllocationMetadata, seed: u8) -> String {
    with_semantic_metadata(metadata, || {
        let mut value = String::with_capacity(LEN);
        for index in 0..LEN {
            value.push(payload_byte(seed, index) as char);
        }
        value
    })
}

fn payload_matches(value: &str, seed: u8) -> bool {
    value.len() == LEN
        && value
            .as_bytes()
            .iter()
            .enumerate()
            .all(|(index, byte)| *byte == payload_byte(seed, index))
}

#[test]
fn boxed_str_into_string_rebinds_exact_identity_and_fails_closed() {
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();

    semantic_auto_metadata_disable();
    semantic_stats_reset();
    let transfer_before = semantic_ownership_transfer_snapshot();
    let validation_before = semantic_metadata_validation_snapshot();

    let source = AllocationMetadata::for_type(0xB05E_9101)
        .with_module(0xC0DE_9101)
        .with_callsite(0xA110_9101)
        .with_flags(FLAG_TYPE_ISOLATED);
    let target = AllocationMetadata {
        type_id: 0x57A1_9101,
        ..source
    };
    let boxed = boxed_str_with_metadata(source, 3);
    let source_ptr = boxed.as_ptr();
    let string = __unialloc_semantic_boxed_str_into_string(boxed, source.type_id, target.type_id);
    assert_eq!(string.as_ptr(), source_ptr);
    assert_eq!(string.capacity(), LEN);
    assert!(payload_matches(&string, 3));
    drop(string);

    let source_control = boxed_str_with_metadata(source, 5);
    assert_ne!(
        source_control.as_ptr(),
        source_ptr,
        "the old Box<str> identity must not reuse String-owned storage"
    );
    drop(source_control);
    let target_control = string_with_metadata(target, 7);
    assert_eq!(
        target_control.as_ptr(),
        source_ptr,
        "the rebound String identity should recover the transferred storage"
    );
    drop(target_control);

    let rejected_source = AllocationMetadata::for_type(0xB05E_9102)
        .with_module(0xC0DE_9102)
        .with_callsite(0xA110_9102)
        .with_flags(FLAG_TYPE_ISOLATED);
    let rejected_target = AllocationMetadata {
        type_id: 0x57A1_9102,
        ..rejected_source
    };
    let rejected_box = boxed_str_with_metadata(rejected_source, 11);
    let rejected_ptr = rejected_box.as_ptr();
    let rejected_string = __unialloc_semantic_boxed_str_into_string(
        rejected_box,
        rejected_source.type_id ^ 1,
        rejected_target.type_id,
    );
    assert_eq!(rejected_string.as_ptr(), rejected_ptr);
    assert!(payload_matches(&rejected_string, 11));
    drop(rejected_string);
    let rejected_target_control = string_with_metadata(rejected_target, 13);
    assert_ne!(rejected_target_control.as_ptr(), rejected_ptr);
    drop(rejected_target_control);
    let rejected_source_control = boxed_str_with_metadata(rejected_source, 17);
    assert_eq!(
        rejected_source_control.as_ptr(),
        rejected_ptr,
        "a rejected transfer must retain the authoritative source identity"
    );
    drop(rejected_source_control);

    let tagged_source = AllocationMetadata::for_type(0xB05E_9103)
        .with_module(0xC0DE_9103)
        .with_callsite(0xA110_9103)
        .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
    let tagged_target = AllocationMetadata {
        type_id: 0x57A1_9103,
        ..tagged_source
    };
    let tagged_box = boxed_str_with_metadata(tagged_source, 19);
    let tagged_ptr = tagged_box.as_ptr();
    let tagged_string = __unialloc_semantic_boxed_str_into_string(
        tagged_box,
        tagged_source.type_id,
        tagged_target.type_id,
    );
    assert_eq!(tagged_string.as_ptr(), tagged_ptr);
    assert!(payload_matches(&tagged_string, 19));
    drop(tagged_string);
    let tagged_source_control = boxed_str_with_metadata(tagged_source, 23);
    assert_ne!(tagged_source_control.as_ptr(), tagged_ptr);
    drop(tagged_source_control);
    let tagged_target_control = string_with_metadata(tagged_target, 29);
    assert_eq!(tagged_target_control.as_ptr(), tagged_ptr);
    drop(tagged_target_control);

    let missing = boxed_str_without_metadata(31);
    let missing_ptr = missing.as_ptr();
    let missing_string =
        __unialloc_semantic_boxed_str_into_string(missing, 0xB05E_9104, 0x57A1_9104);
    assert_eq!(missing_string.as_ptr(), missing_ptr);
    assert!(payload_matches(&missing_string, 31));
    drop(missing_string);

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
        "accepted and fail-closed String drops must keep recovery identity coherent"
    );
    semantic_stats_recording_disable();
}
