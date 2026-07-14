#![no_std]
#![allow(unused_imports)]
#![allow(dead_code)]
#![allow(clippy::uninit_assumed_init)]
#![feature(allocator_api)]
#![feature(thread_local)]
#![cfg_attr(not(unialloc_has_stable_alloc_c_string), feature(alloc_c_string))]
#![cfg_attr(not(unialloc_has_stable_raw_ref_op), feature(raw_ref_op))]
#![cfg_attr(
    not(unialloc_has_stable_alloc_layout_extra),
    feature(alloc_layout_extra)
)]
#![allow(incomplete_features)]
#![allow(unknown_lints)]
// `core::intrinsics::{likely, unlikely}` is still the compatibility path for
// the pinned paper toolchain.  Keep the use explicit instead of silently
// dropping branch hints from allocator hot paths on old builds.
#![allow(internal_features)]
#![feature(core_intrinsics)]
#![cfg_attr(not(feature = "fixed_heap"), feature(slice_ptr_get))]
#![cfg_attr(not(unialloc_has_stable_slice_ptr_len), feature(slice_ptr_len))]
#![cfg_attr(not(unialloc_has_stable_asm_const), feature(asm_const))]
#![cfg_attr(
    not(unialloc_has_stable_nonnull_slice_from_raw_parts),
    feature(nonnull_slice_from_raw_parts)
)]
#![cfg_attr(not(unialloc_has_stable_const_mut_refs), feature(const_mut_refs))]
#![feature(generic_const_exprs)]

pub mod alloc_api;
mod cache;
mod collections;
mod error;
mod freelist;
mod mm;
mod page;
mod pal;
mod prelude;
mod sc;
mod size_class;
#[cfg(not(feature = "fixed_heap"))]
mod sync;
mod zone;

include!(concat!(env!("OUT_DIR"), "/consts.rs"));
extern crate alloc;

pub use cache::{thread_cache_footprint_snapshot, ThreadCacheFootprintSnapshot};

#[cfg(feature = "stats")]
pub use cache::{
    thread_cache_flush_stats_reset, thread_cache_flush_stats_snapshot,
    ThreadCacheFlushStatsSnapshot,
};
#[cfg(feature = "stats")]
pub use zone::{zone_retained_empty_slab_snapshot, ZoneRetainedEmptySlabSnapshot};

#[cfg(feature = "fixed_heap")]
use core::alloc::{GlobalAlloc, Layout};

