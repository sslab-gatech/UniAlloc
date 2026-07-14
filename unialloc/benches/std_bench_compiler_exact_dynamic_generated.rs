#![cfg(feature = "stats")]
#![feature(test)]

#[cfg(feature = "bench_scudo")]
compile_error!("compiler semantic benchmarks are UniAlloc-only and cannot produce Scudo timing");

extern crate test;

use std::alloc::Layout;
use std::mem::{align_of, size_of};

use test::{black_box, Bencher};
use unialloc::alloc_api::{
    __unialloc_alloc_with_metadata, __unialloc_dealloc_with_metadata,
    __unialloc_realloc_with_split_metadata, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_stats_snapshot, SemanticTypeStatsSnapshot, FLAG_METADATA_SEGREGATED,
    FLAG_TYPE_ISOLATED,
};
use unialloc::UniAlloc;

#[cfg_attr(not(feature = "fixed_heap"), global_allocator)]
static A: UniAlloc = UniAlloc;

const MODULE_EXACT_DYNAMIC_GENERATED: u64 = 0xC002_EA07_0000_0001;
const EXACT_DYNAMIC_FLAGS: u32 = FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED;

#[derive(Clone, Copy)]
struct ExactSite {
    type_id: u64,
    callsite: u64,
}

const OLD_VEC_SITE: ExactSite = ExactSite {
    type_id: 0xC002_EA07_0000_0101,
    callsite: 0xC002_EA07_1000_0101,
};
const GROWN_VEC_SITE: ExactSite = ExactSite {
    type_id: 0xC002_EA07_0000_0102,
    callsite: 0xC002_EA07_1000_0102,
};
const BOX_SITE: ExactSite = ExactSite {
    type_id: 0xC002_EA07_0000_0103,
    callsite: 0xC002_EA07_1000_0103,
};

fn empty_type_stats_row() -> SemanticTypeStatsSnapshot {
    SemanticTypeStatsSnapshot::empty()
}

unsafe fn alloc_array(site: ExactSite, len: usize) -> (*mut u64, Layout) {
    let layout = Layout::from_size_align(size_of::<u64>() * len, align_of::<u64>())
        .expect("valid array layout");
    let ptr = __unialloc_alloc_with_metadata(
        layout.size(),
        layout.align(),
        site.type_id,
        MODULE_EXACT_DYNAMIC_GENERATED,
        EXACT_DYNAMIC_FLAGS,
        site.callsite,
    ) as *mut u64;
    assert!(!ptr.is_null(), "metadata allocation returned null");
    (ptr, layout)
}

unsafe fn realloc_array(
    ptr: *mut u64,
    old_layout: Layout,
    old_site: ExactSite,
    new_site: ExactSite,
    new_len: usize,
) -> (*mut u64, Layout) {
    let new_layout = Layout::from_size_align(size_of::<u64>() * new_len, align_of::<u64>())
        .expect("valid grown array layout");
    let grown = __unialloc_realloc_with_split_metadata(
        ptr as *mut u8,
        old_layout.size(),
        old_layout.align(),
        new_layout.size(),
        old_site.type_id,
        MODULE_EXACT_DYNAMIC_GENERATED,
        EXACT_DYNAMIC_FLAGS,
        old_site.callsite,
        new_site.type_id,
        MODULE_EXACT_DYNAMIC_GENERATED,
        EXACT_DYNAMIC_FLAGS,
        new_site.callsite,
    ) as *mut u64;
    assert!(!grown.is_null(), "metadata realloc returned null");
    (grown, new_layout)
}

unsafe fn dealloc_array(ptr: *mut u64, layout: Layout, site: ExactSite) {
    assert!(__unialloc_dealloc_with_metadata(
        ptr as *mut u8,
        layout.size(),
        layout.align(),
        site.type_id,
        MODULE_EXACT_DYNAMIC_GENERATED,
        EXACT_DYNAMIC_FLAGS,
        site.callsite,
    ));
}

