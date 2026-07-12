#![cfg_attr(feature = "fixed_heap", allow(dead_code, unused_imports))]

use std::fmt::Write as _;
use std::mem::{align_of, size_of};
use std::thread;

use unialloc::{
    semantic_auto_metadata_disable, semantic_metadata_validation_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_stats_recording_disable, semantic_type_stats_snapshot,
    type_isolation_side_cache_snapshot, SemanticTypeStatsSnapshot, TypeIsolationSideCacheSnapshot,
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

const OBJECTS: usize = 4;

#[repr(C)]
struct ProducerPayload([u64; 8]);

#[repr(C)]
struct ConsumerPayload([u64; 8]);

#[derive(Clone, Copy, Debug)]
struct WorkerEvidence {
    producer_addresses: [usize; OBJECTS],
    consumer_addresses: [usize; OBJECTS],
    recovered_producer_addresses: [usize; OBJECTS],
    side_cache: TypeIsolationSideCacheSnapshot,
}

#[inline(never)]
fn producer_box(seed: u64) -> Box<ProducerPayload> {
    Box::new(ProducerPayload([
        seed,
        seed ^ 0x1111_1111_1111_1111,
        seed ^ 0x2222_2222_2222_2222,
        seed ^ 0x3333_3333_3333_3333,
        seed ^ 0x4444_4444_4444_4444,
        seed ^ 0x5555_5555_5555_5555,
        seed ^ 0x6666_6666_6666_6666,
        seed ^ 0x7777_7777_7777_7777,
    ]))
}

#[inline(never)]
fn consumer_box(seed: u64) -> Box<ConsumerPayload> {
    Box::new(ConsumerPayload([
        seed,
        seed ^ 0x8888_8888_8888_8888,
        seed ^ 0x9999_9999_9999_9999,
        seed ^ 0xAAAA_AAAA_AAAA_AAAA,
        seed ^ 0xBBBB_BBBB_BBBB_BBBB,
        seed ^ 0xCCCC_CCCC_CCCC_CCCC,
        seed ^ 0xDDDD_DDDD_DDDD_DDDD,
        seed ^ 0xEEEE_EEEE_EEEE_EEEE,
    ]))
}

#[inline]
fn address_of<T>(value: &Box<T>) -> usize {
    (&**value as *const T) as usize
}

fn addresses_json(values: &[usize; OBJECTS]) -> String {
    let mut out = String::from("[");
    for (index, value) in values.iter().enumerate() {
        if index != 0 {
            out.push(',');
        }
        let _ = write!(out, "{}", value);
    }
    out.push(']');
    out
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

    assert_eq!(size_of::<ProducerPayload>(), size_of::<ConsumerPayload>());
    assert_eq!(align_of::<ProducerPayload>(), align_of::<ConsumerPayload>());
    assert_eq!(size_of::<ProducerPayload>(), 64);

    // The executable contains no manual semantic metadata or allocation ABI
    // calls. Runtime typed activity and cache isolation therefore depend on the
    // rustc_driver MIR provider override used by the companion runner.
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let p0 = producer_box(0xA110_0000);
    let p1 = producer_box(0xA110_0001);
    let p2 = producer_box(0xA110_0002);
    let p3 = producer_box(0xA110_0003);
    let produced = [
        address_of(&p0),
        address_of(&p1),
        address_of(&p2),
        address_of(&p3),
    ];

    let worker = thread::spawn(move || {
        // All producer objects are freed on this one foreign worker. The pass'
        // cross-thread placement metadata makes recovery records process-visible,
        // while the recovered producer identity selects the worker's typed cache.
        drop(p0);
        drop(p1);
        drop(p2);
        drop(p3);

        let c0 = consumer_box(0xC003_0000);
        let c1 = consumer_box(0xC003_0001);
        let c2 = consumer_box(0xC003_0002);
        let c3 = consumer_box(0xC003_0003);
        let consumer_addresses = [
            address_of(&c0),
            address_of(&c1),
            address_of(&c2),
            address_of(&c3),
        ];
        for address in consumer_addresses {
            assert!(
                !produced.contains(&address),
                "a distinct compiler-derived type identity reused producer storage"
            );
        }
        drop(c0);
        drop(c1);
        drop(c2);
        drop(c3);

        let r0 = producer_box(0xA110_1000);
        let r1 = producer_box(0xA110_1001);
        let r2 = producer_box(0xA110_1002);
        let r3 = producer_box(0xA110_1003);
        let recovered_producer_addresses = [
            address_of(&r0),
            address_of(&r1),
            address_of(&r2),
            address_of(&r3),
        ];
        for (index, address) in recovered_producer_addresses.iter().enumerate() {
            assert!(
                produced.contains(address),
                "the recorded producer identity did not reuse its own worker-cached storage"
            );
            assert!(
                !recovered_producer_addresses[..index].contains(address),
                "the producer cache returned one address more than once"
            );
        }
        drop(r0);
        drop(r1);
        drop(r2);
        drop(r3);

        let side_cache = type_isolation_side_cache_snapshot();
        assert_eq!(
            side_cache.corrupt_slots, 0,
            "type-isolation side cache reported corrupt slots"
        );
        WorkerEvidence {
            producer_addresses: produced,
            consumer_addresses,
            recovered_producer_addresses,
            side_cache,
        }
    });

    let evidence = worker
        .join()
        .expect("type-isolation security worker should finish");
    let stats = semantic_stats_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let mut rows = [SemanticTypeStatsSnapshot::empty(); 256];
    let row_count = semantic_type_stats_snapshot(&mut rows);

    assert!(stats.typed_allocations >= OBJECTS * 3, "{:?}", stats);
    assert!(stats.typed_deallocations >= OBJECTS * 3, "{:?}", stats);
    assert!(stats.typed_cache_hits >= OBJECTS, "{:?}", stats);
    assert!(stats.typed_cache_inserts >= OBJECTS * 2, "{:?}", stats);
    assert_eq!(stats.semantic_type_stats_dropped_events, 0, "{:?}", stats);
    // The foreign Box drop has no matching active compiler scope after drop
    // glue lowering, so recovery correctly uses the allocation-side record
    // without incrementing the exact requested-vs-recorded match counter. The
    // address assertions above prove that the recovered identity, rather than
    // unknown fallback metadata, selected the worker's typed cache.
    assert_eq!(
        validation.recovery_identity_mismatches, 0,
        "{:?}",
        validation
    );

    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();

    let producer_addresses = addresses_json(&evidence.producer_addresses);
    let consumer_addresses = addresses_json(&evidence.consumer_addresses);
    let recovered_producer_addresses = addresses_json(&evidence.recovered_producer_addresses);
    let type_rows = type_rows_json(&rows, row_count);
    println!(
        concat!(
            "{{",
            "\"source\":\"rustc_driver_mir_type_isolation_security_probe\",",
            "\"identity_basis\":\"compiler-derived-rust-object-type\",",
            "\"same_layout_bytes\":{},",
            "\"same_layout_align\":{},",
            "\"objects\":{},",
            "\"wrong_type_reuse_blocked\":true,",
            "\"producer_reuse_complete\":true,",
            "\"producer_addresses\":{},",
            "\"consumer_addresses\":{},",
            "\"recovered_producer_addresses\":{},",
            "\"total_allocations\":{},",
            "\"typed_allocations\":{},",
            "\"fallback_allocations\":{},",
            "\"typed_deallocations\":{},",
            "\"fallback_deallocations\":{},",
            "\"typed_cache_hits\":{},",
            "\"typed_cache_inserts\":{},",
            "\"typed_cache_bypasses\":{},",
            "\"semantic_type_stats_dropped_events\":{},",
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
        OBJECTS,
        producer_addresses,
        consumer_addresses,
        recovered_producer_addresses,
        stats.total_allocations,
        stats.typed_allocations,
        stats.fallback_allocations,
        stats.typed_deallocations,
        stats.fallback_deallocations,
        stats.typed_cache_hits,
        stats.typed_cache_inserts,
        stats.typed_cache_bypasses,
        stats.semantic_type_stats_dropped_events,
        validation.recovery_identity_matches,
        validation.recovery_identity_mismatches,
        evidence.side_cache.inline_occupied,
        evidence.side_cache.occupied_slots,
        evidence.side_cache.occupied_entries,
        evidence.side_cache.corrupt_slots,
        type_rows,
    );
}
