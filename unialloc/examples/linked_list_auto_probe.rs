use std::collections::LinkedList;

use unialloc::{
    semantic_auto_metadata_disable, semantic_auto_metadata_enable, semantic_stats_reset,
    semantic_stats_snapshot, UniAlloc, AUTO_LAYOUT_MODULE_ID, FLAG_TYPE_ISOLATED,
};

#[global_allocator]
static A: UniAlloc = UniAlloc;

#[repr(C)]
struct NodeProxy {
    next: usize,
    prev: usize,
    value: i32,
}

fn prefill_same_layout_cache() {
    let mut objects = Vec::new();
    for idx in 0..64usize {
        objects.push(Box::new(NodeProxy {
            next: idx,
            prev: idx + 1,
            value: idx as i32,
        }));
    }
    drop(objects);
}

fn main() {
    let count = std::env::args()
        .nth(1)
        .and_then(|arg| arg.parse::<usize>().ok())
        .unwrap_or(100_000);
    let prefill = std::env::args().any(|arg| arg == "--prefill");

    semantic_stats_reset();
    semantic_auto_metadata_enable(AUTO_LAYOUT_MODULE_ID, FLAG_TYPE_ISOLATED, 0xA110_1145);

    if prefill {
        prefill_same_layout_cache();
    }

    let mut list = LinkedList::new();
    for idx in 0..count {
        list.push_back(idx as i32);
    }
    drop(list);

    semantic_auto_metadata_disable();
    let snap = semantic_stats_snapshot();
    println!(
        "linked_list_auto_probe count={} prefill={} total={} typed={} cache_hits={} cache_inserts={} cache_bypasses={}",
        count,
        prefill,
        snap.total_allocations,
        snap.typed_allocations,
        snap.typed_cache_hits,
        snap.typed_cache_inserts,
        snap.typed_cache_bypasses
    );
}
