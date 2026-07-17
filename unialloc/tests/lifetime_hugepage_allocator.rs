#![cfg(all(feature = "lifetime_hugepage", not(feature = "fixed_heap")))]
#![feature(allocator_api)]

use core::alloc::{Allocator, GlobalAlloc, Layout};
use unialloc::alloc_api::type_isolation::{
    __unialloc_semantic_scope_pop, __unialloc_semantic_scope_push_hints,
    semantic_allocation_slow_path_enabled, take_auto_deallocation_metadata,
};
use unialloc::alloc_api::FLAG_GUARD_PAGES;
use unialloc::{
    delayed_free_snapshot, lifetime_hugepage_adaptive_site_force_track_all_disable,
    lifetime_hugepage_adaptive_site_force_track_all_enable,
    lifetime_hugepage_adaptive_site_recording_disable,
    lifetime_hugepage_adaptive_site_recording_enable, lifetime_hugepage_adaptive_site_snapshot,
    lifetime_hugepage_advance_epoch, lifetime_hugepage_compiler_directed_telemetry_enable,
    lifetime_hugepage_compiler_directed_telemetry_enabled, lifetime_hugepage_configure,
    lifetime_hugepage_configure_with_backend, lifetime_hugepage_phase_flush_current_thread,
    lifetime_hugepage_stats_reset, lifetime_hugepage_stats_snapshot,
    lifetime_hugepage_trim_retained_empty_extents, type_isolation_side_cache_snapshot,
    with_semantic_metadata, AllocationMetadata, LifetimeAdaptiveSiteSnapshot,
    LifetimeHugepagePolicy, LifetimeHugepageStatsSnapshot, LifetimePageBackend, SemanticAlloc,
    UniAlloc, FLAG_DELAYED_FREE, LIFETIME_HINT_BOUNDED_PROCESS_LONG,
    LIFETIME_HINT_DYNAMIC_BUFFER_OBSERVE, LIFETIME_HINT_EPHEMERAL, LIFETIME_HINT_LOCAL_DROP_FACT,
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

fn adaptive_metadata(type_id: u64, callsite: u64, hint: u16) -> AllocationMetadata {
    metadata(type_id, hint).with_callsite(callsite)
}

fn flags_zero_scoped_alloc(
    alloc: &UniAlloc,
    layout: Layout,
    metadata: AllocationMetadata,
    expected_slow_path: bool,
) -> *mut u8 {
    assert_eq!(metadata.flags, 0);
    __unialloc_semantic_scope_push_hints(
        metadata.type_id,
        metadata.module_id,
        metadata.flags,
        metadata.lifetime_hint,
        metadata.placement_hint,
        metadata.callsite,
    );
    assert_eq!(semantic_allocation_slow_path_enabled(), expected_slow_path);
    let ptr = unsafe { GlobalAlloc::alloc(alloc, layout) };
    assert!(!ptr.is_null());
    assert_eq!(take_auto_deallocation_metadata(ptr, layout), None);
    __unialloc_semantic_scope_pop();
    ptr
}

fn assert_runtime_validation_closes(snapshot: LifetimeHugepageStatsSnapshot) {
    assert_eq!(
        snapshot.runtime_validated_objects,
        snapshot.predictor_true_positive_objects
            + snapshot.predictor_true_negative_objects
            + snapshot.predictor_false_positive_objects
            + snapshot.predictor_false_negative_objects
    );
    assert_eq!(
        snapshot.runtime_validated_bytes,
        snapshot.predictor_true_positive_bytes
            + snapshot.predictor_true_negative_bytes
            + snapshot.predictor_false_positive_bytes
            + snapshot.predictor_false_negative_bytes
    );
    assert_eq!(
        snapshot.adaptive_short_observations + snapshot.adaptive_long_observations,
        snapshot.adaptive_static_hint_true_positive_objects
            + snapshot.adaptive_static_hint_true_negative_objects
            + snapshot.adaptive_static_hint_false_positive_objects
            + snapshot.adaptive_static_hint_false_negative_objects
            + snapshot.adaptive_static_hint_abstained_objects
    );
    assert_eq!(
        snapshot.adaptive_decisive_observation_bytes,
        snapshot.adaptive_static_hint_true_positive_bytes
            + snapshot.adaptive_static_hint_true_negative_bytes
            + snapshot.adaptive_static_hint_false_positive_bytes
            + snapshot.adaptive_static_hint_false_negative_bytes
            + snapshot.adaptive_static_hint_abstained_bytes
    );
    assert_eq!(
        snapshot.adaptive_training_allocations
            + snapshot.adaptive_short_routed_allocations
            + snapshot.adaptive_long_routed_allocations,
        snapshot.adaptive_short_observations
            + snapshot.adaptive_long_observations
            + snapshot.adaptive_censored_observations
            + snapshot.adaptive_trailer_corruptions
            + snapshot.adaptive_live_trailers
    );
}

fn assert_compiler_directed_runtime_state_is_idle(snapshot: LifetimeHugepageStatsSnapshot) {
    assert_eq!(snapshot.runtime_validated_objects, 0);
    assert_eq!(snapshot.runtime_validated_bytes, 0);
    assert_eq!(snapshot.adaptive_pressure_bytes, 0);
    assert_eq!(snapshot.adaptive_epoch_advances, 0);
    assert_eq!(snapshot.adaptive_site_count, 0);
    assert_eq!(snapshot.adaptive_eligible_allocations, 0);
    assert_eq!(snapshot.adaptive_training_allocations, 0);
    assert_eq!(snapshot.adaptive_observation_site_count, 0);
    assert_eq!(snapshot.adaptive_observation_table_bypasses, 0);
    assert_eq!(snapshot.adaptive_live_trailers, 0);
    assert_eq!(snapshot.adaptive_live_survival_registrations, 0);
    assert_eq!(snapshot.adaptive_live_survival_scans, 0);
    assert_eq!(snapshot.adaptive_short_observations, 0);
    assert_eq!(snapshot.adaptive_long_observations, 0);
    assert_eq!(snapshot.adaptive_censored_observations, 0);
}

#[test]
fn enabled_adaptive_policy_observes_flags_zero_compiler_scopes() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(4096, 64).unwrap();
    let long_metadata = AllocationMetadata::for_type(0xA11C_0000)
        .with_module(0xC0DE_0000)
        .with_flags(0)
        .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED)
        .with_callsite(0xCA11_0000);
    let unknown_metadata = AllocationMetadata::for_type(0xA11C_0001)
        .with_module(0xC0DE_0000)
        .with_flags(0)
        .with_callsite(0xCA11_0001);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_adaptive_site_recording_enable());
    assert!(lifetime_hugepage_adaptive_site_force_track_all_enable(
        16,
        16 * layout.size(),
    ));
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary
    ));
    let cache_before = type_isolation_side_cache_snapshot();

    let long_ptr = flags_zero_scoped_alloc(&alloc, layout, long_metadata, true);
    let unknown_ptr = flags_zero_scoped_alloc(&alloc, layout, unknown_metadata, true);

    let routed = lifetime_hugepage_stats_snapshot();
    assert_eq!(routed.routed_allocations, 2);
    assert_eq!(routed.adaptive_eligible_allocations, 2);
    assert_eq!(routed.adaptive_force_track_all_admitted_allocations, 2);
    assert_eq!(
        routed.adaptive_force_track_all_admitted_requested_bytes,
        2 * layout.size()
    );

    let mut rows = [LifetimeAdaptiveSiteSnapshot::empty(); 4];
    assert_eq!(lifetime_hugepage_adaptive_site_snapshot(&mut rows), 2);
    assert!(rows.iter().take(2).any(|row| {
        row.callsite == long_metadata.callsite
            && row.type_id == long_metadata.type_id
            && row.requested_size == layout.size()
    }));
    assert!(rows.iter().take(2).any(|row| {
        row.callsite == unknown_metadata.callsite
            && row.type_id == unknown_metadata.type_id
            && row.requested_size == layout.size()
    }));

    let resized = Layout::from_size_align(layout.size() - 16, layout.align()).unwrap();
    let long_ptr = unsafe { GlobalAlloc::realloc(&alloc, long_ptr, layout, resized.size()) };
    assert!(!long_ptr.is_null());
    let long_address = long_ptr as usize;
    std::thread::spawn(move || unsafe {
        GlobalAlloc::dealloc(&UniAlloc::new(), long_address as *mut u8, resized);
    })
    .join()
    .unwrap();
    unsafe {
        GlobalAlloc::dealloc(&alloc, unknown_ptr, layout);
    }
    let released = lifetime_hugepage_stats_snapshot();
    assert_eq!(released.live_objects, 0);
    assert_eq!(released.current_retained_empty_extents, 1);
    assert!(!released.all_mappings_released);
    assert!(lifetime_hugepage_trim_retained_empty_extents());
    assert!(lifetime_hugepage_stats_snapshot().all_mappings_released);
    assert_eq!(type_isolation_side_cache_snapshot(), cache_before);

    assert!(lifetime_hugepage_adaptive_site_force_track_all_disable());
    assert!(lifetime_hugepage_adaptive_site_recording_disable());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn flags_zero_lifetime_scope_admission_is_policy_specific_and_recovery_free() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(2048, 64).unwrap();
    let exact = AllocationMetadata::for_type(0xA11C_0100)
        .with_module(0xC0DE_0100)
        .with_flags(0)
        .with_callsite(0xCA11_0100);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
    let cache_before = type_isolation_side_cache_snapshot();
    let disabled_ptr = flags_zero_scoped_alloc(
        &alloc,
        layout,
        exact.with_lifetime_hint(LIFETIME_HINT_LONG_LIVED),
        false,
    );
    unsafe { GlobalAlloc::dealloc(&alloc, disabled_ptr, layout) };
    assert_eq!(type_isolation_side_cache_snapshot(), cache_before);

    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::CompilerInferredOrdinary
    ));
    for hint in [0, LIFETIME_HINT_LONG_LIVED, LIFETIME_HINT_LOCAL_DROP_FACT] {
        let ptr = flags_zero_scoped_alloc(&alloc, layout, exact.with_lifetime_hint(hint), false);
        unsafe { GlobalAlloc::dealloc(&alloc, ptr, layout) };
    }
    assert_eq!(lifetime_hugepage_stats_snapshot().routed_allocations, 0);

    let bounded_ptr = flags_zero_scoped_alloc(
        &alloc,
        layout,
        exact.with_lifetime_hint(LIFETIME_HINT_BOUNDED_PROCESS_LONG),
        true,
    );
    unsafe { GlobalAlloc::dealloc(&alloc, bounded_ptr, layout) };
    let compiler = lifetime_hugepage_stats_snapshot();
    assert_eq!(compiler.compiler_inferred_direct_long_routes, 1);
    assert_eq!(compiler.live_objects, 0);
    assert_eq!(compiler.current_retained_empty_extents, 1);
    assert!(!compiler.all_mappings_released);
    assert_eq!(type_isolation_side_cache_snapshot(), cache_before);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::SegregatedOrdinary
    ));
    for hint in [0, LIFETIME_HINT_LOCAL_DROP_FACT] {
        let ptr = flags_zero_scoped_alloc(&alloc, layout, exact.with_lifetime_hint(hint), false);
        unsafe { GlobalAlloc::dealloc(&alloc, ptr, layout) };
    }
    for hint in [LIFETIME_HINT_EPHEMERAL, LIFETIME_HINT_LONG_LIVED] {
        let ptr = flags_zero_scoped_alloc(&alloc, layout, exact.with_lifetime_hint(hint), true);
        unsafe { GlobalAlloc::dealloc(&alloc, ptr, layout) };
    }
    let legacy = lifetime_hugepage_stats_snapshot();
    assert_eq!(legacy.routed_allocations, 2);
    assert_eq!(legacy.live_objects, 0);
    assert!(legacy.all_mappings_released);
    assert_eq!(type_isolation_side_cache_snapshot(), cache_before);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn compiler_directed_policies_route_only_medium_long_without_runtime_measurement() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(4096, 64).unwrap();
    let base = AllocationMetadata::for_type(0xA11C_0180)
        .with_module(0xC0DE_0180)
        .with_flags(0)
        .with_callsite(0xCA11_0180);

    for policy in [
        LifetimeHugepagePolicy::CompilerDirectedHugepage,
        LifetimeHugepagePolicy::CompilerDirectedOrdinary,
    ] {
        assert!(lifetime_hugepage_configure(policy));
        assert!(lifetime_hugepage_compiler_directed_telemetry_enable());
        assert!(lifetime_hugepage_compiler_directed_telemetry_enabled());
        assert!(lifetime_hugepage_stats_reset());
        let cache_before = type_isolation_side_cache_snapshot();

        // Unknown and observation-only metadata use the base allocator before
        // ARENA is acquired. Explicit SemanticAlloc calls make that admission
        // decision observable without enabling compiler-scope recovery state.
        for (offset, hint) in [
            (1u64, 0u16),
            (2u64, LIFETIME_HINT_LOCAL_DROP_FACT),
            (3u64, LIFETIME_HINT_BOUNDED_PROCESS_LONG),
            (4u64, LIFETIME_HINT_DYNAMIC_BUFFER_OBSERVE),
            (5u64, 0x7eed),
        ] {
            let unknown = AllocationMetadata {
                type_id: base.type_id + offset,
                ..base
            }
            .with_callsite(base.callsite + offset)
            .with_lifetime_hint(hint);
            let ptr = unsafe { alloc.alloc_with_metadata(layout, unknown) };
            assert!(!ptr.is_null());
            assert_eq!(take_auto_deallocation_metadata(ptr, layout), None);
            unsafe { alloc.dealloc_with_metadata(ptr, layout, unknown) };
        }

        let short = base.with_lifetime_hint(LIFETIME_HINT_EPHEMERAL);
        let long = AllocationMetadata {
            type_id: base.type_id + 6,
            ..base
        }
        .with_callsite(base.callsite + 6)
        .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);
        let short_ptr = flags_zero_scoped_alloc(&alloc, layout, short, false);
        let long_ptr = flags_zero_scoped_alloc(&alloc, layout, long, true);

        let routed = lifetime_hugepage_stats_snapshot();
        assert_eq!(routed.policy, policy);
        assert!(routed.compiler_directed_telemetry);
        assert_eq!(routed.routed_allocations, 1);
        assert_eq!(routed.live_ephemeral_objects, 0);
        assert_eq!(routed.live_long_lived_objects, 1);
        assert_eq!(routed.compiler_directed_unknown_bypasses, 5);
        assert_eq!(
            routed.compiler_directed_bypass_requested_bytes,
            5 * layout.size()
        );
        assert_eq!(routed.compiler_directed_arena_lock_acquisitions, 1);
        assert_eq!(routed.compiler_directed_short_routes, 0);
        assert_eq!(routed.compiler_directed_short_requested_bytes, 0);
        assert_eq!(routed.compiler_directed_short_slot_bytes, 0);
        assert_eq!(routed.compiler_directed_long_routes, 1);
        assert_eq!(routed.compiler_directed_long_requested_bytes, layout.size());
        assert_eq!(routed.compiler_directed_long_slot_bytes, layout.size());
        assert_eq!(routed.compiler_directed_routes_without_trailer, 1);
        assert_eq!(routed.compiler_directed_deallocations, 0);
        assert_compiler_directed_runtime_state_is_idle(routed);

        match policy {
            LifetimeHugepagePolicy::CompilerDirectedHugepage => {
                assert_eq!(routed.current_ordinary_extents, 0);
                assert_eq!(routed.current_thp_extents, 1);
                assert_eq!(routed.thp_candidate_extent_mappings, 1);
                assert_eq!(routed.compiler_directed_density_promotion_attempts, 0);
            }
            LifetimeHugepagePolicy::CompilerDirectedOrdinary => {
                assert_eq!(routed.current_ordinary_extents, 1);
                assert_eq!(routed.current_thp_extents, 0);
                assert_eq!(routed.thp_candidate_extent_mappings, 0);
            }
            _ => unreachable!(),
        }

        let resized = Layout::from_size_align(layout.size() + 16, layout.align()).unwrap();
        let long_ptr = unsafe { GlobalAlloc::realloc(&alloc, long_ptr, layout, resized.size()) };
        assert!(!long_ptr.is_null());
        assert_eq!(take_auto_deallocation_metadata(long_ptr, resized), None);
        let long_address = long_ptr as usize;
        std::thread::spawn(move || unsafe {
            GlobalAlloc::dealloc(&UniAlloc::new(), long_address as *mut u8, resized);
        })
        .join()
        .unwrap();
        unsafe { GlobalAlloc::dealloc(&alloc, short_ptr, layout) };
        let released = lifetime_hugepage_stats_snapshot();
        assert_eq!(released.routed_deallocations, 1);
        assert_eq!(released.compiler_directed_deallocations, 1);
        assert_compiler_directed_runtime_state_is_idle(released);
        assert_eq!(type_isolation_side_cache_snapshot(), cache_before);
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(!lifetime_hugepage_compiler_directed_telemetry_enabled());
    }
}

