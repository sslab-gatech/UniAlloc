#![cfg_attr(feature = "fixed_heap", allow(dead_code, unused_imports))]

use std::fmt::Write as _;
use std::panic::{catch_unwind, AssertUnwindSafe};

use unialloc::{
    semantic_auto_metadata_disable, semantic_metadata_validation_snapshot,
    semantic_scope_depth_snapshot, semantic_stats_recording_disable, semantic_stats_reset,
    semantic_stats_snapshot, semantic_type_stats_recording_disable, semantic_type_stats_snapshot,
    type_isolation_side_cache_snapshot, SemanticScopeDepthSnapshot, SemanticTypeStatsSnapshot,
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

#[derive(Debug)]
struct PanicOnClone(u64);

impl Clone for PanicOnClone {
    fn clone(&self) -> Self {
        if self.0 == 3 {
            panic!("compiler semantic-scope unwind probe");
        }
        Self(self.0)
    }
}

#[repr(C)]
struct PostUnwindPayload([u64; 8]);

#[inline(never)]
fn trigger_panicking_vec_extend(seed: &[PanicOnClone]) {
    let mut values = Vec::with_capacity(seed.len());
    values.extend_from_slice(seed);
    std::hint::black_box(values);
}

#[inline(never)]
fn outer_box_after_caught_panic(
    seed: &[PanicOnClone],
    panic_observed: &mut bool,
    restored_outer_depth: &mut SemanticScopeDepthSnapshot,
) -> Box<PostUnwindPayload> {
    *panic_observed = catch_unwind(AssertUnwindSafe(|| {
        trigger_panicking_vec_extend(seed);
    }))
    .is_err();
    assert!(
        *panic_observed,
        "Vec::extend_from_slice should panic while cloning"
    );

    // This function is itself called through a compiler-inserted Box scope.
    // The inner Vec scope must unwind-pop back to that still-active outer scope,
    // rather than leaking the Vec identity or clearing the outer identity.
    *restored_outer_depth = semantic_scope_depth_snapshot();
    assert_eq!(
        restored_outer_depth.main_depth, 1,
        "{:?}",
        restored_outer_depth
    );
    assert_eq!(
        restored_outer_depth.overflow_depth, 0,
        "{:?}",
        restored_outer_depth
    );
    assert_eq!(
        restored_outer_depth.represented_depth, 1,
        "{:?}",
        restored_outer_depth
    );

    // Counter reset deliberately leaves the TLS scope stack untouched. The
    // following ordinary Box allocation must therefore pair with its Drop even
    // after the nested unwind/restore sequence.
    semantic_stats_reset();
    Box::new(PostUnwindPayload([
        0xC002_0000_0000_0001,
        0xC002_0000_0000_0002,
        0xC002_0000_0000_0003,
        0xC002_0000_0000_0004,
        0xC002_0000_0000_0005,
        0xC002_0000_0000_0006,
        0xC002_0000_0000_0007,
        0xC002_0000_0000_0008,
    ]))
}

fn depth_is_zero(snapshot: SemanticScopeDepthSnapshot) -> bool {
    snapshot.main_depth == 0
        && snapshot.overflow_depth == 0
        && snapshot.represented_depth == 0
        && !snapshot.active_overflow_scope_represented
}

fn type_rows_json(rows: &[SemanticTypeStatsSnapshot], row_count: usize) -> String {
    let mut out = String::from("[");
    let mut emitted = 0usize;
    for row in rows.iter().take(std::cmp::min(row_count, rows.len())) {
        if row.allocations == 0 && row.deallocations == 0 {
            continue;
        }
        if emitted != 0 {
            out.push(',');
        }
        emitted += 1;
        let _ = write!(
            out,
            concat!(
                "{{",
                "\"type_id\":{},",
                "\"module_id\":{},",
                "\"callsite\":{},",
                "\"allocations\":{},",
                "\"allocated_bytes\":{},",
                "\"deallocations\":{},",
                "\"cache_hits\":{},",
                "\"cache_inserts\":{},",
                "\"cache_bypasses\":{},",
                "\"observed_alloc_size\":{},",
                "\"observed_alloc_align\":{},",
                "\"observed_dealloc_size\":{},",
                "\"observed_dealloc_align\":{},",
                "\"policy_flags_seen\":{}",
                "}}"
            ),
            row.type_id,
            row.module_id,
            row.callsite,
            row.allocations,
            row.allocated_bytes,
            row.deallocations,
            row.cache_hits,
            row.cache_inserts,
            row.cache_bypasses,
            row.observed_alloc_size,
            row.observed_alloc_align,
            row.observed_dealloc_size,
            row.observed_dealloc_align,
            row.policy_flags_seen,
        );
    }
    out.push(']');
    out
}

fn main() {
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();

    // The source deliberately uses ordinary Vec/Box APIs. The companion
    // rustc_driver runner is solely responsible for inserting semantic scopes.
    semantic_auto_metadata_disable();
    let initial_depth = semantic_scope_depth_snapshot();
    assert!(
        depth_is_zero(initial_depth),
        "unexpected initial scope depth"
    );

    let seed = [
        PanicOnClone(0),
        PanicOnClone(1),
        PanicOnClone(2),
        PanicOnClone(3),
        PanicOnClone(4),
    ];
    let mut panic_observed = false;
    let mut restored_outer_depth = initial_depth;
    let value = outer_box_after_caught_panic(&seed, &mut panic_observed, &mut restored_outer_depth);
    let post_unwind_depth = semantic_scope_depth_snapshot();
    assert!(
        depth_is_zero(post_unwind_depth),
        "outer compiler-inserted semantic scope did not pop after returning: {:?}",
        post_unwind_depth
    );

    let post_unwind_address = (&*value as *const PostUnwindPayload) as usize;
    let checksum = value
        .0
        .iter()
        .fold(0u64, |acc, item| acc.rotate_left(7) ^ item);
    let after_alloc = semantic_stats_snapshot();
    drop(value);
    let after_drop = semantic_stats_snapshot();
    let final_depth = semantic_scope_depth_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    let mut type_rows = [SemanticTypeStatsSnapshot::empty(); 64];
    let type_row_count = semantic_type_stats_snapshot(&mut type_rows);

    assert_ne!(post_unwind_address, 0);
    assert_ne!(checksum, 0);
    assert_eq!(after_alloc.total_allocations, 1, "{:?}", after_alloc);
    assert_eq!(after_alloc.typed_allocations, 1, "{:?}", after_alloc);
    assert_eq!(after_alloc.fallback_allocations, 0, "{:?}", after_alloc);
    assert_eq!(after_drop.total_allocations, 1, "{:?}", after_drop);
    assert_eq!(after_drop.typed_allocations, 1, "{:?}", after_drop);
    assert_eq!(after_drop.typed_deallocations, 1, "{:?}", after_drop);
    assert_eq!(after_drop.fallback_allocations, 0, "{:?}", after_drop);
    assert_eq!(after_drop.fallback_deallocations, 0, "{:?}", after_drop);
    assert!(
        depth_is_zero(final_depth),
        "final scope depth leaked: {:?}",
        final_depth
    );
    assert_eq!(
        validation.recovery_identity_mismatches, 0,
        "{:?}",
        validation
    );
    assert_eq!(side_cache.corrupt_slots, 0, "{:?}", side_cache);

    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();
    let type_rows = type_rows_json(&type_rows, type_row_count);
    println!(
        concat!(
            "{{",
            "\"source\":\"rustc_driver_mir_semantic_scope_unwind_probe\",",
            "\"panic_observed\":{},",
            "\"initial_main_depth\":{},",
            "\"initial_overflow_depth\":{},",
            "\"initial_represented_depth\":{},",
            "\"post_unwind_main_depth\":{},",
            "\"post_unwind_overflow_depth\":{},",
            "\"post_unwind_represented_depth\":{},",
            "\"restored_outer_main_depth\":{},",
            "\"restored_outer_overflow_depth\":{},",
            "\"restored_outer_represented_depth\":{},",
            "\"final_main_depth\":{},",
            "\"final_overflow_depth\":{},",
            "\"final_represented_depth\":{},",
            "\"post_unwind_address\":{},",
            "\"post_unwind_checksum\":{},",
            "\"post_alloc_total_allocations\":{},",
            "\"post_alloc_typed_allocations\":{},",
            "\"post_alloc_fallback_allocations\":{},",
            "\"post_drop_typed_deallocations\":{},",
            "\"post_drop_fallback_deallocations\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_corrupt_slots\":{},",
            "\"type_rows\":{}",
            "}}"
        ),
        panic_observed,
        initial_depth.main_depth,
        initial_depth.overflow_depth,
        initial_depth.represented_depth,
        post_unwind_depth.main_depth,
        post_unwind_depth.overflow_depth,
        post_unwind_depth.represented_depth,
        restored_outer_depth.main_depth,
        restored_outer_depth.overflow_depth,
        restored_outer_depth.represented_depth,
        final_depth.main_depth,
        final_depth.overflow_depth,
        final_depth.represented_depth,
        post_unwind_address,
        checksum,
        after_alloc.total_allocations,
        after_alloc.typed_allocations,
        after_alloc.fallback_allocations,
        after_drop.typed_deallocations,
        after_drop.fallback_deallocations,
        validation.recovery_identity_mismatches,
        side_cache.corrupt_slots,
        type_rows,
    );
}
