#![cfg(all(feature = "lifetime_hugepage", not(feature = "fixed_heap")))]

use core::alloc::{GlobalAlloc, Layout};
use unialloc::{
    delayed_free_snapshot, lifetime_hugepage_configure,
    lifetime_hugepage_phase_flush_current_thread, lifetime_hugepage_stats_reset,
    lifetime_hugepage_stats_snapshot, with_semantic_metadata, AllocationMetadata,
    LifetimeHugepagePolicy, SemanticAlloc, UniAlloc, FLAG_DELAYED_FREE, LIFETIME_HINT_EPHEMERAL,
    LIFETIME_HINT_LONG_LIVED, LIFETIME_HUGEPAGE_EXTENT_BYTES,
    LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES,
};

static TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn test_guard() -> std::sync::MutexGuard<'static, ()> {
    TEST_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn metadata(type_id: u64, hint: u16) -> AllocationMetadata {
    AllocationMetadata::for_type(type_id).with_lifetime_hint(hint)
}

#[test]
fn lifetime_payload_arena_routes_reuses_reallocates_and_reclaims() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(4096, 64).unwrap();
    let resized_layout = Layout::from_size_align(4080, 64).unwrap();
    let long = metadata(0xA11C_0001, LIFETIME_HINT_LONG_LIVED);
    let ephemeral = metadata(0xA11C_0002, LIFETIME_HINT_EPHEMERAL);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::SegregatedOrdinary
    ));

    unsafe {
        let long_ptr = alloc.alloc_with_metadata(layout, long);
        let ephemeral_a = alloc.alloc_with_metadata(layout, ephemeral);
        let ephemeral_b = alloc.alloc_with_metadata(layout, ephemeral);
        assert!(!long_ptr.is_null());
        assert!(!ephemeral_a.is_null());
        assert!(!ephemeral_b.is_null());
        long_ptr.write(0x5a);

        let peak = lifetime_hugepage_stats_snapshot();
        assert_eq!(peak.current_ordinary_extents, 2);
        assert_eq!(peak.current_hugetlb_extents, 0);
        assert_eq!(peak.live_objects, 3);

        alloc.dealloc_with_metadata(ephemeral_a, layout, ephemeral);
        let recycled = alloc.alloc_with_metadata(layout, ephemeral);
        assert_eq!(recycled, ephemeral_a, "released slots should be reused");
        recycled.write(0x6b);

        let resized = alloc.realloc_with_split_metadata(
            recycled,
            layout,
            resized_layout.size(),
            ephemeral,
            ephemeral,
        );
        assert_eq!(
            resized, recycled,
            "same identity should reuse a fitting slot"
        );
        assert_eq!(resized.read(), 0x6b);

        let moved = alloc.realloc_with_split_metadata(
            resized,
            resized_layout,
            resized_layout.size(),
            ephemeral,
            long,
        );
        assert!(!moved.is_null());
        assert_ne!(moved, resized, "a lifetime-class change must move arenas");
        assert_eq!(moved.read(), 0x6b, "moved realloc must preserve payload");

        alloc.dealloc_with_metadata(ephemeral_b, layout, ephemeral);
        let after_ephemeral = lifetime_hugepage_stats_snapshot();
        assert_eq!(after_ephemeral.current_ordinary_extents, 1);
        assert_eq!(after_ephemeral.live_ephemeral_objects, 0);
        assert!(after_ephemeral.extent_unmaps >= 1);
        assert_eq!(long_ptr.read(), 0x5a);

        alloc.dealloc_with_metadata(long_ptr, layout, long);
        alloc.dealloc_with_metadata(moved, resized_layout, long);
    }

    let drained = lifetime_hugepage_stats_snapshot();
    assert_eq!(drained.live_objects, 0);
    assert_eq!(drained.current_extents, 0);
    assert!(drained.all_mappings_released);

    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn unknown_falls_back_and_global_drop_uses_arena_provenance() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(1024, 16).unwrap();
    let long = metadata(0xA11C_0010, LIFETIME_HINT_LONG_LIVED);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::SegregatedOrdinary
    ));

    unsafe {
        let fallback = alloc.alloc_with_metadata(layout, AllocationMetadata::unknown());
        assert!(!fallback.is_null());
        alloc.dealloc_with_metadata(fallback, layout, AllocationMetadata::unknown());

        let arena_ptr = with_semantic_metadata(long, || GlobalAlloc::alloc(&alloc, layout));
        assert!(!arena_ptr.is_null());
        assert_eq!(lifetime_hugepage_stats_snapshot().live_objects, 1);

        // Model a Box/Vec Drop site after the compiler metadata scope ended.
        GlobalAlloc::dealloc(&alloc, arena_ptr, layout);
    }

    let snapshot = lifetime_hugepage_stats_snapshot();
    assert_eq!(snapshot.routed_allocations, 1);
    assert!(snapshot.unknown_bypasses >= 1);
    assert_eq!(snapshot.live_objects, 0);
    assert!(snapshot.all_mappings_released);

    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn tiered_policy_and_cross_thread_free_preserve_backend_provenance() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(2048, 64).unwrap();
    let long = metadata(0xA11C_0020, LIFETIME_HINT_LONG_LIVED);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::LongLivedHugepage
    ));

    let ptr = unsafe { alloc.alloc_with_metadata(layout, long) };
    assert!(!ptr.is_null());
    let mapped = lifetime_hugepage_stats_snapshot();
    assert_eq!(mapped.live_long_lived_objects, 1);
    assert_eq!(mapped.current_extents, 1);
    assert_eq!(
        mapped.hugetlb_extent_mappings + mapped.hugetlb_fallback_extent_mappings,
        1
    );

    let ptr_addr = ptr as usize;
    std::thread::spawn(move || unsafe {
        // No active metadata exists on this thread. Pointer provenance still
        // selects the arena backend.
        GlobalAlloc::dealloc(&alloc, ptr_addr as *mut u8, layout);
    })
    .join()
    .unwrap();

    let released = lifetime_hugepage_stats_snapshot();
    assert_eq!(released.live_objects, 0);
    assert!(released.all_mappings_released);

    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn exact_identity_owns_regions_and_policy_routing_precedes_tls_cache_hits() {
    let _guard = test_guard();
    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));

    std::thread::spawn(|| {
        let alloc = UniAlloc::new();
        let layout = Layout::from_size_align(4096, 64).unwrap();
        let first_type = metadata(0xA11C_0030, LIFETIME_HINT_LONG_LIVED);
        let second_type = metadata(0xA11C_0031, LIFETIME_HINT_LONG_LIVED);

        unsafe {
            // Seed the existing semantic cache before activating the policy.
            let cached = alloc.alloc_with_metadata(layout, first_type);
            assert!(!cached.is_null());
            alloc.dealloc_with_metadata(cached, layout, first_type);
        }
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::SegregatedOrdinary
        ));

        unsafe {
            let first = alloc.alloc_with_metadata(layout, first_type);
            let second = alloc.alloc_with_metadata(layout, second_type);
            assert!(!first.is_null());
            assert!(!second.is_null());
            let active = lifetime_hugepage_stats_snapshot();
            assert_eq!(active.routed_allocations, 2);
            assert_eq!(active.current_extents, 1);
            assert_eq!(active.current_identity_regions, 2);
            let first_extent = (first as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
            let second_extent = (second as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
            assert_eq!(first_extent, second_extent);
            assert_ne!(
                (first as usize - first_extent) / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES,
                (second as usize - second_extent) / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES,
            );

            alloc.dealloc_with_metadata(first, layout, first_type);
            alloc.dealloc_with_metadata(second, layout, second_type);
        }
        assert!(lifetime_hugepage_phase_flush_current_thread() >= 1);
    })
    .join()
    .unwrap();

    let released = lifetime_hugepage_stats_snapshot();
    assert_eq!(released.live_objects, 0);
    assert!(released.all_mappings_released);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn delayed_free_eviction_and_phase_flush_terminally_release_arena_extent() {
    let _guard = test_guard();
    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::SegregatedOrdinary
    ));

    std::thread::spawn(|| {
        let alloc = UniAlloc::new();
        let layout = Layout::from_size_align(2048, 64).unwrap();
        let delayed = metadata(0xA11C_0040, LIFETIME_HINT_EPHEMERAL).with_flags(FLAG_DELAYED_FREE);
        let mut ptrs = Vec::new();
        unsafe {
            for _ in 0..128 {
                let ptr = alloc.alloc_with_metadata(layout, delayed);
                assert!(!ptr.is_null());
                ptrs.push(ptr);
            }
            for ptr in ptrs.drain(..) {
                alloc.dealloc_with_metadata(ptr, layout, delayed);
            }
        }
        let retained = delayed_free_snapshot().occupied_slots;
        assert!(retained > 0 && retained < 128);
        let after_eviction = lifetime_hugepage_stats_snapshot();
        assert_eq!(after_eviction.live_objects, retained);
        assert!(after_eviction.routed_deallocations > 0);
        assert_eq!(lifetime_hugepage_phase_flush_current_thread(), retained);
    })
    .join()
    .unwrap();

    let released = lifetime_hugepage_stats_snapshot();
    assert_eq!(delayed_free_snapshot().occupied_slots, 0);
    assert_eq!(released.live_objects, 0);
    assert!(released.all_mappings_released);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn many_exact_types_share_hugepage_extents_but_keep_distinct_regions() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(4096, 64).unwrap();

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::SegregatedOrdinary
    ));

    let mut values = Vec::new();
    unsafe {
        for type_index in 0..40u64 {
            let exact = metadata(0xA11C_1000 + type_index, LIFETIME_HINT_LONG_LIVED);
            let ptr = alloc.alloc_with_metadata(layout, exact);
            assert!(!ptr.is_null());
            (ptr as *mut u64).write(type_index);
            values.push((ptr, exact, type_index));
        }
    }
    let packed = lifetime_hugepage_stats_snapshot();
    assert_eq!(packed.current_identity_regions, 40);
    assert_eq!(packed.current_extents, 2);

    let first_extent = (values[0].0 as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
    let first_extent_regions = values
        .iter()
        .filter(|(ptr, _, _)| {
            (*ptr as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1) == first_extent
        })
        .count();
    assert_eq!(first_extent_regions, 32);

    unsafe {
        let mut still_live = Vec::new();
        for (ptr, exact, type_index) in values.drain(..) {
            let extent = (ptr as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
            if extent == first_extent {
                assert_eq!((ptr as *const u64).read(), type_index);
                alloc.dealloc_with_metadata(ptr, layout, exact);
            } else {
                still_live.push((ptr, exact, type_index));
            }
        }
        let partially_reclaimed = lifetime_hugepage_stats_snapshot();
        assert_eq!(partially_reclaimed.current_extents, 1);
        assert_eq!(partially_reclaimed.current_identity_regions, 8);
        for (ptr, exact, type_index) in still_live {
            assert_eq!((ptr as *const u64).read(), type_index);
            alloc.dealloc_with_metadata(ptr, layout, exact);
        }
    }
    let released = lifetime_hugepage_stats_snapshot();
    assert_eq!(released.current_identity_regions, 0);
    assert!(released.all_mappings_released);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn identity_region_is_reassigned_only_after_its_last_live_slot() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(4096, 64).unwrap();
    let first_type = metadata(0xA11C_2000, LIFETIME_HINT_LONG_LIVED);
    let second_type = metadata(0xA11C_2001, LIFETIME_HINT_LONG_LIVED);
    let third_type = metadata(0xA11C_2002, LIFETIME_HINT_LONG_LIVED);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::SegregatedOrdinary
    ));

    let mut first_values = Vec::new();
    unsafe {
        for index in 0..17u64 {
            let ptr = alloc.alloc_with_metadata(layout, first_type);
            assert!(!ptr.is_null());
            (ptr as *mut u64).write(index);
            first_values.push(ptr);
        }
    }
    let extent = (first_values[0] as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
    let first_region =
        (first_values[0] as usize - extent) / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
    let spill_region =
        (first_values[16] as usize - extent) / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
    assert_ne!(first_region, spill_region);

    unsafe {
        for ptr in first_values.drain(..15) {
            alloc.dealloc_with_metadata(ptr, layout, first_type);
        }
        let sibling = alloc.alloc_with_metadata(layout, second_type);
        assert!(!sibling.is_null());
        let sibling_region = (sibling as usize - extent) / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
        assert_ne!(sibling_region, first_region);

        alloc.dealloc_with_metadata(first_values.remove(0), layout, first_type);
        let reassigned = alloc.alloc_with_metadata(layout, third_type);
        assert!(!reassigned.is_null());
        let reassigned_region =
            (reassigned as usize - extent) / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
        assert_eq!(reassigned_region, first_region);
        assert_eq!((first_values[0] as *const u64).read(), 16);

        alloc.dealloc_with_metadata(first_values.remove(0), layout, first_type);
        alloc.dealloc_with_metadata(sibling, layout, second_type);
        alloc.dealloc_with_metadata(reassigned, layout, third_type);
    }

    let released = lifetime_hugepage_stats_snapshot();
    assert_eq!(
        released.identity_region_assignments,
        released.identity_region_releases
    );
    assert!(released.all_mappings_released);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}