#[test]
fn compiler_directed_telemetry_is_default_off_on_the_production_path() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(4096, 32).unwrap();
    let base = AllocationMetadata::for_type(0xA11C_0190)
        .with_module(0xC0DE_0190)
        .with_flags(0)
        .with_callsite(0xCA11_0190);

    for policy in [
        LifetimeHugepagePolicy::CompilerDirectedHugepage,
        LifetimeHugepagePolicy::CompilerDirectedOrdinary,
    ] {
        assert!(lifetime_hugepage_configure(policy));
        assert!(!lifetime_hugepage_compiler_directed_telemetry_enabled());
        assert!(lifetime_hugepage_stats_reset());

        let unknown = base.with_lifetime_hint(0);
        let unknown_ptr = unsafe { alloc.alloc_with_metadata(layout, unknown) };
        assert!(!unknown_ptr.is_null());
        unsafe { alloc.dealloc_with_metadata(unknown_ptr, layout, unknown) };

        let short_ptr = flags_zero_scoped_alloc(
            &alloc,
            layout,
            base.with_lifetime_hint(LIFETIME_HINT_EPHEMERAL),
            false,
        );
        let long_ptr = flags_zero_scoped_alloc(
            &alloc,
            layout,
            AllocationMetadata {
                type_id: base.type_id + 1,
                ..base
            }
            .with_callsite(base.callsite + 1)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED),
            true,
        );
        unsafe {
            GlobalAlloc::dealloc(&alloc, short_ptr, layout);
            GlobalAlloc::dealloc(&alloc, long_ptr, layout);
        }

        let snapshot = lifetime_hugepage_stats_snapshot();
        assert_eq!(snapshot.routed_allocations, 1);
        assert!(!snapshot.compiler_directed_telemetry);
        assert_eq!(snapshot.compiler_directed_unknown_bypasses, 0);
        assert_eq!(snapshot.compiler_directed_arena_lock_acquisitions, 0);
        assert_eq!(snapshot.compiler_directed_short_routes, 0);
        assert_eq!(snapshot.compiler_directed_long_routes, 0);
        assert_eq!(snapshot.compiler_directed_deallocations, 0);
        assert_eq!(snapshot.compiler_directed_routes_without_trailer, 0);
        assert_compiler_directed_runtime_state_is_idle(snapshot);
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
    }
}

