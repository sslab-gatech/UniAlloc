#![cfg(feature = "stats")]
#![feature(test)]

#[cfg(feature = "bench_scudo")]
compile_error!("compiler semantic benchmarks are UniAlloc-only and cannot produce Scudo timing");

extern crate test;

use std::collections::{BTreeMap, VecDeque};

use test::{black_box, Bencher};
use unialloc::alloc_api::{
    __unialloc_semantic_scope_enter, __unialloc_semantic_scope_exit, semantic_stats_reset,
    semantic_stats_snapshot, semantic_type_stats_snapshot, AllocationMetadata,
    SemanticTypeStatsSnapshot, FLAG_METADATA_SEGREGATED, FLAG_TYPE_ISOLATED,
};
use unialloc::UniAlloc;

#[cfg_attr(not(feature = "fixed_heap"), global_allocator)]
static A: UniAlloc = UniAlloc;

const MODULE_PROTO_GENERATED: u64 = 0xC002_5007_0000_0001;
const PROTO_FLAGS: u32 = FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED;

#[derive(Clone, Copy)]
struct ProtoSite {
    type_id: u64,
    callsite: u64,
}

const PROTO_SITES: &[ProtoSite] = &[
    ProtoSite {
        type_id: 0xC002_5007_0000_0101,
        callsite: 0xC002_5007_1000_0101,
    },
    ProtoSite {
        type_id: 0xC002_5007_0000_0102,
        callsite: 0xC002_5007_1000_0102,
    },
    ProtoSite {
        type_id: 0xC002_5007_0000_0103,
        callsite: 0xC002_5007_1000_0103,
    },
];

struct SemanticScopeGuard(AllocationMetadata);

impl SemanticScopeGuard {
    fn enter(site: ProtoSite) -> Self {
        Self(__unialloc_semantic_scope_enter(
            site.type_id,
            MODULE_PROTO_GENERATED,
            PROTO_FLAGS,
            site.callsite,
        ))
    }
}

impl Drop for SemanticScopeGuard {
    fn drop(&mut self) {
        __unialloc_semantic_scope_exit(self.0);
    }
}

fn empty_type_stats_row() -> SemanticTypeStatsSnapshot {
    SemanticTypeStatsSnapshot::empty()
}

#[inline(never)]
fn run_vec_site(rounds: usize) -> usize {
    let _scope = SemanticScopeGuard::enter(PROTO_SITES[0]);
    let mut checksum = 0usize;
    for round in 0..rounds {
        let mut values = Vec::with_capacity(128);
        for item in 0..128usize {
            values.push(item ^ round.rotate_left(3));
        }
        checksum ^= values.len();
        checksum = checksum.wrapping_add(values[round % values.len()]);
        black_box(values);
    }
    checksum
}

#[inline(never)]
fn run_string_site(rounds: usize) -> usize {
    let _scope = SemanticScopeGuard::enter(PROTO_SITES[1]);
    let mut checksum = 0usize;
    for round in 0..rounds {
        let mut text = String::with_capacity(96);
        text.push_str("unialloc compiler proto generated site ");
        text.push_str(&round.to_string());
        checksum = checksum.wrapping_add(text.len());
        checksum ^= text.as_bytes()[round % text.len()] as usize;
        black_box(text);
    }
    checksum
}

#[inline(never)]
fn run_collection_site(rounds: usize) -> usize {
    let _scope = SemanticScopeGuard::enter(PROTO_SITES[2]);
    let mut checksum = 0usize;
    for round in 0..rounds {
        let mut map = BTreeMap::new();
        let mut deque = VecDeque::with_capacity(64);
        for item in 0..64usize {
            map.insert(item, item ^ round);
            deque.push_back(round.wrapping_add(item));
        }
        while let Some(front) = deque.pop_front() {
            checksum ^= front;
        }
        checksum = checksum.wrapping_add(map.values().copied().sum::<usize>());
        black_box(map);
    }
    checksum
}

#[inline(never)]
fn run_proto_generated_workload(rounds: usize) -> usize {
    run_vec_site(rounds)
        .rotate_left(7)
        .wrapping_add(run_string_site(rounds).rotate_left(13))
        ^ run_collection_site(rounds).rotate_left(19)
}

fn assert_proto_generated_metadata() -> usize {
    semantic_stats_reset();
    let checksum = run_proto_generated_workload(12);
    let snap = semantic_stats_snapshot();
    let mut rows = [empty_type_stats_row(); 16];
    let row_count = semantic_type_stats_snapshot(&mut rows);
    let matching_rows: Vec<_> = rows
        .iter()
        .take(row_count.min(rows.len()))
        .filter(|row| row.module_id == MODULE_PROTO_GENERATED && row.allocations > 0)
        .collect();

    assert_ne!(checksum, 0, "workload checksum must prove real work ran");
    assert!(
        snap.typed_allocations > 0,
        "semantic scope must produce typed allocations, got {:?}",
        snap
    );
    assert!(
        matching_rows.len() >= PROTO_SITES.len(),
        "expected one typed row per generated proto site, got {:?}",
        matching_rows
    );
    for site in PROTO_SITES {
        assert!(
            matching_rows.iter().any(|row| {
                row.type_id == site.type_id
                    && row.callsite == site.callsite
                    && row.policy_flags_seen & PROTO_FLAGS == PROTO_FLAGS
            }),
            "missing typed row for site type_id={:#x} callsite={:#x}; rows={:?}",
            site.type_id,
            site.callsite,
            matching_rows
        );
    }
    checksum
}

#[test]
fn std_bench_compiler_proto_generated_emits_real_typed_rows() {
    black_box(assert_proto_generated_metadata());
}

#[bench]
fn std_bench_compiler_proto_generated_semantic_scope_probe(b: &mut Bencher) {
    b.iter(|| black_box(run_proto_generated_workload(4)));
}
