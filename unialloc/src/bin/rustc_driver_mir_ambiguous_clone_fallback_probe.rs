#![cfg_attr(feature = "fixed_heap", allow(dead_code, unused_imports))]

use std::fmt::Write as _;
use std::mem::{align_of, size_of};

use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable, semantic_stats_reset,
    semantic_stats_snapshot, semantic_type_stats_recording_disable,
    type_isolation_side_cache_snapshot, SemanticFallbackAttributionSnapshot, SemanticStatsSnapshot,
    UniAlloc,
};

#[cfg(feature = "fixed_heap")]
#[path = "../bin_support/fixed_heap_probe_global.rs"]
mod fixed_heap_probe_global;

#[cfg(feature = "fixed_heap")]
#[global_allocator]
static A: fixed_heap_probe_global::FixedHeapProbeAllocator =
    fixed_heap_probe_global::FixedHeapProbeAllocator;

#[cfg(not(feature = "fixed_heap"))]
#[global_allocator]
static A: UniAlloc = UniAlloc;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
struct ProducerPayload([u64; 8]);

#[inline(never)]
fn payload(seed: u64) -> ProducerPayload {
    ProducerPayload([
        seed,
        seed ^ 0x1111_1111_1111_1111,
        seed ^ 0x2222_2222_2222_2222,
        seed ^ 0x3333_3333_3333_3333,
        seed ^ 0x4444_4444_4444_4444,
        seed ^ 0x5555_5555_5555_5555,
        seed ^ 0x6666_6666_6666_6666,
        seed ^ 0x7777_7777_7777_7777,
    ])
}

#[inline(never)]
fn checksum(values: &[ProducerPayload]) -> u64 {
    values
        .iter()
        .fold(0xA6B1_6005_1C0A_E001_u64, |outer, value| {
            value
                .0
                .iter()
                .fold(outer.rotate_left(5), |acc, word| acc.rotate_left(7) ^ word)
        })
}

fn stats_delta(before: SemanticStatsSnapshot, after: SemanticStatsSnapshot, field: &str) -> usize {
    match field {
        "typed_allocations" => after
            .typed_allocations
            .saturating_sub(before.typed_allocations),
        "typed_deallocations" => after
            .typed_deallocations
            .saturating_sub(before.typed_deallocations),
        "fallback_allocations" => after
            .fallback_allocations
            .saturating_sub(before.fallback_allocations),
        "fallback_deallocations" => after
            .fallback_deallocations
            .saturating_sub(before.fallback_deallocations),
        _ => 0,
    }
}

fn fallback_delta(
    before: SemanticFallbackAttributionSnapshot,
    after: SemanticFallbackAttributionSnapshot,
    field: &str,
) -> usize {
    match field {
        "raw_alloc_no_metadata" => after
            .raw_alloc_no_metadata
            .saturating_sub(before.raw_alloc_no_metadata),
        "raw_alloc_no_metadata_bytes" => after
            .raw_alloc_no_metadata_bytes
            .saturating_sub(before.raw_alloc_no_metadata_bytes),
        "raw_dealloc_no_metadata" => after
            .raw_dealloc_no_metadata
            .saturating_sub(before.raw_dealloc_no_metadata),
        "raw_realloc_no_metadata" => after
            .raw_realloc_no_metadata
            .saturating_sub(before.raw_realloc_no_metadata),
        "realloc_recorded_old_metadata_new_allocations" => after
            .realloc_recorded_old_metadata_new_allocations
            .saturating_sub(before.realloc_recorded_old_metadata_new_allocations),
        _ => 0,
    }
}