pub use alloc_api::{
    active_allocation_metadata, auto_deallocation_metadata, delayed_free_snapshot,
    lifetime_placement_class, metadata_pointer_auth_runtime_probe,
    metadata_segregation_side_cache_snapshot, semantic_auto_compiler_metadata_enable,
    semantic_auto_compiler_metadata_enabled, semantic_auto_compiler_metadata_stream_enable,
    semantic_auto_compiler_metadata_stream_enabled,
    semantic_auto_compiler_metadata_stream_thread_local_recovery_enable,
    semantic_auto_compiler_metadata_thread_local_recovery_enable, semantic_auto_metadata_disable,
    semantic_auto_metadata_enable, semantic_auto_metadata_enabled,
    semantic_auto_metadata_type_id_basis, semantic_fallback_attribution_snapshot,
    semantic_layout_id, semantic_metadata_validation_snapshot,
    semantic_ownership_transfer_snapshot, semantic_runtime_slow_path_enabled,
    semantic_scope_depth_snapshot, semantic_stats_recording_disable,
    semantic_stats_recording_enable, semantic_stats_recording_enabled, semantic_stats_reset,
    semantic_stats_snapshot, semantic_type_id, semantic_type_stats_recording_disable,
    semantic_type_stats_recording_enable, semantic_type_stats_recording_enabled,
    semantic_type_stats_snapshot, type_isolation_side_cache_snapshot, with_rust_type_metadata_at,
    with_semantic_metadata, AllocationMetadata, DelayedFreeSnapshot, LifetimePlacementClass,
    MetadataPointerAuthRuntimeProbe, MetadataSegregationSideCacheSnapshot, SemanticAlloc,
    SemanticFallbackAttributionSnapshot, SemanticMetadataValidationSnapshot,
    SemanticOwnershipTransferSnapshot, SemanticScopeDepthSnapshot, SemanticStatsSnapshot,
    SemanticTypeStatsSnapshot, TypeIsolationSideCacheSnapshot, AUTO_LAYOUT_MODULE_ID,
    FLAG_DELAYED_FREE, FLAG_HUGEPAGE_METADATA, FLAG_TYPE_ISOLATED, LIFETIME_HINT_EPHEMERAL,
    LIFETIME_HINT_LONG_LIVED, SEMANTIC_FALLBACK_ATTRIBUTION_SNAPSHOT_ABI_VERSION,
    SEMANTIC_METADATA_VALIDATION_SNAPSHOT_ABI_VERSION, SEMANTIC_STATS_SNAPSHOT_ABI_VERSION,
    SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION,
};
#[cfg(not(feature = "fixed_heap"))]
pub use alloc_api::{
    hugepage_metadata_side_cache_backing_snapshot, hugepage_metadata_side_cache_snapshot,
    HugepageMetadataSideCacheSnapshot,
};
#[cfg(all(feature = "lifetime_hugepage", not(feature = "fixed_heap")))]
pub use alloc_api::{
    lifetime_hugepage_configure, lifetime_hugepage_phase_flush_current_thread,
    lifetime_hugepage_policy, lifetime_hugepage_stats_reset, lifetime_hugepage_stats_snapshot,
    LifetimeHugepagePolicy, LifetimeHugepageStatsSnapshot, LIFETIME_HUGEPAGE_EXTENT_BYTES,
    LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES,
};
pub use cache::RustAllocator as UniAlloc;
pub use pal::arch::*;
#[cfg(not(feature = "fixed_heap"))]
pub use pal::sys_alloc::{
    hugepage_aligned_fallback_supported, hugepage_fallback_alignment,
    hugepage_mmap_platform_status_snapshot, hugepage_mmap_stats_snapshot, mmap_huge_with_backing,
    HugePageMmapBacking, HugePageMmapPlatformErrorStage, HugePageMmapPlatformStatus,
    HugePageMmapResult, HugePageMmapStats,
};

#[cfg(not(feature = "fixed_heap"))]
pub fn platform_thread_local_backend() -> &'static str {
    pal::sync::general_thread_local::backend_name()
}

#[cfg(feature = "fixed_heap")]
pub fn platform_thread_local_backend() -> &'static str {
    "fixed_heap_no_thread_local_destructor"
}

#[cfg(not(feature = "fixed_heap"))]
pub fn platform_tls_key_ready() -> bool {
    pal::sync::general_thread_local::tls_key_ready()
}

#[cfg(feature = "fixed_heap")]
pub fn platform_tls_key_ready() -> bool {
    true
}

#[cfg(not(feature = "fixed_heap"))]
pub fn platform_tls_save_failure_count() -> usize {
    pal::sync::general_thread_local::tls_save_failure_count()
}

#[cfg(feature = "fixed_heap")]
pub fn platform_tls_save_failure_count() -> usize {
    0
}

pub const fn platform_allocator_backend() -> &'static str {
    if cfg!(feature = "fixed_heap") {
        "fixed_heap"
    } else {
        "page_heap_thread_cache"
    }
}

pub const fn platform_system_backend() -> &'static str {
    if cfg!(feature = "fixed_heap") {
        "fixed_heap"
    } else if cfg!(target_os = "windows") {
        "windows_virtual_alloc"
    } else if cfg!(target_os = "macos") {
        "darwin_mmap"
    } else if cfg!(target_os = "linux") {
        "linux_mmap"
    } else if cfg!(unix) {
        "unix_mmap"
    } else {
        "unknown"
    }
}

