#![cfg_attr(feature = "fixed_heap", allow(dead_code, unused_imports))]

use std::alloc::{alloc, dealloc, handle_alloc_error, Layout};
use std::fmt::Write as _;
use std::ptr::{read_volatile, write_volatile};

use unialloc::{
    semantic_auto_metadata_disable, semantic_metadata_validation_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_stats_recording_disable, semantic_type_stats_snapshot,
    type_isolation_side_cache_snapshot, SemanticTypeStatsSnapshot, UniAlloc,
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

#[inline(never)]
unsafe fn run_expect_positive_control() -> u64 {
    let layout = Layout::new::<[u64; 4]>()
        .align_to(64)
        .expect("valid typed Layout alignment");
    assert_eq!(layout.size(), 32);
    assert_eq!(layout.align(), 64);

    let ptr = alloc(layout);
    if ptr.is_null() {
        handle_alloc_error(layout);
    }
    let words = ptr as *mut u64;
    let mut checksum = 0xE8EC_7001_C002_u64;
    for index in 0..4usize {
        let value = 0xE8EC_0000_0000_0000_u64 ^ ((index as u64) << 17);
        write_volatile(words.add(index), value);
        checksum = checksum.rotate_left(7) ^ read_volatile(words.add(index));
    }
    dealloc(ptr, layout);
    checksum
}

#[inline(never)]
unsafe fn run_fallback_negative_control() -> (usize, usize, usize, u64) {
    // The source Result carries `[u64; 4]` Layout provenance, but align 3 is
    // invalid, so `unwrap_or_else` synthesizes a different `[u8; 37]` Layout.
    // A compiler pass must not attribute the selected Layout to `[u64; 4]`.
    let layout = Layout::new::<[u64; 4]>()
        .align_to(3)
        .unwrap_or_else(|_| Layout::new::<[u8; 37]>());
    assert_eq!(layout.size(), 37);
    assert_eq!(layout.align(), 1);

    let ptr = alloc(layout);
    if ptr.is_null() {
        handle_alloc_error(layout);
    }
    let mut checksum = 0xFA11_BACC_C002_u64;
    for index in 0..layout.size() {
        let value = (index as u8).wrapping_mul(29) ^ 0xA7;
        write_volatile(ptr.add(index), value);
        checksum = checksum.rotate_left(5) ^ u64::from(read_volatile(ptr.add(index)));
    }

    // This probe deliberately leaves one 37-byte allocation live. Deallocating
    // it from a distinct MIR callsite would introduce a second callsite-fallback
    // identity and obscure the provenance property under test.
    (ptr as usize, layout.size(), layout.align(), checksum)
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
                "\"deallocations\":{},",
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
            row.deallocations,
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

    // No manual metadata ABI is used. Typed rows must therefore come from the
    // rustc-driver allocator-call rewrite exercised by the companion runner.
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let positive_checksum = unsafe { run_expect_positive_control() };
    let (fallback_ptr, fallback_size, fallback_align, fallback_checksum) =
        unsafe { run_fallback_negative_control() };
    assert_ne!(positive_checksum, 0);
    assert_ne!(fallback_checksum, 0);
    assert_ne!(fallback_ptr, 0);

    let stats = semantic_stats_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    let mut rows = [SemanticTypeStatsSnapshot::empty(); 16];
    let row_count = semantic_type_stats_snapshot(&mut rows);

    assert!(stats.typed_allocations >= 2, "{:?}", stats);
    assert!(stats.typed_deallocations >= 1, "{:?}", stats);
    assert_eq!(stats.fallback_allocations, 0, "{:?}", stats);
    assert_eq!(
        validation.recovery_identity_mismatches, 0,
        "{:?}",
        validation
    );
    assert_eq!(side_cache.corrupt_slots, 0, "{:?}", side_cache);

    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();

    let type_rows = type_rows_json(&rows, row_count);
    println!(
        concat!(
            "{{",
            "\"source\":\"rustc_driver_mir_layout_fallback_provenance_probe\",",
            "\"manual_metadata_abi_calls\":false,",
            "\"positive_layout_size\":32,",
            "\"positive_layout_align\":64,",
            "\"fallback_layout_size\":{},",
            "\"fallback_layout_align\":{},",
            "\"fallback_pointer\":{},",
            "\"positive_checksum\":{},",
            "\"fallback_checksum\":{},",
            "\"typed_allocations\":{},",
            "\"typed_deallocations\":{},",
            "\"fallback_allocations\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_corrupt_slots\":{},",
            "\"type_rows\":{}",
            "}}"
        ),
        fallback_size,
        fallback_align,
        fallback_ptr,
        positive_checksum,
        fallback_checksum,
        stats.typed_allocations,
        stats.typed_deallocations,
        stats.fallback_allocations,
        validation.recovery_identity_mismatches,
        side_cache.corrupt_slots,
        type_rows,
    );
}