#[cfg(target_os = "linux")]
#[test]
fn compiler_directed_thp_collapses_only_after_the_extent_is_full() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(4096, 64).unwrap();
    let slots_per_extent = LIFETIME_HUGEPAGE_EXTENT_BYTES / layout.size();
    let metadata = AllocationMetadata::for_type(0xA11C_0198)
        .with_module(0xC0DE_0198)
        .with_flags(0)
        .with_callsite(0xCA11_0198)
        .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);

    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::CompilerDirectedHugepage
    ));
    assert!(lifetime_hugepage_compiler_directed_telemetry_enable());
    assert!(lifetime_hugepage_stats_reset());

    let mut pointers = Vec::with_capacity(slots_per_extent);
    for _ in 0..(slots_per_extent - 1) {
        pointers.push(flags_zero_scoped_alloc(&alloc, layout, metadata, true));
    }
    let before_full = lifetime_hugepage_stats_snapshot();
    assert_eq!(before_full.current_extents, 1);
    assert_eq!(before_full.thp_advice_attempts, 0);
    assert_eq!(before_full.thp_collapse_attempts, 0);
    assert_eq!(before_full.compiler_directed_density_promotion_attempts, 0);

    pointers.push(flags_zero_scoped_alloc(&alloc, layout, metadata, true));
    let promoted_extent_base = pointers[0] as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
    let full = lifetime_hugepage_stats_snapshot();
    assert_eq!(full.current_extents, 1);
    assert_eq!(full.thp_advice_attempts, 1);
    assert_eq!(full.thp_advice_successes + full.thp_advice_errors, 1);
    if full.thp_advice_successes == 1 {
        assert_eq!(full.thp_collapse_attempts, 1);
        assert_eq!(full.thp_collapse_successes + full.thp_collapse_errors, 1);
    }
    assert_eq!(full.compiler_directed_density_promotion_attempts, 1);
    assert_eq!(
        full.compiler_directed_density_promotion_successes
            + full.compiler_directed_density_promotion_errors,
        1
    );

    for ptr in pointers.drain(..) {
        unsafe { GlobalAlloc::dealloc(&alloc, ptr, layout) };
    }
    if full.thp_collapse_successes == 1 {
        let retained = lifetime_hugepage_stats_snapshot();
        assert_eq!(retained.current_extents, 1);
        assert_eq!(retained.current_retained_empty_extents, 1);
        assert_eq!(retained.extent_unmaps, 0);

        let other_site = metadata.with_callsite(metadata.callsite.wrapping_add(1));
        let other_ptr = flags_zero_scoped_alloc(&alloc, layout, other_site, true);
        let other_extent_base = other_ptr as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
        assert_ne!(other_extent_base, promoted_extent_base);
        let isolated = lifetime_hugepage_stats_snapshot();
        assert_eq!(isolated.current_extents, 2);
        assert_eq!(isolated.thp_extent_mappings, 2);
        assert_eq!(isolated.thp_collapse_attempts, 1);
        unsafe { GlobalAlloc::dealloc(&alloc, other_ptr, layout) };

        for _ in 0..slots_per_extent {
            pointers.push(flags_zero_scoped_alloc(&alloc, layout, metadata, true));
        }
        assert!(pointers.iter().all(|ptr| {
            *ptr as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1) == promoted_extent_base
        }));
        let reused_full = lifetime_hugepage_stats_snapshot();
        assert_eq!(reused_full.thp_extent_mappings, 2);
        assert_eq!(reused_full.thp_collapse_attempts, 1);
        assert_eq!(reused_full.retained_empty_extent_reuse_hits, 1);
        for ptr in pointers.drain(..) {
            unsafe { GlobalAlloc::dealloc(&alloc, ptr, layout) };
        }

        let sparse_ptr = flags_zero_scoped_alloc(&alloc, layout, metadata, true);
        assert_eq!(
            sparse_ptr as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1),
            promoted_extent_base
        );
        unsafe { GlobalAlloc::dealloc(&alloc, sparse_ptr, layout) };
        let sparse_evicted = lifetime_hugepage_stats_snapshot();
        assert_eq!(sparse_evicted.retained_empty_extent_reuse_hits, 2);
        assert_eq!(sparse_evicted.current_extents, 1);
        assert_eq!(sparse_evicted.current_retained_empty_extents, 1);
        assert_eq!(sparse_evicted.extent_unmaps, 1);
    }
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
    assert!(lifetime_hugepage_stats_reset());
}

#[cfg(target_os = "linux")]
#[test]
fn compiler_directed_thp_requires_one_exact_compiler_cohort() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(16 * 1024, 64).unwrap();
    let regions_per_extent =
        LIFETIME_HUGEPAGE_EXTENT_BYTES / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
    let slots_per_region = LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES / layout.size();
    let base = AllocationMetadata::for_type(0xA11C_019B)
        .with_module(0xC0DE_019B)
        .with_flags(0)
        .with_callsite(0xCA11_019B)
        .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);

    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::CompilerDirectedHugepage
    ));
    assert!(lifetime_hugepage_compiler_directed_telemetry_enable());
    assert!(lifetime_hugepage_stats_reset());
    let mut pointers = Vec::with_capacity(regions_per_extent * slots_per_region);
    for site_offset in 0..regions_per_extent as u64 {
        let site = base.with_callsite(base.callsite + site_offset);
        for _ in 0..slots_per_region {
            pointers.push(flags_zero_scoped_alloc(&alloc, layout, site, true));
        }
    }
    let mixed = lifetime_hugepage_stats_snapshot();
    assert_eq!(mixed.current_extents, 1);
    assert_eq!(
        mixed.routed_allocations,
        regions_per_extent * slots_per_region
    );
    assert_eq!(mixed.thp_advice_attempts, 0);
    assert_eq!(mixed.thp_collapse_attempts, 0);
    assert_eq!(mixed.compiler_directed_density_promotion_attempts, 0);

    for ptr in pointers {
        unsafe { GlobalAlloc::dealloc(&alloc, ptr, layout) };
    }
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
    assert!(lifetime_hugepage_stats_reset());
}