fn main() {
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();

    assert_eq!(size_of::<ProducerPayload>(), 64);
    assert_eq!(align_of::<ProducerPayload>(), 8);

    // The binary intentionally uses only ordinary Rust `Result::clone` over a
    // `Vec`/`String` result.  It does not manually call UniAlloc metadata or
    // allocator ABIs; the companion runner verifies the compiler pass audits
    // the call as ambiguous and fail-closed.
    semantic_auto_metadata_disable();

    let mut source_vec = Vec::with_capacity(4);
    for index in 0..4_u64 {
        source_vec.push(payload(0xC10A_E000 + index));
    }
    let source_buffer = source_vec.as_ptr() as usize;
    let source_checksum = checksum(&source_vec);
    let source = Ok::<Vec<ProducerPayload>, String>(source_vec);

    semantic_stats_reset();
    let before_stats = semantic_stats_snapshot();
    let before_fallback = semantic_fallback_attribution_snapshot();

    let cloned = <Result<Vec<ProducerPayload>, String> as Clone>::clone(&source);
    let after_clone_stats = semantic_stats_snapshot();
    let after_clone_fallback = semantic_fallback_attribution_snapshot();

    let cloned_vec = cloned
        .as_ref()
        .expect("ambiguous clone should preserve Ok variant");
    let cloned_buffer = cloned_vec.as_ptr() as usize;
    assert_ne!(
        source_buffer, cloned_buffer,
        "Vec::clone must allocate a distinct buffer"
    );
    let cloned_len = cloned_vec.len();
    assert_eq!(cloned_len, 4);
    assert_eq!(checksum(cloned_vec), source_checksum);

    let clone_typed_allocations = stats_delta(before_stats, after_clone_stats, "typed_allocations");
    let clone_fallback_allocations =
        stats_delta(before_stats, after_clone_stats, "fallback_allocations");
    let clone_raw_alloc_no_metadata = fallback_delta(
        before_fallback,
        after_clone_fallback,
        "raw_alloc_no_metadata",
    );
    let clone_raw_alloc_no_metadata_bytes = fallback_delta(
        before_fallback,
        after_clone_fallback,
        "raw_alloc_no_metadata_bytes",
    );
    let clone_raw_realloc_no_metadata = fallback_delta(
        before_fallback,
        after_clone_fallback,
        "raw_realloc_no_metadata",
    );
    let clone_recorded_old_realloc_fallback = fallback_delta(
        before_fallback,
        after_clone_fallback,
        "realloc_recorded_old_metadata_new_allocations",
    );

    assert_eq!(
        clone_typed_allocations, 0,
        "ambiguous Clone must not create typed metadata"
    );
    assert!(
        clone_fallback_allocations >= 1,
        "ambiguous Clone should allocate through fallback"
    );
    assert!(
        clone_raw_alloc_no_metadata >= 1,
        "ambiguous Clone allocation should be raw fallback"
    );
    assert_eq!(
        clone_raw_realloc_no_metadata, 0,
        "Result::clone should not use raw realloc"
    );
    assert_eq!(clone_recorded_old_realloc_fallback, 0);

    drop(cloned);
    let after_drop_stats = semantic_stats_snapshot();
    let after_drop_fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();

    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();

    let clone_raw_dealloc_no_metadata = fallback_delta(
        after_clone_fallback,
        after_drop_fallback,
        "raw_dealloc_no_metadata",
    );

    assert_eq!(
        validation.recovery_identity_mismatches, 0,
        "ambiguous fallback must not mismatch metadata identities"
    );
    assert_eq!(
        side_cache.corrupt_slots, 0,
        "side cache must remain uncorrupted"
    );

    let mut output = String::new();
    let _ = write!(
        output,
        concat!(
            "{{",
            "\"source\":\"rustc_driver_mir_ambiguous_clone_fallback_probe\",",
            "\"clone_function\":\"Result::clone\",",
            "\"result_variant\":\"Ok\",",
            "\"same_layout_bytes\":{},",
            "\"source_len\":{},",
            "\"cloned_len\":{},",
            "\"source_buffer\":{},",
            "\"cloned_buffer\":{},",
            "\"buffers_distinct\":true,",
            "\"checksum\":{},",
            "\"clone_typed_allocations\":{},",
            "\"clone_typed_deallocations\":{},",
            "\"clone_fallback_allocations\":{},",
            "\"clone_fallback_deallocations_before_drop\":{},",
            "\"clone_raw_alloc_no_metadata\":{},",
            "\"clone_raw_alloc_no_metadata_bytes\":{},",
            "\"clone_raw_realloc_no_metadata\":{},",
            "\"clone_recorded_old_realloc_fallback\":{},",
            "\"drop_fallback_deallocations\":{},",
            "\"drop_raw_dealloc_no_metadata\":{},",
            "\"recovery_identity_matches\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_corrupt_slots\":{}",
            "}}"
        ),
        size_of::<ProducerPayload>(),
        4,
        cloned_len,
        source_buffer,
        cloned_buffer,
        source_checksum,
        clone_typed_allocations,
        stats_delta(before_stats, after_clone_stats, "typed_deallocations"),
        clone_fallback_allocations,
        stats_delta(before_stats, after_clone_stats, "fallback_deallocations"),
        clone_raw_alloc_no_metadata,
        clone_raw_alloc_no_metadata_bytes,
        clone_raw_realloc_no_metadata,
        clone_recorded_old_realloc_fallback,
        stats_delta(
            after_clone_stats,
            after_drop_stats,
            "fallback_deallocations"
        ),
        clone_raw_dealloc_no_metadata,
        validation.recovery_identity_matches,
        validation.recovery_identity_mismatches,
        side_cache.corrupt_slots,
    );
    println!("{}", output);
}
