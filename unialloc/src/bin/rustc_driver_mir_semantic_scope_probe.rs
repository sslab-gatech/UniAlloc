#![cfg_attr(feature = "fixed_heap", allow(dead_code, unused_imports))]
use std::alloc::{alloc, dealloc, Layout};
use std::collections::{BTreeMap, BTreeSet, BinaryHeap, HashMap, HashSet, LinkedList, VecDeque};
use std::ffi::{CString, OsString};
use std::fmt::Write as _;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::PathBuf;
use std::pin::Pin;
use std::ptr;
use std::rc::Rc;
use std::sync::Arc;

use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_scope_depth_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_stats_snapshot, SemanticMetadataValidationSnapshot, SemanticScopeDepthSnapshot,
    SemanticTypeStatsSnapshot, UniAlloc,
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

const RUSTC_DRIVER_LOWERING_MODULE_ID: u64 = 0xC002_DA00_0000_0001;

#[inline(never)]
fn black_box<T>(value: T) -> T {
    unsafe {
        let ret = std::ptr::read_volatile(&value);
        std::mem::forget(value);
        ret
    }
}

#[allow(dead_code)]
struct HeapOwner<T> {
    inner: T,
}

#[derive(Debug)]
struct PanicOnClone(u64);

impl Clone for PanicOnClone {
    fn clone(&self) -> Self {
        if self.0 == 3 {
            panic!("unialloc MIR semantic-scope unwind probe clone panic");
        }
        Self(self.0)
    }
}

#[derive(Clone, Copy, Debug)]
struct UnwindScopeCleanupCheck {
    panic_observed: bool,
    direct_alloc_total_allocations: usize,
    direct_alloc_typed_allocations: usize,
    direct_alloc_fallback_allocations: usize,
    direct_alloc_typed_deallocations: usize,
    direct_alloc_fallback_deallocations: usize,
    validated: bool,
}

#[derive(Clone, Copy, Debug)]
struct DeepScopeOverflowCheck {
    leaf_main_depth: usize,
    leaf_overflow_depth: usize,
    leaf_overflow_capacity: usize,
    leaf_represented_depth: usize,
    leaf_active_overflow_scope_represented: bool,
    post_main_depth: usize,
    post_overflow_depth: usize,
    post_represented_depth: usize,
    returned_len: usize,
    checksum: u64,
    validated: bool,
}

#[derive(Clone, Copy, Debug)]
struct UnrepresentedScopeFallbackCheck {
    leaf_main_depth: usize,
    leaf_overflow_depth: usize,
    leaf_overflow_capacity: usize,
    leaf_represented_depth: usize,
    leaf_active_overflow_scope_represented: bool,
    direct_alloc_total_allocations: usize,
    direct_alloc_typed_allocations: usize,
    direct_alloc_fallback_allocations: usize,
    direct_alloc_typed_deallocations: usize,
    direct_alloc_fallback_deallocations: usize,
    post_main_depth: usize,
    post_overflow_depth: usize,
    post_represented_depth: usize,
    validated: bool,
}

fn empty_scope_depth_snapshot() -> SemanticScopeDepthSnapshot {
    SemanticScopeDepthSnapshot {
        main_depth: 0,
        overflow_depth: 0,
        overflow_capacity: 0,
        represented_depth: 0,
        active_overflow_scope_represented: false,
    }
}

#[inline(never)]
fn direct_alloc_dealloc_probe(byte: u8) {
    let layout = Layout::from_size_align(64, std::mem::align_of::<usize>())
        .expect("valid direct allocation layout");
    unsafe {
        let ptr = alloc(layout);
        assert!(
            !ptr.is_null(),
            "direct allocation failed in semantic-scope probe"
        );
        ptr::write_volatile(ptr, byte);
        let observed = ptr::read_volatile(ptr);
        assert_eq!(observed, byte);
        dealloc(ptr, layout);
    }
}

#[inline(never)]
fn trigger_panicking_vec_extend(seed: &[PanicOnClone]) {
    let mut values = Vec::with_capacity(seed.len());
    values.extend_from_slice(seed);
    drop(black_box(values));
}

