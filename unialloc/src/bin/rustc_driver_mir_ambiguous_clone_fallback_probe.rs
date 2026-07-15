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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
struct ConsumerPayload([u64; 8]);

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

#[inline(never)]
fn supported_seed_protected_buffer() -> usize {
    let mut protected = Vec::<ProducerPayload>::with_capacity(4);
    for index in 0..4_u64 {
        protected.push(payload(0x5EED_0000 + index));
    }
    assert_ne!(checksum(&protected), 0);
    let address = protected.as_ptr() as usize;
    drop(protected);
    address
}

#[inline(never)]
fn supported_recover_protected_buffer(expected_address: usize) -> usize {
    let mut recovered = Vec::<ProducerPayload>::with_capacity(4);
    let address = recovered.as_ptr() as usize;
    assert_eq!(
        address, expected_address,
        "supported typed allocation did not recover its protected cache entry"
    );
    for index in 0..4_u64 {
        recovered.push(payload(0xC0DE_0000 + index));
    }
    assert_ne!(checksum(&recovered), 0);
    drop(recovered);
    address
}

#[inline(never)]
fn supported_wrong_type_seed_buffer() -> usize {
    let mut wrong_type = Vec::<ConsumerPayload>::with_capacity(4);
    for index in 0..4_u64 {
        wrong_type.push(ConsumerPayload(payload(0xBAD0_0000 + index).0));
    }
    let address = wrong_type.as_ptr() as usize;
    assert_ne!(address, 0);
    drop(wrong_type);
    address
}

#[inline(never)]
fn supported_plain_option_clone(
    source: &Option<Vec<ProducerPayload>>,
) -> Option<Vec<ProducerPayload>> {
    <Option<Vec<ProducerPayload>> as Clone>::clone(source)
}

