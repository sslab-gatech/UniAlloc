use std::collections::{BTreeMap, BinaryHeap, VecDeque};

use unialloc::alloc_api::{
    semantic_stats_reset, semantic_stats_snapshot, with_rust_type_metadata_at, FLAG_DELAYED_FREE,
    FLAG_FORCE_INITIALIZE, FLAG_METADATA_SEGREGATED, FLAG_TYPE_ISOLATED,
};
use unialloc::UniAlloc;

#[global_allocator]
static A: UniAlloc = UniAlloc;

const MODULE_STD_SCOPED: u64 = 0xA110_5C0E;

#[inline(never)]
fn black_box<T>(value: T) -> T {
    unsafe {
        let ret = std::ptr::read_volatile(&value);
        std::mem::forget(value);
        ret
    }
}

fn emit_events(label: &str) {
    let snap = semantic_stats_snapshot();
    println!(
        "{{\"source\":\"scoped_std_coverage_example\",\"type_id_basis\":\"source-scoped-rust-type-id\",\"label\":\"{}\",\"typed\":true,\"type_id\":\"aggregate\",\"count\":{},\"bytes\":{},\"cache_hits\":{},\"cache_inserts\":{},\"cache_bypasses\":{},\"delayed_free_enqueues\":{},\"delayed_free_flushes\":{}}}",
        label,
        snap.typed_allocations,
        snap.typed_allocated_bytes,
        snap.typed_cache_hits,
        snap.typed_cache_inserts,
        snap.typed_cache_bypasses,
        snap.delayed_free_enqueues,
        snap.delayed_free_flushes
    );
    println!(
        "{{\"source\":\"scoped_std_coverage_example\",\"type_id_basis\":\"source-scoped-rust-type-id\",\"label\":\"{}\",\"typed\":false,\"type_id\":0,\"count\":{},\"bytes\":{}}}",
        label, snap.fallback_allocations, snap.fallback_allocated_bytes
    );
}

fn fallback_std_activity(scale: usize) {
    let mut v = Vec::with_capacity(scale);
    for i in 0..scale {
        v.push(i as u64);
    }
    drop(black_box(v));
}

fn run_vec_scope() {
    semantic_stats_reset();
    with_rust_type_metadata_at::<Vec<u64>, _>(
        MODULE_STD_SCOPED,
        FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
        0x5C00_0001,
        || {
            for _ in 0..32 {
                let mut v = Vec::with_capacity(64);
                v.extend(0..128u64);
                v.truncate(32);
                drop(black_box(v));
            }
        },
    );
    fallback_std_activity(8);
    emit_events("scoped-vec");
}

fn run_string_scope() {
    semantic_stats_reset();
    with_rust_type_metadata_at::<String, _>(
        MODULE_STD_SCOPED,
        FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE,
        0x5C00_0002,
        || {
            for i in 0..48 {
                let mut s = String::with_capacity(96);
                s.push_str("unialloc-scoped-std-coverage");
                s.push_str(&i.to_string());
                drop(black_box(s));
            }
        },
    );
    fallback_std_activity(12);
    emit_events("scoped-string");
}

fn run_deque_heap_scope() {
    semantic_stats_reset();
    with_rust_type_metadata_at::<VecDeque<u64>, _>(
        MODULE_STD_SCOPED,
        FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE,
        0x5C00_0003,
        || {
            for _ in 0..24 {
                let mut d = VecDeque::with_capacity(80);
                for i in 0..80 {
                    d.push_back(i);
                }
                while d.pop_front().is_some() {}
                drop(black_box(d));
            }
        },
    );
    with_rust_type_metadata_at::<BinaryHeap<u64>, _>(
        MODULE_STD_SCOPED,
        FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
        0x5C00_0004,
        || {
            for _ in 0..24 {
                let mut h = BinaryHeap::with_capacity(80);
                h.extend(0..80u64);
                while h.pop().is_some() {}
                drop(black_box(h));
            }
        },
    );
    fallback_std_activity(16);
    emit_events("scoped-deque-heap");
}

fn run_map_scope() {
    semantic_stats_reset();
    with_rust_type_metadata_at::<BTreeMap<u64, u64>, _>(
        MODULE_STD_SCOPED,
        FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
        0x5C00_0005,
        || {
            for round in 0..16u64 {
                let mut map = BTreeMap::new();
                for i in 0..96u64 {
                    map.insert(i, i ^ round);
                }
                for i in (0..96u64).step_by(3) {
                    map.remove(&i);
                }
                drop(black_box(map));
            }
        },
    );
    fallback_std_activity(20);
    emit_events("scoped-btree-map");
}

fn main() {
    run_vec_scope();
    run_string_scope();
    run_deque_heap_scope();
    run_map_scope();
}