#[cfg(feature = "fixed_heap")]
pub fn fixed_heap_ready() -> bool {
    sc::fixed_heap_roots_initialized()
}

#[cfg(not(feature = "fixed_heap"))]
pub fn fixed_heap_ready() -> bool {
    false
}

pub const CONSTRAINED_BOOT_SAMPLE_ABI_VERSION: u32 = 2;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
pub struct UniallocConstrainedBootSample {
    pub boot_cycle: usize,
    pub fixed_heap_ready: bool,
    pub c_abi_invoked: bool,
    pub c_abi_ready_after_init: bool,
    pub c_abi_round_trips: usize,
    pub c_abi_invalid_layouts_rejected: usize,
    pub c_abi_over_page_alignment_checked: bool,
    pub c_abi_over_page_alignment: usize,
    pub allocator_total_allocations: usize,
    pub allocator_total_deallocations: usize,
    pub allocator_typed_allocations: usize,
    pub allocator_typed_deallocations: usize,
    pub allocator_fallback_allocations: usize,
    pub allocator_fallback_deallocations: usize,
    pub allocator_total_allocated_bytes: usize,
    pub allocator_typed_allocated_bytes: usize,
    pub allocator_fallback_allocated_bytes: usize,
    pub allocator_coverage_basis_points: usize,
    pub allocator_type_stats_rows: usize,
    pub allocator_type_stats_dropped_events: usize,
    pub allocator_type_stats_probe_matched: bool,
}

pub fn constrained_boot_sample_snapshot(
    boot_cycle: usize,
    c_abi_invoked: bool,
    c_abi_ready_after_init: bool,
    c_abi_round_trips: usize,
    c_abi_invalid_layouts_rejected: usize,
    c_abi_over_page_alignment_checked: bool,
    c_abi_over_page_alignment: usize,
    allocator_type_stats_rows: usize,
    allocator_type_stats_probe_matched: bool,
) -> UniallocConstrainedBootSample {
    let stats = semantic_stats_snapshot();
    UniallocConstrainedBootSample {
        boot_cycle,
        fixed_heap_ready: fixed_heap_ready(),
        c_abi_invoked,
        c_abi_ready_after_init,
        c_abi_round_trips,
        c_abi_invalid_layouts_rejected,
        c_abi_over_page_alignment_checked,
        c_abi_over_page_alignment,
        allocator_total_allocations: stats.total_allocations,
        allocator_total_deallocations: stats.total_deallocations,
        allocator_typed_allocations: stats.typed_allocations,
        allocator_typed_deallocations: stats.typed_deallocations,
        allocator_fallback_allocations: stats.fallback_allocations,
        allocator_fallback_deallocations: stats.fallback_deallocations,
        allocator_total_allocated_bytes: stats.total_allocated_bytes,
        allocator_typed_allocated_bytes: stats.typed_allocated_bytes,
        allocator_fallback_allocated_bytes: stats.fallback_allocated_bytes,
        allocator_coverage_basis_points: stats.coverage_basis_points,
        allocator_type_stats_rows,
        allocator_type_stats_dropped_events: stats.semantic_type_stats_dropped_events,
        allocator_type_stats_probe_matched,
    }
}

#[no_mangle]
pub extern "C" fn __unialloc_constrained_boot_sample_abi_version() -> u32 {
    CONSTRAINED_BOOT_SAMPLE_ABI_VERSION
}

#[no_mangle]
pub extern "C" fn __unialloc_constrained_boot_sample_size() -> usize {
    core::mem::size_of::<UniallocConstrainedBootSample>()
}

