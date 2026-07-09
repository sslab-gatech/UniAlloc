#![feature(allocator_api)]

use std::alloc::{GlobalAlloc, Layout};

use unialloc::alloc_api::{
    AllocationMetadata, SemanticAlloc, FLAG_DELAYED_FREE, FLAG_FORCE_INITIALIZE,
    FLAG_METADATA_SEGREGATED, FLAG_TYPE_ISOLATED,
};
use unialloc::{semantic_stats_reset, semantic_stats_snapshot, UniAlloc};

#[global_allocator]
static A: UniAlloc = UniAlloc;

fn main() {
    semantic_stats_reset();

    let typed_cases = [
        (
            Layout::from_size_align(48, 8).expect("valid layout"),
            AllocationMetadata::for_type(0x1001)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED)
                .with_module(1)
                .with_callsite(0xA110_C001),
        ),
        (
            Layout::from_size_align(96, 16).expect("valid layout"),
            AllocationMetadata::for_type(0x1002)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE)
                .with_module(1)
                .with_callsite(0xA110_C002),
        ),
        (
            Layout::from_size_align(128, 16).expect("valid layout"),
            AllocationMetadata::for_type(0x1003)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE)
                .with_module(1)
                .with_callsite(0xA110_C003),
        ),
    ];

    for (idx, (layout, metadata)) in typed_cases.iter().copied().enumerate() {
        let ptr = unsafe { A.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null(), "typed allocation failed");
        unsafe {
            A.dealloc_with_metadata(ptr, layout, metadata);
        }

        if idx == 0 {
            let reused = unsafe { A.alloc_with_metadata(layout, metadata) };
            assert_eq!(
                reused, ptr,
                "typed cache did not return the matching object"
            );
            unsafe {
                A.dealloc_with_metadata(reused, layout, metadata);
            }
        }
    }

    let fallback_layout = Layout::from_size_align(64, 8).expect("valid layout");
    let fallback = unsafe { GlobalAlloc::alloc(&A, fallback_layout) };
    assert!(!fallback.is_null(), "fallback allocation failed");
    unsafe {
        GlobalAlloc::dealloc(&A, fallback, fallback_layout);
    }

    let snap = semantic_stats_snapshot();
    println!(
        "{{\"source\":\"semantic_coverage_example\",\"type_id_basis\":\"manual-semantic-api-type-id\",\"typed\":true,\"type_id\":\"aggregate\",\"count\":{},\"bytes\":{},\"cache_hits\":{},\"cache_inserts\":{},\"cache_bypasses\":{},\"delayed_free_enqueues\":{},\"delayed_free_flushes\":{}}}",
        snap.typed_allocations,
        snap.typed_allocated_bytes,
        snap.typed_cache_hits,
        snap.typed_cache_inserts,
        snap.typed_cache_bypasses,
        snap.delayed_free_enqueues,
        snap.delayed_free_flushes
    );
    println!(
        "{{\"source\":\"semantic_coverage_example\",\"type_id_basis\":\"manual-semantic-api-type-id\",\"typed\":false,\"type_id\":0,\"count\":{},\"bytes\":{}}}",
        snap.fallback_allocations,
        snap.fallback_allocated_bytes
    );
}
