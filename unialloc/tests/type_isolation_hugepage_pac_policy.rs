#![cfg(all(
    feature = "stats",
    feature = "type_isolation",
    feature = "hugepage",
    feature = "pac"
))]

#[cfg(feature = "fixed_heap")]
#[path = "../src/bin_support/fixed_heap_probe_global.rs"]
mod fixed_heap_probe_global;

use unialloc::alloc_api::FLAG_POINTER_AUTH;
use unialloc::{
    semantic_auto_metadata_disable, semantic_metadata_validation_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    type_isolation_side_cache_snapshot, with_semantic_metadata, AllocationMetadata,
    FLAG_HUGEPAGE_METADATA, FLAG_TYPE_ISOLATED,
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
fn hugepage_and_pac_policy_composition_preserves_cache_identity() {
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();

    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let authenticated = AllocationMetadata::for_type(0x571F_E003)
        .with_module(0xC0DE_E003)
        .with_callsite(0xA110_E021)
        .with_flags(FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH);
    let authenticated_hugepage = AllocationMetadata {
        flags: authenticated.flags | FLAG_HUGEPAGE_METADATA,
        callsite: 0xA110_E022,
        ..authenticated
    };

    let ordinary = vec_with_metadata(authenticated);
    let ordinary_ptr = ordinary.as_ptr() as usize;
    assert_eq!(ordinary.capacity(), CAPACITY);
    drop(ordinary);
    let after_ordinary_free = semantic_stats_snapshot();
    let ordinary_signs = after_ordinary_free.metadata_pac_auth_signs
        + after_ordinary_free.metadata_pac_software_fallback_signs;
    assert!(ordinary_signs > 0, "{:?}", after_ordinary_free);

    let hugepage = vec_with_metadata(authenticated_hugepage);
    let hugepage_ptr = hugepage.as_ptr() as usize;
    assert_eq!(hugepage.capacity(), CAPACITY);
    assert_ne!(
        hugepage_ptr, ordinary_ptr,
        "adding the hugepage policy must not consume authenticated ordinary-domain storage"
    );
    drop(hugepage);
    let after_hugepage_free = semantic_stats_snapshot();
    let composed_signs = after_hugepage_free.metadata_pac_auth_signs
        + after_hugepage_free.metadata_pac_software_fallback_signs;
    assert!(
        composed_signs > ordinary_signs,
        "the PAC + hugepage policy must sign its own cached metadata: {:?}",
        after_hugepage_free
    );

    let recovered_ordinary = vec_with_metadata(authenticated);
    assert_eq!(
        recovered_ordinary.as_ptr() as usize,
        ordinary_ptr,
        "the authenticated ordinary policy must recover only its own entry"
    );
    let after_ordinary_recovery = semantic_stats_snapshot();
    let ordinary_verifications = after_ordinary_recovery.metadata_pac_auth_verifications
        + after_ordinary_recovery.metadata_pac_software_fallback_verifications;
    assert!(ordinary_verifications > 0, "{:?}", after_ordinary_recovery);
    drop(recovered_ordinary);

    let recovered_hugepage = vec_with_metadata(authenticated_hugepage);
    assert_eq!(
        recovered_hugepage.as_ptr() as usize,
        hugepage_ptr,
        "the authenticated hugepage policy must recover its own entry even when hugepage backing falls back"
    );
    let after_hugepage_recovery = semantic_stats_snapshot();
    let composed_verifications = after_hugepage_recovery.metadata_pac_auth_verifications
        + after_hugepage_recovery.metadata_pac_software_fallback_verifications;
    assert!(
        composed_verifications > ordinary_verifications,
        "the PAC + hugepage policy must verify its own cached metadata: {:?}",
        after_hugepage_recovery
    );
    drop(recovered_hugepage);

    let stats = semantic_stats_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    assert_eq!(stats.fallback_allocations, 0, "{stats:?}");
    assert_eq!(stats.fallback_deallocations, 0, "{stats:?}");
    assert!(
        stats.metadata_pac_auth_signs + stats.metadata_pac_software_fallback_signs > 0,
        "the combined policy must authenticate cached metadata through hardware PAC or the safe software fallback: {:?}",
        stats
    );
    assert!(
        stats.metadata_pac_auth_verifications + stats.metadata_pac_software_fallback_verifications
            > 0,
        "the combined policy must verify authenticated metadata before reuse: {:?}",
        stats
    );
    assert_eq!(stats.metadata_pac_auth_failures, 0, "{stats:?}");
    assert_eq!(
        stats.metadata_pac_software_fallback_failures, 0,
        "{stats:?}"
    );
    assert_eq!(
        stats.policy_flags_seen & (FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH | FLAG_HUGEPAGE_METADATA),
        FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH | FLAG_HUGEPAGE_METADATA,
        "{stats:?}"
    );
    assert_eq!(validation.recovery_identity_mismatches, 0, "{validation:?}");
    assert_eq!(side_cache.corrupt_slots, 0, "{side_cache:?}");
    semantic_stats_recording_disable();
}