#[no_mangle]
pub unsafe extern "C" fn __unialloc_constrained_boot_sample_checked(
    out: *mut UniallocConstrainedBootSample,
    out_size: usize,
    boot_cycle: usize,
    c_abi_invoked: bool,
    c_abi_ready_after_init: bool,
    c_abi_round_trips: usize,
    c_abi_invalid_layouts_rejected: usize,
    c_abi_over_page_alignment_checked: bool,
    c_abi_over_page_alignment: usize,
    allocator_type_stats_rows: usize,
    allocator_type_stats_probe_matched: bool,
) -> bool {
    if out.is_null() || out_size < core::mem::size_of::<UniallocConstrainedBootSample>() {
        return false;
    }
    out.write(constrained_boot_sample_snapshot(
        boot_cycle,
        c_abi_invoked,
        c_abi_ready_after_init,
        c_abi_round_trips,
        c_abi_invalid_layouts_rejected,
        c_abi_over_page_alignment_checked,
        c_abi_over_page_alignment,
        allocator_type_stats_rows,
        allocator_type_stats_probe_matched,
    ));
    true
}

#[cfg(all(test, feature = "fixed_heap", not(feature = "separate_sc_backend")))]
static FIXED_HEAP_INIT_RACE_TEST_ENABLED: core::sync::atomic::AtomicBool =
    core::sync::atomic::AtomicBool::new(false);
#[cfg(all(test, feature = "fixed_heap", not(feature = "separate_sc_backend")))]
static FIXED_HEAP_INIT_RACE_TEST_ARRIVALS: core::sync::atomic::AtomicUsize =
    core::sync::atomic::AtomicUsize::new(0);
#[cfg(all(test, feature = "fixed_heap", not(feature = "separate_sc_backend")))]
static FIXED_HEAP_INIT_RACE_TEST_PERMITS: core::sync::atomic::AtomicUsize =
    core::sync::atomic::AtomicUsize::new(0);

#[cfg(all(test, feature = "fixed_heap", not(feature = "separate_sc_backend")))]
fn fixed_heap_init_race_test_gate_before_serialization() {
    use core::sync::atomic::Ordering;
    if !FIXED_HEAP_INIT_RACE_TEST_ENABLED.load(Ordering::Acquire) {
        return;
    }
    let ticket = FIXED_HEAP_INIT_RACE_TEST_ARRIVALS.fetch_add(1, Ordering::AcqRel);
    while FIXED_HEAP_INIT_RACE_TEST_PERMITS.load(Ordering::Acquire) <= ticket {
        core::hint::spin_loop();
    }
}

#[cfg(feature = "fixed_heap")]
#[no_mangle]
pub unsafe extern "C" fn unialloc_fixed_heap_try_init(
    heap_start: usize,
    heap_size: usize,
    page_size: usize,
) -> bool {
    // C/kernel callers use this as an ensure-ready ABI.  Once global fixed-heap
    // roots are published, do not re-run init_with_range: doing so could reset
    // allocator metadata while existing allocations are still live.
    if fixed_heap_ready() {
        return true;
    }
    UniAlloc.try_init(heap_start, heap_size, page_size)
}

#[cfg(feature = "fixed_heap")]
#[no_mangle]
pub unsafe extern "C" fn unialloc_fixed_heap_try_extend(size: usize, page_size: usize) -> bool {
    if !fixed_heap_ready() {
        return false;
    }
    UniAlloc.try_extend(size, page_size)
}

#[cfg(feature = "fixed_heap")]
#[no_mangle]
pub unsafe extern "C" fn unialloc_fixed_heap_extend(size: usize, page_size: usize) {
    let _ = unialloc_fixed_heap_try_extend(size, page_size);
}

#[cfg(feature = "fixed_heap")]
#[no_mangle]
pub extern "C" fn unialloc_fixed_heap_ready() -> bool {
    fixed_heap_ready()
}

#[cfg(feature = "fixed_heap")]
fn unialloc_ffi_layout(size: usize, align: usize) -> Option<Layout> {
    if size == 0 {
        return None;
    }
    Layout::from_size_align(size, align).ok()
}