#[test]
fn adaptive_flags_zero_scope_requires_nonzero_callsite_and_type() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(1024, 32).unwrap();
    let invalid = [
        AllocationMetadata::for_type(0xA11C_0200)
            .with_module(0xC0DE_0200)
            .with_flags(0)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED),
        AllocationMetadata::for_type(0)
            .with_module(0xC0DE_0200)
            .with_flags(0)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED)
            .with_callsite(0xCA11_0200),
    ];

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary
    ));
    let cache_before = type_isolation_side_cache_snapshot();
    for metadata in invalid {
        let ptr = flags_zero_scoped_alloc(&alloc, layout, metadata, false);
        unsafe { GlobalAlloc::dealloc(&alloc, ptr, layout) };
    }
    let snapshot = lifetime_hugepage_stats_snapshot();
    assert_eq!(snapshot.routed_allocations, 0);
    assert_eq!(snapshot.adaptive_eligible_allocations, 0);
    assert_eq!(snapshot.current_extents, 0);
    assert_eq!(type_isolation_side_cache_snapshot(), cache_before);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
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
    assert_eq!(lifetime_hugepage_advance_epoch(), 2);

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
    assert_eq!(released.predictor_true_positive_objects, 1);
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
fn matching_identity_reuses_its_region_before_assigning_another() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(4096, 64).unwrap();
    let first_type = metadata(0xA11C_0032, LIFETIME_HINT_LONG_LIVED);
    let second_type = metadata(0xA11C_0033, LIFETIME_HINT_LONG_LIVED);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::SegregatedOrdinary
    ));

    unsafe {
        let first = alloc.alloc_with_metadata(layout, first_type);
        let sibling = alloc.alloc_with_metadata(layout, second_type);
        let first_again = alloc.alloc_with_metadata(layout, first_type);
        assert!(!first.is_null() && !sibling.is_null() && !first_again.is_null());

        let extent = (first as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
        let region =
            |ptr: *mut u8| ((ptr as usize) - extent) / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
        assert_eq!(region(first_again), region(first));
        assert_ne!(region(sibling), region(first));

        let active = lifetime_hugepage_stats_snapshot();
        assert_eq!(active.current_extents, 1);
        assert_eq!(active.current_identity_regions, 2);
        assert_eq!(active.identity_region_assignments, 2);

        alloc.dealloc_with_metadata(first, layout, first_type);
        alloc.dealloc_with_metadata(sibling, layout, second_type);
        alloc.dealloc_with_metadata(first_again, layout, first_type);
    }

    let released = lifetime_hugepage_stats_snapshot();
    assert_eq!(released.identity_region_assignments, 2);
    assert_eq!(released.identity_region_releases, 2);
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

#[test]
fn runtime_epoch_oracle_reports_predictor_and_placement_confusion() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(4096, 64).unwrap();
    let predicted_long_actual_long = metadata(0xA11C_3000, LIFETIME_HINT_LONG_LIVED);
    let predicted_short_actual_short = metadata(0xA11C_3001, LIFETIME_HINT_EPHEMERAL);
    let predicted_long_actual_short = metadata(0xA11C_3002, LIFETIME_HINT_LONG_LIVED);
    let predicted_short_actual_long = metadata(0xA11C_3003, LIFETIME_HINT_EPHEMERAL);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::LongLivedHugepage
    ));

    unsafe {
        let tp = alloc.alloc_with_metadata(layout, predicted_long_actual_long);
        let tn = alloc.alloc_with_metadata(layout, predicted_short_actual_short);
        let fp = alloc.alloc_with_metadata(layout, predicted_long_actual_short);
        let fn_ptr = alloc.alloc_with_metadata(layout, predicted_short_actual_long);
        assert!(!tp.is_null() && !tn.is_null() && !fp.is_null() && !fn_ptr.is_null());

        alloc.dealloc_with_metadata(tn, layout, predicted_short_actual_short);
        alloc.dealloc_with_metadata(fp, layout, predicted_long_actual_short);
        let same_epoch = lifetime_hugepage_stats_snapshot();
        assert_eq!(same_epoch.runtime_validated_objects, 2);
        assert_eq!(same_epoch.predictor_true_negative_objects, 1);
        assert_eq!(same_epoch.predictor_false_positive_objects, 1);
        assert_eq!(same_epoch.predictor_true_positive_objects, 0);
        assert_eq!(same_epoch.predictor_false_negative_objects, 0);

        assert_eq!(lifetime_hugepage_advance_epoch(), 2);
        assert_eq!((tp as *const u8).read(), 0);
        assert_eq!((fn_ptr as *const u8).read(), 0);
        alloc.dealloc_with_metadata(tp, layout, predicted_long_actual_long);
        alloc.dealloc_with_metadata(fn_ptr, layout, predicted_short_actual_long);
    }

    let final_stats = lifetime_hugepage_stats_snapshot();
    assert_eq!(final_stats.runtime_validated_objects, 4);
    assert_eq!(final_stats.runtime_validated_bytes, 4 * layout.size());
    assert_eq!(final_stats.predictor_true_positive_objects, 1);
    assert_eq!(final_stats.predictor_true_negative_objects, 1);
    assert_eq!(final_stats.predictor_false_positive_objects, 1);
    assert_eq!(final_stats.predictor_false_negative_objects, 1);
    if final_stats.hugetlb_extent_mappings != 0 {
        assert_eq!(final_stats.placement_true_positive_objects, 1);
        assert_eq!(final_stats.placement_true_negative_objects, 1);
        assert_eq!(final_stats.placement_false_positive_objects, 1);
        assert_eq!(final_stats.placement_false_negative_objects, 1);
    } else {
        assert_eq!(final_stats.placement_true_positive_objects, 0);
        assert_eq!(final_stats.placement_true_negative_objects, 2);
        assert_eq!(final_stats.placement_false_positive_objects, 0);
        assert_eq!(final_stats.placement_false_negative_objects, 2);
    }
    assert_eq!(final_stats.phase_advances, 1);
    assert!(final_stats.all_mappings_released);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn epoch_cohort_policy_separates_live_extents_without_early_reclamation() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(4096, 64).unwrap();
    let long = metadata(0xA11C_3010, LIFETIME_HINT_LONG_LIVED);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::EpochCohortHugepage
    ));

    unsafe {
        let first_epoch = alloc.alloc_with_metadata(layout, long);
        assert!(!first_epoch.is_null());
        first_epoch.write(0x31);
        assert_eq!(lifetime_hugepage_advance_epoch(), 2);
        let second_epoch = alloc.alloc_with_metadata(layout, long);
        assert!(!second_epoch.is_null());
        second_epoch.write(0x32);

        let mixed_cohort_snapshot = lifetime_hugepage_stats_snapshot();
        let empty_region_bytes = 31 * unialloc::LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
        assert_eq!(
            mixed_cohort_snapshot.cohort_pinned_unassigned_region_bytes,
            empty_region_bytes
        );
        assert_eq!(
            mixed_cohort_snapshot.reusable_unassigned_region_bytes,
            empty_region_bytes
        );

        let first_extent = (first_epoch as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
        let second_extent = (second_epoch as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
        assert_ne!(first_extent, second_extent);

        alloc.dealloc_with_metadata(first_epoch, layout, long);
        let after_first = lifetime_hugepage_stats_snapshot();
        assert_eq!(after_first.current_extents, 1);
        assert_eq!(second_epoch.read(), 0x32);
        assert_eq!(after_first.predictor_true_positive_objects, 1);

        alloc.dealloc_with_metadata(second_epoch, layout, long);
    }

    let released = lifetime_hugepage_stats_snapshot();
    assert_eq!(released.epoch_cohort_extent_mappings, 2);
    assert_eq!(released.predictor_false_positive_objects, 1);
    assert!(released.all_mappings_released);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn unknown_and_delayed_objects_keep_runtime_validation_coverage_honest() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(1024, 16).unwrap();
    let classified = metadata(0xA11C_3020, LIFETIME_HINT_EPHEMERAL);
    let delayed = metadata(0xA11C_3021, LIFETIME_HINT_EPHEMERAL).with_flags(FLAG_DELAYED_FREE);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::SegregatedOrdinary
    ));

    unsafe {
        let unknown = alloc.alloc_with_metadata(layout, AllocationMetadata::unknown());
        let known = alloc.alloc_with_metadata(layout, classified);
        let retained = alloc.alloc_with_metadata(layout, delayed);
        assert!(!unknown.is_null() && !known.is_null() && !retained.is_null());
        alloc.dealloc_with_metadata(unknown, layout, AllocationMetadata::unknown());
        alloc.dealloc_with_metadata(known, layout, classified);
        alloc.dealloc_with_metadata(retained, layout, delayed);
    }
    let retained_count = delayed_free_snapshot().occupied_slots;
    assert!(retained_count > 0);
    assert_eq!(
        lifetime_hugepage_phase_flush_current_thread(),
        retained_count
    );

    let snapshot = lifetime_hugepage_stats_snapshot();
    assert_eq!(snapshot.unknown_bypasses, 1);
    assert_eq!(snapshot.unknown_bypass_requested_bytes, layout.size());
    assert_eq!(snapshot.runtime_validated_objects, 1);
    assert_eq!(snapshot.runtime_validation_excluded_objects, 1);
    assert_eq!(snapshot.runtime_delayed_free_excluded_objects, 1);
    assert_eq!(snapshot.runtime_mixed_epoch_excluded_objects, 0);
    assert_eq!(snapshot.predictor_true_negative_objects, 1);
    assert!(snapshot.all_mappings_released);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn runtime_oracle_preserves_non_cohort_region_reuse_and_excludes_mixed_epochs() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(1024, 16).unwrap();
    let long = metadata(0xA11C_3030, LIFETIME_HINT_LONG_LIVED);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::LongLivedHugepage
    ));

    unsafe {
        let first_epoch = alloc.alloc_with_metadata(layout, long);
        assert!(!first_epoch.is_null());
        assert_eq!(lifetime_hugepage_advance_epoch(), 2);
        let second_epoch = alloc.alloc_with_metadata(layout, long);
        assert!(!second_epoch.is_null());

        let first_region = (first_epoch as usize) & !(LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES - 1);
        let second_region =
            (second_epoch as usize) & !(LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES - 1);
        assert_eq!(first_region, second_region);
        assert_eq!(
            lifetime_hugepage_stats_snapshot().current_identity_regions,
            1
        );

        alloc.dealloc_with_metadata(first_epoch, layout, long);
        alloc.dealloc_with_metadata(second_epoch, layout, long);
    }

    let released = lifetime_hugepage_stats_snapshot();
    assert_eq!(released.runtime_validated_objects, 0);
    assert_eq!(released.runtime_validation_excluded_objects, 2);
    assert_eq!(released.runtime_delayed_free_excluded_objects, 0);
    assert_eq!(released.runtime_mixed_epoch_excluded_objects, 2);
    assert!(released.all_mappings_released);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[cfg(target_os = "linux")]