#[inline(never)]
fn validate_unwind_scope_cleanup() -> UnwindScopeCleanupCheck {
    let seed = [
        PanicOnClone(0),
        PanicOnClone(1),
        PanicOnClone(2),
        PanicOnClone(3),
        PanicOnClone(4),
    ];

    semantic_stats_reset();
    let panic_observed = catch_unwind(AssertUnwindSafe(|| {
        trigger_panicking_vec_extend(&seed);
    }))
    .is_err();

    // Reset counters after the caught panic but deliberately do not touch the
    // thread-local semantic-scope stack.  If the compiler-inserted cleanup
    // edge failed to run __unialloc_semantic_scope_pop, this direct low-level
    // allocation is incorrectly charged as typed lowered-module evidence.
    semantic_stats_reset();
    direct_alloc_dealloc_probe(0xAB);
    let snap = semantic_stats_snapshot();
    let validated = panic_observed
        && snap.total_allocations == 1
        && snap.typed_allocations == 0
        && snap.fallback_allocations == 1
        && snap.typed_deallocations == 0
        && snap.fallback_deallocations == 1;

    UnwindScopeCleanupCheck {
        panic_observed,
        direct_alloc_total_allocations: snap.total_allocations,
        direct_alloc_typed_allocations: snap.typed_allocations,
        direct_alloc_fallback_allocations: snap.fallback_allocations,
        direct_alloc_typed_deallocations: snap.typed_deallocations,
        direct_alloc_fallback_deallocations: snap.fallback_deallocations,
        validated,
    }
}

#[inline(never)]
fn recursive_vec_scope_probe(
    depth: usize,
    leaf_snapshot: &mut SemanticScopeDepthSnapshot,
) -> Vec<u64> {
    if depth == 0 {
        *leaf_snapshot = semantic_scope_depth_snapshot();
        let mut values = Vec::with_capacity(4);
        values.extend([0xC002_u64, 0xDA7A, 0x5C0E, 0xF00D]);
        return values;
    }

    let mut values = recursive_vec_scope_probe(depth - 1, leaf_snapshot);
    values.push(depth as u64);
    values
}

#[inline(never)]
fn validate_deep_compiler_scope_overflow() -> DeepScopeOverflowCheck {
    let mut leaf_snapshot = empty_scope_depth_snapshot();
    let values = recursive_vec_scope_probe(72, &mut leaf_snapshot);
    let returned_len = values.len();
    let checksum = values.iter().fold(0_u64, |acc, value| {
        acc.wrapping_mul(131).wrapping_add(*value)
    });
    drop(black_box(values));

    let post_snapshot = semantic_scope_depth_snapshot();
    let validated = leaf_snapshot.main_depth >= 64
        && leaf_snapshot.overflow_depth > 0
        && leaf_snapshot.represented_depth > 64
        && leaf_snapshot.active_overflow_scope_represented
        && post_snapshot.main_depth == 0
        && post_snapshot.overflow_depth == 0
        && post_snapshot.represented_depth == 0
        && returned_len == 76
        && checksum != 0;

    DeepScopeOverflowCheck {
        leaf_main_depth: leaf_snapshot.main_depth,
        leaf_overflow_depth: leaf_snapshot.overflow_depth,
        leaf_overflow_capacity: leaf_snapshot.overflow_capacity,
        leaf_represented_depth: leaf_snapshot.represented_depth,
        leaf_active_overflow_scope_represented: leaf_snapshot.active_overflow_scope_represented,
        post_main_depth: post_snapshot.main_depth,
        post_overflow_depth: post_snapshot.overflow_depth,
        post_represented_depth: post_snapshot.represented_depth,
        returned_len,
        checksum,
        validated,
    }
}

#[inline(never)]
fn recursive_unrepresented_scope_probe(
    depth: usize,
    leaf_snapshot: &mut SemanticScopeDepthSnapshot,
) -> Vec<u64> {
    if depth == 0 {
        let mut values = Vec::with_capacity(1);
        values.push(0xC002_DA7A);
        *leaf_snapshot = semantic_scope_depth_snapshot();
        semantic_stats_reset();
        direct_alloc_dealloc_probe(0xCD);
        return values;
    }

    recursive_unrepresented_scope_probe(depth - 1, leaf_snapshot)
}

