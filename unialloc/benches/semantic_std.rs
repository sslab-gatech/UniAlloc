#![feature(test)]

#[cfg(feature = "bench_scudo")]
compile_error!("semantic_std is UniAlloc-only and cannot produce Scudo allocator timing");

extern crate test;

use std::boxed::Box;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap, LinkedList, VecDeque};

use test::{black_box, Bencher};
use unialloc::alloc_api::{
    semantic_stats_reset, semantic_stats_snapshot, with_rust_type_metadata_at, FLAG_DELAYED_FREE,
    FLAG_FORCE_INITIALIZE, FLAG_METADATA_SEGREGATED, FLAG_TYPE_ISOLATED,
};
use unialloc::UniAlloc;

#[cfg_attr(not(feature = "fixed_heap"), global_allocator)]
static A: UniAlloc = UniAlloc;

const MODULE_SEMANTIC_STD_BENCH: u64 = 0xA110_BE7C;

fn emit_events(benchmark: &str, workload_category: &str) {
    let snap = semantic_stats_snapshot();
    println!(
        "{{\"source\":\"semantic_std_bench\",\"type_id_basis\":\"source-scoped-rust-type-id\",\"workload_category\":\"{}\",\"benchmark\":\"{}\",\"typed\":true,\"type_id\":\"aggregate\",\"count\":{},\"bytes\":{},\"cache_hits\":{},\"cache_inserts\":{},\"cache_bypasses\":{},\"delayed_free_enqueues\":{},\"delayed_free_flushes\":{}}}",
        workload_category,
        benchmark,
        snap.typed_allocations,
        snap.typed_allocated_bytes,
        snap.typed_cache_hits,
        snap.typed_cache_inserts,
        snap.typed_cache_bypasses,
        snap.delayed_free_enqueues,
        snap.delayed_free_flushes
    );
    println!(
        "{{\"source\":\"semantic_std_bench\",\"type_id_basis\":\"source-scoped-rust-type-id\",\"workload_category\":\"{}\",\"benchmark\":\"{}\",\"typed\":false,\"type_id\":0,\"count\":{},\"bytes\":{}}}",
        workload_category, benchmark, snap.fallback_allocations, snap.fallback_allocated_bytes
    );
}

fn scoped_iter<T: 'static, F>(
    b: &mut Bencher,
    benchmark: &str,
    workload_category: &str,
    flags: u32,
    callsite: u64,
    mut f: F,
) where
    F: FnMut(),
{
    semantic_stats_reset();
    b.iter(|| {
        with_rust_type_metadata_at::<T, _>(MODULE_SEMANTIC_STD_BENCH, flags, callsite, || {
            f();
        })
    });
    emit_events(benchmark, workload_category);
}

fn scoped_iter_custom<F>(b: &mut Bencher, benchmark: &str, workload_category: &str, mut f: F)
where
    F: FnMut(),
{
    semantic_stats_reset();
    b.iter(|| f());
    emit_events(benchmark, workload_category);
}

#[bench]
fn scoped_vec_growth(b: &mut Bencher) {
    scoped_iter::<Vec<u64>, _>(
        b,
        "scoped_vec_growth",
        "vec",
        FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
        0xBE7C_0001,
        || {
            let mut v = Vec::with_capacity(64);
            v.extend(0..256u64);
            v.truncate(96);
            black_box(v);
        },
    );
}

#[bench]
fn scoped_string_build(b: &mut Bencher) {
    scoped_iter::<String, _>(
        b,
        "scoped_string_build",
        "string",
        FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE,
        0xBE7C_0002,
        || {
            let mut s = String::with_capacity(128);
            s.push_str("unialloc semantic std bench ");
            for i in 0..16 {
                s.push_str(&i.to_string());
            }
            black_box(s);
        },
    );
}

#[bench]
fn scoped_vec_deque_churn(b: &mut Bencher) {
    scoped_iter::<VecDeque<u64>, _>(
        b,
        "scoped_vec_deque_churn",
        "vec_deque",
        FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE,
        0xBE7C_0003,
        || {
            let mut d = VecDeque::with_capacity(96);
            for i in 0..96 {
                d.push_back(i);
            }
            for _ in 0..48 {
                black_box(d.pop_front());
            }
            black_box(d);
        },
    );
}

#[bench]
fn scoped_binary_heap_churn(b: &mut Bencher) {
    scoped_iter::<BinaryHeap<u64>, _>(
        b,
        "scoped_binary_heap_churn",
        "binary_heap",
        FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
        0xBE7C_0004,
        || {
            let mut h = BinaryHeap::with_capacity(128);
            h.extend((0..128u64).rev());
            for _ in 0..64 {
                black_box(h.pop());
            }
            black_box(h);
        },
    );
}

