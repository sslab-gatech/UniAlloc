#![cfg_attr(feature = "fixed_heap", allow(dead_code, unused_imports))]
use std::alloc::{alloc, dealloc, handle_alloc_error, Layout};
use std::collections::VecDeque;
use std::fmt::Write as _;
use std::ptr::{read_volatile, write_volatile};
use std::sync::Arc;
use std::thread;

use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_stats_snapshot, SemanticFallbackAttributionSnapshot,
    SemanticMetadataValidationSnapshot, SemanticTypeStatsSnapshot, UniAlloc,
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

const RUSTC_DRIVER_LOWERING_MODULE_ID: u64 = 0xC002_DA00_0000_0001;

#[derive(Clone, Copy, Debug)]
struct WorkerTrace {
    checksum: u64,
    start_fallback: SemanticFallbackAttributionSnapshot,
    end_fallback: SemanticFallbackAttributionSnapshot,
}

#[derive(Clone, Copy, Debug)]
struct WorkloadTrace {
    checksum: u64,
    start_fallback: SemanticFallbackAttributionSnapshot,
    after_same_thread_queue_fallback: SemanticFallbackAttributionSnapshot,
    after_vec_fill_fallback: SemanticFallbackAttributionSnapshot,
    after_arc_new_fallback: SemanticFallbackAttributionSnapshot,
    before_spawn_fallback: SemanticFallbackAttributionSnapshot,
    after_spawn_fallback: SemanticFallbackAttributionSnapshot,
    worker_start_fallback: SemanticFallbackAttributionSnapshot,
    worker_end_fallback: SemanticFallbackAttributionSnapshot,
    after_join_fallback: SemanticFallbackAttributionSnapshot,
}

#[derive(Clone, Copy, Debug)]
struct ScopedWorkloadTrace {
    checksum: u64,
    before_scope_fallback: SemanticFallbackAttributionSnapshot,
    before_scope_spawn_fallback: SemanticFallbackAttributionSnapshot,
    after_scope_spawn_fallback: SemanticFallbackAttributionSnapshot,
    worker_start_fallback: SemanticFallbackAttributionSnapshot,
    worker_end_fallback: SemanticFallbackAttributionSnapshot,
    after_scope_fallback: SemanticFallbackAttributionSnapshot,
}

fn empty_type_stats_row() -> SemanticTypeStatsSnapshot {
    SemanticTypeStatsSnapshot::empty()
}

fn lowered_type_rows_json(rows: &[SemanticTypeStatsSnapshot], row_count: usize) -> String {
    let mut out = String::from("[");
    let take = std::cmp::min(row_count, rows.len());
    let mut emitted = 0usize;
    for row in rows.iter().take(take).filter(|row| {
        row.module_id == RUSTC_DRIVER_LOWERING_MODULE_ID
            && (row.allocations > 0 || row.deallocations > 0)
    }) {
        if emitted > 0 {
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
            row.policy_flags_seen
        );
    }
    out.push(']');
    out
}

#[inline(never)]
unsafe fn same_thread_direct_allocator_probe(round: u64) -> u64 {
    // This direct std::alloc allocation lives entirely on the spawning thread.
    // The rustc_driver pass should still audit it as a direct allocator-call
    // candidate, but it must not inherit the cross-thread recovery placement
    // hint merely because the same MIR body later calls `thread::spawn`.
    let layout = Layout::new::<[u8; 24]>();
    let ptr = alloc(layout);
    if ptr.is_null() {
        handle_alloc_error(layout);
    }
    let mut checksum = 0_u64;
    for index in 0..layout.size() {
        let value = (round as u8).rotate_left((index % 7) as u32)
            ^ (index as u8).wrapping_mul(11)
            ^ 0xC7_u8;
        write_volatile(ptr.add(index), value);
        checksum = checksum.rotate_left(3) ^ (read_volatile(ptr.add(index)) as u64);
    }
    dealloc(ptr, layout);
    checksum
}