#[test]
fn thp_backend_counts_advice_separately_from_epoch_confirmed_collapse() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(4096, 64).unwrap();
    let eager_long = metadata(0xA11C_4000, LIFETIME_HINT_LONG_LIVED);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::LongLivedHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));

    unsafe {
        let ptr = alloc.alloc_with_metadata(layout, eager_long);
        assert!(!ptr.is_null());
        ptr.write(0x41);

        let eager = lifetime_hugepage_stats_snapshot();
        assert_eq!(eager.backend, LifetimePageBackend::TransparentHugepage);
        assert_eq!(eager.current_extents, 1);
        assert_eq!(eager.current_thp_extents, 1);
        assert_eq!(eager.current_ordinary_extents, 0);
        assert_eq!(eager.current_hugetlb_extents, 0);
        assert_eq!(eager.thp_extent_mappings, 1);
        assert_eq!(eager.thp_candidate_extent_mappings, 0);
        assert_eq!(eager.thp_advice_attempts, 1);
        assert_eq!(
            eager.thp_advice_successes + eager.thp_advice_errors,
            eager.thp_advice_attempts
        );
        assert_eq!(eager.thp_collapse_attempts, 0);
        assert_eq!(eager.current_thp_collapse_confirmed_extents, 0);

        alloc.dealloc_with_metadata(ptr, layout, eager_long);
    }

    let eager_released = lifetime_hugepage_stats_snapshot();
    if eager_released.thp_advice_successes == 1 {
        assert_eq!(eager_released.current_extents, 1);
        assert_eq!(eager_released.current_thp_extents, 1);
        assert_eq!(eager_released.current_retained_empty_extents, 1);
        assert_eq!(eager_released.extent_unmaps, 0);
        assert!(!eager_released.all_mappings_released);
    } else {
        assert_eq!(eager_released.current_extents, 0);
        assert_eq!(eager_released.current_thp_extents, 0);
        assert_eq!(eager_released.extent_unmaps, 1);
        assert!(eager_released.all_mappings_released);
    }
    // Advice success is VMA intent. Without collapse/smaps evidence the
    // allocator conservatively records this same-epoch release as ordinary.
    assert_eq!(eager_released.placement_true_negative_objects, 1);
    assert_eq!(eager_released.placement_false_positive_objects, 0);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::EpochCohortHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));

    const OBJECTS_PER_EXTENT: usize = LIFETIME_HUGEPAGE_EXTENT_BYTES / 4096;
    let epoch_long = metadata(0xA11C_4001, LIFETIME_HINT_LONG_LIVED);
    let mut ptrs = Vec::with_capacity(OBJECTS_PER_EXTENT);
    unsafe {
        for index in 0..OBJECTS_PER_EXTENT {
            let ptr = alloc.alloc_with_metadata(layout, epoch_long);
            assert!(!ptr.is_null());
            // Initializing every routed object gives MADV_COLLAPSE a normal
            // resident workload without adding allocator-side prefaulting.
            ptr.write((index & 0xff) as u8);
            ptrs.push(ptr);
        }
    }

    let candidate = lifetime_hugepage_stats_snapshot();
    assert_eq!(candidate.current_extents, 1);
    assert_eq!(candidate.current_thp_extents, 1);
    assert_eq!(candidate.current_ordinary_extents, 0);
    assert_eq!(candidate.current_hugetlb_extents, 0);
    assert_eq!(candidate.thp_extent_mappings, 1);
    assert_eq!(candidate.thp_candidate_extent_mappings, 1);
    assert_eq!(candidate.thp_advice_attempts, 0);
    assert_eq!(candidate.thp_collapse_attempts, 0);
    assert_eq!(candidate.current_thp_collapse_confirmed_extents, 0);

    assert_eq!(lifetime_hugepage_advance_epoch(), 2);
    let promoted = lifetime_hugepage_stats_snapshot();
    assert_eq!(promoted.thp_collapse_eligible_extents, 1);
    assert_eq!(promoted.thp_collapse_low_occupancy_skips, 0);
    assert_eq!(promoted.thp_advice_attempts, 1);
    assert_eq!(
        promoted.thp_advice_successes + promoted.thp_advice_errors,
        promoted.thp_advice_attempts
    );
    if promoted.thp_advice_successes == 1 {
        assert_eq!(promoted.thp_collapse_attempts, 1);
        assert_eq!(
            promoted.thp_collapse_successes + promoted.thp_collapse_errors,
            promoted.thp_collapse_attempts,
            "MADV_COLLAPSE must end in either point-in-time success or an explicit error count"
        );
        if promoted.thp_collapse_errors == 1 {
            assert_ne!(
                promoted.thp_collapse_last_error_code, 0,
                "a collapse error must retain its Linux errno"
            );
        } else {
            assert_eq!(promoted.thp_collapse_last_error_code, 0);
        }
    } else {
        assert_eq!(promoted.thp_advice_errors, 1);
        assert_eq!(promoted.thp_collapse_attempts, 0);
    }
    assert_eq!(
        promoted.current_thp_collapse_confirmed_extents,
        promoted.thp_collapse_successes
    );

    unsafe {
        assert_eq!(ptrs[0].read(), 0);
        assert_eq!(ptrs[OBJECTS_PER_EXTENT - 1].read(), 0xff);
        for ptr in ptrs {
            alloc.dealloc_with_metadata(ptr, layout, epoch_long);
        }
    }

    let epoch_released = lifetime_hugepage_stats_snapshot();
    assert_eq!(epoch_released.current_extents, 0);
    assert_eq!(epoch_released.current_thp_extents, 0);
    assert_eq!(epoch_released.current_thp_collapse_confirmed_extents, 0);
    assert_eq!(epoch_released.extent_unmaps, 1);
    assert_eq!(
        epoch_released.thp_collapse_successes + epoch_released.thp_collapse_errors,
        epoch_released.thp_collapse_attempts
    );
    if epoch_released.thp_collapse_successes == 1 {
        assert_eq!(
            epoch_released.placement_true_positive_objects,
            OBJECTS_PER_EXTENT
        );
        assert_eq!(epoch_released.placement_false_negative_objects, 0);
    } else {
        assert_eq!(epoch_released.placement_true_positive_objects, 0);
        assert_eq!(
            epoch_released.placement_false_negative_objects,
            OBJECTS_PER_EXTENT
        );
    }
    assert!(epoch_released.all_mappings_released);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn adaptive_runtime_learns_short_without_manual_epoch_markers() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(64, 16).unwrap();
    let short = adaptive_metadata(0xADA0_0001, 0xADA0_1001, 0);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));

    unsafe {
        for _ in 0..8 {
            let ptr = alloc.alloc_with_metadata(layout, short);
            assert!(!ptr.is_null());
            core::ptr::write_bytes(ptr, 0x5a, layout.size());
            alloc.dealloc_with_metadata(ptr, layout, short);
        }
    }

    let trained = lifetime_hugepage_stats_snapshot();
    assert_eq!(trained.phase_advances, 0);
    assert_eq!(trained.adaptive_site_count, 1);
    assert_eq!(trained.adaptive_short_sites, 1);
    assert_eq!(trained.adaptive_long_sites, 0);
    assert_eq!(trained.adaptive_short_observations, 8);
    assert_eq!(trained.adaptive_training_allocations, 8);
    assert_eq!(trained.adaptive_trailer_corruptions, 0);
    assert_eq!(trained.adaptive_static_hint_abstained_objects, 8);
    assert_runtime_validation_closes(trained);

    let mappings_before_sampling = trained.ordinary_extent_mappings;
    let reuse_before_sampling = trained.retained_empty_extent_reuse_hits;
    unsafe {
        for _ in 0..256 {
            let ptr = alloc.alloc_with_metadata(layout, short);
            assert!(!ptr.is_null());
            alloc.dealloc_with_metadata(ptr, layout, short);
        }
    }
    let applied = lifetime_hugepage_stats_snapshot();
    assert_eq!(applied.adaptive_short_routed_allocations, 1);
    assert_eq!(applied.adaptive_short_bypassed_allocations, 255);
    assert_eq!(applied.adaptive_long_routed_allocations, 0);
    assert_eq!(applied.adaptive_live_trailers, 0);
    assert_eq!(applied.ordinary_extent_mappings, mappings_before_sampling);
    assert_eq!(
        applied.retained_empty_extent_reuse_hits,
        reuse_before_sampling + 1
    );
    assert_eq!(applied.phase_advances, 0);
    assert_runtime_validation_closes(applied);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn adaptive_runtime_caps_cold_inflight_training() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(128, 16).unwrap();
    let cold = adaptive_metadata(0xADA0_0007, 0xADA0_1007, 0);
    let mut pointers = [core::ptr::null_mut(); 32];

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));

    unsafe {
        for ptr in &mut pointers {
            *ptr = alloc.alloc_with_metadata(layout, cold);
            assert!(!(*ptr).is_null());
        }
    }
    let allocated = lifetime_hugepage_stats_snapshot();
    assert_eq!(allocated.adaptive_eligible_allocations, 32);
    assert_eq!(allocated.adaptive_training_allocations, 8);
    assert_eq!(allocated.adaptive_cold_bypassed_allocations, 24);
    assert_eq!(allocated.adaptive_live_trailers, 8);

    unsafe {
        for ptr in pointers {
            alloc.dealloc_with_metadata(ptr, layout, cold);
        }
    }
    let released = lifetime_hugepage_stats_snapshot();
    assert_eq!(released.adaptive_short_observations, 8);
    assert_eq!(released.adaptive_short_sites, 1);
    assert_eq!(released.adaptive_live_trailers, 0);
    assert_eq!(released.current_retained_empty_extents, 1);
    assert!(!released.all_mappings_released);
    assert_runtime_validation_closes(released);
    assert!(lifetime_hugepage_trim_retained_empty_extents());
    assert!(lifetime_hugepage_stats_snapshot().all_mappings_released);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn adaptive_force_track_all_routes_ordinary_until_guard_and_exports_bypass() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(64, 16).unwrap();
    let site = adaptive_metadata(0xADA0_F001, 0xADA0_F101, 0);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_adaptive_site_recording_enable());
    assert!(!lifetime_hugepage_adaptive_site_force_track_all_enable(
        0, 128
    ));
    assert!(lifetime_hugepage_adaptive_site_force_track_all_enable(
        2, 128
    ));
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));

    unsafe {
        for _ in 0..3 {
            let ptr = alloc.alloc_with_metadata(layout, site);
            assert!(!ptr.is_null());
            core::ptr::write_bytes(ptr, 0x5a, layout.size());
            alloc.dealloc_with_metadata(ptr, layout, site);
        }
    }

    let stats = lifetime_hugepage_stats_snapshot();
    assert!(stats.adaptive_force_track_all);
    assert_eq!(stats.adaptive_force_track_all_maximum_allocations, 2);
    assert_eq!(stats.adaptive_force_track_all_maximum_requested_bytes, 128);
    assert_eq!(stats.adaptive_force_track_all_admitted_allocations, 2);
    assert_eq!(stats.adaptive_force_track_all_admitted_requested_bytes, 128);
    assert_eq!(stats.adaptive_force_track_all_guard_bypasses, 1);
    assert_eq!(stats.adaptive_eligible_allocations, 3);
    assert_eq!(stats.adaptive_training_allocations, 2);
    assert_eq!(stats.adaptive_cold_bypassed_allocations, 0);
    assert_eq!(stats.adaptive_short_bypassed_allocations, 0);
    assert_eq!(stats.thp_extent_mappings, 0);
    assert_eq!(stats.thp_advice_attempts, 0);
    assert_eq!(stats.nohugepage_advice_failures, 0);

    let mut rows = [LifetimeAdaptiveSiteSnapshot::empty(); 1];
    assert_eq!(lifetime_hugepage_adaptive_site_snapshot(&mut rows), 1);
    assert_eq!(rows[0].allocation_count, 3);
    assert_eq!(rows[0].tracked_allocations, 2);
    assert_eq!(rows[0].bypassed_allocations, 1);
    assert_eq!(rows[0].short_outcomes, 2);

    assert!(lifetime_hugepage_adaptive_site_force_track_all_disable());
    assert!(lifetime_hugepage_adaptive_site_recording_disable());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn adaptive_force_track_counts_unsupported_semantic_pressure_before_local_drop() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let target_layout = Layout::from_size_align(4096, 64).unwrap();
    let pressure_layout = Layout::from_size_align(16 * 1024 * 1024, 64).unwrap();
    let target = adaptive_metadata(0xADA0_F002, 0xADA0_F102, 0);
    let pressure = adaptive_metadata(0xADA0_F003, 0xADA0_F103, 0);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_adaptive_site_recording_enable());
    assert!(lifetime_hugepage_adaptive_site_force_track_all_enable(
        16,
        64 * 1024 * 1024,
    ));
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));

    unsafe {
        let target_ptr = alloc.alloc_with_metadata(target_layout, target);
        assert!(!target_ptr.is_null());
        let pressure_ptr = alloc.alloc_with_metadata(pressure_layout, pressure);
        assert!(!pressure_ptr.is_null());
        pressure_ptr.write_volatile(0x5a);
        alloc.dealloc_with_metadata(pressure_ptr, pressure_layout, pressure);
        alloc.dealloc_with_metadata(target_ptr, target_layout, target);
    }

    let stats = lifetime_hugepage_stats_snapshot();
    assert_eq!(stats.adaptive_force_track_all_pressure_allocations, 2);
    assert_eq!(
        stats.adaptive_force_track_all_pressure_requested_bytes,
        target_layout.size() + pressure_layout.size()
    );
    assert_eq!(stats.adaptive_force_track_all_admitted_allocations, 1);
    assert_eq!(stats.unsupported_layout_bypasses, 1);
    assert_eq!(stats.adaptive_long_observations, 1);
    assert_eq!(stats.adaptive_short_observations, 0);

    let mut rows = [LifetimeAdaptiveSiteSnapshot::empty(); 1];
    assert_eq!(lifetime_hugepage_adaptive_site_snapshot(&mut rows), 1);
    assert_eq!(rows[0].callsite, target.callsite);
    assert_eq!(rows[0].long_outcomes, 1);
    assert!(rows[0].maximum_completed_age_bytes >= 16 * 1024 * 1024);

    assert!(lifetime_hugepage_adaptive_site_force_track_all_disable());
    assert!(lifetime_hugepage_adaptive_site_recording_disable());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn adaptive_force_track_counts_raw_and_guard_page_pressure_generations() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let target_layout = Layout::from_size_align(4096, 64).unwrap();
    let pressure_layout = Layout::from_size_align(16 * 1024 * 1024, 64).unwrap();
    let target = adaptive_metadata(0xADA0_F004, 0xADA0_F104, 0);
    let guard_pressure =
        adaptive_metadata(0xADA0_F005, 0xADA0_F105, 0).with_flags(FLAG_GUARD_PAGES);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_adaptive_site_recording_enable());
    assert!(lifetime_hugepage_adaptive_site_force_track_all_enable(
        16,
        128 * 1024 * 1024,
    ));
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));

    unsafe {
        let first_target = alloc.alloc_with_metadata(target_layout, target);
        assert!(!first_target.is_null());
        let raw = GlobalAlloc::alloc(&alloc, pressure_layout);
        assert!(!raw.is_null());
        raw.write_volatile(0x5a);
        GlobalAlloc::dealloc(&alloc, raw, pressure_layout);
        alloc.dealloc_with_metadata(first_target, target_layout, target);

        let second_target = alloc.alloc_with_metadata(target_layout, target);
        assert!(!second_target.is_null());
        let guarded = alloc.alloc_with_metadata(pressure_layout, guard_pressure);
        assert!(!guarded.is_null());
        guarded.write_volatile(0xa5);
        alloc.dealloc_with_metadata(guarded, pressure_layout, guard_pressure);
        alloc.dealloc_with_metadata(second_target, target_layout, target);
    }

    let stats = lifetime_hugepage_stats_snapshot();
    assert_eq!(stats.adaptive_force_track_all_pressure_allocations, 4);
    assert_eq!(stats.adaptive_force_track_all_raw_pressure_allocations, 1);
    assert_eq!(
        stats.adaptive_force_track_all_raw_pressure_requested_bytes,
        pressure_layout.size()
    );
    assert_eq!(stats.adaptive_long_observations, 2);
    assert_eq!(stats.adaptive_short_observations, 0);

    let mut rows = [LifetimeAdaptiveSiteSnapshot::empty(); 1];
    assert_eq!(lifetime_hugepage_adaptive_site_snapshot(&mut rows), 1);
    assert_eq!(rows[0].long_outcomes, 2);
    assert!(rows[0].maximum_completed_age_bytes >= 16 * 1024 * 1024);

    assert!(lifetime_hugepage_adaptive_site_force_track_all_disable());
    assert!(lifetime_hugepage_adaptive_site_recording_disable());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn adaptive_force_track_counts_alignment_changing_raw_reallocation() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let target_layout = Layout::from_size_align(4096, 64).unwrap();
    let old_layout = Layout::from_size_align(4096, 64).unwrap();
    let new_layout = Layout::from_size_align(16 * 1024 * 1024, 4096).unwrap();
    let target = adaptive_metadata(0xADA0_F006, 0xADA0_F106, 0);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_adaptive_site_recording_enable());
    assert!(lifetime_hugepage_adaptive_site_force_track_all_enable(
        16,
        128 * 1024 * 1024,
    ));
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));

    unsafe {
        let target_ptr = alloc.alloc_with_metadata(target_layout, target);
        assert!(!target_ptr.is_null());
        let old = Allocator::allocate(&alloc, old_layout).unwrap();
        let old_ptr = core::ptr::NonNull::new_unchecked(old.as_ptr() as *mut u8);
        let grown = Allocator::grow(&alloc, old_ptr, old_layout, new_layout).unwrap();
        let grown_ptr = core::ptr::NonNull::new_unchecked(grown.as_ptr() as *mut u8);
        grown_ptr.as_ptr().write_volatile(0x5a);
        Allocator::deallocate(&alloc, grown_ptr, new_layout);
        alloc.dealloc_with_metadata(target_ptr, target_layout, target);
    }

    let stats = lifetime_hugepage_stats_snapshot();
    assert_eq!(stats.adaptive_force_track_all_pressure_allocations, 3);
    assert_eq!(stats.adaptive_force_track_all_raw_pressure_allocations, 1);
    assert_eq!(
        stats.adaptive_force_track_all_raw_reallocation_pressure_allocations,
        1
    );
    assert_eq!(
        stats.adaptive_force_track_all_raw_reallocation_pressure_requested_bytes,
        new_layout.size()
    );
    assert_eq!(stats.adaptive_long_observations, 1);
    assert_eq!(stats.adaptive_short_observations, 0);

    assert!(lifetime_hugepage_adaptive_site_force_track_all_disable());
    assert!(lifetime_hugepage_adaptive_site_recording_disable());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn adaptive_force_track_counts_layout_derived_raw_only_semantic_pressure() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let target_layout = Layout::from_size_align(4096, 64).unwrap();
    let pressure_layout = Layout::from_size_align(16 * 1024 * 1024, 64).unwrap();
    let target = adaptive_metadata(0xADA0_F007, 0xADA0_F107, 0);
    let pressure = adaptive_metadata(0xADA0_F008, 0xADA0_F108, 0).with_placement_hint(0xA770);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_adaptive_site_recording_enable());
    assert!(lifetime_hugepage_adaptive_site_force_track_all_enable(
        16,
        64 * 1024 * 1024,
    ));
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));

    unsafe {
        let target_ptr = alloc.alloc_with_metadata(target_layout, target);
        assert!(!target_ptr.is_null());
        let pressure_ptr = alloc.alloc_with_metadata(pressure_layout, pressure);
        assert!(!pressure_ptr.is_null());
        pressure_ptr.write_volatile(0x5a);
        alloc.dealloc_with_metadata(pressure_ptr, pressure_layout, pressure);
        alloc.dealloc_with_metadata(target_ptr, target_layout, target);
    }

    let stats = lifetime_hugepage_stats_snapshot();
    assert_eq!(stats.adaptive_force_track_all_pressure_allocations, 2);
    assert_eq!(
        stats.adaptive_force_track_all_pressure_requested_bytes,
        target_layout.size() + pressure_layout.size()
    );
    assert_eq!(stats.adaptive_force_track_all_admitted_allocations, 1);
    assert_eq!(stats.adaptive_long_observations, 1);
    assert_eq!(stats.adaptive_short_observations, 0);

    assert!(lifetime_hugepage_adaptive_site_force_track_all_disable());
    assert!(lifetime_hugepage_adaptive_site_recording_disable());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn adaptive_force_track_counts_missing_identity_semantic_pressure() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let target_layout = Layout::from_size_align(4096, 64).unwrap();
    let pressure_layout = Layout::from_size_align(4096, 64).unwrap();
    let target = adaptive_metadata(0xADA0_F009, 0xADA0_F109, 0);
    let pressure_without_callsite = metadata(0xADA0_F00A, 0);
    const PRESSURE_ALLOCATIONS: usize = (8 * 1024 * 1024) / 4096;

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_adaptive_site_recording_enable());
    assert!(lifetime_hugepage_adaptive_site_force_track_all_enable(
        4096,
        64 * 1024 * 1024,
    ));
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));

    unsafe {
        let target_ptr = alloc.alloc_with_metadata(target_layout, target);
        assert!(!target_ptr.is_null());
        for _ in 0..PRESSURE_ALLOCATIONS {
            let pressure_ptr =
                alloc.alloc_with_metadata(pressure_layout, pressure_without_callsite);
            assert!(!pressure_ptr.is_null());
            pressure_ptr.write_volatile(0x5a);
            alloc.dealloc_with_metadata(pressure_ptr, pressure_layout, pressure_without_callsite);
        }
        alloc.dealloc_with_metadata(target_ptr, target_layout, target);
    }

    let stats = lifetime_hugepage_stats_snapshot();
    assert_eq!(
        stats.adaptive_force_track_all_pressure_allocations,
        PRESSURE_ALLOCATIONS + 1
    );
    assert_eq!(
        stats.adaptive_force_track_all_pressure_requested_bytes,
        target_layout.size() + PRESSURE_ALLOCATIONS * pressure_layout.size()
    );
    assert_eq!(
        stats.adaptive_missing_identity_bypasses,
        PRESSURE_ALLOCATIONS
    );
    assert_eq!(stats.adaptive_force_track_all_admitted_allocations, 1);
    assert_eq!(stats.adaptive_long_observations, 1);
    assert_eq!(stats.adaptive_short_observations, 0);

    assert!(lifetime_hugepage_adaptive_site_force_track_all_disable());
    assert!(lifetime_hugepage_adaptive_site_recording_disable());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn adaptive_runtime_backs_off_after_censored_cold_training() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let survivor_layout = Layout::from_size_align(64, 16).unwrap();
    let pressure_layout = Layout::from_size_align(4096, 64).unwrap();
    let survivor = adaptive_metadata(0xADA0_0008, 0xADA0_1008, 0);
    let pressure = adaptive_metadata(0xADA0_0009, 0xADA0_1009, 0);
    const CENSORED_PRESSURE_OBJECTS: usize = (2 * 1024 * 1024) / 4096;

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));

    unsafe {
        for _ in 0..8 {
            let survivor_ptr = alloc.alloc_with_metadata(survivor_layout, survivor);
            assert!(!survivor_ptr.is_null());
            for _ in 0..CENSORED_PRESSURE_OBJECTS {
                let pressure_ptr = alloc.alloc_with_metadata(pressure_layout, pressure);
                assert!(!pressure_ptr.is_null());
                alloc.dealloc_with_metadata(pressure_ptr, pressure_layout, pressure);
            }
            alloc.dealloc_with_metadata(survivor_ptr, survivor_layout, survivor);
        }
    }

    let trained = lifetime_hugepage_stats_snapshot();
    assert_eq!(trained.adaptive_censored_observations, 8);
    assert_eq!(trained.adaptive_cold_sites, 1);
    let training_before = trained.adaptive_training_allocations;
    let bypasses_before = trained.adaptive_cold_bypassed_allocations;
    let mappings_before = trained.ordinary_extent_mappings;
    let reuse_before = trained.retained_empty_extent_reuse_hits;

    unsafe {
        for _ in 0..256 {
            let ptr = alloc.alloc_with_metadata(survivor_layout, survivor);
            assert!(!ptr.is_null());
            alloc.dealloc_with_metadata(ptr, survivor_layout, survivor);
        }
    }

    let sampled = lifetime_hugepage_stats_snapshot();
    assert_eq!(
        sampled.adaptive_training_allocations - training_before,
        1,
        "a censored Cold site should switch to periodic sampling"
    );
    assert_eq!(
        sampled.adaptive_cold_bypassed_allocations - bypasses_before,
        255
    );
    assert_eq!(sampled.ordinary_extent_mappings, mappings_before);
    assert_eq!(sampled.retained_empty_extent_reuse_hits, reuse_before + 1);
    assert_eq!(sampled.adaptive_live_trailers, 0);
    assert_runtime_validation_closes(sampled);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn adaptive_runtime_learns_long_and_keeps_sparse_candidate_unbacked() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let survivor_layout = Layout::from_size_align(64, 16).unwrap();
    let pressure_layout = Layout::from_size_align(4096, 64).unwrap();
    let long = adaptive_metadata(0xADA0_0002, 0xADA0_1002, 0);
    let pressure = adaptive_metadata(0xADA0_0003, 0xADA0_1003, 0);
    const PRESSURE_OBJECTS: usize = (8 * 1024 * 1024) / 4096;

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));

    unsafe {
        for _ in 0..8 {
            let survivor = alloc.alloc_with_metadata(survivor_layout, long);
            assert!(!survivor.is_null());
            let mut pressure_ptrs = Vec::with_capacity(PRESSURE_OBJECTS);
            for _ in 0..PRESSURE_OBJECTS {
                let ptr = alloc.alloc_with_metadata(pressure_layout, pressure);
                assert!(!ptr.is_null());
                pressure_ptrs.push(ptr);
            }
            for ptr in pressure_ptrs {
                alloc.dealloc_with_metadata(ptr, pressure_layout, pressure);
            }
            alloc.dealloc_with_metadata(survivor, survivor_layout, long);
        }
    }

    let trained = lifetime_hugepage_stats_snapshot();
    assert_eq!(trained.phase_advances, 0);
    assert!(trained.adaptive_epoch_advances >= 32);
    assert_eq!(trained.adaptive_long_sites, 1);
    assert!(trained.adaptive_long_observations >= 8);
    assert_eq!(trained.adaptive_trailer_corruptions, 0);
    assert_runtime_validation_closes(trained);

    assert!(lifetime_hugepage_stats_reset());
    let reset_window = lifetime_hugepage_stats_snapshot();
    assert_eq!(reset_window.adaptive_long_sites, 1);
    assert_eq!(reset_window.adaptive_eligible_allocations, 0);
    assert_eq!(reset_window.adaptive_pressure_bytes, 0);
    let thp_before = reset_window.thp_extent_mappings;
    unsafe {
        let ptr = alloc.alloc_with_metadata(survivor_layout, long);
        assert!(!ptr.is_null());
        alloc.dealloc_with_metadata(ptr, survivor_layout, long);
    }
    let applied = lifetime_hugepage_stats_snapshot();
    assert_eq!(applied.adaptive_long_routed_allocations, 1);
    assert!(applied.thp_extent_mappings > thp_before);
    assert_eq!(applied.thp_candidate_extent_mappings, 1);
    assert_eq!(applied.thp_advice_attempts, 0);
    assert_eq!(applied.adaptive_live_trailers, 0);
    assert_runtime_validation_closes(applied);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn adaptive_runtime_fails_closed_without_exact_site_identity() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(256, 16).unwrap();

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));
    unsafe {
        let metadata = AllocationMetadata::for_type(0xADA0_0004);
        let ptr = alloc.alloc_with_metadata(layout, metadata);
        assert!(!ptr.is_null());
        alloc.dealloc_with_metadata(ptr, layout, metadata);
    }
    let snapshot = lifetime_hugepage_stats_snapshot();
    assert_eq!(snapshot.adaptive_missing_identity_bypasses, 1);
    assert_eq!(snapshot.adaptive_eligible_allocations, 0);
    assert_eq!(snapshot.current_extents, 0);
    assert_runtime_validation_closes(snapshot);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn adaptive_runtime_recovers_cross_thread_site_from_slot_trailer() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(512, 32).unwrap();
    let metadata = adaptive_metadata(0xADA0_0005, 0xADA0_1005, LIFETIME_HINT_LONG_LIVED);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));
    let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
    assert!(!ptr.is_null());
    let ptr_address = ptr as usize;
    std::thread::spawn(move || unsafe {
        UniAlloc::new().dealloc_with_metadata(ptr_address as *mut u8, layout, metadata);
    })
    .join()
    .unwrap();

    let snapshot = lifetime_hugepage_stats_snapshot();
    assert_eq!(snapshot.adaptive_short_observations, 1);
    assert_eq!(snapshot.adaptive_live_trailers, 0);
    assert_eq!(snapshot.adaptive_trailer_corruptions, 0);
    assert_eq!(snapshot.adaptive_static_hint_false_positive_objects, 1);
    assert_eq!(snapshot.adaptive_static_hint_abstained_objects, 0);
    assert_eq!(snapshot.current_retained_empty_extents, 1);
    assert!(!snapshot.all_mappings_released);
    assert_runtime_validation_closes(snapshot);
    assert!(lifetime_hugepage_trim_retained_empty_extents());
    assert!(lifetime_hugepage_stats_snapshot().all_mappings_released);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}

