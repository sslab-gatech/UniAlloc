#![cfg(all(feature = "stats", feature = "type_isolation"))]

#[cfg(feature = "fixed_heap")]
#[path = "../src/bin_support/fixed_heap_probe_global.rs"]
mod fixed_heap_probe_global;

use unialloc::alloc_api::type_isolation::__unialloc_semantic_string_into_boxed_str;
use unialloc::alloc_api::FLAG_MEMORY_TAGGING;
use unialloc::{
    active_allocation_metadata, semantic_auto_metadata_disable, semantic_auto_metadata_enable,
    semantic_fallback_attribution_snapshot, semantic_metadata_validation_snapshot,
    semantic_ownership_transfer_snapshot, semantic_stats_recording_disable, semantic_stats_reset,
    semantic_stats_snapshot, with_semantic_metadata, AllocationMetadata, FLAG_TYPE_ISOLATED,
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
const SPARE_CAPACITY: usize = 512;

fn payload_byte(seed: u8, index: usize) -> u8 {
    b'a' + ((seed.wrapping_add(index as u8).wrapping_mul(13)) % 26)
}

fn string_with_metadata(capacity: usize, metadata: AllocationMetadata, seed: u8) -> String {
    with_semantic_metadata(metadata, || string_without_metadata(capacity, seed))
}

fn string_without_metadata(capacity: usize, seed: u8) -> String {
    let mut value = String::with_capacity(capacity);
    for index in 0..LEN {
        value.push(payload_byte(seed, index) as char);
    }
    value
}

fn boxed_str_with_metadata(metadata: AllocationMetadata, seed: u8) -> Box<str> {
    let mut payload = [0_u8; LEN];
    for (index, byte) in payload.iter_mut().enumerate() {
        *byte = payload_byte(seed, index);
    }
    let payload = std::str::from_utf8(&payload).expect("ASCII payload is valid UTF-8");
    with_semantic_metadata(metadata, || Box::<str>::from(payload))
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
fn string_into_boxed_str_transfers_exact_and_moved_identity_and_fails_closed() {
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();

    semantic_auto_metadata_disable();
    semantic_stats_reset();
    let transfer_before = semantic_ownership_transfer_snapshot();
    let validation_before = semantic_metadata_validation_snapshot();
    let typed_fallback_before = semantic_fallback_attribution_snapshot();

    let exact_source = AllocationMetadata::for_type(0x57A1_8001)
        .with_module(0xC0DE_8001)
        .with_callsite(0xA110_8001)
        .with_flags(FLAG_TYPE_ISOLATED);
    let exact_target = AllocationMetadata {
        type_id: 0xB05E_8001,
        ..exact_source
    };
    let exact = string_with_metadata(LEN, exact_source, 3);
    assert_eq!(exact.capacity(), LEN);
    let exact_ptr = exact.as_ptr();
    let exact_box = __unialloc_semantic_string_into_boxed_str(
        exact,
        exact_source.type_id,
        exact_target.type_id,
    );
    assert_eq!(exact_box.as_ptr(), exact_ptr);
    assert!(payload_matches(&exact_box, 3));
    drop(exact_box);

    let exact_wrong_source = string_with_metadata(LEN, exact_source, 5);
    assert_ne!(
        exact_wrong_source.as_ptr(),
        exact_ptr,
        "the old String identity must not reuse Box<str>-owned storage"
    );
    drop(exact_wrong_source);
    let exact_same_target = boxed_str_with_metadata(exact_target, 7);
    assert_eq!(
        exact_same_target.as_ptr(),
        exact_ptr,
        "the rebound Box<str> identity should recover exact-capacity storage"
    );
    drop(exact_same_target);

    let moved_source = AllocationMetadata::for_type(0x57A1_8002)
        .with_module(0xC0DE_8002)
        .with_callsite(0xA110_8002)
        .with_flags(FLAG_TYPE_ISOLATED);
    let moved_target = AllocationMetadata {
        type_id: 0xB05E_8002,
        ..moved_source
    };
    let moved = string_with_metadata(SPARE_CAPACITY, moved_source, 11);
    assert_eq!(moved.capacity(), SPARE_CAPACITY);
    let moved_old_ptr = moved.as_ptr();
    let moved_box = __unialloc_semantic_string_into_boxed_str(
        moved,
        moved_source.type_id,
        moved_target.type_id,
    );
    let moved_new_ptr = moved_box.as_ptr();
    assert_ne!(moved_new_ptr, moved_old_ptr);
    assert!(payload_matches(&moved_box, 11));
    drop(moved_box);

    let moved_wrong_source = string_with_metadata(LEN, moved_source, 13);
    assert_ne!(moved_wrong_source.as_ptr(), moved_new_ptr);
    drop(moved_wrong_source);
    let moved_same_target = boxed_str_with_metadata(moved_target, 17);
    assert_eq!(moved_same_target.as_ptr(), moved_new_ptr);
    drop(moved_same_target);
    let moved_recovered_source = string_with_metadata(SPARE_CAPACITY, moved_source, 19);
    assert_eq!(
        moved_recovered_source.as_ptr(),
        moved_old_ptr,
        "released spare-capacity storage must retain the old String identity"
    );
    drop(moved_recovered_source);

    let wrong_source = AllocationMetadata::for_type(0x57A1_8003)
        .with_module(0xC0DE_8003)
        .with_callsite(0xA110_8003)
        .with_flags(FLAG_TYPE_ISOLATED);
    let wrong_target = AllocationMetadata {
        type_id: 0xB05E_8003,
        ..wrong_source
    };
    let wrong = string_with_metadata(SPARE_CAPACITY, wrong_source, 23);
    let wrong_old_ptr = wrong.as_ptr();
    let wrong_box = __unialloc_semantic_string_into_boxed_str(
        wrong,
        wrong_source.type_id ^ 1,
        wrong_target.type_id,
    );
    let wrong_new_ptr = wrong_box.as_ptr();
    assert_ne!(wrong_new_ptr, wrong_old_ptr);
    assert!(payload_matches(&wrong_box, 23));
    drop(wrong_box);
    let wrong_target_control = boxed_str_with_metadata(wrong_target, 29);
    assert_ne!(wrong_target_control.as_ptr(), wrong_new_ptr);
    drop(wrong_target_control);
    let wrong_source_control = string_with_metadata(LEN, wrong_source, 31);
    assert_eq!(
        wrong_source_control.as_ptr(),
        wrong_new_ptr,
        "a rejected moved shrink must retain the source identity on its new allocation"
    );
    drop(wrong_source_control);
    let wrong_old_source_control = string_with_metadata(SPARE_CAPACITY, wrong_source, 37);
    assert_eq!(wrong_old_source_control.as_ptr(), wrong_old_ptr);
    drop(wrong_old_source_control);

    let tagged_source = AllocationMetadata::for_type(0x57A1_8004)
        .with_module(0xC0DE_8004)
        .with_callsite(0xA110_8004)
        .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
    let tagged_target = AllocationMetadata {
        type_id: 0xB05E_8004,
        ..tagged_source
    };
    let tagged = string_with_metadata(SPARE_CAPACITY, tagged_source, 41);
    let tagged_old_ptr = tagged.as_ptr();
    let tagged_box = __unialloc_semantic_string_into_boxed_str(
        tagged,
        tagged_source.type_id,
        tagged_target.type_id,
    );
    let tagged_new_ptr = tagged_box.as_ptr();
    assert_ne!(tagged_new_ptr, tagged_old_ptr);
    assert!(payload_matches(&tagged_box, 41));
    drop(tagged_box);
    let tagged_target_control = boxed_str_with_metadata(tagged_target, 43);
    assert_ne!(tagged_target_control.as_ptr(), tagged_new_ptr);
    drop(tagged_target_control);
    let tagged_source_control = string_with_metadata(LEN, tagged_source, 47);
    assert_eq!(
        tagged_source_control.as_ptr(),
        tagged_new_ptr,
        "a tagged moved shrink must keep its authenticated source policy"
    );
    drop(tagged_source_control);
    let tagged_old_source_control = string_with_metadata(SPARE_CAPACITY, tagged_source, 53);
    assert_eq!(tagged_old_source_control.as_ptr(), tagged_old_ptr);
    drop(tagged_old_source_control);

    let typed_fallback_after = semantic_fallback_attribution_snapshot();
    assert_eq!(
        typed_fallback_after
            .raw_realloc_no_metadata
            .saturating_sub(typed_fallback_before.raw_realloc_no_metadata),
        0,
        "trusted accepted and rejected shrinks must not become raw reallocations"
    );
    assert_eq!(
        typed_fallback_after
            .raw_realloc_moved_dealloc_no_metadata
            .saturating_sub(typed_fallback_before.raw_realloc_moved_dealloc_no_metadata),
        0
    );

    let missing = string_without_metadata(SPARE_CAPACITY, 59);
    let missing_old_ptr = missing.as_ptr();
    let missing_stats_before = semantic_stats_snapshot();
    let missing_fallback_before = semantic_fallback_attribution_snapshot();
    let outer = AllocationMetadata::for_type(0x0A7E_8005)
        .with_module(0xC0DE_8005)
        .with_callsite(0xA110_8005)
        .with_flags(FLAG_TYPE_ISOLATED);
    semantic_auto_metadata_enable(0xC0DE_8006, FLAG_TYPE_ISOLATED, 0xA110_8006);
    let missing_box = with_semantic_metadata(outer, || {
        let boxed = __unialloc_semantic_string_into_boxed_str(missing, 0x57A1_8005, 0xB05E_8005);
        assert_eq!(active_allocation_metadata(), Some(outer));
        boxed
    });
    semantic_auto_metadata_disable();
    assert_eq!(active_allocation_metadata(), None);
    let missing_new_ptr = missing_box.as_ptr();
    assert_ne!(missing_new_ptr, missing_old_ptr);
    assert!(payload_matches(&missing_box, 59));
    let missing_stats_after = semantic_stats_snapshot();
    let missing_fallback_after = semantic_fallback_attribution_snapshot();
    assert_eq!(
        missing_stats_after
            .typed_allocations
            .saturating_sub(missing_stats_before.typed_allocations),
        0,
        "missing recovery state must not inherit outer or auto type metadata"
    );
    assert_eq!(
        missing_stats_after
            .fallback_allocations
            .saturating_sub(missing_stats_before.fallback_allocations),
        1
    );
    assert_eq!(
        missing_fallback_after
            .raw_realloc_no_metadata
            .saturating_sub(missing_fallback_before.raw_realloc_no_metadata),
        1
    );
    assert_eq!(
        missing_fallback_after
            .raw_realloc_moved_dealloc_no_metadata
            .saturating_sub(missing_fallback_before.raw_realloc_moved_dealloc_no_metadata),
        1
    );
    drop(missing_box);

    let transfer_after = semantic_ownership_transfer_snapshot();
    assert_eq!(
        transfer_after
            .attempted
            .saturating_sub(transfer_before.attempted),
        5
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
        3
    );
    assert_eq!(
        semantic_metadata_validation_snapshot()
            .recovery_identity_mismatches
            .saturating_sub(validation_before.recovery_identity_mismatches),
        0,
        "accepted and fail-closed Box<str> drops must keep recovery identity coherent"
    );
    semantic_stats_recording_disable();
}
