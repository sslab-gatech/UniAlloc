#![cfg_attr(feature = "fixed_heap", allow(dead_code, unused_imports))]

use std::fmt::Write as _;
use std::mem::{align_of, size_of};
use std::thread;

use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable, semantic_stats_reset,
    semantic_stats_snapshot, semantic_type_stats_recording_disable, semantic_type_stats_snapshot,
    type_isolation_side_cache_snapshot, SemanticFallbackAttributionSnapshot, SemanticStatsSnapshot,
    SemanticTypeStatsSnapshot, TypeIsolationSideCacheSnapshot, UniAlloc,
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

const FINAL_LEN: usize = 8;

#[derive(Clone, Copy)]
#[repr(C)]
struct ProducerPayload([u64; 8]);

#[derive(Clone, Copy)]
#[repr(C)]
struct ConsumerPayload([u64; 8]);

#[derive(Clone, Copy, Debug)]
struct WorkerEvidence {
    producer_final_buffer: usize,
    consumer_buffer: usize,
    recovered_producer_buffer: usize,
    producer_capacity: usize,
    producer_checksum: u64,
    side_cache: TypeIsolationSideCacheSnapshot,
}

#[inline(never)]
fn producer_payload(seed: u64) -> ProducerPayload {
    ProducerPayload([
        seed,
        seed ^ 0x1111_1111_1111_1111,
        seed ^ 0x2222_2222_2222_2222,
        seed ^ 0x3333_3333_3333_3333,
        seed ^ 0x4444_4444_4444_4444,
        seed ^ 0x5555_5555_5555_5555,
        seed ^ 0x6666_6666_6666_6666,
        seed ^ 0x7777_7777_7777_7777,
    ])
}

#[inline(never)]
fn consumer_payload(seed: u64) -> ConsumerPayload {
    ConsumerPayload([
        seed,
        seed ^ 0x8888_8888_8888_8888,
        seed ^ 0x9999_9999_9999_9999,
        seed ^ 0xAAAA_AAAA_AAAA_AAAA,
        seed ^ 0xBBBB_BBBB_BBBB_BBBB,
        seed ^ 0xCCCC_CCCC_CCCC_CCCC,
        seed ^ 0xDDDD_DDDD_DDDD_DDDD,
        seed ^ 0xEEEE_EEEE_EEEE_EEEE,
    ])
}

#[inline(never)]
fn producer_checksum(values: &[ProducerPayload]) -> u64 {
    values.iter().fold(0xC002_0EAA_110C_u64, |outer, value| {
        value
            .0
            .iter()
            .fold(outer.rotate_left(7), |acc, word| acc.rotate_left(3) ^ word)
    })
}