#[test]
fn adaptive_runtime_in_place_realloc_preserves_hidden_trailer() {
    let _guard = test_guard();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(4096, 64).unwrap();
    let resized = Layout::from_size_align(4080, 64).unwrap();
    let metadata = adaptive_metadata(0xADA0_0006, 0xADA0_1006, 0);

    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure_with_backend(
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        LifetimePageBackend::TransparentHugepage,
    ));
    unsafe {
        let ptr = alloc.alloc_with_metadata(layout, metadata);
        assert!(!ptr.is_null());
        core::ptr::write_bytes(ptr, 0xa5, layout.size());
        let result =
            alloc.realloc_with_split_metadata(ptr, layout, resized.size(), metadata, metadata);
        assert_eq!(result, ptr);
        assert_eq!(result.read(), 0xa5);
        assert_eq!(result.add(resized.size() - 1).read(), 0xa5);
        alloc.dealloc_with_metadata(result, resized, metadata);
    }
    let snapshot = lifetime_hugepage_stats_snapshot();
    assert_eq!(snapshot.adaptive_eligible_allocations, 1);
    assert_eq!(snapshot.adaptive_short_observations, 1);
    assert_eq!(snapshot.adaptive_trailer_corruptions, 0);
    assert_eq!(snapshot.adaptive_live_trailers, 0);
    assert_runtime_validation_closes(snapshot);
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::Disabled
    ));
}