#[inline(never)]
fn cross_thread_escape_workload() -> WorkloadTrace {
    let start_fallback = semantic_fallback_attribution_snapshot();
    let direct_allocator_checksum = unsafe { same_thread_direct_allocator_probe(0xC002) };

    let same_thread_queue_sum = {
        let mut same_thread_queue = VecDeque::with_capacity(8);
        for item in 0..8_u64 {
            same_thread_queue.push_back(item ^ 0xC002_1EAF);
        }
        same_thread_queue
            .iter()
            .fold(0_u64, |acc, value| acc.wrapping_add(*value))
    };
    let after_same_thread_queue_fallback = semantic_fallback_attribution_snapshot();

    let mut values = Vec::with_capacity(32);
    for item in 0..32_u64 {
        values.push(item ^ 0xC002_C705_5EED);
    }
    let after_vec_fill_fallback = semantic_fallback_attribution_snapshot();

    let shared_values = Arc::new([
        0xA11C_u64, 0xA11D, 0xA11E, 0xA11F, 0xA120, 0xA121, 0xA122, 0xA123,
    ]);
    let after_arc_new_fallback = semantic_fallback_attribution_snapshot();
    let before_spawn_fallback = semantic_fallback_attribution_snapshot();

    let handle = thread::spawn(move || {
        let worker_start_fallback = semantic_fallback_attribution_snapshot();
        let vec_sum = values
            .iter()
            .fold(0_u64, |acc, value| acc.wrapping_add(*value));
        let checksum = shared_values
            .iter()
            .fold(vec_sum, |acc, value| acc.wrapping_add(*value));
        let worker_end_fallback = semantic_fallback_attribution_snapshot();
        WorkerTrace {
            checksum,
            start_fallback: worker_start_fallback,
            end_fallback: worker_end_fallback,
        }
    });
    let after_spawn_fallback = semantic_fallback_attribution_snapshot();
    let worker = handle.join().expect("worker should finish");
    let after_join_fallback = semantic_fallback_attribution_snapshot();
    WorkloadTrace {
        checksum: worker
            .checksum
            .wrapping_add(same_thread_queue_sum)
            .wrapping_add(direct_allocator_checksum),
        start_fallback,
        after_same_thread_queue_fallback,
        after_vec_fill_fallback,
        after_arc_new_fallback,
        before_spawn_fallback,
        after_spawn_fallback,
        worker_start_fallback: worker.start_fallback,
        worker_end_fallback: worker.end_fallback,
        after_join_fallback,
    }
}

#[inline(never)]
fn scoped_thread_no_spawn_negative_control() -> u64 {
    // Negative control: this MIR body contains a std scoped-thread constructor
    // and a heap-backed VecDeque, but it never calls `Scope::spawn`. Treating
    // `std::thread::scope` itself as a cross-thread escape would make the pass'
    // body-level fallback over-tag this non-escaping queue.
    let mut same_thread_queue = VecDeque::with_capacity(6);
    for item in 0..6_u64 {
        same_thread_queue.push_back(item ^ 0xC002_5C0F_E000);
    }

    let scoped_sum = thread::scope(|_| {
        same_thread_queue
            .iter()
            .fold(0_u64, |acc, value| acc.wrapping_add(*value))
    });

    scoped_sum ^ same_thread_queue.len() as u64
}

#[inline(never)]
fn scoped_thread_escape_workload() -> ScopedWorkloadTrace {
    let before_scope_fallback = semantic_fallback_attribution_snapshot();
    let mut scoped_trace = ScopedWorkloadTrace {
        checksum: 0,
        before_scope_fallback,
        before_scope_spawn_fallback: before_scope_fallback,
        after_scope_spawn_fallback: before_scope_fallback,
        worker_start_fallback: before_scope_fallback,
        worker_end_fallback: before_scope_fallback,
        after_scope_fallback: before_scope_fallback,
    };

    thread::scope(|scope| {
        // This allocation intentionally happens in the same MIR body that calls
        // `std::thread::Scope::spawn`.  The rustc_driver pass needs a
        // marker-specific scoped-spawn match here so heap-object metadata can
        // request cross-thread recovery when the object is moved into and
        // dropped by the scoped worker.
        let scoped_values = vec![
            0x5C0F_ED00_u64,
            0x5C0F_ED01,
            0x5C0F_ED02,
            0x5C0F_ED03,
            0x5C0F_ED04,
            0x5C0F_ED05,
        ];
        scoped_trace.before_scope_spawn_fallback = semantic_fallback_attribution_snapshot();

        let handle = scope.spawn(move || {
            let worker_start_fallback = semantic_fallback_attribution_snapshot();
            let checksum = scoped_values
                .into_iter()
                .fold(0x5C0F_EDA7_u64, |acc, value| acc.rotate_left(5) ^ value);
            let worker_end_fallback = semantic_fallback_attribution_snapshot();
            WorkerTrace {
                checksum,
                start_fallback: worker_start_fallback,
                end_fallback: worker_end_fallback,
            }
        });

        scoped_trace.after_scope_spawn_fallback = semantic_fallback_attribution_snapshot();
        let worker = handle.join().expect("scoped worker should finish");
        scoped_trace.worker_start_fallback = worker.start_fallback;
        scoped_trace.worker_end_fallback = worker.end_fallback;
        scoped_trace.checksum = worker.checksum;
    });

    scoped_trace.after_scope_fallback = semantic_fallback_attribution_snapshot();
    scoped_trace
}

