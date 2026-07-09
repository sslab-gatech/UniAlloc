use std::alloc::{dealloc, Layout};

use unialloc::alloc_api::{
    __unialloc_alloc_with_metadata_hints, __unialloc_dealloc_with_metadata_hints,
    __unialloc_semantic_scope_enter_hints, __unialloc_semantic_scope_exit,
};
use unialloc::{
    semantic_auto_metadata_disable, semantic_metadata_validation_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, AllocationMetadata, SemanticAlloc,
    UniAlloc, FLAG_TYPE_ISOLATED,
};

#[global_allocator]
static A: UniAlloc = UniAlloc;

fn json_bool(value: bool) -> &'static str {
    if value {
        "true"
    } else {
        "false"
    }
}

fn fail(message: &str) -> ! {
    println!(
        "{{\"source\":\"semantic_metadata_validation_probe\",\"passed\":false,\"error\":\"{}\"}}",
        message
    );
    std::process::exit(2);
}

unsafe fn alloc_with_recovery(layout: Layout, metadata: AllocationMetadata) -> *mut u8 {
    __unialloc_alloc_with_metadata_hints(
        layout.size(),
        layout.align(),
        metadata.type_id,
        metadata.module_id,
        metadata.flags,
        metadata.lifetime_hint,
        metadata.placement_hint,
        metadata.callsite,
    )
}

unsafe fn dealloc_with_metadata(ptr: *mut u8, layout: Layout, metadata: AllocationMetadata) {
    if !__unialloc_dealloc_with_metadata_hints(
        ptr,
        layout.size(),
        layout.align(),
        metadata.type_id,
        metadata.module_id,
        metadata.flags,
        metadata.lifetime_hint,
        metadata.placement_hint,
        metadata.callsite,
    ) {
        fail("metadata deallocation rejected layout");
    }
}

unsafe fn global_dealloc_in_active_scope(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) {
    let previous = __unialloc_semantic_scope_enter_hints(
        metadata.type_id,
        metadata.module_id,
        metadata.flags,
        metadata.lifetime_hint,
        metadata.placement_hint,
        metadata.callsite,
    );
    dealloc(ptr, layout);
    __unialloc_semantic_scope_exit(previous);
}

fn main() {
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let layout = Layout::from_size_align(
        4 * std::mem::size_of::<usize>(),
        std::mem::align_of::<usize>(),
    )
    .expect("valid metadata-validation probe layout");
    let correct = AllocationMetadata::for_type(0xCAFE_7001)
        .with_module(0xCAFE_7002)
        .with_callsite(0xCAFE_7003)
        .with_flags(FLAG_TYPE_ISOLATED);
    let wrong = AllocationMetadata::for_type(0xBAD0_7001)
        .with_module(correct.module_id)
        .with_callsite(0xBAD0_7003)
        .with_flags(FLAG_TYPE_ISOLATED);

    let mismatched_ptr = unsafe { alloc_with_recovery(layout, correct) };
    if mismatched_ptr.is_null() {
        fail("recovery allocation for mismatch probe returned null");
    }
    unsafe {
        mismatched_ptr.write(0xA5);
        dealloc_with_metadata(mismatched_ptr, layout, wrong);
    }

    let alloc = UniAlloc::new();
    let reused = unsafe { alloc.alloc_with_metadata(layout, correct) };
    if reused.is_null() {
        fail("typed allocation after mismatch probe returned null");
    }
    let reused_under_recorded_identity = reused == mismatched_ptr;
    unsafe {
        alloc.dealloc_with_metadata(reused, layout, correct);
    }

    let matched_ptr = unsafe { alloc_with_recovery(layout, correct) };
    if matched_ptr.is_null() {
        fail("recovery allocation for match probe returned null");
    }
    unsafe {
        dealloc_with_metadata(matched_ptr, layout, correct);
    }

    let active_scope_ptr = unsafe { alloc_with_recovery(layout, correct) };
    if active_scope_ptr.is_null() {
        fail("recovery allocation for active-scope probe returned null");
    }
    unsafe {
        global_dealloc_in_active_scope(active_scope_ptr, layout, wrong);
    }
    let reused_after_active_scope = unsafe { alloc.alloc_with_metadata(layout, correct) };
    if reused_after_active_scope.is_null() {
        fail("typed allocation after active-scope mismatch probe returned null");
    }
    let active_scope_reused_under_recorded_identity = reused_after_active_scope == active_scope_ptr;
    unsafe {
        alloc.dealloc_with_metadata(reused_after_active_scope, layout, correct);
    }

    let validation = semantic_metadata_validation_snapshot();
    semantic_stats_recording_disable();

    let passed = validation.recovery_identity_mismatches >= 2
        && validation.recovery_identity_matches >= 1
        && validation.last_mismatch_requested_type_id == wrong.type_id
        && validation.last_mismatch_recorded_type_id == correct.type_id
        && validation.last_mismatch_requested_module_id == wrong.module_id
        && validation.last_mismatch_recorded_module_id == correct.module_id
        && validation.last_mismatch_requested_callsite == wrong.callsite
        && validation.last_mismatch_recorded_callsite == correct.callsite
        && reused_under_recorded_identity
        && active_scope_reused_under_recorded_identity;

    println!(
        "{{\"source\":\"semantic_metadata_validation_probe\",\"passed\":{},\"recovery_identity_matches\":{},\"recovery_identity_mismatches\":{},\"last_mismatch_requested_type_id\":{},\"last_mismatch_recorded_type_id\":{},\"last_mismatch_requested_module_id\":{},\"last_mismatch_recorded_module_id\":{},\"last_mismatch_requested_callsite\":{},\"last_mismatch_recorded_callsite\":{},\"reused_under_recorded_identity\":{},\"active_scope_reused_under_recorded_identity\":{}}}",
        json_bool(passed),
        validation.recovery_identity_matches,
        validation.recovery_identity_mismatches,
        validation.last_mismatch_requested_type_id,
        validation.last_mismatch_recorded_type_id,
        validation.last_mismatch_requested_module_id,
        validation.last_mismatch_recorded_module_id,
        validation.last_mismatch_requested_callsite,
        validation.last_mismatch_recorded_callsite,
        json_bool(reused_under_recorded_identity),
        json_bool(active_scope_reused_under_recorded_identity),
    );

    if !passed {
        std::process::exit(1);
    }
}