#[cfg(feature = "fixed_heap")]
#[no_mangle]
pub unsafe extern "C" fn unialloc_alloc(size: usize, align: usize) -> *mut u8 {
    match unialloc_ffi_layout(size, align) {
        Some(layout) => GlobalAlloc::alloc(&UniAlloc, layout),
        None => core::ptr::null_mut(),
    }
}

#[cfg(feature = "fixed_heap")]
#[no_mangle]
pub unsafe extern "C" fn unialloc_dealloc(ptr: *mut u8, size: usize, align: usize) {
    if ptr.is_null() {
        return;
    }
    if let Some(layout) = unialloc_ffi_layout(size, align) {
        GlobalAlloc::dealloc(&UniAlloc, ptr, layout);
    }
}

#[cfg(feature = "fixed_heap")]
#[no_mangle]
pub unsafe extern "C" fn unialloc_realloc(
    ptr: *mut u8,
    old_size: usize,
    old_align: usize,
    new_size: usize,
) -> *mut u8 {
    if ptr.is_null() {
        return unialloc_alloc(new_size, old_align);
    }
    if new_size == 0 {
        unialloc_dealloc(ptr, old_size, old_align);
        return core::ptr::null_mut();
    }
    match unialloc_ffi_layout(old_size, old_align) {
        Some(layout) => GlobalAlloc::realloc(&UniAlloc, ptr, layout, new_size),
        None => core::ptr::null_mut(),
    }
}

#[cfg(all(test, feature = "fixed_heap"))]
mod fixed_heap_c_abi_tests {
    use super::*;

    #[cfg(not(feature = "separate_sc_backend"))]
    mod fixed_heap_init_race {
        use super::*;
        extern crate std;

        const RACE_HEAP_BYTES: usize = 64 * 1024 * 1024;
        const RACE_CHILD_ENV: &str = "UNIALLOC_FIXED_HEAP_INIT_RACE_CHILD";
        const RACE_TEST_NAME: &str = concat!(
            "fixed_heap_c_abi_tests::fixed_heap_init_race::",
            "concurrent_fixed_heap_try_init_must_not_replace_live_roots"
        );

        #[repr(align(16384))]
        struct RaceHeap([u8; RACE_HEAP_BYTES]);

        static mut RACE_HEAP_A: RaceHeap = RaceHeap([0; RACE_HEAP_BYTES]);
        static mut RACE_HEAP_B: RaceHeap = RaceHeap([0; RACE_HEAP_BYTES]);

        fn wait_for_race_arrivals(expected: usize) {
            use core::sync::atomic::Ordering;
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
            while FIXED_HEAP_INIT_RACE_TEST_ARRIVALS.load(Ordering::Acquire) < expected {
                assert!(
                    std::time::Instant::now() < deadline,
                    "init caller did not reach test gate"
                );
                std::thread::yield_now();
            }
        }