#[inline(never)]
fn validate_unrepresented_compiler_scope_fallback() -> UnrepresentedScopeFallbackCheck {
    let mut leaf_snapshot = empty_scope_depth_snapshot();
    semantic_stats_reset();
    let values = recursive_unrepresented_scope_probe(136, &mut leaf_snapshot);
    let direct_snap = semantic_stats_snapshot();
    semantic_stats_recording_disable();
    drop(black_box(values));
    semantic_stats_reset();
    let post_snapshot = semantic_scope_depth_snapshot();
    let validated = leaf_snapshot.main_depth >= 64
        && leaf_snapshot.overflow_capacity >= 64
        && leaf_snapshot.overflow_depth > leaf_snapshot.overflow_capacity
        && leaf_snapshot.represented_depth
            == leaf_snapshot.main_depth + leaf_snapshot.overflow_capacity
        && !leaf_snapshot.active_overflow_scope_represented
        && direct_snap.total_allocations == 1
        && direct_snap.typed_allocations == 0
        && direct_snap.fallback_allocations == 1
        && direct_snap.typed_deallocations == 0
        && direct_snap.fallback_deallocations == 1
        && post_snapshot.main_depth == 0
        && post_snapshot.overflow_depth == 0
        && post_snapshot.represented_depth == 0;

    UnrepresentedScopeFallbackCheck {
        leaf_main_depth: leaf_snapshot.main_depth,
        leaf_overflow_depth: leaf_snapshot.overflow_depth,
        leaf_overflow_capacity: leaf_snapshot.overflow_capacity,
        leaf_represented_depth: leaf_snapshot.represented_depth,
        leaf_active_overflow_scope_represented: leaf_snapshot.active_overflow_scope_represented,
        direct_alloc_total_allocations: direct_snap.total_allocations,
        direct_alloc_typed_allocations: direct_snap.typed_allocations,
        direct_alloc_fallback_allocations: direct_snap.fallback_allocations,
        direct_alloc_typed_deallocations: direct_snap.typed_deallocations,
        direct_alloc_fallback_deallocations: direct_snap.fallback_deallocations,
        post_main_depth: post_snapshot.main_depth,
        post_overflow_depth: post_snapshot.overflow_depth,
        post_represented_depth: post_snapshot.represented_depth,
        validated,
    }
}

#[inline(never)]
fn make_optional_vec() -> Option<Vec<u64>> {
    let mut values = Vec::with_capacity(12);
    values.extend(0..12_u64);
    Some(values)
}

#[inline(never)]
fn make_result_vec() -> Result<Vec<u64>, u64> {
    let mut values = Vec::with_capacity(14);
    values.extend((0..14_u64).map(|value| value ^ 0xA110_C002));
    Ok(values)
}

#[inline(never)]
fn make_wrapped_vec() -> HeapOwner<Vec<u64>> {
    let mut values = Vec::with_capacity(16);
    values.extend((0..16_u64).map(|value| value.rotate_left(3)));
    HeapOwner { inner: values }
}

#[inline(never)]
fn make_pinned_box() -> Pin<Box<[u64; 4]>> {
    Box::pin([700_u64, 701, 702, 703])
}

#[inline(never)]
fn make_c_string() -> CString {
    CString::new("unialloc-mir-semantic-scope-c-string").expect("probe string contains no NUL")
}

#[inline(never)]
fn mutate_string_receiver() -> String {
    let mut text = String::with_capacity(1);
    text.push_str("unialloc-mir-semantic-scope-string-push-str-forces-growth");
    text.insert_str(0, "prefix-");
    text.replace_range(0..6, "replaced");
    text
}

#[inline(never)]
fn mutate_pathbuf_receiver() -> PathBuf {
    let mut path = PathBuf::from("u");
    path.push("nialloc-mir-semantic-scope-pathbuf-receiver-push-forces-growth");
    path
}

#[inline(never)]
fn mutate_os_string_receiver() -> OsString {
    let mut text = OsString::from("u");
    text.push("nialloc-mir-semantic-scope-osstring-receiver-push-forces-growth");
    text
}

#[inline(never)]
fn make_indirect_vec() -> Vec<u64> {
    let mut values = Vec::with_capacity(10);
    values.extend((0..10_u64).map(|value| value ^ 0xC002_1D1E));
    values
}