#[inline(never)]
fn run_exact_dynamic_realloc(rounds: usize) -> usize {
    let mut checksum = 0usize;
    for round in 0..rounds {
        unsafe {
            let (ptr, layout) = alloc_array(OLD_VEC_SITE, 64);
            for idx in 0..64usize {
                ptr.add(idx).write((round ^ idx).wrapping_mul(3) as u64);
            }
            let (grown, grown_layout) =
                realloc_array(ptr, layout, OLD_VEC_SITE, GROWN_VEC_SITE, 128);
            for idx in 0..64usize {
                let value = grown.add(idx).read();
                assert_eq!(value, (round ^ idx).wrapping_mul(3) as u64);
                checksum = checksum.wrapping_add(value as usize);
            }
            for idx in 64..128usize {
                let value = (round.wrapping_add(idx)).rotate_left(5) as u64;
                grown.add(idx).write(value);
                checksum ^= value as usize;
            }
            dealloc_array(grown, grown_layout, GROWN_VEC_SITE);
        }
    }
    checksum
}

#[inline(never)]
fn run_exact_dynamic_box_like(rounds: usize) -> usize {
    let mut checksum = 0usize;
    let layout = Layout::new::<[u64; 8]>();
    for round in 0..rounds {
        unsafe {
            let ptr = __unialloc_alloc_with_metadata(
                layout.size(),
                layout.align(),
                BOX_SITE.type_id,
                MODULE_EXACT_DYNAMIC_GENERATED,
                EXACT_DYNAMIC_FLAGS,
                BOX_SITE.callsite,
            ) as *mut [u64; 8];
            assert!(!ptr.is_null(), "box-like metadata allocation returned null");
            for idx in 0..8usize {
                (*ptr)[idx] = (round + idx).wrapping_mul(17) as u64;
                checksum ^= (*ptr)[idx] as usize;
            }
            assert!(__unialloc_dealloc_with_metadata(
                ptr as *mut u8,
                layout.size(),
                layout.align(),
                BOX_SITE.type_id,
                MODULE_EXACT_DYNAMIC_GENERATED,
                EXACT_DYNAMIC_FLAGS,
                BOX_SITE.callsite,
            ));
        }
    }
    checksum
}

#[inline(never)]
fn run_exact_dynamic_generated_workload(rounds: usize) -> usize {
    run_exact_dynamic_realloc(rounds).rotate_left(11) ^ run_exact_dynamic_box_like(rounds)
}

fn assert_exact_dynamic_metadata() -> usize {
    semantic_stats_reset();
    let checksum = run_exact_dynamic_generated_workload(16);
    let snap = semantic_stats_snapshot();
    let mut rows = [empty_type_stats_row(); 16];
    let row_count = semantic_type_stats_snapshot(&mut rows);
    let matching_rows: Vec<_> = rows
        .iter()
        .take(row_count.min(rows.len()))
        .filter(|row| row.module_id == MODULE_EXACT_DYNAMIC_GENERATED && row.allocations > 0)
        .collect();

    assert_ne!(checksum, 0, "workload checksum must prove real work ran");
    assert!(
        snap.typed_allocations >= 3,
        "exact dynamic ABI must produce typed allocations, got {:?}",
        snap
    );
    for site in [OLD_VEC_SITE, GROWN_VEC_SITE, BOX_SITE] {
        assert!(
            matching_rows.iter().any(|row| {
                row.type_id == site.type_id
                    && row.callsite == site.callsite
                    && row.policy_flags_seen & EXACT_DYNAMIC_FLAGS == EXACT_DYNAMIC_FLAGS
            }),
            "missing exact-dynamic typed row for site type_id={:#x} callsite={:#x}; rows={:?}",
            site.type_id,
            site.callsite,
            matching_rows
        );
    }
    checksum
}

#[test]
fn std_bench_compiler_exact_dynamic_generated_emits_real_typed_rows() {
    black_box(assert_exact_dynamic_metadata());
}

#[bench]
fn std_bench_compiler_exact_dynamic_generated_realloc_probe(b: &mut Bencher) {
    b.iter(|| black_box(run_exact_dynamic_generated_workload(4)));
}