        #[test]
        fn concurrent_fixed_heap_try_init_must_not_replace_live_roots() {
            use core::sync::atomic::Ordering;

            // Other fixed-heap unit tests lazily initialize process-wide roots.
            // Re-exec this one test so its race fixture always owns a fresh set of
            // allocator globals, even when the full test binary runs in parallel.
            if std::env::var_os(RACE_CHILD_ENV).is_none() {
                let status = std::process::Command::new(
                    std::env::current_exe().expect("current fixed-heap test executable"),
                )
                .args(["--exact", RACE_TEST_NAME, "--nocapture"])
                .env(RACE_CHILD_ENV, "1")
                .status()
                .expect("spawn isolated fixed-heap init race test");
                assert!(status.success(), "isolated fixed-heap race test failed");
                return;
            }

            assert!(!unialloc_fixed_heap_ready());
            let heap_a = unsafe { core::ptr::addr_of_mut!(RACE_HEAP_A.0).cast::<u8>() as usize };
            let heap_b = unsafe { core::ptr::addr_of_mut!(RACE_HEAP_B.0).cast::<u8>() as usize };
            assert_eq!(heap_a % crate::PAGE_SIZE, 0);
            assert_eq!(heap_b % crate::PAGE_SIZE, 0);

            FIXED_HEAP_INIT_RACE_TEST_ARRIVALS.store(0, Ordering::Release);
            FIXED_HEAP_INIT_RACE_TEST_PERMITS.store(0, Ordering::Release);
            FIXED_HEAP_INIT_RACE_TEST_ENABLED.store(true, Ordering::Release);

            let first = std::thread::spawn(move || unsafe {
                UniAlloc.try_init(heap_a, RACE_HEAP_BYTES, crate::PAGE_SIZE)
            });
            wait_for_race_arrivals(1);
            let second = std::thread::spawn(move || unsafe {
                UniAlloc.try_init(heap_b, RACE_HEAP_BYTES, crate::PAGE_SIZE)
            });
            wait_for_race_arrivals(2);

            FIXED_HEAP_INIT_RACE_TEST_PERMITS.store(1, Ordering::Release);
            assert!(first.join().expect("first init thread"));
            let first_tcache = crate::cache::GlobalTcache_ptr.load(Ordering::Acquire);
            let first_zone = crate::zone::GLOBAL_ZONE_ptr.load(Ordering::Acquire);
            assert!(!first_tcache.is_null());
            assert!(!first_zone.is_null());
            let live = unsafe { unialloc_alloc(32, 8) };
            assert!(!live.is_null());

            FIXED_HEAP_INIT_RACE_TEST_PERMITS.store(2, Ordering::Release);
            assert!(second.join().expect("second init thread"));
            FIXED_HEAP_INIT_RACE_TEST_ENABLED.store(false, Ordering::Release);

            let final_tcache = crate::cache::GlobalTcache_ptr.load(Ordering::Acquire);
            let final_zone = crate::zone::GLOBAL_ZONE_ptr.load(Ordering::Acquire);
            assert_eq!(
                final_tcache, first_tcache,
                "a stale concurrent caller replaced the live fixed-heap thread-cache root"
            );
            assert_eq!(
                final_zone, first_zone,
                "a stale concurrent caller replaced the live fixed-heap zone root"
            );
            unsafe { unialloc_dealloc(live, 32, 8) };
        }
    }

