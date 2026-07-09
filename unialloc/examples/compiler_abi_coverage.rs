#![feature(allocator_api)]

use std::alloc::Layout;
use std::collections::{BTreeMap, BinaryHeap, VecDeque};
use std::mem::{align_of, size_of};

use unialloc::alloc_api::{
    __unialloc_alloc_with_metadata, __unialloc_dealloc_with_metadata,
    __unialloc_realloc_with_split_metadata, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_id, FLAG_DELAYED_FREE, FLAG_FORCE_INITIALIZE, FLAG_METADATA_SEGREGATED,
    FLAG_TYPE_ISOLATED,
};
use unialloc::UniAlloc;

#[global_allocator]
static A: UniAlloc = UniAlloc;

const MODULE_STD_PROXY: u64 = 0xA110_C0DE;

#[inline(never)]
fn black_box<T>(value: T) -> T {
    unsafe {
        let ret = std::ptr::read_volatile(&value);
        std::mem::forget(value);
        ret
    }
}

#[repr(C)]
#[derive(Clone, Copy)]
struct BTreeNodeProxy {
    len: usize,
    keys: [u64; 11],
    values: [u64; 11],
}

#[repr(C)]
#[derive(Clone, Copy)]
struct VecDequeBlockProxy {
    head: usize,
    tail: usize,
    values: [u64; 32],
}

#[repr(C)]
#[derive(Clone, Copy)]
struct StringBufferProxy {
    bytes: [u8; 96],
}

#[repr(C)]
#[derive(Clone, Copy)]
struct MapEntryProxy {
    key: u64,
    value: u64,
    next_hash: u64,
}

unsafe fn abi_alloc<T>(flags: u32, callsite: u64) -> *mut T {
    let layout = Layout::new::<T>();
    __unialloc_alloc_with_metadata(
        layout.size(),
        layout.align(),
        semantic_type_id::<T>(),
        MODULE_STD_PROXY,
        flags,
        callsite,
    ) as *mut T
}

unsafe fn abi_alloc_array<T>(len: usize, flags: u32, callsite: u64) -> (*mut T, Layout) {
    let layout =
        Layout::from_size_align(size_of::<T>() * len, align_of::<T>()).expect("valid array layout");
    let ptr = __unialloc_alloc_with_metadata(
        layout.size(),
        layout.align(),
        semantic_type_id::<T>(),
        MODULE_STD_PROXY,
        flags,
        callsite,
    ) as *mut T;
    (ptr, layout)
}

unsafe fn abi_dealloc<T>(ptr: *mut T, flags: u32, callsite: u64) {
    let layout = Layout::new::<T>();
    __unialloc_dealloc_with_metadata(
        ptr as *mut u8,
        layout.size(),
        layout.align(),
        semantic_type_id::<T>(),
        MODULE_STD_PROXY,
        flags,
        callsite,
    );
}

unsafe fn abi_dealloc_array<T>(ptr: *mut T, layout: Layout, flags: u32, callsite: u64) {
    __unialloc_dealloc_with_metadata(
        ptr as *mut u8,
        layout.size(),
        layout.align(),
        semantic_type_id::<T>(),
        MODULE_STD_PROXY,
        flags,
        callsite,
    );
}

unsafe fn abi_realloc_array<T>(
    ptr: *mut T,
    old_layout: Layout,
    new_len: usize,
    flags: u32,
    old_callsite: u64,
    new_callsite: u64,
) -> (*mut T, Layout) {
    let new_layout = Layout::from_size_align(size_of::<T>() * new_len, align_of::<T>())
        .expect("valid new array layout");
    let new_ptr = __unialloc_realloc_with_split_metadata(
        ptr as *mut u8,
        old_layout.size(),
        old_layout.align(),
        new_layout.size(),
        semantic_type_id::<T>(),
        MODULE_STD_PROXY,
        flags,
        old_callsite,
        semantic_type_id::<T>(),
        MODULE_STD_PROXY,
        flags,
        new_callsite,
    ) as *mut T;
    (new_ptr, new_layout)
}

fn fallback_std_activity(scale: usize) {
    let mut vec = Vec::with_capacity(scale);
    for i in 0..scale {
        vec.push(i as u64);
    }

    let mut heap = BinaryHeap::new();
    for item in vec.iter().copied() {
        heap.push(item);
    }

    let mut deque = VecDeque::with_capacity(scale);
    while let Some(item) = heap.pop() {
        deque.push_back(item);
    }

    let mut map = BTreeMap::new();
    for (idx, item) in deque.iter().copied().enumerate() {
        map.insert(idx as u64, item);
    }

    drop(black_box((vec, deque, map)));
}