#[inline(never)]
fn call_vec_factory(factory: fn() -> Vec<u64>) -> Vec<u64> {
    factory()
}

#[inline(never)]
fn run_workload() {
    let mut values = Vec::with_capacity(64);
    for item in 0..128_u64 {
        values.push(item ^ 0xC002);
    }
    values.truncate(32);
    values.shrink_to_fit();
    drop(black_box(values));

    let mut deque = VecDeque::with_capacity(32);
    for item in 0..64_u64 {
        deque.push_back(item);
    }
    while deque.pop_front().is_some() {}
    drop(black_box(deque));

    let mut heap = BinaryHeap::with_capacity(64);
    for item in 0..96_u64 {
        heap.push(item ^ 0xB1A5);
    }
    for _ in 0..48_u64 {
        let _ = heap.pop();
    }
    heap.shrink_to_fit();
    drop(black_box(heap));

    let mut map = BTreeMap::new();
    for item in 0..96_u64 {
        map.insert(item, item.rotate_left(7));
    }
    for item in 0..48_u64 {
        let _ = map.remove(&item);
    }
    map.clear();
    drop(black_box(map));

    let mut set = BTreeSet::new();
    for item in 0..96_u64 {
        set.insert(item ^ 0xB7EE);
    }
    for item in 0..48_u64 {
        let _ = set.remove(&(item ^ 0xB7EE));
    }
    set.clear();
    drop(black_box(set));

    let mut list = LinkedList::new();
    for item in 0..96_u64 {
        list.push_back(item ^ 0xA11C);
    }
    while list.pop_front().is_some() {}
    drop(black_box(list));

    let mut hash_map = HashMap::with_capacity(64);
    for item in 0..128_u64 {
        hash_map.insert(item, item.rotate_right(11));
    }
    for item in 0..64_u64 {
        let _ = hash_map.remove(&item);
    }
    hash_map.clear();
    drop(black_box(hash_map));

    let mut hash_set = HashSet::with_capacity(64);
    for item in 0..128_u64 {
        hash_set.insert(item ^ 0x5E7);
    }
    for item in 0..64_u64 {
        let _ = hash_set.remove(&(item ^ 0x5E7));
    }
    hash_set.clear();
    drop(black_box(hash_set));

    let from_hash_map = HashMap::from([
        (200_u64, 201_u64),
        (202_u64, 203_u64),
        (204_u64, 205_u64),
        (206_u64, 207_u64),
    ]);
    drop(black_box(from_hash_map));

    let from_hash_set = HashSet::from([300_u64, 301_u64, 302_u64, 303_u64]);
    drop(black_box(from_hash_set));

    let rc_values = Rc::new([400_u64, 401, 402, 403, 404, 405, 406, 407]);
    drop(black_box(rc_values));

    let arc_values = Arc::new([500_u64, 501, 502, 503, 504, 505, 506, 507]);
    drop(black_box(arc_values));

    let path = PathBuf::from("/tmp/unialloc-mir-semantic-scope-path");
    drop(black_box(path));

    let os_string = OsString::from("unialloc-mir-semantic-scope-os-string");
    drop(black_box(os_string));

    let optional_vec = make_optional_vec();
    drop(black_box(optional_vec));

    let result_vec = make_result_vec();
    drop(black_box(result_vec));

    let wrapped_vec = make_wrapped_vec();
    drop(black_box(wrapped_vec));

    let pinned_box = make_pinned_box();
    drop(black_box(pinned_box));

    let c_string = make_c_string();
    drop(black_box(c_string));

    let mutated_string = mutate_string_receiver();
    drop(black_box(mutated_string));

    let mutated_path = mutate_pathbuf_receiver();
    drop(black_box(mutated_path));

    let mutated_os_string = mutate_os_string_receiver();
    drop(black_box(mutated_os_string));

    let indirect_vec = call_vec_factory(make_indirect_vec);
    drop(black_box(indirect_vec));
}

fn empty_type_stats_row() -> SemanticTypeStatsSnapshot {
    SemanticTypeStatsSnapshot::empty()
}