    fn ensure_ready() -> spin::MutexGuard<'static, ()> {
        let guard = crate::sc::fixed_heap_test_guard();
        assert!(unialloc_fixed_heap_ready());
        guard
    }

    #[test]
    fn fixed_heap_c_abi_alloc_realloc_dealloc_round_trip() {
        unsafe {
            let _fixed_heap_guard = ensure_ready();
            let ptr = unialloc_alloc(32, 8);
            assert!(!ptr.is_null());
            core::ptr::write_bytes(ptr, 0xA5, 32);

            let grown = unialloc_realloc(ptr, 32, 8, 64);
            assert!(!grown.is_null());
            for index in 0..32 {
                assert_eq!(*grown.add(index), 0xA5);
            }
            unialloc_dealloc(grown, 64, 8);
        }
    }

    #[test]
    fn fixed_heap_c_abi_rejects_invalid_layouts_without_allocating() {
        unsafe {
            let _fixed_heap_guard = ensure_ready();
            assert!(unialloc_alloc(0, 8).is_null());
            assert!(unialloc_alloc(16, 3).is_null());
            assert!(unialloc_realloc(core::ptr::null_mut(), 16, 3, 32).is_null());
            unialloc_dealloc(core::ptr::null_mut(), 16, 8);
        }
    }

    #[test]
    fn fixed_heap_c_abi_try_init_is_idempotent_after_ready() {
        unsafe {
            let _fixed_heap_guard = ensure_ready();
            assert!(unialloc_fixed_heap_try_init(0, 0, 0));
            assert!(unialloc_fixed_heap_ready());

            let ptr = unialloc_alloc(32, 8);
            assert!(!ptr.is_null());
            unialloc_dealloc(ptr, 32, 8);
        }
    }

    #[test]
    fn fixed_heap_c_abi_try_extend_rejects_invalid_without_breaking_alloc() {
        unsafe {
            let _fixed_heap_guard = ensure_ready();
            assert!(!unialloc_fixed_heap_try_extend(0, crate::PAGE_SIZE));
            assert!(!unialloc_fixed_heap_try_extend(crate::PAGE_SIZE, 3));

            let ptr = unialloc_alloc(32, 8);
            assert!(!ptr.is_null());
            unialloc_dealloc(ptr, 32, 8);
        }
    }

    #[cfg(feature = "stats")]
    #[test]
    fn constrained_boot_sample_abi_reports_fixed_heap_and_allocator_counters() {
        unsafe {
            let _fixed_heap_guard = ensure_ready();
            semantic_stats_reset();

            let ptr = unialloc_alloc(64, 8);
            assert!(!ptr.is_null());
            core::ptr::write_bytes(ptr, 0xAB, 64);
            unialloc_dealloc(ptr, 64, 8);

            let mut sample = UniallocConstrainedBootSample {
                boot_cycle: 0,
                fixed_heap_ready: false,
                c_abi_invoked: false,
                c_abi_ready_after_init: false,
                c_abi_round_trips: 0,
                c_abi_invalid_layouts_rejected: 0,
                c_abi_over_page_alignment_checked: false,
                c_abi_over_page_alignment: 0,
                allocator_total_allocations: 0,
                allocator_total_deallocations: 0,
                allocator_typed_allocations: 0,
                allocator_typed_deallocations: 0,
                allocator_fallback_allocations: 0,
                allocator_fallback_deallocations: 0,
                allocator_total_allocated_bytes: 0,
                allocator_typed_allocated_bytes: 0,
                allocator_fallback_allocated_bytes: 0,
                allocator_coverage_basis_points: 0,
                allocator_type_stats_rows: 0,
                allocator_type_stats_dropped_events: usize::MAX,
                allocator_type_stats_probe_matched: false,
            };
            assert_eq!(
                __unialloc_constrained_boot_sample_abi_version(),
                CONSTRAINED_BOOT_SAMPLE_ABI_VERSION
            );
            assert_eq!(
                __unialloc_constrained_boot_sample_size(),
                core::mem::size_of::<UniallocConstrainedBootSample>()
            );
            assert!(__unialloc_constrained_boot_sample_checked(
                &mut sample,
                core::mem::size_of::<UniallocConstrainedBootSample>(),
                1,
                true,
                true,
                2,
                3,
                true,
                crate::PAGE_SIZE * 2,
                1,
                true,
            ));

            assert_eq!(sample.boot_cycle, 1);
            assert!(sample.fixed_heap_ready);
            assert!(sample.c_abi_invoked);
            assert!(sample.c_abi_ready_after_init);
            assert_eq!(sample.c_abi_round_trips, 2);
            assert_eq!(sample.c_abi_invalid_layouts_rejected, 3);
            assert!(sample.c_abi_over_page_alignment_checked);
            assert!(sample.allocator_total_allocations >= 1);
            assert!(sample.allocator_total_deallocations >= 1);
            assert!(sample.allocator_total_allocated_bytes >= 64);
            assert_eq!(sample.allocator_type_stats_rows, 1);
            assert_eq!(sample.allocator_type_stats_dropped_events, 0);
            assert!(sample.allocator_type_stats_probe_matched);
        }
    }
}

// use core::panic::PanicInfo;

// #[panic_handler]
// fn panic1(info: &PanicInfo) -> ! {
//     println!("{}", info);
//     loop {}
// }