#[bench]
fn scoped_btree_map_churn(b: &mut Bencher) {
    scoped_iter::<BTreeMap<u64, u64>, _>(
        b,
        "scoped_btree_map_churn",
        "btree_map",
        FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
        0xBE7C_0005,
        || {
            let mut map = BTreeMap::new();
            for i in 0..128 {
                map.insert(i, i ^ 0x5a5a);
            }
            for i in (0..128).step_by(4) {
                black_box(map.remove(&i));
            }
            black_box(map);
        },
    );
}

#[bench]
fn scoped_boxed_slice(b: &mut Bencher) {
    scoped_iter::<Box<[u64]>, _>(
        b,
        "scoped_boxed_slice",
        "slice",
        FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE,
        0xBE7C_0006,
        || {
            let slice: Box<[u64]> = (0..128u64).collect::<Vec<_>>().into_boxed_slice();
            black_box(slice);
        },
    );
}

#[bench]
fn scoped_vec_clone_extend_surface(b: &mut Bencher) {
    scoped_iter::<Vec<u64>, _>(
        b,
        "scoped_vec_clone_extend_surface",
        "vec",
        FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
        0xBE7C_0007,
        || {
            let base: Vec<_> = (0..384u64).collect();
            let mut dst = base.clone();
            dst.extend(base.iter().copied().rev());
            dst.shrink_to_fit();
            black_box(dst);
        },
    );
}

#[bench]
fn scoped_string_insert_lossy_surface(b: &mut Bencher) {
    scoped_iter::<String, _>(
        b,
        "scoped_string_insert_lossy_surface",
        "string",
        FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE,
        0xBE7C_0008,
        || {
            let invalid = b"Hello\xC0\x80 There\xE6\x83 Goodbye";
            let mut s = String::from_utf8_lossy(invalid).into_owned();
            s.insert_str(5, " scoped semantic metadata ");
            s.shrink_to_fit();
            black_box(s);
        },
    );
}

#[bench]
fn scoped_btree_set_churn(b: &mut Bencher) {
    scoped_iter::<BTreeSet<u64>, _>(
        b,
        "scoped_btree_set_churn",
        "btree_set",
        FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
        0xBE7C_0009,
        || {
            let mut set = BTreeSet::new();
            for i in 0..160u64 {
                set.insert(i ^ 0x55aa);
            }
            let right = set.split_off(&96);
            black_box((set, right));
        },
    );
}

#[bench]
fn scoped_linked_list_churn(b: &mut Bencher) {
    scoped_iter::<LinkedList<u64>, _>(
        b,
        "scoped_linked_list_churn",
        "linked_list",
        FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE,
        0xBE7C_000A,
        || {
            let mut list = LinkedList::new();
            for i in 0..96u64 {
                list.push_back(i);
                list.push_front(i ^ 0xff);
            }
            for _ in 0..64 {
                black_box(list.pop_back());
                black_box(list.pop_front());
            }
            black_box(list);
        },
    );
}

#[bench]
fn scoped_vec_deque_wraparound_surface(b: &mut Bencher) {
    scoped_iter::<VecDeque<u64>, _>(
        b,
        "scoped_vec_deque_wraparound_surface",
        "vec_deque",
        FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE,
        0xBE7C_000B,
        || {
            let mut d: VecDeque<_> = (0..192u64).collect();
            for i in 0..96u64 {
                black_box(d.pop_front());
                d.push_back(i + 1_000);
            }
            d.make_contiguous().sort_unstable();
            black_box(d);
        },
    );
}

#[bench]
fn scoped_mixed_compiler_surface(b: &mut Bencher) {
    scoped_iter_custom(
        b,
        "scoped_mixed_compiler_surface",
        "mixed_std_alloc",
        || {
            with_rust_type_metadata_at::<Vec<u64>, _>(
                MODULE_SEMANTIC_STD_BENCH,
                FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
                0xBE7C_0101,
                || {
                    let mut values: Vec<_> = (0..256u64).rev().collect();
                    values.sort_unstable();
                    black_box(values);
                },
            );
            with_rust_type_metadata_at::<String, _>(
                MODULE_SEMANTIC_STD_BENCH,
                FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE,
                0xBE7C_0102,
                || {
                    let text = (0..32).map(|i| i.to_string()).collect::<Vec<_>>().join(",");
                    black_box(text);
                },
            );
            with_rust_type_metadata_at::<LinkedList<u64>, _>(
                MODULE_SEMANTIC_STD_BENCH,
                FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE,
                0xBE7C_0103,
                || {
                    let mut list = LinkedList::new();
                    for i in 0..32u64 {
                        list.push_back(i);
                    }
                    black_box(list);
                },
            );
            with_rust_type_metadata_at::<BTreeMap<u64, u64>, _>(
                MODULE_SEMANTIC_STD_BENCH,
                FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
                0xBE7C_0104,
                || {
                    let map: BTreeMap<_, _> = (0..64u64).map(|i| (i, i * 3)).collect();
                    black_box(map);
                },
            );
        },
    );
}
