//! Compile-time layout contract between the UniAlloc runtime and the
//! Rust-for-Linux bridge.
//!
//! These records cross an `extern "C"` boundary.  A successful no_std final
//! crate build therefore proves that the bridge and the linked runtime agree
//! on the ABI version, record size, and every runtime-visible field offset.

use core::mem::{align_of, offset_of, size_of};

use crate::unialloc_bridge::{
    ConstrainedBootSample as BridgeConstrainedBootSample,
    SemanticFallbackAttributionSnapshot as BridgeFallbackAttributionSnapshot,
    SemanticMetadataValidationSnapshot as BridgeMetadataValidationSnapshot,
    SemanticStatsSnapshot as BridgeStatsSnapshot,
    SemanticTypeStatsSnapshot as BridgeTypeStatsSnapshot,
};
use unialloc::{
    SemanticFallbackAttributionSnapshot as RuntimeFallbackAttributionSnapshot,
    SemanticMetadataValidationSnapshot as RuntimeMetadataValidationSnapshot,
    SemanticStatsSnapshot as RuntimeStatsSnapshot,
    SemanticTypeStatsSnapshot as RuntimeTypeStatsSnapshot, UniallocConstrainedBootSample,
};

macro_rules! assert_abi_layout_pair {
    ($bridge:ty, $runtime:ty, size = $size:expr; $( $field:ident = $offset:expr ),+ $(,)?) => {
        const _: () = {
            assert!(size_of::<$bridge>() == $size);
            assert!(size_of::<$runtime>() == $size);
            assert!(align_of::<$bridge>() == 8);
            assert!(align_of::<$runtime>() == 8);
            $(
                assert!(offset_of!($bridge, $field) == $offset);
                assert!(offset_of!($runtime, $field) == $offset);
            )+
        };
    };
}

const _: () = {
    assert!(unialloc::SEMANTIC_STATS_SNAPSHOT_ABI_VERSION == 3);
    assert!(unialloc::SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION == 2);
    assert!(unialloc::SEMANTIC_FALLBACK_ATTRIBUTION_SNAPSHOT_ABI_VERSION == 1);
    assert!(unialloc::SEMANTIC_METADATA_VALIDATION_SNAPSHOT_ABI_VERSION == 1);
    assert!(unialloc::CONSTRAINED_BOOT_SAMPLE_ABI_VERSION == 2);
    assert!(
        crate::unialloc_bridge::UNIALLOC_SEMANTIC_STATS_SNAPSHOT_ABI_VERSION
            == unialloc::SEMANTIC_STATS_SNAPSHOT_ABI_VERSION
    );
    assert!(
        crate::unialloc_bridge::UNIALLOC_SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION
            == unialloc::SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION
    );
    assert!(
        crate::unialloc_bridge::UNIALLOC_SEMANTIC_FALLBACK_ATTRIBUTION_SNAPSHOT_ABI_VERSION
            == unialloc::SEMANTIC_FALLBACK_ATTRIBUTION_SNAPSHOT_ABI_VERSION
    );
    assert!(
        crate::unialloc_bridge::UNIALLOC_SEMANTIC_METADATA_VALIDATION_SNAPSHOT_ABI_VERSION
            == unialloc::SEMANTIC_METADATA_VALIDATION_SNAPSHOT_ABI_VERSION
    );
    assert!(
        crate::unialloc_bridge::UNIALLOC_CONSTRAINED_BOOT_SAMPLE_ABI_VERSION
            == unialloc::CONSTRAINED_BOOT_SAMPLE_ABI_VERSION
    );
};

assert_abi_layout_pair!(
    BridgeStatsSnapshot,
    RuntimeStatsSnapshot,
    size = 192;
    total_allocations = 0,
    typed_allocations = 8,
    fallback_allocations = 16,
    total_allocated_bytes = 24,
    typed_allocated_bytes = 32,
    fallback_allocated_bytes = 40,
    typed_deallocations = 48,
    fallback_deallocations = 56,
    policy_flags_seen = 64,
    last_type_id = 72,
    coverage_basis_points = 80,
    typed_cache_hits = 88,
    typed_cache_inserts = 96,
    typed_cache_bypasses = 104,
    delayed_free_enqueues = 112,
    delayed_free_flushes = 120,
    metadata_pac_auth_signs = 128,
    metadata_pac_auth_verifications = 136,
    metadata_pac_auth_failures = 144,
    metadata_pac_software_fallback_signs = 152,
    metadata_pac_software_fallback_verifications = 160,
    metadata_pac_software_fallback_failures = 168,
    total_deallocations = 176,
    semantic_type_stats_dropped_events = 184,
);

assert_abi_layout_pair!(
    BridgeTypeStatsSnapshot,
    RuntimeTypeStatsSnapshot,
    size = 112;
    type_id = 0,
    module_id = 8,
    callsite = 16,
    allocations = 24,
    allocated_bytes = 32,
    deallocations = 40,
    cache_hits = 48,
    cache_inserts = 56,
    cache_bypasses = 64,
    observed_alloc_size = 72,
    observed_alloc_align = 80,
    observed_dealloc_size = 88,
    observed_dealloc_align = 96,
    policy_flags_seen = 104,
);

assert_abi_layout_pair!(
    BridgeFallbackAttributionSnapshot,
    RuntimeFallbackAttributionSnapshot,
    size = 64;
    raw_alloc_no_metadata = 0,
    raw_alloc_no_metadata_bytes = 8,
    raw_dealloc_no_metadata = 16,
    raw_realloc_no_metadata = 24,
    raw_realloc_no_metadata_bytes = 32,
    raw_realloc_moved_dealloc_no_metadata = 40,
    realloc_recorded_old_metadata_new_allocations = 48,
    realloc_recorded_old_metadata_new_allocation_bytes = 56,
);

assert_abi_layout_pair!(
    BridgeMetadataValidationSnapshot,
    RuntimeMetadataValidationSnapshot,
    size = 64;
    recovery_identity_matches = 0,
    recovery_identity_mismatches = 8,
    last_mismatch_requested_type_id = 16,
    last_mismatch_recorded_type_id = 24,
    last_mismatch_requested_module_id = 32,
    last_mismatch_recorded_module_id = 40,
    last_mismatch_requested_callsite = 48,
    last_mismatch_recorded_callsite = 56,
);

assert_abi_layout_pair!(
    BridgeConstrainedBootSample,
    UniallocConstrainedBootSample,
    size = 152;
    boot_cycle = 0,
    fixed_heap_ready = 8,
    c_abi_invoked = 9,
    c_abi_ready_after_init = 10,
    c_abi_round_trips = 16,
    c_abi_invalid_layouts_rejected = 24,
    c_abi_over_page_alignment_checked = 32,
    c_abi_over_page_alignment = 40,
    allocator_total_allocations = 48,
    allocator_total_deallocations = 56,
    allocator_typed_allocations = 64,
    allocator_typed_deallocations = 72,
    allocator_fallback_allocations = 80,
    allocator_fallback_deallocations = 88,
    allocator_total_allocated_bytes = 96,
    allocator_typed_allocated_bytes = 104,
    allocator_fallback_allocated_bytes = 112,
    allocator_coverage_basis_points = 120,
    allocator_type_stats_rows = 128,
    allocator_type_stats_dropped_events = 136,
    allocator_type_stats_probe_matched = 144,
);
