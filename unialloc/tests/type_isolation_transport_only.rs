#![cfg(all(
    feature = "type_isolation",
    not(feature = "stats"),
    not(feature = "reclaim_checks"),
    not(feature = "quarantine"),
    not(feature = "fixed_heap")
))]

use core::alloc::{GlobalAlloc, Layout};

use unialloc::alloc_api::type_isolation::{
    __unialloc_semantic_scope_pop, __unialloc_semantic_scope_push,
    __unialloc_semantic_scope_push_hints,
    semantic_address_lifecycle_tracking_active_for_diagnostics,
    semantic_allocation_slow_path_enabled, take_auto_deallocation_metadata,
    PLACEMENT_HINT_CROSS_THREAD_RECOVERY,
};
use unialloc::{
    active_allocation_metadata, semantic_runtime_slow_path_enabled, semantic_scope_depth_snapshot,
    type_isolation_side_cache_snapshot, AllocationMetadata, UniAlloc, FLAG_TYPE_ISOLATED,
};

#[test]
fn release_transport_only_scope_is_raw_until_policy_activation() {
    assert!(!semantic_address_lifecycle_tracking_active_for_diagnostics());
    assert!(!semantic_allocation_slow_path_enabled());
    assert!(!semantic_runtime_slow_path_enabled());

    let allocator = UniAlloc::new();
    let old_layout = Layout::from_size_align(64, 8).unwrap();
    let new_layout = Layout::from_size_align(192, old_layout.align()).unwrap();
    let transport = AllocationMetadata::for_type(0xC002_9001)
        .with_module(0xC0DE)
        .with_flags(0)
        .with_lifetime_hint(0x51)
        .with_placement_hint(0x27)
        .with_callsite(0xA110_9001);
    let cache_before = type_isolation_side_cache_snapshot();

    __unialloc_semantic_scope_push_hints(
        transport.type_id,
        transport.module_id,
        transport.flags,
        transport.lifetime_hint,
        transport.placement_hint,
        transport.callsite,
    );
    assert_eq!(
        active_allocation_metadata(),
        Some(
            transport.with_placement_hint(
                transport.placement_hint | PLACEMENT_HINT_CROSS_THREAD_RECOVERY
            )
        )
    );
    assert!(!semantic_allocation_slow_path_enabled());
    assert!(!semantic_runtime_slow_path_enabled());
    assert!(!semantic_address_lifecycle_tracking_active_for_diagnostics());

    let ptr = unsafe { GlobalAlloc::alloc(&allocator, old_layout) };
    assert!(!ptr.is_null());
    unsafe {
        for offset in 0..old_layout.size() {
            ptr.add(offset).write((offset as u8).wrapping_mul(31));
        }
    }
    assert_eq!(take_auto_deallocation_metadata(ptr, old_layout), None);

    let moved = unsafe { GlobalAlloc::realloc(&allocator, ptr, old_layout, new_layout.size()) };
    assert!(!moved.is_null());
    for offset in 0..old_layout.size() {
        assert_eq!(
            unsafe { moved.add(offset).read() },
            (offset as u8).wrapping_mul(31)
        );
    }
    assert_eq!(take_auto_deallocation_metadata(moved, new_layout), None);
    unsafe {
        GlobalAlloc::dealloc(&allocator, moved, new_layout);
    }
    assert_eq!(type_isolation_side_cache_snapshot(), cache_before);
    assert!(!semantic_address_lifecycle_tracking_active_for_diagnostics());
    __unialloc_semantic_scope_pop();
    assert_eq!(active_allocation_metadata(), None);
    assert_eq!(semantic_scope_depth_snapshot().represented_depth, 0);

    let policy = AllocationMetadata::for_type(0xC002_9002)
        .with_module(0xC0DE)
        .with_flags(FLAG_TYPE_ISOLATED)
        .with_callsite(0xA110_9002);
    __unialloc_semantic_scope_push_hints(
        transport.type_id,
        transport.module_id,
        transport.flags,
        transport.lifetime_hint,
        transport.placement_hint,
        transport.callsite,
    );
    assert!(!semantic_address_lifecycle_tracking_active_for_diagnostics());
    assert!(!semantic_allocation_slow_path_enabled());

    __unialloc_semantic_scope_push(
        policy.type_id,
        policy.module_id,
        policy.flags,
        policy.callsite,
    );
    assert!(semantic_address_lifecycle_tracking_active_for_diagnostics());
    assert!(semantic_allocation_slow_path_enabled());
    __unialloc_semantic_scope_pop();

    assert_eq!(
        active_allocation_metadata(),
        Some(
            transport.with_placement_hint(
                transport.placement_hint | PLACEMENT_HINT_CROSS_THREAD_RECOVERY
            )
        )
    );
    assert!(semantic_address_lifecycle_tracking_active_for_diagnostics());
    assert!(!semantic_allocation_slow_path_enabled());
    assert!(!semantic_runtime_slow_path_enabled());
    __unialloc_semantic_scope_pop();
    assert_eq!(active_allocation_metadata(), None);
    assert_eq!(semantic_scope_depth_snapshot().represented_depth, 0);
    assert!(semantic_address_lifecycle_tracking_active_for_diagnostics());
}
