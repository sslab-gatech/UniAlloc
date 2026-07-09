use std::collections::{BTreeMap, VecDeque};
use std::mem::{align_of, size_of};

use unialloc::alloc_api::{
    __unialloc_alloc_with_metadata, __unialloc_dealloc_with_metadata,
    __unialloc_realloc_with_split_metadata, __unialloc_semantic_scope_enter,
    __unialloc_semantic_scope_exit, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_stats_snapshot, AllocationMetadata, SemanticTypeStatsSnapshot,
    FLAG_METADATA_SEGREGATED, FLAG_TYPE_ISOLATED,
};
use unialloc::UniAlloc;

#[global_allocator]
static A: UniAlloc = UniAlloc;

const MODULE_EXACT_DYNAMIC: u64 = 0xC002_EA00_0000_0001;
const EXACT_DYNAMIC_FLAGS: u32 = FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED;

#[derive(Clone, Copy)]
struct AllocationSite {
    type_id: u64,
    callsite: u64,
}

const SITES: &[AllocationSite] = &[
    AllocationSite {
        type_id: 0xC002_EA00_0000_0101,
        callsite: 0xC002_EA00_1000_0101,
    },
    AllocationSite {
        type_id: 0xC002_EA00_0000_0102,
        callsite: 0xC002_EA00_1000_0102,
    },
    AllocationSite {
        type_id: 0xC002_EA00_0000_0103,
        callsite: 0xC002_EA00_1000_0103,
    },
    AllocationSite {
        type_id: 0xC002_EA00_0000_0104,
        callsite: 0xC002_EA00_1000_0104,
    },
    AllocationSite {
        type_id: 0xC002_EA00_0000_0105,
        callsite: 0xC002_EA00_1000_0105,
    },
    AllocationSite {
        type_id: 0xC002_EA00_0000_0106,
        callsite: 0xC002_EA00_1000_0106,
    },
];

struct CompilerSiteScope {
    previous: AllocationMetadata,
}

impl CompilerSiteScope {
    fn enter(site: AllocationSite) -> Self {
        let previous = __unialloc_semantic_scope_enter(
            site.type_id,
            MODULE_EXACT_DYNAMIC,
            EXACT_DYNAMIC_FLAGS,
            site.callsite,
        );
        Self { previous }
    }
}

impl Drop for CompilerSiteScope {
    fn drop(&mut self) {
        __unialloc_semantic_scope_exit(self.previous);
    }
}

#[inline(never)]
fn black_box<T>(value: T) -> T {
    unsafe {
        let ret = std::ptr::read_volatile(&value);
        std::mem::forget(value);
        ret
    }
}

#[inline(never)]
fn with_compiler_site<R>(site: AllocationSite, f: impl FnOnce() -> R) -> R {
    let _scope = CompilerSiteScope::enter(site);
    f()
}

fn run_vec_site() {
    with_compiler_site(SITES[0], || {
        for round in 0..24_u64 {
            let mut values = Vec::with_capacity(64);
            values.extend(round..round + 96);
            values.truncate(48);
            drop(black_box(values));
        }
    });
}

fn run_string_site() {
    with_compiler_site(SITES[1], || {
        for round in 0..24 {
            let mut text = String::with_capacity(96);
            text.push_str("unialloc exact dynamic allocation site ");
            text.push_str(&round.to_string());
            drop(black_box(text));
        }
    });
}

fn run_btree_site() {
    with_compiler_site(SITES[2], || {
        for round in 0..12_u64 {
            let mut map = BTreeMap::new();
            for item in 0..96_u64 {
                map.insert(item, item ^ round);
            }
            for item in (0..96_u64).step_by(4) {
                map.remove(&item);
            }
            drop(black_box(map));
        }
    });
}

fn run_deque_site() {
    with_compiler_site(SITES[3], || {
        for round in 0..16_u64 {
            let mut deque = VecDeque::with_capacity(96);
            for item in 0..96_u64 {
                deque.push_back(item ^ round);
            }
            while deque.pop_front().is_some() {}
            drop(black_box(deque));
        }
    });
}

fn run_direct_realloc_split_abi_site() {
    let old_site = SITES[4];
    let new_site = SITES[5];
    let old_size = 64 * size_of::<u64>();
    let new_size = 128 * size_of::<u64>();
    let align = align_of::<u64>();

    for round in 0..24_u64 {
        unsafe {
            let ptr = __unialloc_alloc_with_metadata(
                old_size,
                align,
                old_site.type_id,
                MODULE_EXACT_DYNAMIC,
                EXACT_DYNAMIC_FLAGS,
                old_site.callsite,
            ) as *mut u64;
            assert!(!ptr.is_null());
            for idx in 0..64 {
                ptr.add(idx).write(round ^ idx as u64);
            }

            let grown = __unialloc_realloc_with_split_metadata(
                ptr as *mut u8,
                old_size,
                align,
                new_size,
                old_site.type_id,
                MODULE_EXACT_DYNAMIC,
                EXACT_DYNAMIC_FLAGS,
                old_site.callsite,
                new_site.type_id,
                MODULE_EXACT_DYNAMIC,
                EXACT_DYNAMIC_FLAGS,
                new_site.callsite,
            ) as *mut u64;
            assert!(!grown.is_null());
            for idx in 64..128 {
                grown.add(idx).write(round ^ idx as u64);
            }
            assert!(__unialloc_dealloc_with_metadata(
                grown as *mut u8,
                new_size,
                align,
                new_site.type_id,
                MODULE_EXACT_DYNAMIC,
                EXACT_DYNAMIC_FLAGS,
                new_site.callsite,
            ));
        }
    }
}