fn fallback_allocation_components(snapshot: SemanticFallbackAttributionSnapshot) -> usize {
    snapshot
        .raw_alloc_no_metadata
        .saturating_add(snapshot.raw_realloc_no_metadata)
        .saturating_add(snapshot.realloc_recorded_old_metadata_new_allocations)
}

fn semantic_metadata_validation_json(snapshot: SemanticMetadataValidationSnapshot) -> String {
    format!(
        concat!(
            "{{",
            "\"recovery_identity_matches\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"last_mismatch_requested_type_id\":{},",
            "\"last_mismatch_recorded_type_id\":{},",
            "\"last_mismatch_requested_module_id\":{},",
            "\"last_mismatch_recorded_module_id\":{},",
            "\"last_mismatch_requested_callsite\":{},",
            "\"last_mismatch_recorded_callsite\":{}",
            "}}"
        ),
        snapshot.recovery_identity_matches,
        snapshot.recovery_identity_mismatches,
        snapshot.last_mismatch_requested_type_id,
        snapshot.last_mismatch_recorded_type_id,
        snapshot.last_mismatch_requested_module_id,
        snapshot.last_mismatch_recorded_module_id,
        snapshot.last_mismatch_requested_callsite,
        snapshot.last_mismatch_recorded_callsite
    )
}