#[inline(never)]
fn supported_plain_result_clone(
    source: &Result<Vec<ProducerPayload>, u8>,
) -> Result<Vec<ProducerPayload>, u8> {
    <Result<Vec<ProducerPayload>, u8> as Clone>::clone(source)
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
        "typed_cache_hits" => after
            .typed_cache_hits
            .saturating_sub(before.typed_cache_hits),
        "typed_cache_inserts" => after
            .typed_cache_inserts
            .saturating_sub(before.typed_cache_inserts),
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
    assert_eq!(size_of::<ConsumerPayload>(), size_of::<ProducerPayload>());
    assert_eq!(align_of::<ConsumerPayload>(), align_of::<ProducerPayload>());

    // The binary intentionally uses only ordinary Rust `Vec`, `Option::clone`,
    // and `Result::clone` operations. It does not manually call UniAlloc
    // metadata or allocator ABIs. The companion runner proves every broad
    // Option/Result Clone remains fail-closed while exact Vec controls still
    // receive compiler scopes and retain their typed-cache entries.
    semantic_auto_metadata_disable();

    // Keep the source allocation live so the protected seed below cannot
    // obtain the same address by ordinary lifetime reuse.
    let mut source_vec = Vec::with_capacity(4);
    for index in 0..4_u64 {
        source_vec.push(payload(0xC10A_E000 + index));
    }
    let source_buffer = source_vec.as_ptr() as usize;
    let source_checksum = checksum(&source_vec);
    let source = Ok::<Vec<ProducerPayload>, String>(source_vec);

    let mut plain_source_vec = Vec::with_capacity(4);
    for index in 0..4_u64 {
        plain_source_vec.push(payload(0x0110_0000 + index));
    }
    let plain_source_buffer = plain_source_vec.as_ptr() as usize;
    let plain_source_checksum = checksum(&plain_source_vec);
    let plain_source = Some(plain_source_vec);
    assert_ne!(plain_source_buffer, source_buffer);

    let mut plain_result_source_vec = Vec::with_capacity(4);
    for index in 0..4_u64 {
        plain_result_source_vec.push(payload(0xAE50_1700 + index));
    }
    let plain_result_source_buffer = plain_result_source_vec.as_ptr() as usize;
    let plain_result_source_checksum = checksum(&plain_result_source_vec);
    let plain_result_source = Ok::<Vec<ProducerPayload>, u8>(plain_result_source_vec);
    assert_ne!(plain_result_source_buffer, source_buffer);
    assert_ne!(plain_result_source_buffer, plain_source_buffer);

    semantic_stats_reset();

    let before_seed_stats = semantic_stats_snapshot();
    let before_seed_fallback = semantic_fallback_attribution_snapshot();
    let protected_buffer = supported_seed_protected_buffer();
    let after_seed_stats = semantic_stats_snapshot();
    let after_seed_fallback = semantic_fallback_attribution_snapshot();

    let seed_typed_allocations =
        stats_delta(before_seed_stats, after_seed_stats, "typed_allocations");
    let seed_typed_deallocations =
        stats_delta(before_seed_stats, after_seed_stats, "typed_deallocations");
    let seed_typed_cache_inserts =
        stats_delta(before_seed_stats, after_seed_stats, "typed_cache_inserts");
    assert_eq!(seed_typed_allocations, 1);
    assert_eq!(seed_typed_deallocations, 1);
    assert_eq!(seed_typed_cache_inserts, 1);
    assert_eq!(
        fallback_delta(
            before_seed_fallback,
            after_seed_fallback,
            "raw_alloc_no_metadata"
        ),
        0
    );
    assert_eq!(
        fallback_delta(
            before_seed_fallback,
            after_seed_fallback,
            "raw_dealloc_no_metadata"
        ),
        0
    );
    let seed_type_id = after_seed_stats.last_type_id;
    assert_ne!(seed_type_id, 0);

    let before_wrong_type_stats = semantic_stats_snapshot();
    let before_wrong_type_fallback = semantic_fallback_attribution_snapshot();
    let wrong_type_buffer = supported_wrong_type_seed_buffer();
    let after_wrong_type_stats = semantic_stats_snapshot();
    let after_wrong_type_fallback = semantic_fallback_attribution_snapshot();
    let wrong_type_typed_allocations = stats_delta(
        before_wrong_type_stats,
        after_wrong_type_stats,
        "typed_allocations",
    );
    let wrong_type_typed_deallocations = stats_delta(
        before_wrong_type_stats,
        after_wrong_type_stats,
        "typed_deallocations",
    );
    let wrong_type_typed_cache_inserts = stats_delta(
        before_wrong_type_stats,
        after_wrong_type_stats,
        "typed_cache_inserts",
    );
    let wrong_type_fallback_allocations = stats_delta(
        before_wrong_type_stats,
        after_wrong_type_stats,
        "fallback_allocations",
    );
    let wrong_type_fallback_deallocations = stats_delta(
        before_wrong_type_stats,
        after_wrong_type_stats,
        "fallback_deallocations",
    );
    let wrong_type_raw_alloc_no_metadata = fallback_delta(
        before_wrong_type_fallback,
        after_wrong_type_fallback,
        "raw_alloc_no_metadata",
    );
    let wrong_type_raw_dealloc_no_metadata = fallback_delta(
        before_wrong_type_fallback,
        after_wrong_type_fallback,
        "raw_dealloc_no_metadata",
    );
    assert_eq!(wrong_type_typed_allocations, 1);
    assert_eq!(wrong_type_typed_deallocations, 1);
    assert_eq!(wrong_type_typed_cache_inserts, 1);
    assert_eq!(wrong_type_fallback_allocations, 0);
    assert_eq!(wrong_type_fallback_deallocations, 0);
    assert_eq!(wrong_type_raw_alloc_no_metadata, 0);
    assert_eq!(wrong_type_raw_dealloc_no_metadata, 0);
    let wrong_type_id = after_wrong_type_stats.last_type_id;
    assert_ne!(wrong_type_id, 0);
    assert_ne!(wrong_type_id, seed_type_id);
    assert_ne!(wrong_type_buffer, protected_buffer);

    let before_plain_clone_stats = semantic_stats_snapshot();
    let before_plain_clone_fallback = semantic_fallback_attribution_snapshot();
    let plain_cloned = supported_plain_option_clone(&plain_source);
    let after_plain_clone_stats = semantic_stats_snapshot();
    let after_plain_clone_fallback = semantic_fallback_attribution_snapshot();
    let plain_cloned_vec = plain_cloned
        .as_ref()
        .expect("single-owner clone should preserve Some variant");
    let plain_cloned_buffer = plain_cloned_vec.as_ptr() as usize;
    let plain_cloned_len = plain_cloned_vec.len();
    assert_eq!(plain_cloned_len, 4);
    assert_eq!(checksum(plain_cloned_vec), plain_source_checksum);
    assert_ne!(plain_cloned_buffer, plain_source_buffer);
    assert_ne!(
        plain_cloned_buffer, protected_buffer,
        "audit-only Option clone consumed a protected typed cache entry"
    );
    assert_ne!(
        plain_cloned_buffer, wrong_type_buffer,
        "supported Option clone reused same-layout Consumer storage"
    );

    let plain_clone_typed_allocations = stats_delta(
        before_plain_clone_stats,
        after_plain_clone_stats,
        "typed_allocations",
    );
    let plain_clone_typed_cache_hits = stats_delta(
        before_plain_clone_stats,
        after_plain_clone_stats,
        "typed_cache_hits",
    );
    let plain_clone_fallback_allocations = stats_delta(
        before_plain_clone_stats,
        after_plain_clone_stats,
        "fallback_allocations",
    );
    let plain_clone_raw_alloc_no_metadata = fallback_delta(
        before_plain_clone_fallback,
        after_plain_clone_fallback,
        "raw_alloc_no_metadata",
    );
    let plain_clone_raw_realloc_no_metadata = fallback_delta(
        before_plain_clone_fallback,
        after_plain_clone_fallback,
        "raw_realloc_no_metadata",
    );
    assert_eq!(plain_clone_typed_allocations, 0);
    assert_eq!(plain_clone_typed_cache_hits, 0);
    assert_eq!(
        stats_delta(
            before_plain_clone_stats,
            after_plain_clone_stats,
            "typed_deallocations"
        ),
        0
    );
    assert_eq!(plain_clone_fallback_allocations, 1);
    assert_eq!(plain_clone_raw_alloc_no_metadata, 1);
    assert_eq!(plain_clone_raw_realloc_no_metadata, 0);
    let plain_clone_type_id = 0;

    drop(plain_cloned);
    let after_plain_drop_stats = semantic_stats_snapshot();
    let after_plain_drop_fallback = semantic_fallback_attribution_snapshot();
    let plain_clone_typed_deallocations = stats_delta(
        after_plain_clone_stats,
        after_plain_drop_stats,
        "typed_deallocations",
    );
    let plain_clone_typed_cache_inserts = stats_delta(
        after_plain_clone_stats,
        after_plain_drop_stats,
        "typed_cache_inserts",
    );
    let plain_clone_fallback_deallocations = stats_delta(
        after_plain_clone_stats,
        after_plain_drop_stats,
        "fallback_deallocations",
    );
    let plain_clone_raw_dealloc_no_metadata = fallback_delta(
        after_plain_clone_fallback,
        after_plain_drop_fallback,
        "raw_dealloc_no_metadata",
    );
    assert_eq!(plain_clone_typed_deallocations, 0);
    assert_eq!(plain_clone_typed_cache_inserts, 0);
    assert_eq!(plain_clone_fallback_deallocations, 1);
    assert_eq!(plain_clone_raw_dealloc_no_metadata, 1);

    let before_plain_result_clone_stats = semantic_stats_snapshot();
    let before_plain_result_clone_fallback = semantic_fallback_attribution_snapshot();
    let plain_result_cloned = supported_plain_result_clone(&plain_result_source);
    let after_plain_result_clone_stats = semantic_stats_snapshot();
    let after_plain_result_clone_fallback = semantic_fallback_attribution_snapshot();
    let plain_result_cloned_vec = plain_result_cloned
        .as_ref()
        .expect("single-owner Result clone should preserve Ok variant");
    let plain_result_cloned_buffer = plain_result_cloned_vec.as_ptr() as usize;
    let plain_result_cloned_len = plain_result_cloned_vec.len();
    assert_eq!(plain_result_cloned_len, 4);
    assert_eq!(
        checksum(plain_result_cloned_vec),
        plain_result_source_checksum
    );
    assert_ne!(plain_result_cloned_buffer, plain_result_source_buffer);
    assert_ne!(
        plain_result_cloned_buffer, protected_buffer,
        "audit-only Result clone consumed a protected typed cache entry"
    );
    assert_ne!(
        plain_result_cloned_buffer, wrong_type_buffer,
        "supported Result clone reused same-layout Consumer storage"
    );

    let plain_result_typed_allocations = stats_delta(
        before_plain_result_clone_stats,
        after_plain_result_clone_stats,
        "typed_allocations",
    );
    let plain_result_typed_cache_hits = stats_delta(
        before_plain_result_clone_stats,
        after_plain_result_clone_stats,
        "typed_cache_hits",
    );
    let plain_result_fallback_allocations = stats_delta(
        before_plain_result_clone_stats,
        after_plain_result_clone_stats,
        "fallback_allocations",
    );
    let plain_result_raw_alloc_no_metadata = fallback_delta(
        before_plain_result_clone_fallback,
        after_plain_result_clone_fallback,
        "raw_alloc_no_metadata",
    );
    let plain_result_raw_realloc_no_metadata = fallback_delta(
        before_plain_result_clone_fallback,
        after_plain_result_clone_fallback,
        "raw_realloc_no_metadata",
    );
    assert_eq!(plain_result_typed_allocations, 0);
    assert_eq!(plain_result_typed_cache_hits, 0);
    assert_eq!(
        stats_delta(
            before_plain_result_clone_stats,
            after_plain_result_clone_stats,
            "typed_deallocations"
        ),
        0
    );
    assert_eq!(plain_result_fallback_allocations, 1);
    assert_eq!(plain_result_raw_alloc_no_metadata, 1);
    assert_eq!(plain_result_raw_realloc_no_metadata, 0);
    let plain_result_type_id = 0;

    drop(plain_result_cloned);
    let after_plain_result_drop_stats = semantic_stats_snapshot();
    let after_plain_result_drop_fallback = semantic_fallback_attribution_snapshot();
    let plain_result_typed_deallocations = stats_delta(
        after_plain_result_clone_stats,
        after_plain_result_drop_stats,
        "typed_deallocations",
    );
    let plain_result_typed_cache_inserts = stats_delta(
        after_plain_result_clone_stats,
        after_plain_result_drop_stats,
        "typed_cache_inserts",
    );
    let plain_result_fallback_deallocations = stats_delta(
        after_plain_result_clone_stats,
        after_plain_result_drop_stats,
        "fallback_deallocations",
    );
    let plain_result_raw_dealloc_no_metadata = fallback_delta(
        after_plain_result_clone_fallback,
        after_plain_result_drop_fallback,
        "raw_dealloc_no_metadata",
    );
    assert_eq!(plain_result_typed_deallocations, 0);
    assert_eq!(plain_result_typed_cache_inserts, 0);
    assert_eq!(plain_result_fallback_deallocations, 1);
    assert_eq!(plain_result_raw_dealloc_no_metadata, 1);

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
    assert_eq!(
        clone_fallback_allocations, 1,
        "ambiguous Clone should perform exactly one fallback allocation"
    );
    assert_eq!(
        clone_raw_alloc_no_metadata, 1,
        "ambiguous Clone allocation should be exactly one raw fallback"
    );
    assert_eq!(clone_raw_alloc_no_metadata_bytes, 256);
    assert_eq!(
        clone_raw_realloc_no_metadata, 0,
        "Result::clone should not use raw realloc"
    );
    assert_eq!(clone_recorded_old_realloc_fallback, 0);

    drop(cloned);
    let after_drop_stats = semantic_stats_snapshot();
    let after_drop_fallback = semantic_fallback_attribution_snapshot();

    let clone_raw_dealloc_no_metadata = fallback_delta(
        after_clone_fallback,
        after_drop_fallback,
        "raw_dealloc_no_metadata",
    );
    assert_eq!(
        stats_delta(
            after_clone_stats,
            after_drop_stats,
            "fallback_deallocations"
        ),
        1
    );
    assert_eq!(clone_raw_dealloc_no_metadata, 1);

    let fallback_avoided_protected_buffer = cloned_buffer != protected_buffer;
    assert!(
        fallback_avoided_protected_buffer,
        "untyped ambiguous fallback reused a protected typed cache entry"
    );

    let before_recovery_stats = semantic_stats_snapshot();
    let before_recovery_fallback = semantic_fallback_attribution_snapshot();
    let recovered_buffer = supported_recover_protected_buffer(protected_buffer);
    let after_recovery_stats = semantic_stats_snapshot();
    let after_recovery_fallback = semantic_fallback_attribution_snapshot();
    let recovery_typed_allocations = stats_delta(
        before_recovery_stats,
        after_recovery_stats,
        "typed_allocations",
    );
    let recovery_typed_deallocations = stats_delta(
        before_recovery_stats,
        after_recovery_stats,
        "typed_deallocations",
    );
    let recovery_typed_cache_hits = stats_delta(
        before_recovery_stats,
        after_recovery_stats,
        "typed_cache_hits",
    );
    let recovery_typed_cache_inserts = stats_delta(
        before_recovery_stats,
        after_recovery_stats,
        "typed_cache_inserts",
    );
    assert_eq!(recovery_typed_allocations, 1);
    assert_eq!(recovery_typed_deallocations, 1);
    assert_eq!(recovery_typed_cache_hits, 1);
    assert_eq!(recovery_typed_cache_inserts, 1);
    assert_eq!(
        fallback_delta(
            before_recovery_fallback,
            after_recovery_fallback,
            "raw_alloc_no_metadata"
        ),
        0
    );
    assert_eq!(
        fallback_delta(
            before_recovery_fallback,
            after_recovery_fallback,
            "raw_dealloc_no_metadata"
        ),
        0
    );
    let recovery_type_id = after_recovery_stats.last_type_id;
    assert_eq!(recovery_type_id, seed_type_id);
    let typed_recovery_preserved = recovered_buffer == protected_buffer;
    assert!(typed_recovery_preserved);

    let retained_source = match source {
        Ok(values) => values,
        Err(_) => unreachable!("probe source is always the Ok variant"),
    };
    drop(retained_source);
    drop(plain_source);
    drop(plain_result_source);
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();

    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();

    assert_eq!(
        validation.recovery_identity_mismatches, 0,
        "ambiguous fallback must not mismatch metadata identities"
    );
    assert_eq!(
        side_cache.corrupt_slots, 0,
        "side cache must remain uncorrupted"
    );

    let plain_result_clone_json = format!(
        concat!(
            "{{",
            "\"clone_function\":\"Result::clone\",",
            "\"variant\":\"Ok\",",
            "\"source_buffer\":{},",
            "\"cloned_buffer\":{},",
            "\"cloned_len\":{},",
            "\"buffers_distinct\":true,",
            "\"reused_protected_buffer\":false,",
            "\"avoided_protected_buffer\":true,",
            "\"avoided_wrong_type_buffer\":true,",
            "\"checksum\":{},",
            "\"type_id\":{},",
            "\"typed_allocations\":{},",
            "\"typed_deallocations\":{},",
            "\"typed_cache_hits\":{},",
            "\"typed_cache_inserts\":{},",
            "\"fallback_allocations\":{},",
            "\"fallback_deallocations\":{},",
            "\"raw_alloc_no_metadata\":{},",
            "\"raw_dealloc_no_metadata\":{},",
            "\"raw_realloc_no_metadata\":{}",
            "}}"
        ),
        plain_result_source_buffer,
        plain_result_cloned_buffer,
        plain_result_cloned_len,
        plain_result_source_checksum,
        plain_result_type_id,
        plain_result_typed_allocations,
        plain_result_typed_deallocations,
        plain_result_typed_cache_hits,
        plain_result_typed_cache_inserts,
        plain_result_fallback_allocations,
        plain_result_fallback_deallocations,
        plain_result_raw_alloc_no_metadata,
        plain_result_raw_dealloc_no_metadata,
        plain_result_raw_realloc_no_metadata,
    );

    let mut output = String::new();
    let _ = write!(
        output,
        concat!(
            "{{",
            "\"source\":\"rustc_driver_mir_ambiguous_clone_fallback_probe\",",
            "\"clone_function\":\"Result::clone\",",
            "\"result_variant\":\"Ok\",",
            "\"plain_clone_function\":\"Option::clone\",",
            "\"plain_clone_variant\":\"Some\",",
            "\"plain_result_clone\":{},",
            "\"same_layout_bytes\":{},",
            "\"source_len\":{},",
            "\"cloned_len\":{},",
            "\"source_buffer\":{},",
            "\"cloned_buffer\":{},",
            "\"plain_source_buffer\":{},",
            "\"plain_cloned_buffer\":{},",
            "\"plain_cloned_len\":{},",
            "\"protected_buffer\":{},",
            "\"recovered_buffer\":{},",
            "\"wrong_type_buffer\":{},",
            "\"buffers_distinct\":true,",
            "\"plain_clone_buffers_distinct\":true,",
            "\"plain_clone_reused_protected_buffer\":false,",
            "\"plain_clone_avoided_protected_buffer\":true,",
            "\"plain_clone_avoided_wrong_type_buffer\":true,",
            "\"fallback_avoided_protected_buffer\":{},",
            "\"typed_recovery_preserved\":{},",
            "\"checksum\":{},",
            "\"plain_checksum\":{},",
            "\"seed_type_id\":{},",
            "\"recovery_type_id\":{},",
            "\"plain_clone_type_id\":{},",
            "\"wrong_type_id\":{},",
            "\"seed_typed_allocations\":{},",
            "\"seed_typed_deallocations\":{},",
            "\"seed_typed_cache_inserts\":{},",
            "\"wrong_type_typed_allocations\":{},",
            "\"wrong_type_typed_deallocations\":{},",
            "\"wrong_type_typed_cache_inserts\":{},",
            "\"wrong_type_fallback_allocations\":{},",
            "\"wrong_type_fallback_deallocations\":{},",
            "\"wrong_type_raw_alloc_no_metadata\":{},",
            "\"wrong_type_raw_dealloc_no_metadata\":{},",
            "\"plain_clone_typed_allocations\":{},",
            "\"plain_clone_typed_deallocations\":{},",
            "\"plain_clone_typed_cache_hits\":{},",
            "\"plain_clone_typed_cache_inserts\":{},",
            "\"plain_clone_fallback_allocations\":{},",
            "\"plain_clone_fallback_deallocations\":{},",
            "\"plain_clone_raw_alloc_no_metadata\":{},",
            "\"plain_clone_raw_dealloc_no_metadata\":{},",
            "\"plain_clone_raw_realloc_no_metadata\":{},",
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
            "\"recovery_typed_allocations\":{},",
            "\"recovery_typed_deallocations\":{},",
            "\"recovery_typed_cache_hits\":{},",
            "\"recovery_typed_cache_inserts\":{},",
            "\"recovery_identity_matches\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_corrupt_slots\":{}",
            "}}"
        ),
        plain_result_clone_json,
        size_of::<ProducerPayload>(),
        4,
        cloned_len,
        source_buffer,
        cloned_buffer,
        plain_source_buffer,
        plain_cloned_buffer,
        plain_cloned_len,
        protected_buffer,
        recovered_buffer,
        wrong_type_buffer,
        fallback_avoided_protected_buffer,
        typed_recovery_preserved,
        source_checksum,
        plain_source_checksum,
        seed_type_id,
        recovery_type_id,
        plain_clone_type_id,
        wrong_type_id,
        seed_typed_allocations,
        seed_typed_deallocations,
        seed_typed_cache_inserts,
        wrong_type_typed_allocations,
        wrong_type_typed_deallocations,
        wrong_type_typed_cache_inserts,
        wrong_type_fallback_allocations,
        wrong_type_fallback_deallocations,
        wrong_type_raw_alloc_no_metadata,
        wrong_type_raw_dealloc_no_metadata,
        plain_clone_typed_allocations,
        plain_clone_typed_deallocations,
        plain_clone_typed_cache_hits,
        plain_clone_typed_cache_inserts,
        plain_clone_fallback_allocations,
        plain_clone_fallback_deallocations,
        plain_clone_raw_alloc_no_metadata,
        plain_clone_raw_dealloc_no_metadata,
        plain_clone_raw_realloc_no_metadata,
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
        recovery_typed_allocations,
        recovery_typed_deallocations,
        recovery_typed_cache_hits,
        recovery_typed_cache_inserts,
        validation.recovery_identity_matches,
        validation.recovery_identity_mismatches,
        side_cache.corrupt_slots,
    );
    println!("{}", output);
}