fn empty_type_stats_row() -> SemanticTypeStatsSnapshot {
    SemanticTypeStatsSnapshot::empty()
}

fn emit_coverage_event() {
    let snap = semantic_stats_snapshot();
    let mut rows = [empty_type_stats_row(); 16];
    let row_count = semantic_type_stats_snapshot(&mut rows);
    for row in rows.iter().take(std::cmp::min(row_count, rows.len())) {
        if row.allocations == 0 {
            continue;
        }
        println!(
            concat!(
                "{{",
                "\"source\":\"compiler_exact_dynamic_probe\",",
                "\"type_id_basis\":\"compiler-assigned-allocation-site-object-type-id\",",
                "\"compiler_site_replay\":false,",
                "\"compiler_site_id_stream_mode\":\"exact-dynamic\",",
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
            "\"source\":\"compiler_exact_dynamic_probe\",",
            "\"type_id_basis\":\"compiler-assigned-allocation-site-object-type-id\",",
            "\"compiler_site_replay\":false,",
            "\"compiler_site_id_stream_mode\":\"exact-dynamic\",",
            "\"event\":\"typed_allocations\",",
            "\"typed\":true,",
            "\"type_id\":\"aggregate\",",
            "\"count\":{},",
            "\"bytes\":{},",
            "\"deallocations\":{},",
            "\"cache_hits\":{},",
            "\"cache_inserts\":{},",
            "\"cache_bypasses\":{},",
            "\"delayed_free_enqueues\":{},",
            "\"delayed_free_flushes\":{},",
            "\"policy_flags_seen\":{},",
            "\"site_count\":{}",
            "}}"
        ),
        snap.typed_allocations,
        snap.typed_allocated_bytes,
        snap.typed_deallocations,
        snap.typed_cache_hits,
        snap.typed_cache_inserts,
        snap.typed_cache_bypasses,
        snap.delayed_free_enqueues,
        snap.delayed_free_flushes,
        snap.policy_flags_seen,
        SITES.len()
    );
    println!(
        concat!(
            "{{",
            "\"source\":\"compiler_exact_dynamic_probe\",",
            "\"type_id_basis\":\"compiler-assigned-allocation-site-object-type-id\",",
            "\"compiler_site_replay\":false,",
            "\"compiler_site_id_stream_mode\":\"exact-dynamic\",",
            "\"event\":\"fallback_allocations\",",
            "\"typed\":false,",
            "\"type_id\":0,",
            "\"count\":{},",
            "\"bytes\":{},",
            "\"deallocations\":{}",
            "}}"
        ),
        snap.fallback_allocations, snap.fallback_allocated_bytes, snap.fallback_deallocations
    );
}

fn assert_exact_dynamic_probe_is_real() {
    let snap = semantic_stats_snapshot();
    assert!(
        snap.total_allocations > 0,
        "exact-dynamic probe should execute real heap allocations"
    );
    assert_eq!(
        snap.fallback_allocations, 0,
        "all probe allocations must be attributed to exact dynamic compiler-site metadata"
    );
    assert_eq!(
        snap.typed_allocations, snap.total_allocations,
        "typed allocation count must cover the full probe workload"
    );
    assert_eq!(
        snap.typed_deallocations, snap.typed_allocations,
        "the probe should deallocate every typed allocation it creates"
    );
    assert!(
        snap.typed_cache_hits > 0 && snap.typed_cache_inserts > 0,
        "the allocator-side type-isolation cache must be exercised, not only counted"
    );
    assert!(
        snap.policy_flags_seen & EXACT_DYNAMIC_FLAGS == EXACT_DYNAMIC_FLAGS,
        "exact-dynamic probe did not exercise the requested type-isolation policy flags"
    );

    let mut rows = [empty_type_stats_row(); 16];
    let row_count = semantic_type_stats_snapshot(&mut rows);
    assert!(
        row_count >= SITES.len(),
        "expected at least one runtime row per exact dynamic allocation site"
    );
    for site in SITES {
        let row = rows
            .iter()
            .take(row_count.min(rows.len()))
            .find(|row| row.type_id == site.type_id && row.callsite == site.callsite)
            .unwrap_or_else(|| {
                panic!(
                    "missing exact dynamic type stats row for type_id={} callsite={}",
                    site.type_id, site.callsite
                )
            });
        assert_eq!(
            row.module_id, MODULE_EXACT_DYNAMIC,
            "exact dynamic row should preserve the compiler module id"
        );
        assert!(
            row.allocations > 0,
            "exact dynamic site {} should execute at least one real allocation",
            site.type_id
        );
        assert_eq!(
            row.allocations, row.deallocations,
            "exact dynamic site {} should balance allocations and deallocations",
            site.type_id
        );
        assert!(
            row.policy_flags_seen & EXACT_DYNAMIC_FLAGS == EXACT_DYNAMIC_FLAGS,
            "exact dynamic site {} did not preserve policy flags",
            site.type_id
        );
    }
}

fn main() {
    semantic_stats_reset();
    run_vec_site();
    run_string_site();
    run_btree_site();
    run_deque_site();
    run_direct_realloc_split_abi_site();
    assert_exact_dynamic_probe_is_real();
    emit_coverage_event();
}
