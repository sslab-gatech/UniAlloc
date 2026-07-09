use std::collections::{BTreeMap, VecDeque};

use unialloc::alloc_api::{
    __unialloc_semantic_scope_enter, __unialloc_semantic_scope_exit, semantic_stats_reset,
    semantic_stats_snapshot, semantic_type_stats_snapshot, SemanticTypeStatsSnapshot,
    FLAG_METADATA_SEGREGATED, FLAG_TYPE_ISOLATED,
};
use unialloc::UniAlloc;

#[global_allocator]
static A: UniAlloc = UniAlloc;

const LOWERED_FLAGS: u32 = FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED;
const LOWERED_MODULE_ID: u64 = 0xC002_DA00_0000_0001;

#[inline(never)]
fn __unialloc_lowered_site<R>(
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    let previous = __unialloc_semantic_scope_enter(type_id, module_id, flags, callsite);
    let result = f();
    __unialloc_semantic_scope_exit(previous);
    result
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
fn run_workload() {
    for round in 0..24_u64 {
        let mut values: Vec<u64> = __unialloc_lowered_site(
            0x989a1294c51ee0f8,
            0xc002da0000000001,
            0x3,
            0x35c602b6c668af77,
            || Vec::with_capacity(64),
        );
        for item in 0..64_u64 {
            __unialloc_lowered_site(
                0xdc6945af1e5e2986,
                0xc002da0000000001,
                0x3,
                0xeca9aeb708566b6a,
                || values.push(round ^ item),
            );
        }
        drop(black_box(values));

        let mut text = __unialloc_lowered_site(
            0x84cc3e202dee93f1,
            0xc002da0000000001,
            0x3,
            0xe81a6e20de060de1,
            || String::with_capacity(96),
        );
        text.push_str("unialloc rustc driver lowered fixture payload");
        text.push_str(" stable");
        drop(black_box(text));

        let mut deque: VecDeque<u64> = __unialloc_lowered_site(
            0xc84f283d4f324901,
            0xc002da0000000001,
            0x3,
            0x1c4cb2a54b993876,
            || VecDeque::with_capacity(64),
        );
        for item in 0..64_u64 {
            __unialloc_lowered_site(
                0x94668e335d0e6235,
                0xc002da0000000001,
                0x3,
                0xec6153450155e928,
                || deque.push_back(round.wrapping_add(item)),
            );
        }
        while __unialloc_lowered_site(
            0xb29543a61005927a,
            0xc002da0000000001,
            0x3,
            0x35c4d067027dd6fd,
            || deque.pop_front(),
        )
        .is_some()
        {}
        drop(black_box(deque));

        let mut map: BTreeMap<u64, u64> = __unialloc_lowered_site(
            0x6d99ef3ed6eaa195,
            0xc002da0000000001,
            0x3,
            0x945e344d65ded235,
            || BTreeMap::new(),
        );
        for item in 0..64_u64 {
            __unialloc_lowered_site(
                0xdee117f3185a39df,
                0xc002da0000000001,
                0x3,
                0x6a91b7e2235cdb82,
                || map.insert(item, item ^ round),
            );
        }
        drop(black_box(map));
    }
}

fn empty_type_stats_row() -> SemanticTypeStatsSnapshot {
    SemanticTypeStatsSnapshot::empty()
}

fn assert_lowering_worked() -> usize {
    let snap = semantic_stats_snapshot();
    assert!(
        snap.total_allocations > 0,
        "lowered fixture did not allocate"
    );
    assert!(
        snap.typed_allocations > 0,
        "lowered fixture did not produce typed allocations"
    );
    assert_eq!(
        snap.fallback_allocations, 0,
        "lowered fixture produced fallback allocations after stats reset"
    );
    assert_eq!(
        snap.typed_allocations, snap.total_allocations,
        "not every allocation was covered by compiler-lowered metadata"
    );
    assert_eq!(
        snap.typed_deallocations, snap.typed_allocations,
        "metadata records did not recover deallocations after scope exit"
    );
    assert_eq!(
        snap.policy_flags_seen & LOWERED_FLAGS,
        LOWERED_FLAGS,
        "lowered metadata flags were not observed by the allocator"
    );

    let mut rows = [empty_type_stats_row(); 32];
    let row_count = semantic_type_stats_snapshot(&mut rows);
    let mut lowered_rows = 0_usize;
    for row in rows.iter().take(std::cmp::min(row_count, rows.len())) {
        if row.allocations == 0 {
            continue;
        }
        assert_eq!(
            row.module_id, LOWERED_MODULE_ID,
            "unexpected module id in lowered row"
        );
        assert!(
            row.callsite != 0,
            "compiler-derived callsite hash was not preserved"
        );
        assert_eq!(
            row.policy_flags_seen & LOWERED_FLAGS,
            LOWERED_FLAGS,
            "lowered row missed policy flags"
        );
        assert_eq!(
            row.deallocations, row.allocations,
            "lowered row did not recover deallocations"
        );
        lowered_rows += 1;
    }
    assert!(
        lowered_rows >= 3,
        "expected at least three distinct lowered allocation rows"
    );
    lowered_rows
}

fn emit_event(lowered_rows: usize) {
    let snap = semantic_stats_snapshot();
    println!(
        concat!(
            "{{",
            "\"source\":\"rustc_driver_lowering_fixture\",",
            "\"type_id_basis\":\"compiler-assigned-allocation-site-object-type-id\",",
            "\"compiler_site_id_stream_mode\":\"rustc-driver-source-span-lowered\",",
            "\"compiler_site_replay\":false,",
            "\"typed\":true,",
            "\"total_allocations\":{},",
            "\"typed_allocations\":{},",
            "\"fallback_allocations\":{},",
            "\"typed_deallocations\":{},",
            "\"typed_allocated_bytes\":{},",
            "\"policy_flags_seen\":{},",
            "\"lowered_rows\":{}",
            "}}"
        ),
        snap.total_allocations,
        snap.typed_allocations,
        snap.fallback_allocations,
        snap.typed_deallocations,
        snap.typed_allocated_bytes,
        snap.policy_flags_seen,
        lowered_rows
    );
}

fn main() {
    assert_eq!(LOWERED_FLAGS, FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
    semantic_stats_reset();
    run_workload();
    let lowered_rows = assert_lowering_worked();
    emit_event(lowered_rows);
}
