use unialloc::alloc_api::{
    __unialloc_alloc_with_metadata, __unialloc_dealloc_with_metadata, semantic_stats_reset,
    semantic_stats_snapshot, semantic_type_stats_snapshot, SemanticTypeStatsSnapshot,
    FLAG_METADATA_SEGREGATED, FLAG_TYPE_ISOLATED,
};
use unialloc::UniAlloc;

#[global_allocator]
static A: UniAlloc = UniAlloc;

#[derive(Clone, Copy)]
struct AllocationSite {
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
    size: usize,
    align: usize,
    source_span: &'static str,
    mir_function: &'static str,
    size_operand: &'static str,
    align_operand: &'static str,
}

const SITES: &[AllocationSite] = &[
    AllocationSite {
        type_id: 18203639043610253563,
        module_id: 13835860698770440193,
        flags: 3,
        callsite: 6451400816569166891,
        size: 64,
        align: 8,
        source_span: "$RUST_SRC/library/alloc/src/macros.rs:52:13: 52:47",
        mir_function: "binary_heap::bench_peek_mut_deref_mut",
        size_operand: "move _6",
        align_operand: "move _7",
    },
    AllocationSite {
        type_id: 5527895896097396666,
        module_id: 13835860698770440193,
        flags: 3,
        callsite: 5557923747705685034,
        size: 80,
        align: 8,
        source_span: "$RUST_SRC/library/alloc/src/macros.rs:52:13: 52:47",
        mir_function: "slice::starts_with_single_element",
        size_operand: "move _5",
        align_operand: "move _6",
    },
    AllocationSite {
        type_id: 5781275959105037166,
        module_id: 13835860698770440193,
        flags: 3,
        callsite: 8412699192074617950,
        size: 96,
        align: 8,
        source_span: "$RUST_SRC/library/alloc/src/macros.rs:52:13: 52:47",
        mir_function: "slice::ends_with_single_element",
        size_operand: "move _5",
        align_operand: "move _6",
    },
    AllocationSite {
        type_id: 9244992202293328724,
        module_id: 13835860698770440193,
        flags: 3,
        callsite: 12843182805126166404,
        size: 112,
        align: 8,
        source_span: "$RUST_SRC/library/alloc/src/macros.rs:52:13: 52:47",
        mir_function: "str::bench_join",
        size_operand: "move _7",
        align_operand: "move _8",
    },
];

fn empty_type_stats_row() -> SemanticTypeStatsSnapshot {
    SemanticTypeStatsSnapshot::empty()
}

fn run_site(site: AllocationSite) {
    assert!(
        site.flags & (FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED) != 0,
        "rustc_driver site should request type-aware metadata flags"
    );
    unsafe {
        let ptr = __unialloc_alloc_with_metadata(
            site.size,
            site.align,
            site.type_id,
            site.module_id,
            site.flags,
            site.callsite,
        );
        assert!(!ptr.is_null(), "direct allocator ABI probe returned null");
        std::ptr::write_bytes(ptr, 0xA5, site.size);
        assert!(
            __unialloc_dealloc_with_metadata(
                ptr,
                site.size,
                site.align,
                site.type_id,
                site.module_id,
                site.flags,
                site.callsite,
            ),
            "direct allocator ABI probe dealloc failed"
        );
    }
}

fn emit_events() {
    let snap = semantic_stats_snapshot();
    let mut rows = [empty_type_stats_row(); 32];
    let row_count = semantic_type_stats_snapshot(&mut rows);
    for row in rows.iter().take(std::cmp::min(row_count, rows.len())) {
        if row.allocations == 0 {
            continue;
        }
        println!(
            concat!(
                "{{",
                "\"source\":\"rustc_driver_direct_allocator_abi_probe\",",
                "\"type_id_basis\":\"compiler-assigned-allocation-site-object-type-id\",",
                "\"compiler_site_replay\":false,",
                "\"compiler_site_id_stream_mode\":\"rustc-driver-direct-allocator-abi\",",
                "\"event\":\"typed_allocation_site\",",
                "\"typed\":true,",
                "\"type_id\":{},",
                "\"module_id\":{},",
                "\"callsite\":{},",
                "\"count\":{},",
                "\"bytes\":{},",
                "\"deallocations\":{},",
                "\"cache_hits\":{},",
                "\"cache_inserts\":{},",
                "\"cache_bypasses\":{},",
                "\"policy_flags_seen\":{},",
                "\"site_count\":{}",
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
            row.policy_flags_seen,
            SITES.len()
        );
    }
    println!(
        concat!(
            "{{",
            "\"source\":\"rustc_driver_direct_allocator_abi_probe\",",
            "\"type_id_basis\":\"compiler-assigned-allocation-site-object-type-id\",",
            "\"compiler_site_replay\":false,",
            "\"compiler_site_id_stream_mode\":\"rustc-driver-direct-allocator-abi\",",
            "\"event\":\"typed_allocations\",",
            "\"typed\":true,",
            "\"type_id\":\"aggregate\",",
            "\"count\":{},",
            "\"bytes\":{},",
            "\"deallocations\":{},",
            "\"fallback_allocations\":{},",
            "\"site_count\":{}",
            "}}"
        ),
        snap.typed_allocations,
        snap.typed_allocated_bytes,
        snap.typed_deallocations,
        snap.fallback_allocations,
        SITES.len()
    );
}

fn assert_probe_is_real() {
    let snap = semantic_stats_snapshot();
    assert_eq!(
        snap.typed_allocations,
        SITES.len(),
        "one typed allocation should be observed for each rustc_driver direct allocator row"
    );
    assert_eq!(
        snap.typed_deallocations,
        SITES.len(),
        "direct allocator ABI probe should deallocate every typed allocation"
    );
    assert_eq!(
        snap.fallback_allocations, 0,
        "direct allocator ABI probe should not fall back to untyped allocations"
    );
}

fn main() {
    for site in SITES {
        println!(
            concat!(
                "{{",
                "\"source\":\"rustc_driver_direct_allocator_abi_probe\",",
                "\"event\":\"input_site\",",
                "\"type_id\":{},",
                "\"callsite\":{},",
                "\"source_span\":{:?},",
                "\"mir_function\":{:?},",
                "\"size_operand\":{:?},",
                "\"align_operand\":{:?}",
                "}}"
            ),
            site.type_id,
            site.callsite,
            site.source_span,
            site.mir_function,
            site.size_operand,
            site.align_operand
        );
    }
    semantic_stats_reset();
    for site in SITES {
        run_site(*site);
    }
    assert_probe_is_real();
    emit_events();
}