fn emit_events(label: &str) {
    let snap = semantic_stats_snapshot();
    println!(
        "{{\"source\":\"compiler_abi_coverage_example\",\"type_id_basis\":\"ffi-abi-proxy-rust-type-id\",\"label\":\"{}\",\"typed\":true,\"type_id\":\"aggregate\",\"count\":{},\"bytes\":{},\"cache_hits\":{},\"cache_inserts\":{},\"cache_bypasses\":{},\"delayed_free_enqueues\":{},\"delayed_free_flushes\":{}}}",
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
        "{{\"source\":\"compiler_abi_coverage_example\",\"type_id_basis\":\"ffi-abi-proxy-rust-type-id\",\"label\":\"{}\",\"typed\":false,\"type_id\":0,\"count\":{},\"bytes\":{}}}",
        label, snap.fallback_allocations, snap.fallback_allocated_bytes
    );
}

fn run_box_pattern() {
    semantic_stats_reset();
    unsafe {
        for i in 0..64u64 {
            let ptr = abi_alloc::<u64>(FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE, 0xB0_0001);
            assert!(!ptr.is_null());
            ptr.write(i);
            abi_dealloc::<u64>(ptr, FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE, 0xB0_0001);
        }
    }
    fallback_std_activity(8);
    emit_events("box-like");
}

fn run_vec_pattern() {
    semantic_stats_reset();
    unsafe {
        let (ptr, layout) =
            abi_alloc_array::<u64>(64, FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED, 0xB0_0002);
        assert!(!ptr.is_null());
        for i in 0..64 {
            ptr.add(i).write(i as u64);
        }

        let (ptr, layout) = abi_realloc_array::<u64>(
            ptr,
            layout,
            128,
            FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
            0xB0_0002,
            0xB0_0003,
        );
        assert!(!ptr.is_null());
        for i in 64..128 {
            ptr.add(i).write(i as u64);
        }
        abi_dealloc_array::<u64>(
            ptr,
            layout,
            FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
            0xB0_0002,
        );
    }
    fallback_std_activity(16);
    emit_events("vec-like");
}

fn run_collection_node_patterns() {
    semantic_stats_reset();
    unsafe {
        for i in 0..16u64 {
            let node =
                abi_alloc::<BTreeNodeProxy>(FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE, 0xB0_0004);
            assert!(!node.is_null());
            node.write(BTreeNodeProxy {
                len: 1,
                keys: [i; 11],
                values: [i + 1; 11],
            });
            abi_dealloc::<BTreeNodeProxy>(node, FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE, 0xB0_0004);

            let block = abi_alloc::<VecDequeBlockProxy>(
                FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
                0xB0_0005,
            );
            assert!(!block.is_null());
            block.write(VecDequeBlockProxy {
                head: 0,
                tail: 1,
                values: [i; 32],
            });
            abi_dealloc::<VecDequeBlockProxy>(
                block,
                FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
                0xB0_0005,
            );
        }
    }
    fallback_std_activity(32);
    emit_events("collection-node-like");
}

fn run_string_and_map_patterns() {
    semantic_stats_reset();
    unsafe {
        for i in 0..32u64 {
            let buf = abi_alloc::<StringBufferProxy>(
                FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE,
                0xB0_0006,
            );
            assert!(!buf.is_null());
            (*buf).bytes[0] = (i % 255) as u8;
            abi_dealloc::<StringBufferProxy>(
                buf,
                FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE,
                0xB0_0006,
            );

            let entry = abi_alloc::<MapEntryProxy>(
                FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
                0xB0_0007,
            );
            assert!(!entry.is_null());
            entry.write(MapEntryProxy {
                key: i,
                value: i + 1,
                next_hash: i ^ 0x9E37_79B9,
            });
            abi_dealloc::<MapEntryProxy>(
                entry,
                FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
                0xB0_0007,
            );
        }
    }
    fallback_std_activity(24);
    emit_events("string-map-like");
}

fn main() {
    run_box_pattern();
    run_vec_pattern();
    run_collection_node_patterns();
    run_string_and_map_patterns();
}
