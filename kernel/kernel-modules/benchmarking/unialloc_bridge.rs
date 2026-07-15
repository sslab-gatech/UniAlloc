//! UniAlloc fixed-heap bridge for the Rust-for-Linux benchmark module.
//!
//! This module deliberately calls UniAlloc through the exported C ABI instead
//! of benchmarking the kernel crate's default allocator by accident.  The heap
//! is a page-aligned static region so the benchmark can run in constrained
//! kernel/module environments without depending on userspace mmap.

use core::alloc::{GlobalAlloc, Layout};
use core::hint::spin_loop;
use core::mem::{size_of, MaybeUninit};
use core::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

#[path = "unialloc_bridge_state.rs"]
mod bridge_state;

use bridge_state::{bridge_initialization_action, BridgeInitializationAction};

const UNIALLOC_KERNEL_HEAP_BYTES: usize = 8 * 1024 * 1024;
const UNIALLOC_KERNEL_INITIAL_HEAP_BYTES: usize = UNIALLOC_KERNEL_HEAP_BYTES / 2;
const UNIALLOC_KERNEL_PAGE_SIZE: usize = 4096;
pub const UNIALLOC_SEMANTIC_STATS_SNAPSHOT_ABI_VERSION: u32 = 4;
pub const UNIALLOC_SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION: u32 = 2;
pub const UNIALLOC_SEMANTIC_FALLBACK_ATTRIBUTION_SNAPSHOT_ABI_VERSION: u32 = 1;
pub const UNIALLOC_SEMANTIC_METADATA_VALIDATION_SNAPSHOT_ABI_VERSION: u32 = 1;
pub const UNIALLOC_CONSTRAINED_BOOT_SAMPLE_ABI_VERSION: u32 = 2;
pub const UNIALLOC_FLAG_TYPE_ISOLATED: u32 = 1 << 0;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct SemanticStatsSnapshot {
    pub total_allocations: usize,
    pub typed_allocations: usize,
    pub fallback_allocations: usize,
    pub total_allocated_bytes: usize,
    pub typed_allocated_bytes: usize,
    pub fallback_allocated_bytes: usize,
    pub typed_deallocations: usize,
    pub fallback_deallocations: usize,
    pub policy_flags_seen: u32,
    pub last_type_id: u64,
    pub coverage_basis_points: usize,
    pub typed_cache_hits: usize,
    pub typed_cache_inserts: usize,
    pub typed_cache_bypasses: usize,
    pub typed_cache_wrong_identity_denials: usize,
    pub last_wrong_identity_requested_type_id: u64,
    pub last_wrong_identity_retained_type_id: u64,
    pub last_wrong_identity_requested_module_id: u64,
    pub last_wrong_identity_retained_module_id: u64,
    pub last_wrong_identity_requested_callsite: u64,
    pub last_wrong_identity_size: usize,
    pub last_wrong_identity_align: usize,
    pub delayed_free_enqueues: usize,
    pub delayed_free_flushes: usize,
    pub metadata_pac_auth_signs: usize,
    pub metadata_pac_auth_verifications: usize,
    pub metadata_pac_auth_failures: usize,
    pub metadata_pac_software_fallback_signs: usize,
    pub metadata_pac_software_fallback_verifications: usize,
    pub metadata_pac_software_fallback_failures: usize,
    pub total_deallocations: usize,
    pub semantic_type_stats_dropped_events: usize,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct SemanticTypeStatsSnapshot {
    pub type_id: u64,
    pub module_id: u64,
    pub callsite: u64,
    pub allocations: usize,
    pub allocated_bytes: usize,
    pub deallocations: usize,
    pub cache_hits: usize,
    pub cache_inserts: usize,
    pub cache_bypasses: usize,
    pub observed_alloc_size: usize,
    pub observed_alloc_align: usize,
    pub observed_dealloc_size: usize,
    pub observed_dealloc_align: usize,
    pub policy_flags_seen: u32,
}

impl SemanticTypeStatsSnapshot {
    pub const fn empty() -> Self {
        Self {
            type_id: 0,
            module_id: 0,
            callsite: 0,
            allocations: 0,
            allocated_bytes: 0,
            deallocations: 0,
            cache_hits: 0,
            cache_inserts: 0,
            cache_bypasses: 0,
            observed_alloc_size: 0,
            observed_alloc_align: 0,
            observed_dealloc_size: 0,
            observed_dealloc_align: 0,
            policy_flags_seen: 0,
        }
    }
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct SemanticFallbackAttributionSnapshot {
    pub raw_alloc_no_metadata: usize,
    pub raw_alloc_no_metadata_bytes: usize,
    pub raw_dealloc_no_metadata: usize,
    pub raw_realloc_no_metadata: usize,
    pub raw_realloc_no_metadata_bytes: usize,
    pub raw_realloc_moved_dealloc_no_metadata: usize,
    pub realloc_recorded_old_metadata_new_allocations: usize,
    pub realloc_recorded_old_metadata_new_allocation_bytes: usize,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct SemanticMetadataValidationSnapshot {
    pub recovery_identity_matches: usize,
    pub recovery_identity_mismatches: usize,
    pub last_mismatch_requested_type_id: u64,
    pub last_mismatch_recorded_type_id: u64,
    pub last_mismatch_requested_module_id: u64,
    pub last_mismatch_recorded_module_id: u64,
    pub last_mismatch_requested_callsite: u64,
    pub last_mismatch_recorded_callsite: u64,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct ConstrainedBootSample {
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

#[derive(Clone, Copy)]
pub struct CAbiProbeReport {
    pub invoked: bool,
    pub ready_after_init: bool,
    pub invalid_layouts_rejected: usize,
    pub round_trips: usize,
    pub over_page_alignment_checked: bool,
    pub over_page_alignment: usize,
    pub passed: bool,
}

#[repr(align(4096))]
struct UniAllocKernelHeap([u8; UNIALLOC_KERNEL_HEAP_BYTES]);

static mut UNIALLOC_KERNEL_HEAP: UniAllocKernelHeap =
    UniAllocKernelHeap([0; UNIALLOC_KERNEL_HEAP_BYTES]);
static UNIALLOC_KERNEL_HEAP_READY: AtomicBool = AtomicBool::new(false);
static UNIALLOC_KERNEL_COMMITTED_BYTES: AtomicUsize = AtomicUsize::new(0);
static UNIALLOC_KERNEL_HEAP_MUTATION_LOCKED: AtomicBool = AtomicBool::new(false);

struct HeapMutationGuard;

impl HeapMutationGuard {
    fn acquire() -> Self {
        while UNIALLOC_KERNEL_HEAP_MUTATION_LOCKED
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            spin_loop();
        }
        Self
    }
}

impl Drop for HeapMutationGuard {
    fn drop(&mut self) {
        UNIALLOC_KERNEL_HEAP_MUTATION_LOCKED.store(false, Ordering::Release);
    }
}

extern "C" {
    fn unialloc_fixed_heap_try_init(heap_start: usize, heap_size: usize, page_size: usize) -> bool;
    fn unialloc_fixed_heap_ready() -> bool;
    fn unialloc_fixed_heap_try_extend(size: usize, page_size: usize) -> bool;
    fn unialloc_alloc(size: usize, align: usize) -> *mut u8;
    fn unialloc_dealloc(ptr: *mut u8, size: usize, align: usize);
    fn unialloc_realloc(
        ptr: *mut u8,
        old_size: usize,
        old_align: usize,
        new_size: usize,
    ) -> *mut u8;
    fn __unialloc_alloc_with_metadata(
        size: usize,
        align: usize,
        type_id: u64,
        module_id: u64,
        flags: u32,
        callsite: u64,
    ) -> *mut u8;
    fn __unialloc_dealloc_with_metadata(
        ptr: *mut u8,
        size: usize,
        align: usize,
        type_id: u64,
        module_id: u64,
        flags: u32,
        callsite: u64,
    ) -> bool;
    fn __unialloc_semantic_stats_snapshot_abi_version() -> u32;
    fn __unialloc_semantic_stats_snapshot_size() -> usize;
    fn __unialloc_semantic_stats_snapshot_checked(
        out: *mut SemanticStatsSnapshot,
        out_size: usize,
    ) -> bool;
    fn __unialloc_semantic_stats_reset();
    fn __unialloc_semantic_type_stats_snapshot_abi_version() -> u32;
    fn __unialloc_semantic_type_stats_snapshot_record_size() -> usize;
    fn __unialloc_semantic_type_stats_snapshot_checked(
        out: *mut SemanticTypeStatsSnapshot,
        len: usize,
        record_size: usize,
    ) -> usize;
    fn __unialloc_semantic_fallback_attribution_snapshot_abi_version() -> u32;
    fn __unialloc_semantic_fallback_attribution_snapshot_size() -> usize;
    fn __unialloc_semantic_fallback_attribution_snapshot_checked(
        out: *mut SemanticFallbackAttributionSnapshot,
        out_size: usize,
    ) -> bool;
    fn __unialloc_semantic_metadata_validation_snapshot_abi_version() -> u32;
    fn __unialloc_semantic_metadata_validation_snapshot_size() -> usize;
    fn __unialloc_semantic_metadata_validation_snapshot_checked(
        out: *mut SemanticMetadataValidationSnapshot,
        out_size: usize,
    ) -> bool;
    fn __unialloc_constrained_boot_sample_abi_version() -> u32;
    fn __unialloc_constrained_boot_sample_size() -> usize;
    fn __unialloc_constrained_boot_sample_checked(
        out: *mut ConstrainedBootSample,
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
    ) -> bool;
}

pub struct UniAllocKernelGlobal;

#[global_allocator]
static UNIALLOC_KERNEL_ALLOCATOR: UniAllocKernelGlobal = UniAllocKernelGlobal;

unsafe impl GlobalAlloc for UniAllocKernelGlobal {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if !is_ready() {
            return core::ptr::null_mut();
        }
        unialloc_alloc(layout.size(), layout.align())
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unialloc_dealloc(ptr, layout.size(), layout.align());
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        if !is_ready() {
            return core::ptr::null_mut();
        }
        unialloc_realloc(ptr, layout.size(), layout.align(), new_size)
    }
}

pub fn ensure_initialized() -> bool {
    if UNIALLOC_KERNEL_HEAP_READY.load(Ordering::Acquire) {
        return true;
    }

    let _guard = HeapMutationGuard::acquire();
    if UNIALLOC_KERNEL_HEAP_READY.load(Ordering::Acquire) {
        return true;
    }
    loop {
        let committed = UNIALLOC_KERNEL_COMMITTED_BYTES.load(Ordering::Acquire);
        let allocator_ready = unsafe { unialloc_fixed_heap_ready() };
        match bridge_initialization_action(committed, UNIALLOC_KERNEL_HEAP_BYTES, allocator_ready) {
            BridgeInitializationAction::Initialize => {
                let accepted = unsafe {
                    let heap_start =
                        core::ptr::addr_of_mut!(UNIALLOC_KERNEL_HEAP.0).cast::<u8>() as usize;
                    unialloc_fixed_heap_try_init(
                        heap_start,
                        UNIALLOC_KERNEL_INITIAL_HEAP_BYTES,
                        UNIALLOC_KERNEL_PAGE_SIZE,
                    )
                };
                if !accepted {
                    return false;
                }
                UNIALLOC_KERNEL_COMMITTED_BYTES
                    .store(UNIALLOC_KERNEL_INITIAL_HEAP_BYTES, Ordering::Release);
            }
            BridgeInitializationAction::Extend(remaining) => {
                if !try_extend_reserved_locked(remaining) {
                    return false;
                }
            }
            BridgeInitializationAction::MarkReady => {
                UNIALLOC_KERNEL_HEAP_READY.store(true, Ordering::Release);
                return true;
            }
            BridgeInitializationAction::Reject => return false,
        }
    }
}

pub fn try_extend_reserved(size: usize) -> bool {
    if size == 0 {
        return true;
    }
    let _guard = HeapMutationGuard::acquire();
    try_extend_reserved_locked(size)
}

fn try_extend_reserved_locked(size: usize) -> bool {
    let committed = UNIALLOC_KERNEL_COMMITTED_BYTES.load(Ordering::Acquire);
    if committed == 0 || !unsafe { unialloc_fixed_heap_ready() } {
        return false;
    }
    let new_committed = match committed.checked_add(size) {
        Some(value) => value,
        None => return false,
    };
    if new_committed > UNIALLOC_KERNEL_HEAP_BYTES {
        return false;
    }
    if !unsafe { unialloc_fixed_heap_try_extend(size, UNIALLOC_KERNEL_PAGE_SIZE) } {
        return false;
    }
    UNIALLOC_KERNEL_COMMITTED_BYTES.store(new_committed, Ordering::Release);
    true
}

pub fn is_ready() -> bool {
    UNIALLOC_KERNEL_HEAP_READY.load(Ordering::Acquire)
}

pub fn committed_bytes() -> usize {
    UNIALLOC_KERNEL_COMMITTED_BYTES.load(Ordering::Acquire)
}

pub fn reset_semantic_stats() {
    unsafe {
        __unialloc_semantic_stats_reset();
    }
}

pub fn semantic_stats_snapshot_abi() -> (u32, usize) {
    unsafe {
        (
            __unialloc_semantic_stats_snapshot_abi_version(),
            __unialloc_semantic_stats_snapshot_size(),
        )
    }
}

pub fn semantic_stats_snapshot() -> Option<SemanticStatsSnapshot> {
    let (runtime_version, runtime_size) = semantic_stats_snapshot_abi();
    if runtime_version != UNIALLOC_SEMANTIC_STATS_SNAPSHOT_ABI_VERSION
        || runtime_size != size_of::<SemanticStatsSnapshot>()
    {
        return None;
    }

    let mut snapshot = MaybeUninit::<SemanticStatsSnapshot>::uninit();
    let copied = unsafe {
        __unialloc_semantic_stats_snapshot_checked(
            snapshot.as_mut_ptr(),
            size_of::<SemanticStatsSnapshot>(),
        )
    };
    if copied {
        Some(unsafe { snapshot.assume_init() })
    } else {
        None
    }
}

/// Allocate with explicit compiler/type metadata through the UniAlloc semantic ABI.
pub unsafe fn semantic_alloc_with_metadata(
    size: usize,
    align: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> *mut u8 {
    __unialloc_alloc_with_metadata(size, align, type_id, module_id, flags, callsite)
}

/// Deallocate with explicit compiler/type metadata through the UniAlloc semantic ABI.
pub unsafe fn semantic_dealloc_with_metadata(
    ptr: *mut u8,
    size: usize,
    align: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> bool {
    __unialloc_dealloc_with_metadata(ptr, size, align, type_id, module_id, flags, callsite)
}

pub fn semantic_type_stats_snapshot_abi() -> (u32, usize) {
    unsafe {
        (
            __unialloc_semantic_type_stats_snapshot_abi_version(),
            __unialloc_semantic_type_stats_snapshot_record_size(),
        )
    }
}

pub fn semantic_type_stats_snapshot(out: &mut [SemanticTypeStatsSnapshot]) -> Option<usize> {
    let (runtime_version, runtime_record_size) = semantic_type_stats_snapshot_abi();
    if runtime_version != UNIALLOC_SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION
        || runtime_record_size != size_of::<SemanticTypeStatsSnapshot>()
    {
        return None;
    }

    Some(unsafe {
        __unialloc_semantic_type_stats_snapshot_checked(
            out.as_mut_ptr(),
            out.len(),
            size_of::<SemanticTypeStatsSnapshot>(),
        )
    })
}

pub fn semantic_fallback_attribution_snapshot_abi() -> (u32, usize) {
    unsafe {
        (
            __unialloc_semantic_fallback_attribution_snapshot_abi_version(),
            __unialloc_semantic_fallback_attribution_snapshot_size(),
        )
    }
}

pub fn semantic_fallback_attribution_snapshot() -> Option<SemanticFallbackAttributionSnapshot> {
    let (runtime_version, runtime_size) = semantic_fallback_attribution_snapshot_abi();
    if runtime_version != UNIALLOC_SEMANTIC_FALLBACK_ATTRIBUTION_SNAPSHOT_ABI_VERSION
        || runtime_size != size_of::<SemanticFallbackAttributionSnapshot>()
    {
        return None;
    }

    let mut snapshot = MaybeUninit::<SemanticFallbackAttributionSnapshot>::uninit();
    let copied = unsafe {
        __unialloc_semantic_fallback_attribution_snapshot_checked(
            snapshot.as_mut_ptr(),
            size_of::<SemanticFallbackAttributionSnapshot>(),
        )
    };
    if copied {
        Some(unsafe { snapshot.assume_init() })
    } else {
        None
    }
}

pub fn semantic_metadata_validation_snapshot_abi() -> (u32, usize) {
    unsafe {
        (
            __unialloc_semantic_metadata_validation_snapshot_abi_version(),
            __unialloc_semantic_metadata_validation_snapshot_size(),
        )
    }
}

pub fn semantic_metadata_validation_snapshot() -> Option<SemanticMetadataValidationSnapshot> {
    let (runtime_version, runtime_size) = semantic_metadata_validation_snapshot_abi();
    if runtime_version != UNIALLOC_SEMANTIC_METADATA_VALIDATION_SNAPSHOT_ABI_VERSION
        || runtime_size != size_of::<SemanticMetadataValidationSnapshot>()
    {
        return None;
    }

    let mut snapshot = MaybeUninit::<SemanticMetadataValidationSnapshot>::uninit();
    let copied = unsafe {
        __unialloc_semantic_metadata_validation_snapshot_checked(
            snapshot.as_mut_ptr(),
            size_of::<SemanticMetadataValidationSnapshot>(),
        )
    };
    if copied {
        Some(unsafe { snapshot.assume_init() })
    } else {
        None
    }
}

pub fn run_c_abi_probe() -> CAbiProbeReport {
    let mut report = CAbiProbeReport {
        invoked: true,
        ready_after_init: is_ready(),
        invalid_layouts_rejected: 0,
        round_trips: 0,
        over_page_alignment_checked: false,
        over_page_alignment: UNIALLOC_KERNEL_PAGE_SIZE * 2,
        passed: true,
    };

    unsafe {
        if unialloc_alloc(0, 8).is_null() {
            report.invalid_layouts_rejected += 1;
        } else {
            report.passed = false;
        }
        if unialloc_alloc(16, 3).is_null() {
            report.invalid_layouts_rejected += 1;
        } else {
            report.passed = false;
        }
        if unialloc_realloc(core::ptr::null_mut(), 16, 3, 32).is_null() {
            report.invalid_layouts_rejected += 1;
        } else {
            report.passed = false;
        }

        let ptr = unialloc_alloc(64, 8);
        if ptr.is_null() {
            report.passed = false;
            return report;
        }
        core::ptr::write_bytes(ptr, 0xA5, 64);
        let grown = unialloc_realloc(ptr, 64, 8, 192);
        if grown.is_null() {
            report.passed = false;
            unialloc_dealloc(ptr, 64, 8);
            return report;
        }
        let mut preserved = true;
        let mut index = 0usize;
        while index < 64 {
            if *grown.add(index) != 0xA5 {
                preserved = false;
            }
            index += 1;
        }
        unialloc_dealloc(grown, 192, 8);
        if preserved {
            report.round_trips += 1;
        } else {
            report.passed = false;
        }

        let over_page_size = UNIALLOC_KERNEL_PAGE_SIZE + 128;
        let over_page = unialloc_alloc(over_page_size, report.over_page_alignment);
        if over_page.is_null() {
            report.passed = false;
            return report;
        }
        if (over_page as usize) % report.over_page_alignment == 0 {
            report.over_page_alignment_checked = true;
            report.round_trips += 1;
        } else {
            report.passed = false;
        }
        unialloc_dealloc(over_page, over_page_size, report.over_page_alignment);
    }

    report.passed &= report.ready_after_init
        && report.invalid_layouts_rejected >= 3
        && report.round_trips >= 2
        && report.over_page_alignment_checked;
    report
}

pub fn constrained_boot_sample(
    boot_cycle: usize,
    c_abi: CAbiProbeReport,
    allocator_type_stats_rows: usize,
    allocator_type_stats_probe_matched: bool,
) -> Option<ConstrainedBootSample> {
    let runtime_version = unsafe { __unialloc_constrained_boot_sample_abi_version() };
    let runtime_size = unsafe { __unialloc_constrained_boot_sample_size() };
    if runtime_version != UNIALLOC_CONSTRAINED_BOOT_SAMPLE_ABI_VERSION
        || runtime_size != size_of::<ConstrainedBootSample>()
    {
        return None;
    }

    let mut sample = MaybeUninit::<ConstrainedBootSample>::uninit();
    let copied = unsafe {
        __unialloc_constrained_boot_sample_checked(
            sample.as_mut_ptr(),
            size_of::<ConstrainedBootSample>(),
            boot_cycle,
            c_abi.invoked,
            c_abi.ready_after_init,
            c_abi.round_trips,
            c_abi.invalid_layouts_rejected,
            c_abi.over_page_alignment_checked,
            c_abi.over_page_alignment,
            allocator_type_stats_rows,
            allocator_type_stats_probe_matched,
        )
    };
    if copied {
        Some(unsafe { sample.assume_init() })
    } else {
        None
    }
}