fn lowered_type_rows_json(rows: &[SemanticTypeStatsSnapshot], row_count: usize) -> String {
    let mut out = String::from("[");
    let take = std::cmp::min(row_count, rows.len());
    let mut emitted = 0usize;
    for row in rows.iter().take(take).filter(|row| {
        row.module_id == RUSTC_DRIVER_LOWERING_MODULE_ID
            && (row.allocations > 0 || row.deallocations > 0)
    }) {
        if emitted > 0 {
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
                "\"allocated_bytes\":{},",
                "\"deallocations\":{},",
                "\"cache_hits\":{},",
                "\"cache_inserts\":{},",
                "\"cache_bypasses\":{},",
                "\"policy_flags_seen\":{}",
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
            row.policy_flags_seen
        );
    }
    out.push(']');
    out
}

fn semantic_metadata_validation_json(snapshot: SemanticMetadataValidationSnapshot) -> String {
    format!(
        concat!(
            "{{",
            "\"recovery_identity_matches\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"last_mismatch_requested_type_id\":{},",
            "\"last_mismatch_recorded_type_id\":{},",
            "\"last_mismatch_requested_module_id\":{},",
            "\"last_mismatch_recorded_module_id\":{},",
            "\"last_mismatch_requested_callsite\":{},",
            "\"last_mismatch_recorded_callsite\":{}",
            "}}"
        ),
        snapshot.recovery_identity_matches,
        snapshot.recovery_identity_mismatches,
        snapshot.last_mismatch_requested_type_id,
        snapshot.last_mismatch_recorded_type_id,
        snapshot.last_mismatch_requested_module_id,
        snapshot.last_mismatch_recorded_module_id,
        snapshot.last_mismatch_requested_callsite,
        snapshot.last_mismatch_recorded_callsite
    )
}

