use unialloc::{
    semantic_auto_metadata_disable, semantic_auto_metadata_enable, semantic_stats_reset,
    semantic_stats_snapshot, UniAlloc, AUTO_LAYOUT_MODULE_ID, FLAG_TYPE_ISOLATED,
};

#[global_allocator]
static A: UniAlloc = UniAlloc;

#[inline(never)]
fn black_box<T>(value: T) -> T {
    unsafe {
        let ret = std::ptr::read_volatile(&value);
        std::mem::forget(value);
        ret
    }
}

fn main() {
    let count = std::env::args()
        .nth(1)
        .and_then(|arg| arg.parse::<usize>().ok())
        .unwrap_or(10_000_000);

    semantic_stats_reset();
    semantic_auto_metadata_enable(AUTO_LAYOUT_MODULE_ID, FLAG_TYPE_ISOLATED, 0xA110_0EC0);

    let mut vec = Vec::<i32>::new();
    for _ in 0..count {
        vec.push(0);
        black_box(&vec);
    }
    drop(vec);

    semantic_auto_metadata_disable();
    let snap = semantic_stats_snapshot();
    println!(
        "vec_push_auto_probe count={} total={} typed={} cache_hits={} cache_inserts={} cache_bypasses={}",
        count,
        snap.total_allocations,
        snap.typed_allocations,
        snap.typed_cache_hits,
        snap.typed_cache_inserts,
        snap.typed_cache_bypasses
    );
}