#[inline(never)]
fn consume_producer_vec(
    values: Vec<ProducerPayload>,
    expected_buffer: usize,
    expected_capacity: usize,
    expected_checksum: u64,
) -> u64 {
    assert_eq!(producer_checksum(&values), expected_checksum);
    assert_eq!(values.as_ptr() as usize, expected_buffer);
    assert_eq!(values.capacity(), expected_capacity);
    // `values` is intentionally dropped by this function's MIR Drop
    // terminator, so the compiler audit can bind the deallocation side of the
    // final cross-thread Vec buffer lifecycle.
    expected_checksum
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

fn assert_one_typed_reallocation(
    before_stats: SemanticStatsSnapshot,
    after_stats: SemanticStatsSnapshot,
    before_fallback: SemanticFallbackAttributionSnapshot,
    after_fallback: SemanticFallbackAttributionSnapshot,
) {
    assert_eq!(
        after_stats
            .typed_allocations
            .saturating_sub(before_stats.typed_allocations),
        1,
        "Vec capacity growth must record one typed replacement allocation"
    );
    assert!(
        after_stats
            .typed_deallocations
            .saturating_sub(before_stats.typed_deallocations)
            <= 1,
        "Vec capacity growth may either reuse in place or release one old buffer"
    );
    assert_eq!(
        after_fallback
            .raw_realloc_no_metadata
            .saturating_sub(before_fallback.raw_realloc_no_metadata),
        0,
        "Vec capacity growth must not use an untyped raw realloc"
    );
    assert_eq!(
        after_fallback
            .realloc_recorded_old_metadata_new_allocations
            .saturating_sub(before_fallback.realloc_recorded_old_metadata_new_allocations),
        0,
        "Vec capacity growth must not lose the current compiler identity"
    );
}

fn main() {
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();

    assert_eq!(size_of::<ProducerPayload>(), size_of::<ConsumerPayload>());
    assert_eq!(align_of::<ProducerPayload>(), align_of::<ConsumerPayload>());
    assert_eq!(size_of::<ProducerPayload>(), 64);

    // No manual metadata or allocator ABI calls are used below. The companion
    // rustc_driver wrapper must create the Vec object identities and scopes.
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let mut producer = Vec::<ProducerPayload>::with_capacity(1);
    producer.push(producer_payload(0xA110_0000));
    let initial_capacity = producer.capacity();
    let initial_buffer = producer.as_ptr() as usize;
    assert_eq!(initial_capacity, 1);

    let before_growth_stats = semantic_stats_snapshot();
    let before_growth_fallback = semantic_fallback_attribution_snapshot();
    producer.reserve_exact(FINAL_LEN - producer.len());
    let after_growth_stats = semantic_stats_snapshot();
    let after_growth_fallback = semantic_fallback_attribution_snapshot();
    assert_one_typed_reallocation(
        before_growth_stats,
        after_growth_stats,
        before_growth_fallback,
        after_growth_fallback,
    );
    let growth_typed_allocations = after_growth_stats
        .typed_allocations
        .saturating_sub(before_growth_stats.typed_allocations);
    let growth_typed_deallocations = after_growth_stats
        .typed_deallocations
        .saturating_sub(before_growth_stats.typed_deallocations);
    let growth_raw_realloc_no_metadata = after_growth_fallback
        .raw_realloc_no_metadata
        .saturating_sub(before_growth_fallback.raw_realloc_no_metadata);
    let growth_recorded_old_metadata_fallback = after_growth_fallback
        .realloc_recorded_old_metadata_new_allocations
        .saturating_sub(before_growth_fallback.realloc_recorded_old_metadata_new_allocations);

    let final_capacity = producer.capacity();
    let final_buffer = producer.as_ptr() as usize;
    assert!(final_capacity >= FINAL_LEN);
    assert!(final_capacity > initial_capacity);
    let growth_moved_buffer = final_buffer != initial_buffer;

    for index in 1..FINAL_LEN {
        producer.push(producer_payload(0xA110_0000 + index as u64));
    }
    assert_eq!(producer.len(), FINAL_LEN);
    assert_eq!(producer.capacity(), final_capacity);
    assert_eq!(producer.as_ptr() as usize, final_buffer);
    let expected_checksum = producer_checksum(&producer);

    let worker = thread::spawn(move || {
        let dropped_checksum =
            consume_producer_vec(producer, final_buffer, final_capacity, expected_checksum);
        assert_eq!(dropped_checksum, expected_checksum);

        let consumer_buffer = {
            let mut consumer = Vec::<ConsumerPayload>::with_capacity(final_capacity);
            let consumer_buffer = consumer.as_ptr() as usize;
            assert_ne!(
                consumer_buffer, final_buffer,
                "same-layout Vec with a distinct compiler type reused producer storage"
            );
            for index in 0..FINAL_LEN {
                consumer.push(consumer_payload(0xC003_0000 + index as u64));
            }
            assert_eq!(consumer.capacity(), final_capacity);
            // Implicit scope-end Drop is required for compiler audit coverage.
            consumer_buffer
        };

        let (recovered_producer_buffer, producer_checksum) = {
            let mut recovered = Vec::<ProducerPayload>::with_capacity(final_capacity);
            let recovered_producer_buffer = recovered.as_ptr() as usize;
            assert_eq!(
                recovered_producer_buffer, final_buffer,
                "producer Vec identity did not recover its cross-thread cached buffer"
            );
            for index in 0..FINAL_LEN {
                recovered.push(producer_payload(0xA110_1000 + index as u64));
            }
            assert_eq!(recovered.capacity(), final_capacity);
            let producer_checksum = producer_checksum(&recovered);
            // Implicit scope-end Drop keeps the second producer lifecycle in
            // the same compiler-derived identity class.
            (recovered_producer_buffer, producer_checksum)
        };

        let side_cache = type_isolation_side_cache_snapshot();
        assert_eq!(
            side_cache.corrupt_slots, 0,
            "type-isolation side cache reported corrupt slots"
        );
        WorkerEvidence {
            producer_final_buffer: final_buffer,
            consumer_buffer,
            recovered_producer_buffer,
            producer_capacity: final_capacity,
            producer_checksum,
            side_cache,
        }
    });

    let evidence = worker
        .join()
        .expect("Vec realloc identity worker should finish");
    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let mut rows = [SemanticTypeStatsSnapshot::empty(); 256];
    let row_count = semantic_type_stats_snapshot(&mut rows);

    assert!(stats.typed_allocations >= 4, "{:?}", stats);
    assert!(stats.typed_deallocations >= 3, "{:?}", stats);
    assert!(stats.typed_cache_hits >= 1, "{:?}", stats);
    assert_eq!(stats.semantic_type_stats_dropped_events, 0, "{:?}", stats);
    assert_eq!(
        validation.recovery_identity_mismatches, 0,
        "{:?}",
        validation
    );

    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();

    let type_rows = type_rows_json(&rows, row_count);
    println!(
        concat!(
            "{{",
            "\"source\":\"rustc_driver_mir_vec_realloc_identity_probe\",",
            "\"identity_basis\":\"compiler-derived-rust-vec-object-type\",",
            "\"same_layout_element_bytes\":{},",
            "\"same_layout_element_align\":{},",
            "\"initial_capacity\":{},",
            "\"final_capacity\":{},",
            "\"capacity_growth_forced\":true,",
            "\"growth_moved_buffer\":{},",
            "\"growth_typed_allocations\":{},",
            "\"growth_typed_deallocations\":{},",
            "\"growth_raw_realloc_no_metadata\":{},",
            "\"growth_recorded_old_metadata_fallback\":{},",
            "\"wrong_type_reuse_blocked\":true,",
            "\"producer_buffer_recovered\":true,",
            "\"initial_buffer\":{},",
            "\"producer_final_buffer\":{},",
            "\"consumer_buffer\":{},",
            "\"recovered_producer_buffer\":{},",
            "\"producer_checksum\":{},",
            "\"typed_allocations\":{},",
            "\"typed_deallocations\":{},",
            "\"typed_cache_hits\":{},",
            "\"typed_cache_inserts\":{},",
            "\"typed_cache_bypasses\":{},",
            "\"semantic_type_stats_dropped_events\":{},",
            "\"raw_realloc_no_metadata\":{},",
            "\"realloc_recorded_old_metadata_new_allocations\":{},",
            "\"recovery_identity_matches\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_inline_occupied\":{},",
            "\"side_cache_occupied_slots\":{},",
            "\"side_cache_occupied_entries\":{},",
            "\"side_cache_corrupt_slots\":{},",
            "\"type_rows\":{}",
            "}}"
        ),
        size_of::<ProducerPayload>(),
        align_of::<ProducerPayload>(),
        initial_capacity,
        evidence.producer_capacity,
        growth_moved_buffer,
        growth_typed_allocations,
        growth_typed_deallocations,
        growth_raw_realloc_no_metadata,
        growth_recorded_old_metadata_fallback,
        initial_buffer,
        evidence.producer_final_buffer,
        evidence.consumer_buffer,
        evidence.recovered_producer_buffer,
        evidence.producer_checksum,
        stats.typed_allocations,
        stats.typed_deallocations,
        stats.typed_cache_hits,
        stats.typed_cache_inserts,
        stats.typed_cache_bypasses,
        stats.semantic_type_stats_dropped_events,
        fallback.raw_realloc_no_metadata,
        fallback.realloc_recorded_old_metadata_new_allocations,
        validation.recovery_identity_matches,
        validation.recovery_identity_mismatches,
        evidence.side_cache.inline_occupied,
        evidence.side_cache.occupied_slots,
        evidence.side_cache.occupied_entries,
        evidence.side_cache.corrupt_slots,
        type_rows,
    );
}