fn main() {
    // This executable intentionally uses ordinary high-level heap APIs only:
    // no manual semantic-scope ABI calls and no runtime auto metadata. A
    // passing run therefore requires the rustc_driver optimized-MIR provider
    // override to solve heap object types and insert UniAlloc semantic scopes.
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();
    semantic_auto_metadata_disable();
    let unwind_scope_cleanup = validate_unwind_scope_cleanup();
    assert!(
        unwind_scope_cleanup.validated,
        "semantic-scope MIR cleanup failed to restore fallback direct allocation after panic: {:?}",
        unwind_scope_cleanup
    );
    semantic_stats_reset();
    let deep_scope_overflow = validate_deep_compiler_scope_overflow();
    assert!(
        deep_scope_overflow.validated,
        "compiler-inserted semantic scopes did not exercise the runtime overflow stack: {:?}",
        deep_scope_overflow
    );
    semantic_stats_reset();
    let unrepresented_scope_fallback = validate_unrepresented_compiler_scope_fallback();
    assert!(
        unrepresented_scope_fallback.validated,
        "compiler-inserted semantic scopes beyond the bounded overflow stack were not fail-closed fallback: {:?}",
        unrepresented_scope_fallback
    );
    semantic_stats_reset();

    run_workload();

    let snap = semantic_stats_snapshot();
    let fallback_attribution = semantic_fallback_attribution_snapshot();
    let fallback_attribution_allocations = fallback_attribution.raw_alloc_no_metadata
        + fallback_attribution.raw_realloc_no_metadata
        + fallback_attribution.realloc_recorded_old_metadata_new_allocations;
    let mut rows = [empty_type_stats_row(); 64];
    let row_count = semantic_type_stats_snapshot(&mut rows);
    let lowered_rows = rows
        .iter()
        .take(std::cmp::min(row_count, rows.len()))
        .filter(|row| row.allocations > 0 && row.module_id == RUSTC_DRIVER_LOWERING_MODULE_ID)
        .count();
    let lowered_deallocation_rows = rows
        .iter()
        .take(std::cmp::min(row_count, rows.len()))
        .filter(|row| row.deallocations > 0 && row.module_id == RUSTC_DRIVER_LOWERING_MODULE_ID)
        .count();
    let drop_scope_deallocation_rows = rows
        .iter()
        .take(std::cmp::min(row_count, rows.len()))
        .filter(|row| {
            row.allocations == 0
                && row.deallocations > 0
                && row.module_id == RUSTC_DRIVER_LOWERING_MODULE_ID
        })
        .count();
    let type_rows_json = lowered_type_rows_json(&rows, row_count);
    let semantic_metadata_validation = semantic_metadata_validation_snapshot();
    let semantic_metadata_validation_json =
        semantic_metadata_validation_json(semantic_metadata_validation);
    assert!(
        snap.typed_allocations > 0,
        "semantic-scope MIR rewrite did not produce typed allocations: {:?}",
        snap
    );
    assert_eq!(
        snap.fallback_allocations, 0,
        "semantic-scope MIR rewrite left fallback allocations: stats={:?}, attribution={:?}",
        snap, fallback_attribution
    );
    assert!(
        snap.typed_deallocations > 0,
        "ordinary drops did not recover compiler-solved semantic metadata: {:?}",
        snap
    );
    assert!(
        lowered_rows > 0,
        "no typed stats row used the rustc_driver semantic-scope module"
    );

    println!(
        concat!(
            "{{",
            "\"source\":\"rustc_driver_mir_semantic_scope_probe\",",
            "\"type_id_basis\":\"compiler-assigned-allocation-site-object-type-id-rustc-driver-mir-semantic-scope\",",
            "\"compiler_site_id_stream_mode\":\"rustc-driver-mir-semantic-scope\",",
            "\"compiler_site_replay\":false,",
            "\"total_allocations\":{},",
            "\"typed_allocations\":{},",
            "\"fallback_allocations\":{},",
            "\"fallback_attribution_allocations\":{},",
            "\"fallback_attribution_raw_alloc_no_metadata\":{},",
            "\"fallback_attribution_raw_alloc_no_metadata_bytes\":{},",
            "\"fallback_attribution_raw_dealloc_no_metadata\":{},",
            "\"fallback_attribution_raw_realloc_no_metadata\":{},",
            "\"fallback_attribution_raw_realloc_no_metadata_bytes\":{},",
            "\"fallback_attribution_raw_realloc_moved_dealloc_no_metadata\":{},",
            "\"fallback_attribution_realloc_recorded_old_metadata_new_allocations\":{},",
            "\"fallback_attribution_realloc_recorded_old_metadata_new_allocation_bytes\":{},",
            "\"typed_deallocations\":{},",
            "\"policy_flags_seen\":{},",
            "\"lowered_rows\":{},",
            "\"lowered_deallocation_rows\":{},",
            "\"drop_scope_deallocation_rows\":{},",
            "\"semantic_metadata_validation\":{},",
            "\"recovery_identity_matches\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"last_mismatch_requested_type_id\":{},",
            "\"last_mismatch_recorded_type_id\":{},",
            "\"last_mismatch_requested_module_id\":{},",
            "\"last_mismatch_recorded_module_id\":{},",
            "\"last_mismatch_requested_callsite\":{},",
            "\"last_mismatch_recorded_callsite\":{},",
            "\"unwind_scope_cleanup_validated\":{},",
            "\"unwind_scope_panic_observed\":{},",
            "\"post_unwind_direct_alloc_total_allocations\":{},",
            "\"post_unwind_direct_alloc_typed_allocations\":{},",
            "\"post_unwind_direct_alloc_fallback_allocations\":{},",
            "\"post_unwind_direct_alloc_typed_deallocations\":{},",
            "\"post_unwind_direct_alloc_fallback_deallocations\":{},",
            "\"deep_scope_overflow_validated\":{},",
            "\"deep_scope_leaf_main_depth\":{},",
            "\"deep_scope_leaf_overflow_depth\":{},",
            "\"deep_scope_leaf_overflow_capacity\":{},",
            "\"deep_scope_leaf_represented_depth\":{},",
            "\"deep_scope_leaf_active_overflow_scope_represented\":{},",
            "\"deep_scope_post_main_depth\":{},",
            "\"deep_scope_post_overflow_depth\":{},",
            "\"deep_scope_post_represented_depth\":{},",
            "\"deep_scope_returned_len\":{},",
            "\"deep_scope_checksum\":{},",
            "\"unrepresented_scope_fallback_validated\":{},",
            "\"unrepresented_scope_leaf_main_depth\":{},",
            "\"unrepresented_scope_leaf_overflow_depth\":{},",
            "\"unrepresented_scope_leaf_overflow_capacity\":{},",
            "\"unrepresented_scope_leaf_represented_depth\":{},",
            "\"unrepresented_scope_leaf_active_overflow_scope_represented\":{},",
            "\"unrepresented_scope_direct_alloc_total_allocations\":{},",
            "\"unrepresented_scope_direct_alloc_typed_allocations\":{},",
            "\"unrepresented_scope_direct_alloc_fallback_allocations\":{},",
            "\"unrepresented_scope_direct_alloc_typed_deallocations\":{},",
            "\"unrepresented_scope_direct_alloc_fallback_deallocations\":{},",
            "\"unrepresented_scope_post_main_depth\":{},",
            "\"unrepresented_scope_post_overflow_depth\":{},",
            "\"unrepresented_scope_post_represented_depth\":{},",
            "\"type_rows\":{}",
            "}}"
        ),
        snap.total_allocations,
        snap.typed_allocations,
        snap.fallback_allocations,
        fallback_attribution_allocations,
        fallback_attribution.raw_alloc_no_metadata,
        fallback_attribution.raw_alloc_no_metadata_bytes,
        fallback_attribution.raw_dealloc_no_metadata,
        fallback_attribution.raw_realloc_no_metadata,
        fallback_attribution.raw_realloc_no_metadata_bytes,
        fallback_attribution.raw_realloc_moved_dealloc_no_metadata,
        fallback_attribution.realloc_recorded_old_metadata_new_allocations,
        fallback_attribution.realloc_recorded_old_metadata_new_allocation_bytes,
        snap.typed_deallocations,
        snap.policy_flags_seen,
        lowered_rows,
        lowered_deallocation_rows,
        drop_scope_deallocation_rows,
        semantic_metadata_validation_json,
        semantic_metadata_validation.recovery_identity_matches,
        semantic_metadata_validation.recovery_identity_mismatches,
        semantic_metadata_validation.last_mismatch_requested_type_id,
        semantic_metadata_validation.last_mismatch_recorded_type_id,
        semantic_metadata_validation.last_mismatch_requested_module_id,
        semantic_metadata_validation.last_mismatch_recorded_module_id,
        semantic_metadata_validation.last_mismatch_requested_callsite,
        semantic_metadata_validation.last_mismatch_recorded_callsite,
        unwind_scope_cleanup.validated,
        unwind_scope_cleanup.panic_observed,
        unwind_scope_cleanup.direct_alloc_total_allocations,
        unwind_scope_cleanup.direct_alloc_typed_allocations,
        unwind_scope_cleanup.direct_alloc_fallback_allocations,
        unwind_scope_cleanup.direct_alloc_typed_deallocations,
        unwind_scope_cleanup.direct_alloc_fallback_deallocations,
        deep_scope_overflow.validated,
        deep_scope_overflow.leaf_main_depth,
        deep_scope_overflow.leaf_overflow_depth,
        deep_scope_overflow.leaf_overflow_capacity,
        deep_scope_overflow.leaf_represented_depth,
        deep_scope_overflow.leaf_active_overflow_scope_represented,
        deep_scope_overflow.post_main_depth,
        deep_scope_overflow.post_overflow_depth,
        deep_scope_overflow.post_represented_depth,
        deep_scope_overflow.returned_len,
        deep_scope_overflow.checksum,
        unrepresented_scope_fallback.validated,
        unrepresented_scope_fallback.leaf_main_depth,
        unrepresented_scope_fallback.leaf_overflow_depth,
        unrepresented_scope_fallback.leaf_overflow_capacity,
        unrepresented_scope_fallback.leaf_represented_depth,
        unrepresented_scope_fallback.leaf_active_overflow_scope_represented,
        unrepresented_scope_fallback.direct_alloc_total_allocations,
        unrepresented_scope_fallback.direct_alloc_typed_allocations,
        unrepresented_scope_fallback.direct_alloc_fallback_allocations,
        unrepresented_scope_fallback.direct_alloc_typed_deallocations,
        unrepresented_scope_fallback.direct_alloc_fallback_deallocations,
        unrepresented_scope_fallback.post_main_depth,
        unrepresented_scope_fallback.post_overflow_depth,
        unrepresented_scope_fallback.post_represented_depth,
        type_rows_json
    );
}