fn main() {
    // Compile-only probe for rustc_driver MIR lowering: the source contains no
    // manual semantic metadata or allocation ABI calls.  The compiler pass must
    // recognize thread-spawn and scoped-spawn escapes in MIR bodies and tag
    // solved heap object scopes with the cross-thread recovery placement hint.
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let workload = cross_thread_escape_workload();
    let scope_no_spawn_checksum = scoped_thread_no_spawn_negative_control();
    let scoped_workload = scoped_thread_escape_workload();
    assert_ne!(workload.checksum, 0);
    assert_ne!(scope_no_spawn_checksum, 0);
    assert_ne!(scoped_workload.checksum, 0);

    let snap = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let mut rows = [empty_type_stats_row(); 64];
    let row_count = semantic_type_stats_snapshot(&mut rows);
    let lowered_rows = rows
        .iter()
        .take(std::cmp::min(row_count, rows.len()))
        .filter(|row| row.allocations > 0 && row.module_id == RUSTC_DRIVER_LOWERING_MODULE_ID)
        .count();
    let lowered_deallocation_rows = rows
        .iter()
        .take(std::cmp::min(row_count, rows.len()))
        .filter(|row| row.deallocations > 0 && row.module_id == RUSTC_DRIVER_LOWERING_MODULE_ID)
        .count();
    let type_rows_json = lowered_type_rows_json(&rows, row_count);
    let semantic_metadata_validation = semantic_metadata_validation_snapshot();
    let semantic_metadata_validation_json =
        semantic_metadata_validation_json(semantic_metadata_validation);

    assert!(
        snap.typed_allocations > 0,
        "cross-thread MIR hint rewrite did not produce typed allocations: {:?}",
        snap
    );
    assert!(
        snap.typed_deallocations > 0,
        "moved heap object was not recovered after worker-thread drop: {:?}",
        snap
    );
    assert!(
        lowered_rows > 0,
        "no typed allocation row used the rustc_driver semantic-scope module"
    );
    assert!(
        lowered_deallocation_rows > 0,
        "no typed deallocation row used the rustc_driver semantic-scope module"
    );

    println!(
        concat!(
            "{{",
            "\"source\":\"rustc_driver_mir_cross_thread_hint_probe\",",
            "\"type_id_basis\":\"compiler-assigned-allocation-site-object-type-id-rustc-driver-mir-semantic-scope\",",
            "\"compiler_site_id_stream_mode\":\"rustc-driver-mir-cross-thread-hint\",",
            "\"compiler_site_replay\":false,",
            "\"cross_thread_escape\":\"std::thread::spawn\",",
            "\"total_allocations\":{},",
            "\"typed_allocations\":{},",
            "\"fallback_allocations\":{},",
            "\"fallback_attribution_allocations\":{},",
            "\"fallback_attribution_raw_alloc_no_metadata\":{},",
            "\"fallback_attribution_raw_alloc_no_metadata_bytes\":{},",
            "\"fallback_attribution_raw_dealloc_no_metadata\":{},",
            "\"fallback_attribution_raw_realloc_no_metadata\":{},",
            "\"fallback_attribution_raw_realloc_no_metadata_bytes\":{},",
            "\"fallback_attribution_raw_realloc_moved_dealloc_no_metadata\":{},",
            "\"fallback_attribution_realloc_recorded_old_metadata_new_allocations\":{},",
            "\"fallback_attribution_realloc_recorded_old_metadata_new_allocation_bytes\":{},",
            "\"fallback_trace_start_raw_alloc_no_metadata\":{},",
            "\"fallback_trace_after_same_thread_queue_raw_alloc_no_metadata\":{},",
            "\"fallback_trace_after_vec_fill_raw_alloc_no_metadata\":{},",
            "\"fallback_trace_after_arc_new_raw_alloc_no_metadata\":{},",
            "\"fallback_trace_before_spawn_raw_alloc_no_metadata\":{},",
            "\"fallback_trace_after_spawn_raw_alloc_no_metadata\":{},",
            "\"fallback_trace_worker_start_raw_alloc_no_metadata\":{},",
            "\"fallback_trace_worker_end_raw_alloc_no_metadata\":{},",
            "\"fallback_trace_after_join_raw_alloc_no_metadata\":{},",
            "\"scoped_spawn_escape\":\"std::thread::Scope::spawn\",",
            "\"scoped_fallback_trace_before_scope_raw_alloc_no_metadata\":{},",
            "\"scoped_fallback_trace_before_spawn_raw_alloc_no_metadata\":{},",
            "\"scoped_fallback_trace_after_spawn_raw_alloc_no_metadata\":{},",
            "\"scoped_fallback_trace_worker_start_raw_alloc_no_metadata\":{},",
            "\"scoped_fallback_trace_worker_end_raw_alloc_no_metadata\":{},",
            "\"scoped_fallback_trace_after_scope_raw_alloc_no_metadata\":{},",
            "\"fallback_trace_start_raw_alloc_no_metadata_bytes\":{},",
            "\"fallback_trace_after_same_thread_queue_raw_alloc_no_metadata_bytes\":{},",
            "\"fallback_trace_after_vec_fill_raw_alloc_no_metadata_bytes\":{},",
            "\"fallback_trace_after_arc_new_raw_alloc_no_metadata_bytes\":{},",
            "\"fallback_trace_before_spawn_raw_alloc_no_metadata_bytes\":{},",
            "\"fallback_trace_after_spawn_raw_alloc_no_metadata_bytes\":{},",
            "\"fallback_trace_worker_start_raw_alloc_no_metadata_bytes\":{},",
            "\"fallback_trace_worker_end_raw_alloc_no_metadata_bytes\":{},",
            "\"fallback_trace_after_join_raw_alloc_no_metadata_bytes\":{},",
            "\"scoped_fallback_trace_before_scope_raw_alloc_no_metadata_bytes\":{},",
            "\"scoped_fallback_trace_before_spawn_raw_alloc_no_metadata_bytes\":{},",
            "\"scoped_fallback_trace_after_spawn_raw_alloc_no_metadata_bytes\":{},",
            "\"scoped_fallback_trace_worker_start_raw_alloc_no_metadata_bytes\":{},",
            "\"scoped_fallback_trace_worker_end_raw_alloc_no_metadata_bytes\":{},",
            "\"scoped_fallback_trace_after_scope_raw_alloc_no_metadata_bytes\":{},",
            "\"typed_deallocations\":{},",
            "\"policy_flags_seen\":{},",
            "\"lowered_rows\":{},",
            "\"lowered_deallocation_rows\":{},",
            "\"semantic_metadata_validation\":{},",
            "\"recovery_identity_matches\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"last_mismatch_requested_type_id\":{},",
            "\"last_mismatch_recorded_type_id\":{},",
            "\"last_mismatch_requested_module_id\":{},",
            "\"last_mismatch_recorded_module_id\":{},",
            "\"last_mismatch_requested_callsite\":{},",
            "\"last_mismatch_recorded_callsite\":{},",
            "\"checksum\":{},",
            "\"type_rows\":{}",
            "}}"
        ),
        snap.total_allocations,
        snap.typed_allocations,
        snap.fallback_allocations,
        fallback_allocation_components(fallback),
        fallback.raw_alloc_no_metadata,
        fallback.raw_alloc_no_metadata_bytes,
        fallback.raw_dealloc_no_metadata,
        fallback.raw_realloc_no_metadata,
        fallback.raw_realloc_no_metadata_bytes,
        fallback.raw_realloc_moved_dealloc_no_metadata,
        fallback.realloc_recorded_old_metadata_new_allocations,
        fallback.realloc_recorded_old_metadata_new_allocation_bytes,
        workload.start_fallback.raw_alloc_no_metadata,
        workload
            .after_same_thread_queue_fallback
            .raw_alloc_no_metadata,
        workload.after_vec_fill_fallback.raw_alloc_no_metadata,
        workload.after_arc_new_fallback.raw_alloc_no_metadata,
        workload.before_spawn_fallback.raw_alloc_no_metadata,
        workload.after_spawn_fallback.raw_alloc_no_metadata,
        workload.worker_start_fallback.raw_alloc_no_metadata,
        workload.worker_end_fallback.raw_alloc_no_metadata,
        workload.after_join_fallback.raw_alloc_no_metadata,
        scoped_workload.before_scope_fallback.raw_alloc_no_metadata,
        scoped_workload
            .before_scope_spawn_fallback
            .raw_alloc_no_metadata,
        scoped_workload.after_scope_spawn_fallback.raw_alloc_no_metadata,
        scoped_workload.worker_start_fallback.raw_alloc_no_metadata,
        scoped_workload.worker_end_fallback.raw_alloc_no_metadata,
        scoped_workload.after_scope_fallback.raw_alloc_no_metadata,
        workload.start_fallback.raw_alloc_no_metadata_bytes,
        workload
            .after_same_thread_queue_fallback
            .raw_alloc_no_metadata_bytes,
        workload.after_vec_fill_fallback.raw_alloc_no_metadata_bytes,
        workload.after_arc_new_fallback.raw_alloc_no_metadata_bytes,
        workload.before_spawn_fallback.raw_alloc_no_metadata_bytes,
        workload.after_spawn_fallback.raw_alloc_no_metadata_bytes,
        workload.worker_start_fallback.raw_alloc_no_metadata_bytes,
        workload.worker_end_fallback.raw_alloc_no_metadata_bytes,
        workload.after_join_fallback.raw_alloc_no_metadata_bytes,
        scoped_workload.before_scope_fallback.raw_alloc_no_metadata_bytes,
        scoped_workload
            .before_scope_spawn_fallback
            .raw_alloc_no_metadata_bytes,
        scoped_workload
            .after_scope_spawn_fallback
            .raw_alloc_no_metadata_bytes,
        scoped_workload.worker_start_fallback.raw_alloc_no_metadata_bytes,
        scoped_workload.worker_end_fallback.raw_alloc_no_metadata_bytes,
        scoped_workload.after_scope_fallback.raw_alloc_no_metadata_bytes,
        snap.typed_deallocations,
        snap.policy_flags_seen,
        lowered_rows,
        lowered_deallocation_rows,
        semantic_metadata_validation_json,
        semantic_metadata_validation.recovery_identity_matches,
        semantic_metadata_validation.recovery_identity_mismatches,
        semantic_metadata_validation.last_mismatch_requested_type_id,
        semantic_metadata_validation.last_mismatch_recorded_type_id,
        semantic_metadata_validation.last_mismatch_requested_module_id,
        semantic_metadata_validation.last_mismatch_recorded_module_id,
        semantic_metadata_validation.last_mismatch_requested_callsite,
        semantic_metadata_validation.last_mismatch_recorded_callsite,
        workload.checksum ^ scope_no_spawn_checksum ^ scoped_workload.checksum,
        type_rows_json
    );
}
