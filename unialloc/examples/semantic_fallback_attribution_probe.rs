use std::alloc::{GlobalAlloc, Layout};

use unialloc::alloc_api::{
    __unialloc_alloc_with_metadata_hints, PLACEMENT_HINT_CROSS_THREAD_RECOVERY,
};
use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    AllocationMetadata, UniAlloc, FLAG_TYPE_ISOLATED,
};

fn json_bool(value: bool) -> &'static str {
    if value {
        "true"
    } else {
        "false"
    }
}

fn fail(message: &str) -> ! {
    println!(
        "{{\"source\":\"semantic_fallback_attribution_probe\",\"passed\":false,\"error\":\"{}\"}}",
        message
    );
    std::process::exit(2);
}

fn main() {
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(
        4 * std::mem::size_of::<usize>(),
        std::mem::align_of::<usize>(),
    )
    .expect("valid fallback probe layout");
    let bigger_layout = Layout::from_size_align(layout.size() * 2, layout.align())
        .expect("valid fallback realloc layout");

    let raw = unsafe { alloc.alloc(layout) };
    if raw.is_null() {
        fail("raw GlobalAlloc::alloc returned null");
    }
    unsafe {
        raw.write(0xA1);
        alloc.dealloc(raw, layout);
    }

    let raw_realloc_old = unsafe { alloc.alloc(layout) };
    if raw_realloc_old.is_null() {
        fail("raw realloc setup allocation returned null");
    }
    unsafe {
        raw_realloc_old.write(0xB2);
    }
    let raw_realloc_new = unsafe { alloc.realloc(raw_realloc_old, layout, bigger_layout.size()) };
    if raw_realloc_new.is_null() {
        fail("raw GlobalAlloc::realloc returned null");
    }
    unsafe {
        raw_realloc_new.write(0xB3);
        alloc.dealloc(raw_realloc_new, bigger_layout);
    }

    let metadata = AllocationMetadata::for_type(0xFA11_BACC_0001)
        .with_module(0xFA11_BACC_0002)
        .with_callsite(0xFA11_BACC_0003)
        .with_flags(FLAG_TYPE_ISOLATED);
    let recovered_old = unsafe {
        __unialloc_alloc_with_metadata_hints(
            layout.size(),
            layout.align(),
            metadata.type_id,
            metadata.module_id,
            metadata.flags,
            metadata.lifetime_hint,
            PLACEMENT_HINT_CROSS_THREAD_RECOVERY,
            metadata.callsite,
        )
    };
    if recovered_old.is_null() {
        fail("recovery-metadata setup allocation returned null");
    }
    unsafe {
        recovered_old.write(0xC4);
    }
    let recovered_new = unsafe { alloc.realloc(recovered_old, layout, bigger_layout.size()) };
    if recovered_new.is_null() {
        fail("recorded-old-metadata GlobalAlloc::realloc returned null");
    }
    unsafe {
        recovered_new.write(0xC5);
        alloc.dealloc(recovered_new, bigger_layout);
    }

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    semantic_stats_recording_disable();

    let passed = stats.fallback_allocations >= 3
        && stats.fallback_deallocations >= 3
        && stats.typed_allocations >= 1
        && stats.typed_deallocations >= 1
        && fallback.raw_alloc_no_metadata >= 2
        && fallback.raw_alloc_no_metadata_bytes >= layout.size() * 2
        && fallback.raw_dealloc_no_metadata >= 3
        && fallback.raw_realloc_no_metadata >= 1
        && fallback.raw_realloc_no_metadata_bytes >= bigger_layout.size()
        && fallback.realloc_recorded_old_metadata_new_allocations >= 1
        && fallback.realloc_recorded_old_metadata_new_allocation_bytes >= bigger_layout.size();

    println!(
        "{{\"source\":\"semantic_fallback_attribution_probe\",\"passed\":{},\"fallback_allocations\":{},\"fallback_deallocations\":{},\"typed_allocations\":{},\"typed_deallocations\":{},\"raw_alloc_no_metadata\":{},\"raw_alloc_no_metadata_bytes\":{},\"raw_dealloc_no_metadata\":{},\"raw_realloc_no_metadata\":{},\"raw_realloc_no_metadata_bytes\":{},\"raw_realloc_moved_dealloc_no_metadata\":{},\"realloc_recorded_old_metadata_new_allocations\":{},\"realloc_recorded_old_metadata_new_allocation_bytes\":{}}}",
        json_bool(passed),
        stats.fallback_allocations,
        stats.fallback_deallocations,
        stats.typed_allocations,
        stats.typed_deallocations,
        fallback.raw_alloc_no_metadata,
        fallback.raw_alloc_no_metadata_bytes,
        fallback.raw_dealloc_no_metadata,
        fallback.raw_realloc_no_metadata,
        fallback.raw_realloc_no_metadata_bytes,
        fallback.raw_realloc_moved_dealloc_no_metadata,
        fallback.realloc_recorded_old_metadata_new_allocations,
        fallback.realloc_recorded_old_metadata_new_allocation_bytes,
    );

    if !passed {
        std::process::exit(1);
    }
}
