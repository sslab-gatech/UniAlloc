#[cfg(feature = "quarantine")]
use crate::alloc_api::type_isolation::FLAG_DELAYED_FREE;
use crate::alloc_api::type_isolation::{
    active_allocation_metadata, active_allocation_metadata_requires_recovery_record,
    auto_allocation_metadata, auto_deallocation_metadata, auto_reallocation_old_metadata,
    deallocation_metadata_after_recovery_record, recorded_reallocation_old_metadata,
    select_auto_allocation_metadata, semantic_allocation_slow_path_enabled,
    semantic_fallback_attribution_record_raw_alloc_no_metadata,
    semantic_fallback_attribution_record_raw_dealloc_no_metadata,
    semantic_fallback_attribution_record_raw_realloc_moved_dealloc_no_metadata,
    semantic_fallback_attribution_record_raw_realloc_no_metadata,
    semantic_fallback_attribution_record_realloc_recorded_old_metadata_new_allocation,
    semantic_realloc_can_reuse_in_place, semantic_runtime_slow_path_enabled,
    semantic_stats_recording_enabled, take_auto_deallocation_metadata,
    take_recorded_reallocation_old_metadata, with_auto_allocation_recovery_recording,
    without_auto_allocation_recovery_recording, AllocationMetadata, SemanticAlloc, SEMANTIC_STATS,
};
use crate::mm::BackendAllocator as GlobalBackend;
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sys_alloc as system_alloc;
#[cfg(not(feature = "fixed_heap"))]
use crate::size_class::BACKEND_MAX_PAGE;
use crate::*;
use alloc::alloc::{AllocError, Allocator, GlobalAlloc, Layout};
use alloc::boxed::Box;
use core::ptr::{self, NonNull};
use prelude::*;
// Keep only the active thread-cache backend.  The old linux-only CPU and
// alternate thread cache experiments were never reachable from this module and
// are intentionally removed so future allocator-footprint work has one cache
// policy to validate.
mod thread_cache;
use crate::page::{PageBumpAlloc, PG_BUMP};
use crate::sc::META_BUMP;
pub use thread_cache::*;

#[cfg(test)]
#[thread_local]
static mut FAIL_NEXT_ACTIVE_LOCAL_REALLOC_ALLOCATION_FOR_TEST: bool = false;

#[cfg(feature = "quarantine")]
const COMPILED_QUARANTINE_METADATA: AllocationMetadata =
    AllocationMetadata::unknown().with_flags(FLAG_DELAYED_FREE);

#[cfg(all(test, feature = "fixed_heap"))]
#[inline]
fn ensure_fixed_heap_runtime_ready() -> bool {
    crate::sc::ensure_fixed_heap_test_initialized();
    crate::sc::fixed_heap_roots_initialized()
}

#[cfg(all(not(test), feature = "fixed_heap"))]
#[inline]
fn ensure_fixed_heap_runtime_ready() -> bool {
    crate::sc::fixed_heap_roots_initialized()
}

#[inline]
fn dangling_ptr_for_layout(layout: Layout) -> *mut u8 {
    layout.align() as *mut u8
}

#[inline]
fn zero_sized_slice_for_layout(layout: Layout) -> NonNull<[u8]> {
    let ptr = unsafe { NonNull::new_unchecked(dangling_ptr_for_layout(layout)) };
    NonNull::slice_from_raw_parts(ptr, 0)
}

#[inline]
fn nonzero_layout_alignment_supported(layout: Layout) -> bool {
    layout.size() == 0
        || layout.align() <= crate::PAGE_SIZE
        || over_page_alignment_supported(layout)
}

#[cfg(not(feature = "fixed_heap"))]
#[inline]
fn over_page_alignment_supported(layout: Layout) -> bool {
    system_alloc::over_page_aligned_layout_supported(layout)
}

#[cfg(feature = "fixed_heap")]
#[inline]
fn over_page_alignment_supported(layout: Layout) -> bool {
    fixed_heap_over_page_aligned_layout_supported(layout)
}

#[cfg(feature = "fixed_heap")]
#[inline]
fn fixed_heap_over_page_aligned_layout_supported(layout: Layout) -> bool {
    if layout.size() == 0
        || layout.size() > isize::MAX as usize
        || layout.align() <= crate::PAGE_SIZE
        || layout.align() % crate::PAGE_SIZE != 0
    {
        return false;
    }

    let payload_pages = match layout
        .size()
        .checked_add(crate::PAGE_SIZE - 1)
        .and_then(|rounded| rounded.checked_div(crate::PAGE_SIZE))
    {
        Some(pages) if pages != 0 => pages,
        _ => return false,
    };
    let extra_pages = match layout
        .align()
        .checked_div(crate::PAGE_SIZE)
        .and_then(|pages| pages.checked_sub(1))
    {
        Some(extra_pages) => extra_pages,
        None => return false,
    };
    payload_pages
        .checked_add(extra_pages)
        .and_then(|pages| pages.checked_mul(crate::PAGE_SIZE))
        .map(|bytes| bytes <= isize::MAX as usize)
        .unwrap_or(false)
}

#[cfg(not(feature = "fixed_heap"))]
#[inline]
fn hosted_over_page_alignment_exceeds_freelist_split_capacity(layout: Layout) -> bool {
    if layout.align() <= crate::PAGE_SIZE || layout.align() % crate::PAGE_SIZE != 0 {
        return false;
    }
    let align_pages = layout.align() / crate::PAGE_SIZE;
    // `FreeList::alloc_aligned` may have to publish up to `align_pages - 1`
    // pages of prefix or suffix slack.  Hosted free-list classes can represent
    // only `BACKEND_MAX_PAGE` pages; larger slack would make the backend retry
    // and unmap raw runs until luck gives a small split.  Use the page heap for
    // that one-off large-alignment shape instead of burning retries.
    align_pages.saturating_sub(1) > BACKEND_MAX_PAGE
}

#[cfg(not(feature = "fixed_heap"))]
#[inline]
unsafe fn alloc_over_page_aligned_raw(layout: Layout) -> *mut u8 {
    if !system_alloc::over_page_aligned_layout_supported(layout) {
        return core::ptr::null_mut();
    }
    if hosted_over_page_alignment_exceeds_freelist_split_capacity(layout) {
        return system_alloc::PageHeap::default().alloc(layout);
    }
    // Route ordinary hosted over-page-aligned allocations through UniAlloc's
    // page-run backend instead of trimming a one-off system mmap.  The backend
    // path uses `FreeList::alloc_aligned`, so freed aligned runs can coalesce
    // and be reused by later aligned allocations rather than always growing or
    // returning fresh system mappings.
    GlobalBackend.alloc(layout)
}

#[cfg(feature = "fixed_heap")]
#[inline]
unsafe fn alloc_over_page_aligned_raw(layout: Layout) -> *mut u8 {
    if !fixed_heap_over_page_aligned_layout_supported(layout) || !ensure_fixed_heap_runtime_ready()
    {
        return core::ptr::null_mut();
    }
    GlobalBackend.alloc(layout)
}

#[cfg(not(feature = "fixed_heap"))]
#[inline]
unsafe fn dealloc_over_page_aligned_raw(ptr: *mut u8, layout: Layout) {
    if !system_alloc::over_page_aligned_layout_supported(layout) {
        return;
    }
    if hosted_over_page_alignment_exceeds_freelist_split_capacity(layout) {
        system_alloc::PageHeap::default().dealloc(ptr, layout);
    } else {
        GlobalBackend.dealloc(ptr, layout);
    }
}

#[cfg(feature = "fixed_heap")]
#[inline]
unsafe fn dealloc_over_page_aligned_raw(ptr: *mut u8, layout: Layout) {
    if crate::sc::fixed_heap_roots_initialized() {
        GlobalBackend.dealloc(ptr, layout);
    }
}

#[inline]
fn layout_uses_over_page_alignment(layout: Layout) -> bool {
    layout.size() != 0 && layout.align() > crate::PAGE_SIZE
}

#[inline]
fn allocator_grow_layout_supported(old_layout: Layout, new_layout: Layout) -> bool {
    new_layout.size() >= old_layout.size() && nonzero_layout_alignment_supported(new_layout)
}

#[inline]
fn allocator_shrink_layout_supported(old_layout: Layout, new_layout: Layout) -> bool {
    new_layout.size() <= old_layout.size() && nonzero_layout_alignment_supported(new_layout)
}

#[inline]
fn alloc_ptr_to_slice_result(ptr: *mut u8, len: usize) -> Result<NonNull<[u8]>, AllocError> {
    let ptr = NonNull::new(ptr).ok_or(AllocError)?;
    Ok(NonNull::slice_from_raw_parts(ptr, len))
}

#[inline]
fn alloc_ptr_to_layout_slice_result(
    ptr: *mut u8,
    layout: Layout,
) -> Result<NonNull<[u8]>, AllocError> {
    if layout.size() == 0 {
        return Ok(zero_sized_slice_for_layout(layout));
    }
    alloc_ptr_to_slice_result(ptr, layout.size())
}

#[inline]
unsafe fn copy_reallocated_prefix(src: *mut u8, dst: *mut u8, old_size: usize, new_size: usize) {
    ptr::copy_nonoverlapping(src, dst, core::cmp::min(old_size, new_size));
}

#[inline]
fn checked_realloc_layout(layout: Layout, new_size: usize) -> Option<Layout> {
    Layout::from_size_align(new_size, layout.align()).ok()
}

#[inline]
fn fallback_realloc_should_record_dealloc(
    old_ptr: *mut u8,
    old_layout: Layout,
    new_ptr: *mut u8,
) -> bool {
    old_layout.size() != 0 && new_ptr != old_ptr
}

#[inline]
fn checked_static_heap_range(
    heap_start: usize,
    heap_size: usize,
    page_size: usize,
) -> Option<usize> {
    if heap_start == 0
        || heap_size == 0
        || page_size == 0
        || !page_size.is_power_of_two()
        || heap_start % page_size != 0
        || heap_size % page_size != 0
    {
        return None;
    }
    heap_start.checked_add(heap_size)
}

#[inline]
fn with_recovery_recording_if_needed<R>(metadata: AllocationMetadata, f: impl FnOnce() -> R) -> R {
    if metadata.is_layout_derived() {
        f()
    } else {
        with_auto_allocation_recovery_recording(f)
    }
}

#[inline]
unsafe fn dealloc_with_active_or_recorded_metadata(
    alloc: &RustAllocator,
    ptr: *mut u8,
    layout: Layout,
    active_metadata: AllocationMetadata,
) {
    if let Some(recorded_metadata) = take_auto_deallocation_metadata(ptr, layout) {
        // Active compiler drop-site metadata must still pass through the
        // canonical recovery validation path so real compiler/runtime identity
        // mismatches are observable before choosing active or recorded metadata.
        let dealloc_metadata =
            deallocation_metadata_after_recovery_record(active_metadata, recorded_metadata);
        return alloc.dealloc_with_recovered_metadata(ptr, layout, dealloc_metadata);
    }
    alloc.dealloc_with_metadata(ptr, layout, active_metadata);
}

#[inline]
unsafe fn dealloc_reallocated_old_ptr(
    alloc: &RustAllocator,
    ptr: *mut u8,
    layout: Layout,
    fallback_metadata: impl FnOnce() -> Option<AllocationMetadata>,
) {
    if let Some(recovered_metadata) = take_auto_deallocation_metadata(ptr, layout) {
        alloc.dealloc_with_recovered_metadata(ptr, layout, recovered_metadata);
    } else if let Some(metadata) = fallback_metadata() {
        alloc.dealloc_with_metadata(ptr, layout, metadata);
    } else {
        alloc.dealloc_raw(ptr, layout);
    }
}

#[inline]
unsafe fn realloc_with_auto_metadata(
    alloc: &RustAllocator,
    ptr: *mut u8,
    layout: Layout,
    new_layout: Layout,
    new_size: usize,
    alloc_metadata: AllocationMetadata,
) -> *mut u8 {
    if let Some(dealloc_metadata) = auto_reallocation_old_metadata(ptr, layout) {
        if semantic_realloc_can_reuse_in_place(layout, new_size, dealloc_metadata, alloc_metadata) {
            return alloc.realloc_with_split_metadata(
                ptr,
                layout,
                new_size,
                dealloc_metadata,
                alloc_metadata,
            );
        }
    }
    let new_ptr = alloc.alloc_with_metadata(new_layout, alloc_metadata);
    if !new_ptr.is_null() {
        copy_reallocated_prefix(ptr, new_ptr, layout.size(), new_size);
        dealloc_reallocated_old_ptr(alloc, ptr, layout, || auto_deallocation_metadata(layout));
    }
    new_ptr
}

#[inline]
unsafe fn realloc_with_active_metadata(
    alloc: &RustAllocator,
    ptr: *mut u8,
    layout: Layout,
    new_layout: Layout,
    new_size: usize,
    alloc_metadata: AllocationMetadata,
) -> *mut u8 {
    if let Some(dealloc_metadata) = recorded_reallocation_old_metadata(ptr, layout) {
        if semantic_realloc_can_reuse_in_place(layout, new_size, dealloc_metadata, alloc_metadata) {
            return alloc.realloc_with_split_metadata(
                ptr,
                layout,
                new_size,
                dealloc_metadata,
                alloc_metadata,
            );
        }
        let new_ptr = alloc.alloc_with_metadata(new_layout, alloc_metadata);
        if !new_ptr.is_null() {
            copy_reallocated_prefix(ptr, new_ptr, layout.size(), new_size);
            dealloc_reallocated_old_ptr(alloc, ptr, layout, || Some(dealloc_metadata));
        }
        return new_ptr;
    }
    alloc.realloc_with_metadata(ptr, layout, new_size, alloc_metadata)
}

#[inline]
unsafe fn realloc_with_active_local_metadata(
    alloc: &RustAllocator,
    ptr: *mut u8,
    layout: Layout,
    new_layout: Layout,
    new_size: usize,
    alloc_metadata: AllocationMetadata,
) -> *mut u8 {
    if let Some(dealloc_metadata) = recorded_reallocation_old_metadata(ptr, layout) {
        if semantic_realloc_can_reuse_in_place(layout, new_size, dealloc_metadata, alloc_metadata) {
            // A local transition deliberately carries no new recovery record,
            // even when an outer conservative ABI scope is recording.  Commit
            // removal of the old key only after the in-place transition has
            // succeeded.
            let new_ptr = without_auto_allocation_recovery_recording(|| {
                alloc.realloc_with_split_metadata(
                    ptr,
                    layout,
                    new_size,
                    dealloc_metadata,
                    alloc_metadata,
                )
            });
            if !new_ptr.is_null() {
                let _ = take_recorded_reallocation_old_metadata(ptr, layout);
            }
            return new_ptr;
        }
        #[cfg(test)]
        if FAIL_NEXT_ACTIVE_LOCAL_REALLOC_ALLOCATION_FOR_TEST {
            // Deterministically model a backing-allocation failure at the
            // transaction boundary.  Keeping this thread-local avoids
            // perturbing unrelated parallel allocator tests.
            FAIL_NEXT_ACTIVE_LOCAL_REALLOC_ALLOCATION_FOR_TEST = false;
            return core::ptr::null_mut();
        }
        let new_ptr = without_auto_allocation_recovery_recording(|| {
            alloc.alloc_with_metadata(new_layout, alloc_metadata)
        });
        if !new_ptr.is_null() {
            copy_reallocated_prefix(ptr, new_ptr, layout.size(), new_size);
            dealloc_reallocated_old_ptr(alloc, ptr, layout, || Some(dealloc_metadata));
        }
        return new_ptr;
    }
    without_auto_allocation_recovery_recording(|| {
        alloc.realloc_with_metadata(ptr, layout, new_size, alloc_metadata)
    })
}

#[derive(Copy, Clone)]
pub struct RustAllocator;

impl RustAllocator {
    pub const fn new() -> Self {
        Self {}
    }

    #[inline]
    pub(crate) unsafe fn alloc_raw(&self, layout: Layout) -> *mut u8 {
        if layout.size() == 0 {
            return dangling_ptr_for_layout(layout);
        }
        if !nonzero_layout_alignment_supported(layout) {
            return core::ptr::null_mut();
        }
        if layout_uses_over_page_alignment(layout) {
            return alloc_over_page_aligned_raw(layout);
        }

        #[cfg(feature = "fixed_heap")]
        if !ensure_fixed_heap_runtime_ready() {
            return core::ptr::null_mut();
        }

        #[cfg(not(feature = "fixed_heap"))]
        {
            let alloc = &mut (*GlobalTcache);
            return match alloc.allocate(layout) {
                Ok(r) => r.as_ptr(),
                Err(_) => core::ptr::null_mut(),
            };
        }
        #[cfg(feature = "fixed_heap")]
        {
            return with_fixed_tcache_mut(|alloc| match alloc.allocate(layout) {
                Ok(r) => r.as_ptr(),
                Err(_) => core::ptr::null_mut(),
            })
            .unwrap_or(core::ptr::null_mut());
        }
    }

    #[inline]
    pub(crate) unsafe fn dealloc_raw(&self, ptr: *mut u8, layout: Layout) {
        if ptr.is_null() || layout.size() == 0 {
            return;
        }
        if layout_uses_over_page_alignment(layout) {
            dealloc_over_page_aligned_raw(ptr, layout);
            return;
        }
        #[cfg(feature = "fixed_heap")]
        if !ensure_fixed_heap_runtime_ready() {
            return;
        }

        #[cfg(not(feature = "fixed_heap"))]
        {
            let alloc = &mut (*GlobalTcache);
            alloc.deallocate(NonNull::new_unchecked(ptr), layout);
        }
        #[cfg(feature = "fixed_heap")]
        {
            let _ = with_fixed_tcache_mut(|alloc| {
                alloc.deallocate(NonNull::new_unchecked(ptr), layout)
            });
        }
    }

    #[inline]
    pub(crate) unsafe fn realloc_raw(
        &self,
        ptr: *mut u8,
        layout: Layout,
        new_size: usize,
    ) -> *mut u8 {
        if ptr.is_null() && layout.size() != 0 {
            return core::ptr::null_mut();
        }

        #[cfg(feature = "fixed_heap")]
        if !ensure_fixed_heap_runtime_ready() {
            return core::ptr::null_mut();
        }

        let new_layout = match checked_realloc_layout(layout, new_size) {
            Some(layout) => layout,
            None => return core::ptr::null_mut(),
        };
        if !nonzero_layout_alignment_supported(new_layout) {
            return core::ptr::null_mut();
        }
        if layout.size() == 0 {
            return self.alloc_raw(new_layout);
        }
        if new_size == 0 {
            self.dealloc_raw(ptr, layout);
            return dangling_ptr_for_layout(new_layout);
        }
        if layout_uses_over_page_alignment(layout) || layout_uses_over_page_alignment(new_layout) {
            let new_ptr = self.alloc_raw(new_layout);
            if !new_ptr.is_null() {
                copy_reallocated_prefix(ptr, new_ptr, layout.size(), new_size);
                self.dealloc_raw(ptr, layout);
            }
            return new_ptr;
        }
        let old_idx = get_size_class(layout.size()).index();
        let new_idx = get_size_class(new_size).index();

        if old_idx == new_idx {
            ptr
        } else {
            let new_ptr = self.alloc_raw(new_layout);
            if !new_ptr.is_null() {
                ptr::copy_nonoverlapping(ptr, new_ptr, core::cmp::min(layout.size(), new_size));
                self.dealloc_raw(ptr, layout);
            }
            new_ptr
        }
    }

    #[inline]
    unsafe fn move_reallocation_to_layout(
        &self,
        ptr: *mut u8,
        old_layout: Layout,
        new_layout: Layout,
    ) -> *mut u8 {
        let selected_metadata = if let Some(metadata) = active_allocation_metadata() {
            let record_recovery = active_allocation_metadata_requires_recovery_record(metadata);
            Some((metadata, record_recovery, !record_recovery, Some(metadata)))
        } else {
            // Preserve the distinction between "no auto policy configured"
            // and a consuming compiler stream that deliberately yielded no
            // identity for this event (UNKNOWN/exhausted).
            let (auto_policy_enabled, auto_metadata) = select_auto_allocation_metadata(new_layout);
            if let Some(metadata) = auto_metadata {
                Some((
                    metadata,
                    !metadata.is_layout_derived(),
                    false,
                    auto_deallocation_metadata(old_layout),
                ))
            } else if !auto_policy_enabled {
                recorded_reallocation_old_metadata(ptr, old_layout)
                    .map(|metadata| (metadata, true, false, Some(metadata)))
            } else {
                None
            }
        };

        if let Some((new_metadata, record_recovery, suppress_recovery, old_fallback_metadata)) =
            selected_metadata
        {
            // Install the replacement allocation and, when required, its
            // recovery record before touching the old allocation.  A backing
            // allocation or record-install failure therefore leaves the old
            // pointer, payload, and exact recovery record intact.
            let new_ptr = if record_recovery {
                self.alloc_with_recovery_metadata(new_layout, new_metadata)
            } else if suppress_recovery {
                without_auto_allocation_recovery_recording(|| {
                    self.alloc_with_metadata(new_layout, new_metadata)
                })
            } else {
                self.alloc_with_metadata(new_layout, new_metadata)
            };
            if new_ptr.is_null() {
                return new_ptr;
            }
            copy_reallocated_prefix(ptr, new_ptr, old_layout.size(), new_layout.size());
            dealloc_reallocated_old_ptr(self, ptr, old_layout, || old_fallback_metadata);
            return new_ptr;
        }

        // `auto_allocation_metadata` above is a consuming compiler-site
        // selector.  When it deliberately returns None (for example for an
        // UNKNOWN stream entry), do not re-enter `GlobalAlloc::alloc` and
        // accidentally consume a second site for this one allocation event.
        #[cfg(feature = "quarantine")]
        let new_ptr = self.alloc_with_metadata(new_layout, COMPILED_QUARANTINE_METADATA);
        #[cfg(not(feature = "quarantine"))]
        let new_ptr = {
            let new_ptr = self.alloc_raw(new_layout);
            if !new_ptr.is_null() && semantic_stats_recording_enabled() {
                SEMANTIC_STATS.record_alloc(AllocationMetadata::unknown(), new_layout.size());
                semantic_fallback_attribution_record_raw_alloc_no_metadata(new_layout.size());
            }
            new_ptr
        };
        if !new_ptr.is_null() {
            copy_reallocated_prefix(ptr, new_ptr, old_layout.size(), new_layout.size());
            self.dealloc(ptr, old_layout);
        }
        new_ptr
    }

    ///
    /// # Safety
    /// This will statically init heap
    pub unsafe fn init(&self, heap_start: usize, heap_size: usize, page_size: usize) {
        let _ = self.try_init(heap_start, heap_size, page_size);
    }

    ///
    /// # Safety
    /// This will statically init heap and returns whether the range was accepted.
    pub unsafe fn try_init(&self, heap_start: usize, heap_size: usize, page_size: usize) -> bool {
        let heap_end = match checked_static_heap_range(heap_start, heap_size, page_size) {
            Some(heap_end) => heap_end,
            None => return false,
        };
        let accepted = META_BUMP
            .lock()
            .init_with_range(heap_start, heap_end, page_size);
        #[cfg(feature = "fixed_heap")]
        {
            accepted && crate::sc::fixed_heap_roots_initialized()
        }
        #[cfg(not(feature = "fixed_heap"))]
        {
            accepted
        }
    }
    ///
    /// # Safety
    /// Extend the fixed heap size. Assuming the size is just after the previous size
    /// When use this function, require global lock on allcoator
    pub unsafe fn try_extend(&self, size: usize, page_size: usize) -> bool {
        META_BUMP.lock().try_extend(size, page_size)
    }

    pub unsafe fn extend(&self, size: usize, page_size: usize) {
        let _ = self.try_extend(size, page_size);
    }
}

unsafe impl GlobalAlloc for RustAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if layout.size() == 0 {
            return dangling_ptr_for_layout(layout);
        }
        if !cfg!(feature = "quarantine") && !semantic_allocation_slow_path_enabled() {
            return self.alloc_raw(layout);
        }
        if let Some(metadata) = active_allocation_metadata() {
            if active_allocation_metadata_requires_recovery_record(metadata) {
                return self.alloc_with_recovery_metadata(layout, metadata);
            }
            return self.alloc_with_metadata(layout, metadata);
        }
        if let Some(metadata) = auto_allocation_metadata(layout) {
            if metadata.is_layout_derived() {
                return self.alloc_with_metadata(layout, metadata);
            }
            return self.alloc_with_recovery_metadata(layout, metadata);
        }

        #[cfg(feature = "quarantine")]
        {
            self.alloc_with_metadata(layout, COMPILED_QUARANTINE_METADATA)
        }
        #[cfg(not(feature = "quarantine"))]
        {
            let ptr = self.alloc_raw(layout);
            if !ptr.is_null() && semantic_stats_recording_enabled() {
                SEMANTIC_STATS.record_alloc(AllocationMetadata::unknown(), layout.size());
                semantic_fallback_attribution_record_raw_alloc_no_metadata(layout.size());
            }
            ptr
        }
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        let ptr = self.alloc(layout);
        if !ptr.is_null() && layout.size() != 0 {
            ptr::write_bytes(ptr, 0, layout.size());
        }
        ptr
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        if ptr.is_null() || layout.size() == 0 {
            return;
        }
        if !cfg!(feature = "quarantine") && !semantic_runtime_slow_path_enabled() {
            return self.dealloc_raw(ptr, layout);
        }
        if let Some(metadata) = active_allocation_metadata() {
            if !active_allocation_metadata_requires_recovery_record(metadata) {
                return self.dealloc_with_metadata(ptr, layout, metadata);
            }
            return dealloc_with_active_or_recorded_metadata(self, ptr, layout, metadata);
        }
        if let Some(metadata) = take_auto_deallocation_metadata(ptr, layout) {
            return self.dealloc_with_recovered_metadata(ptr, layout, metadata);
        }
        if let Some(metadata) = auto_deallocation_metadata(layout) {
            return self.dealloc_with_metadata(ptr, layout, metadata);
        }

        #[cfg(feature = "quarantine")]
        {
            self.dealloc_with_metadata(ptr, layout, COMPILED_QUARANTINE_METADATA);
        }
        #[cfg(not(feature = "quarantine"))]
        {
            if semantic_stats_recording_enabled() {
                SEMANTIC_STATS.record_dealloc(AllocationMetadata::unknown());
                semantic_fallback_attribution_record_raw_dealloc_no_metadata();
            }
            self.dealloc_raw(ptr, layout)
        }
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        if ptr.is_null() && layout.size() != 0 {
            return core::ptr::null_mut();
        }
        if !cfg!(feature = "quarantine") && !semantic_runtime_slow_path_enabled() {
            return self.realloc_raw(ptr, layout, new_size);
        }
        let new_layout = match checked_realloc_layout(layout, new_size) {
            Some(layout) => layout,
            None => return core::ptr::null_mut(),
        };
        if !nonzero_layout_alignment_supported(new_layout) {
            return core::ptr::null_mut();
        }
        if new_size == 0 {
            self.dealloc(ptr, layout);
            return dangling_ptr_for_layout(new_layout);
        }
        if let Some(alloc_metadata) = active_allocation_metadata() {
            if active_allocation_metadata_requires_recovery_record(alloc_metadata) {
                return with_auto_allocation_recovery_recording(|| {
                    realloc_with_active_metadata(
                        self,
                        ptr,
                        layout,
                        new_layout,
                        new_size,
                        alloc_metadata,
                    )
                });
            }
            return realloc_with_active_local_metadata(
                self,
                ptr,
                layout,
                new_layout,
                new_size,
                alloc_metadata,
            );
        }
        if let Some(alloc_metadata) = auto_allocation_metadata(new_layout) {
            return with_recovery_recording_if_needed(alloc_metadata, || {
                realloc_with_auto_metadata(self, ptr, layout, new_layout, new_size, alloc_metadata)
            });
        }
        if let Some(dealloc_metadata) = recorded_reallocation_old_metadata(ptr, layout) {
            if semantic_realloc_can_reuse_in_place(
                layout,
                new_size,
                dealloc_metadata,
                dealloc_metadata,
            ) {
                // This is not a shrink-only special case: the recovered old
                // metadata is the best available identity for both sides of
                // this unscoped realloc, so preserving the same semantic
                // object/size-class in place avoids an unnecessary raw
                // allocation, prefix copy, and old-pointer release. Temporarily
                // enable recovery-recording so same-pointer, changed-size
                // in-place realloc moves the exact recovery key from the old
                // layout to the new layout instead of leaving stale metadata.
                return with_auto_allocation_recovery_recording(|| {
                    self.realloc_with_split_metadata(
                        ptr,
                        layout,
                        new_size,
                        dealloc_metadata,
                        dealloc_metadata,
                    )
                });
            }
            let new_ptr = self.alloc_raw(new_layout);
            if !new_ptr.is_null() {
                if semantic_stats_recording_enabled() {
                    SEMANTIC_STATS.record_alloc(AllocationMetadata::unknown(), new_size);
                    semantic_fallback_attribution_record_realloc_recorded_old_metadata_new_allocation(
                        new_size,
                    );
                }
                copy_reallocated_prefix(ptr, new_ptr, layout.size(), new_size);
                dealloc_reallocated_old_ptr(self, ptr, layout, || Some(dealloc_metadata));
            }
            return new_ptr;
        }

        #[cfg(feature = "quarantine")]
        {
            self.realloc_with_split_metadata(
                ptr,
                layout,
                new_size,
                COMPILED_QUARANTINE_METADATA,
                COMPILED_QUARANTINE_METADATA,
            )
        }
        #[cfg(not(feature = "quarantine"))]
        {
            if semantic_stats_recording_enabled() {
                let new_ptr = self.realloc_raw(ptr, layout, new_size);
                if !new_ptr.is_null() {
                    SEMANTIC_STATS.record_alloc(AllocationMetadata::unknown(), new_size);
                    semantic_fallback_attribution_record_raw_realloc_no_metadata(new_size);
                    if fallback_realloc_should_record_dealloc(ptr, layout, new_ptr) {
                        SEMANTIC_STATS.record_dealloc(AllocationMetadata::unknown());
                        semantic_fallback_attribution_record_raw_realloc_moved_dealloc_no_metadata(
                        );
                    }
                }
                return new_ptr;
            }
            self.realloc_raw(ptr, layout, new_size)
        }
    }
}

unsafe impl Allocator for RustAllocator {
    fn allocate(&self, layout: Layout) -> Result<NonNull<[u8]>, AllocError> {
        unsafe {
            if layout.size() == 0 {
                return Ok(zero_sized_slice_for_layout(layout));
            }
            let p = self.alloc(layout);
            alloc_ptr_to_slice_result(p, layout.size())
        }
    }

    fn allocate_zeroed(&self, layout: Layout) -> Result<NonNull<[u8]>, AllocError> {
        unsafe {
            if layout.size() == 0 {
                return Ok(zero_sized_slice_for_layout(layout));
            }
            let p = self.alloc_zeroed(layout);
            let block = alloc_ptr_to_slice_result(p, layout.size())?;
            Ok(block)
        }
    }

    unsafe fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
        self.dealloc(ptr.as_ptr() as *mut u8, layout);
    }

    unsafe fn grow(
        &self,
        ptr: NonNull<u8>,
        old_layout: Layout,
        new_layout: Layout,
    ) -> Result<NonNull<[u8]>, AllocError> {
        if !allocator_grow_layout_supported(old_layout, new_layout) {
            return Err(AllocError);
        }
        if old_layout.size() == 0 {
            let new_ptr = self.alloc(new_layout);
            return alloc_ptr_to_layout_slice_result(new_ptr, new_layout);
        }
        if old_layout.align() == new_layout.align() {
            let new_ptr = self.realloc(ptr.as_ptr(), old_layout, new_layout.size());
            return alloc_ptr_to_layout_slice_result(new_ptr, new_layout);
        }

        let new_ptr = self.move_reallocation_to_layout(ptr.as_ptr(), old_layout, new_layout);
        if new_ptr.is_null() {
            return Err(AllocError);
        }
        alloc_ptr_to_layout_slice_result(new_ptr, new_layout)
    }

    unsafe fn grow_zeroed(
        &self,
        ptr: NonNull<u8>,
        old_layout: Layout,
        new_layout: Layout,
    ) -> Result<NonNull<[u8]>, AllocError> {
        let old_size = old_layout.size();
        if !allocator_grow_layout_supported(old_layout, new_layout) {
            return Err(AllocError);
        }
        let new_block = self.grow(ptr, old_layout, new_layout)?;
        let new_ptr = new_block.as_ptr() as *mut u8;
        if new_layout.size() > old_size {
            ptr::write_bytes(new_ptr.add(old_size), 0, new_layout.size() - old_size);
        }
        Ok(new_block)
    }

    unsafe fn shrink(
        &self,
        ptr: NonNull<u8>,
        old_layout: Layout,
        new_layout: Layout,
    ) -> Result<NonNull<[u8]>, AllocError> {
        if !allocator_shrink_layout_supported(old_layout, new_layout) {
            return Err(AllocError);
        }
        if new_layout.size() == 0 {
            self.dealloc(ptr.as_ptr(), old_layout);
            return Ok(zero_sized_slice_for_layout(new_layout));
        }
        if old_layout.align() == new_layout.align() {
            let new_ptr = self.realloc(ptr.as_ptr(), old_layout, new_layout.size());
            return alloc_ptr_to_layout_slice_result(new_ptr, new_layout);
        }

        let new_ptr = self.move_reallocation_to_layout(ptr.as_ptr(), old_layout, new_layout);
        if new_ptr.is_null() {
            return Err(AllocError);
        }
        alloc_ptr_to_layout_slice_result(new_ptr, new_layout)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(feature = "quarantine")]
    use crate::alloc_api::type_isolation::{
        delayed_free_snapshot, drain_current_thread_semantic_state, semantic_auto_metadata_enable,
    };
    use crate::alloc_api::type_isolation::{
        restore_active_metadata, semantic_test_guard, set_active_metadata,
    };
    use crate::alloc_api::type_isolation::{
        semantic_auto_compiler_metadata_stream_enable, semantic_auto_metadata_disable,
        semantic_metadata_validation_snapshot, semantic_stats_recording_disable,
        semantic_stats_reset, semantic_stats_test_exact_recording_enter,
        semantic_stats_test_exact_recording_exit, FLAG_TYPE_ISOLATED, MIN_TYPE_CACHE_OBJECT_SIZE,
        PLACEMENT_HINT_LOCAL_SCOPE_NO_RECOVERY, UNKNOWN_SEMANTIC_ID,
    };
    #[cfg(feature = "stats")]
    use crate::alloc_api::type_isolation::{
        semantic_fallback_attribution_snapshot, semantic_stats_recording_enabled,
        semantic_stats_snapshot, SemanticFallbackAttributionSnapshot,
    };
    use core::mem::align_of;

    #[cfg(feature = "stats")]
    struct SemanticStatsRecordingScope;

    #[cfg(feature = "stats")]
    impl SemanticStatsRecordingScope {
        fn new() -> Self {
            // These cache tests assert exact per-operation counters.  The stats
            // ABI is intentionally process-wide in production, but the parallel
            // test harness can execute unrelated allocator calls while this
            // tiny window is open.  Enter the test-only owner gate before
            // resetting so only this thread contributes to the exact assertion.
            semantic_stats_test_exact_recording_enter();
            semantic_stats_reset();
            assert!(semantic_stats_recording_enabled());
            Self
        }
    }

    #[cfg(feature = "stats")]
    impl Drop for SemanticStatsRecordingScope {
        fn drop(&mut self) {
            semantic_stats_recording_disable();
            semantic_stats_test_exact_recording_exit();
        }
    }

    #[cfg(feature = "stats")]
    fn fallback_delta(
        before: SemanticFallbackAttributionSnapshot,
        after: SemanticFallbackAttributionSnapshot,
    ) -> SemanticFallbackAttributionSnapshot {
        SemanticFallbackAttributionSnapshot {
            raw_alloc_no_metadata: after
                .raw_alloc_no_metadata
                .saturating_sub(before.raw_alloc_no_metadata),
            raw_alloc_no_metadata_bytes: after
                .raw_alloc_no_metadata_bytes
                .saturating_sub(before.raw_alloc_no_metadata_bytes),
            raw_dealloc_no_metadata: after
                .raw_dealloc_no_metadata
                .saturating_sub(before.raw_dealloc_no_metadata),
            raw_realloc_no_metadata: after
                .raw_realloc_no_metadata
                .saturating_sub(before.raw_realloc_no_metadata),
            raw_realloc_no_metadata_bytes: after
                .raw_realloc_no_metadata_bytes
                .saturating_sub(before.raw_realloc_no_metadata_bytes),
            raw_realloc_moved_dealloc_no_metadata: after
                .raw_realloc_moved_dealloc_no_metadata
                .saturating_sub(before.raw_realloc_moved_dealloc_no_metadata),
            realloc_recorded_old_metadata_new_allocations: after
                .realloc_recorded_old_metadata_new_allocations
                .saturating_sub(before.realloc_recorded_old_metadata_new_allocations),
            realloc_recorded_old_metadata_new_allocation_bytes: after
                .realloc_recorded_old_metadata_new_allocation_bytes
                .saturating_sub(before.realloc_recorded_old_metadata_new_allocation_bytes),
        }
    }

    #[cfg(feature = "quarantine")]
    struct CompiledQuarantineTestCleanup {
        alloc: RustAllocator,
    }

    #[cfg(feature = "quarantine")]
    impl CompiledQuarantineTestCleanup {
        fn new(alloc: RustAllocator) -> Self {
            semantic_auto_metadata_disable();
            semantic_stats_recording_disable();
            unsafe {
                restore_active_metadata(AllocationMetadata::unknown());
                let _ = drain_current_thread_semantic_state(&alloc);
            }
            Self { alloc }
        }
    }

    #[cfg(feature = "quarantine")]
    impl Drop for CompiledQuarantineTestCleanup {
        fn drop(&mut self) {
            semantic_auto_metadata_disable();
            semantic_stats_recording_disable();
            unsafe {
                restore_active_metadata(AllocationMetadata::unknown());
                let _ = drain_current_thread_semantic_state(&self.alloc);
            }
        }
    }

    #[cfg(feature = "quarantine")]
    #[test]
    fn compiled_quarantine_global_dealloc_enqueues_unscoped_fallback() {
        let _guard = semantic_test_guard();
        let alloc = RustAllocator::new();
        let _cleanup = CompiledQuarantineTestCleanup::new(alloc);
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();

        let ptr = unsafe { GlobalAlloc::alloc(&alloc, layout) };
        assert!(!ptr.is_null());
        unsafe {
            GlobalAlloc::dealloc(&alloc, ptr, layout);
        }

        let snapshot = delayed_free_snapshot();
        assert_eq!(snapshot.occupied_slots, 1);
        assert!(snapshot.retained_bytes >= layout.size());
        assert!(snapshot.retained_accounting_matches);
    }

    #[cfg(feature = "quarantine")]
    #[test]
    fn compiled_quarantine_global_realloc_preserves_prefix_and_quarantines_old_block() {
        let _guard = semantic_test_guard();
        let alloc = RustAllocator::new();
        let _cleanup = CompiledQuarantineTestCleanup::new(alloc);
        let old_layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();
        let new_size = 1024;
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();

        let ptr = unsafe { GlobalAlloc::alloc(&alloc, old_layout) };
        assert!(!ptr.is_null());
        unsafe {
            for offset in 0..old_layout.size() {
                ptr.add(offset).write((offset as u8) ^ 0xA5);
            }
        }

        let new_ptr = unsafe { GlobalAlloc::realloc(&alloc, ptr, old_layout, new_size) };
        assert!(!new_ptr.is_null());
        assert_ne!(new_ptr, ptr, "cross-size-class realloc must move");
        unsafe {
            for offset in 0..old_layout.size() {
                assert_eq!(new_ptr.add(offset).read(), (offset as u8) ^ 0xA5);
            }
        }
        assert_eq!(
            delayed_free_snapshot().occupied_slots,
            1,
            "successful moving realloc must quarantine the old allocation"
        );

        unsafe {
            GlobalAlloc::dealloc(&alloc, new_ptr, new_layout);
        }
        assert_eq!(delayed_free_snapshot().occupied_slots, 2);
    }

    #[cfg(feature = "quarantine")]
    #[test]
    fn compiled_quarantine_eviction_stays_bounded_and_thread_drain_cleans_it() {
        let _guard = semantic_test_guard();
        let alloc = RustAllocator::new();
        let _cleanup = CompiledQuarantineTestCleanup::new(alloc);
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();
        let mut ptrs = [core::ptr::null_mut(); 40];

        for ptr in ptrs.iter_mut() {
            *ptr = unsafe { GlobalAlloc::alloc(&alloc, layout) };
            assert!(!ptr.is_null());
        }
        for ptr in ptrs {
            unsafe {
                GlobalAlloc::dealloc(&alloc, ptr, layout);
            }
        }

        let full = delayed_free_snapshot();
        assert_eq!(full.occupied_slots, 32);
        assert!(full.retained_accounting_matches);
        let released = unsafe { drain_current_thread_semantic_state(&alloc) };
        assert_eq!(released, full.occupied_slots);
        assert_eq!(delayed_free_snapshot().occupied_slots, 0);
    }

    #[cfg(feature = "quarantine")]
    #[test]
    fn compiled_quarantine_does_not_override_scoped_or_recovered_metadata() {
        let _guard = semantic_test_guard();
        let alloc = RustAllocator::new();
        let _cleanup = CompiledQuarantineTestCleanup::new(alloc);
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_DF01)
            .with_module(0xC0DE_DF01)
            .with_callsite(0xA110_DF01)
            .with_flags(FLAG_TYPE_ISOLATED);

        let previous = unsafe { set_active_metadata(metadata) };
        let scoped_ptr = unsafe { GlobalAlloc::alloc(&alloc, layout) };
        assert!(!scoped_ptr.is_null());
        unsafe {
            GlobalAlloc::dealloc(&alloc, scoped_ptr, layout);
            restore_active_metadata(previous);
        }
        assert_eq!(
            delayed_free_snapshot().occupied_slots,
            0,
            "compiled fallback must not add delayed-free to active scoped metadata"
        );
        unsafe {
            let _ = drain_current_thread_semantic_state(&alloc);
        }

        let recovered_ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, metadata) };
        assert!(!recovered_ptr.is_null());
        unsafe {
            GlobalAlloc::dealloc(&alloc, recovered_ptr, layout);
        }
        assert_eq!(
            delayed_free_snapshot().occupied_slots,
            0,
            "recorded metadata must take precedence over the compiled fallback"
        );
    }

    #[cfg(feature = "quarantine")]
    #[test]
    fn compiled_quarantine_does_not_override_auto_metadata() {
        let _guard = semantic_test_guard();
        let alloc = RustAllocator::new();
        let _cleanup = CompiledQuarantineTestCleanup::new(alloc);
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();
        semantic_auto_metadata_enable(0xC0DE_DF02, FLAG_TYPE_ISOLATED, 0xA110_DF02);

        let ptr = unsafe { GlobalAlloc::alloc(&alloc, layout) };
        assert!(!ptr.is_null());
        unsafe {
            GlobalAlloc::dealloc(&alloc, ptr, layout);
        }
        assert_eq!(
            delayed_free_snapshot().occupied_slots,
            0,
            "configured auto metadata must take precedence without inheriting delayed-free"
        );
    }

    #[test]
    fn allocator_slice_result_rejects_null_alloc_ptr() {
        assert!(alloc_ptr_to_slice_result(core::ptr::null_mut(), 64).is_err());
    }

    #[test]
    fn allocator_slice_result_preserves_non_null_len() {
        let mut byte = 0u8;
        let slice = alloc_ptr_to_slice_result(&mut byte, 1).expect("non-null allocation pointer");
        assert_eq!(slice.len(), 1);
    }

    #[test]
    fn zero_sized_layout_slice_uses_requested_alignment() {
        let layout = Layout::from_size_align(0, 64).unwrap();
        let slice = zero_sized_slice_for_layout(layout);
        assert_eq!(slice.len(), 0);
        assert_eq!((slice.as_ptr() as *mut u8 as usize) % layout.align(), 0);
    }

    #[test]
    fn rust_allocator_allocate_zero_sized_layout_is_aligned_non_null() {
        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(0, 128).unwrap();
        let block = alloc.allocate(layout).expect("zero-size allocation");
        let ptr = block.as_ptr() as *mut u8 as usize;
        assert_ne!(ptr, 0);
        assert_eq!(ptr % layout.align(), 0);
        assert_eq!(block.len(), 0);
    }

    #[test]
    fn rust_allocator_allocate_nonzero_slice_len_matches_request() {
        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(37, align_of::<usize>()).unwrap();
        let block = alloc.allocate(layout).expect("nonzero allocation");
        let ptr = block.as_ptr() as *mut u8;

        assert!(!ptr.is_null());
        assert_eq!(ptr as usize % layout.align(), 0);
        assert_eq!(block.len(), layout.size());

        unsafe {
            alloc.deallocate(NonNull::new(ptr).unwrap(), layout);
        }
    }

    #[test]
    fn rust_allocator_allocate_zeroed_returns_zero_filled_slice() {
        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let block = alloc.allocate_zeroed(layout).expect("zeroed allocation");
        let ptr = block.as_ptr() as *mut u8;

        assert!(!ptr.is_null());
        assert_eq!(ptr as usize % layout.align(), 0);
        assert_eq!(block.len(), layout.size());
        unsafe {
            for offset in 0..layout.size() {
                assert_eq!(*ptr.add(offset), 0, "byte {offset} must be zeroed");
            }
            alloc.deallocate(NonNull::new(ptr).unwrap(), layout);
        }
    }

    #[test]
    fn rust_allocator_global_alloc_zeroed_returns_zero_filled_block() {
        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(80, align_of::<usize>()).unwrap();
        let ptr = unsafe { GlobalAlloc::alloc_zeroed(&alloc, layout) };

        assert!(!ptr.is_null());
        assert_eq!(ptr as usize % layout.align(), 0);
        unsafe {
            for offset in 0..layout.size() {
                assert_eq!(*ptr.add(offset), 0, "byte {offset} must be zeroed");
            }
            GlobalAlloc::dealloc(&alloc, ptr, layout);
        }
    }

    #[test]
    fn rust_allocator_allocate_zeroed_zero_sized_layout_is_aligned_non_null() {
        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(0, 256).unwrap();
        let block = alloc
            .allocate_zeroed(layout)
            .expect("zero-size zeroed allocation");
        let ptr = block.as_ptr() as *mut u8 as usize;
        assert_ne!(ptr, 0);
        assert_eq!(ptr % layout.align(), 0);
        assert_eq!(block.len(), 0);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn rust_allocator_allocates_nonzero_over_page_alignment_via_page_run_backend() {
        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(8, crate::PAGE_SIZE * 2).unwrap();

        let raw = unsafe { alloc.alloc_raw(layout) };
        assert!(!raw.is_null());
        assert_eq!(raw as usize % layout.align(), 0);
        unsafe {
            raw.write(0xA5);
            alloc.dealloc_raw(raw, layout);
        }

        let block = alloc.allocate(layout).expect("overaligned allocation");
        let ptr = block.as_ptr() as *mut u8;
        assert_eq!(ptr as usize % layout.align(), 0);
        assert_eq!(block.len(), layout.size());
        unsafe {
            alloc.deallocate(NonNull::new(ptr).unwrap(), layout);
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hosted_large_over_page_alignment_uses_direct_page_heap_only_when_split_is_unrepresentable() {
        let small = Layout::from_size_align(8, crate::PAGE_SIZE * 2).unwrap();
        assert!(!hosted_over_page_alignment_exceeds_freelist_split_capacity(
            small
        ));

        let boundary = Layout::from_size_align(8, crate::PAGE_SIZE * BACKEND_MAX_PAGE)
            .expect("largest power-of-two split below the direct-map threshold");
        assert!(!hosted_over_page_alignment_exceeds_freelist_split_capacity(
            boundary
        ));

        let oversized = Layout::from_size_align(8, crate::PAGE_SIZE * BACKEND_MAX_PAGE * 2)
            .expect("oversized power-of-two split test layout");
        assert!(hosted_over_page_alignment_exceeds_freelist_split_capacity(
            oversized
        ));
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn rust_allocator_allocates_large_over_page_alignment_without_freelist_churn() {
        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(8, crate::PAGE_SIZE * 512).unwrap();
        assert!(
            system_alloc::over_page_aligned_layout_supported(layout),
            "host page heap should advertise support for this exact over-page layout"
        );

        let raw = unsafe { alloc.alloc_raw(layout) };
        assert!(
            !raw.is_null(),
            "advertised over-page alignment support must not be lost in the freelist path"
        );
        assert_eq!(raw as usize % layout.align(), 0);
        unsafe {
            raw.write(0xA5);
            alloc.dealloc_raw(raw, layout);
        }

        let block = alloc
            .allocate(layout)
            .expect("large over-page allocation should use a real aligned backing run");
        let ptr = block.as_ptr() as *mut u8;
        assert_eq!(ptr as usize % layout.align(), 0);
        assert_eq!(block.len(), layout.size());
        unsafe {
            ptr.write(0x5A);
            alloc.deallocate(NonNull::new(ptr).unwrap(), layout);
        }
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn rust_allocator_allocates_nonzero_over_page_alignment_with_fixed_heap_backend() {
        let _fixed_heap_guard = crate::sc::fixed_heap_test_guard();
        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(8, crate::PAGE_SIZE * 2).unwrap();

        let raw = unsafe { alloc.alloc_raw(layout) };
        assert!(!raw.is_null());
        assert_eq!(raw as usize % layout.align(), 0);
        unsafe {
            raw.write(0xA5);
            alloc.dealloc_raw(raw, layout);
        }

        let block = alloc
            .allocate(layout)
            .expect("fixed-heap over-page allocation");
        let ptr = block.as_ptr() as *mut u8;
        assert_eq!(ptr as usize % layout.align(), 0);
        assert_eq!(block.len(), layout.size());
        unsafe {
            alloc.deallocate(NonNull::new(ptr).unwrap(), layout);
        }
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn rust_allocator_allocates_page512_alignment_after_fixed_heap_slack_chunk_split() {
        let _fixed_heap_guard = crate::sc::fixed_heap_test_guard();
        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(8, crate::PAGE_SIZE * 512).unwrap();
        assert!(
            fixed_heap_over_page_aligned_layout_supported(layout),
            "fixed-heap support check should agree with this regression layout"
        );

        let raw = unsafe { alloc.alloc_raw(layout) };
        assert!(
            !raw.is_null(),
            "fixed-heap over-page support must not be lost when alignment slack is larger than one free-list class"
        );
        assert_eq!(raw as usize % layout.align(), 0);
        unsafe {
            raw.write(0xA5);
            alloc.dealloc_raw(raw, layout);
        }

        let block = alloc
            .allocate(layout)
            .expect("large fixed-heap over-page allocation");
        let ptr = block.as_ptr() as *mut u8;
        assert_eq!(ptr as usize % layout.align(), 0);
        assert_eq!(block.len(), layout.size());
        unsafe {
            ptr.write(0x5A);
            alloc.deallocate(NonNull::new(ptr).unwrap(), layout);
        }
    }

    #[cfg(feature = "stats")]
    #[test]
    fn fallback_stats_count_successful_overaligned_global_alloc() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(8, crate::PAGE_SIZE * 2).unwrap();
        let ptr = {
            let _stats = SemanticStatsRecordingScope::new();
            unsafe { alloc.alloc(layout) }
        };
        #[cfg(not(feature = "fixed_heap"))]
        {
            assert!(
                !ptr.is_null(),
                "overaligned raw allocation should satisfy over-page alignment"
            );
            assert_eq!(ptr as usize % layout.align(), 0);
        }
        #[cfg(feature = "fixed_heap")]
        {
            assert!(
                !ptr.is_null(),
                "fixed-heap mode should satisfy over-page alignment through the backend split path"
            );
            assert_eq!(ptr as usize % layout.align(), 0);
        }

        let snap = semantic_stats_snapshot();
        #[cfg(not(feature = "fixed_heap"))]
        {
            assert_eq!(snap.total_allocations, 1);
            assert_eq!(snap.fallback_allocations, 1);
            unsafe {
                alloc.dealloc(ptr, layout);
            }
        }
        #[cfg(feature = "fixed_heap")]
        {
            assert_eq!(snap.total_allocations, 1);
            assert_eq!(snap.fallback_allocations, 1);
            unsafe {
                alloc.dealloc(ptr, layout);
            }
        }
    }

    #[cfg(all(feature = "stats", not(feature = "quarantine")))]
    #[test]
    fn fallback_attribution_counts_raw_global_alloc_and_dealloc() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE * 2, align_of::<usize>()).unwrap();
        let ptr = {
            let _stats = SemanticStatsRecordingScope::new();
            unsafe { alloc.alloc(layout) }
        };
        assert!(!ptr.is_null());
        let alloc_stats = semantic_stats_snapshot();
        let alloc_fallback = semantic_fallback_attribution_snapshot();
        assert_eq!(alloc_stats.fallback_allocations, 1);
        assert_eq!(alloc_fallback.raw_alloc_no_metadata, 1);
        assert_eq!(alloc_fallback.raw_alloc_no_metadata_bytes, layout.size());
        assert_eq!(alloc_fallback.raw_realloc_no_metadata, 0);
        assert_eq!(
            alloc_fallback.realloc_recorded_old_metadata_new_allocations,
            0
        );

        {
            let _stats = SemanticStatsRecordingScope::new();
            unsafe {
                alloc.dealloc(ptr, layout);
            }
        }
        let dealloc_stats = semantic_stats_snapshot();
        let dealloc_fallback = semantic_fallback_attribution_snapshot();
        assert_eq!(dealloc_stats.fallback_deallocations, 1);
        assert_eq!(dealloc_fallback.raw_dealloc_no_metadata, 1);
    }

    #[test]
    fn realloc_recorded_old_metadata_reuses_in_place_and_moves_changed_size_record() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(25, align_of::<usize>()).unwrap();
        let new_size = 32;
        let new_layout = Layout::from_size_align(new_size, layout.align()).unwrap();
        let metadata = AllocationMetadata::for_type(0xFA11_BACC_2001)
            .with_module(0xFA11_BACC_2002)
            .with_callsite(0xFA11_BACC_2003)
            .with_flags(FLAG_TYPE_ISOLATED);
        assert_eq!(
            get_size_class(layout.size()).index(),
            get_size_class(new_size).index(),
            "regression requires old and new layouts to stay in the same size class"
        );
        assert!(semantic_realloc_can_reuse_in_place(
            layout, new_size, metadata, metadata
        ));

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        for offset in 0..layout.size() {
            unsafe {
                ptr.add(offset).write((offset as u8) ^ 0xA5);
            }
        }
        assert_eq!(
            recorded_reallocation_old_metadata(ptr, layout),
            Some(metadata)
        );

        #[cfg(feature = "stats")]
        let fallback_before;
        #[cfg(feature = "stats")]
        let new_ptr = {
            let _stats = SemanticStatsRecordingScope::new();
            fallback_before = semantic_fallback_attribution_snapshot();
            unsafe { alloc.realloc(ptr, layout, new_size) }
        };
        #[cfg(not(feature = "stats"))]
        let new_ptr = unsafe { alloc.realloc(ptr, layout, new_size) };
        assert_eq!(
            new_ptr, ptr,
            "recorded old metadata should permit same-semantic changed-size realloc to stay in place"
        );
        for offset in 0..layout.size() {
            let byte = unsafe { new_ptr.add(offset).read() };
            assert_eq!(byte, (offset as u8) ^ 0xA5);
        }
        assert_eq!(
            recorded_reallocation_old_metadata(new_ptr, layout),
            None,
            "changed-size in-place realloc must move the recovery record off the old layout key"
        );
        assert_eq!(
            recorded_reallocation_old_metadata(new_ptr, new_layout),
            Some(metadata),
            "changed-size in-place realloc must leave a recovery record under the new layout key"
        );
        #[cfg(feature = "stats")]
        {
            let fallback_after = semantic_fallback_attribution_snapshot();
            let delta = fallback_delta(fallback_before, fallback_after);
            assert_eq!(
                delta.realloc_recorded_old_metadata_new_allocations, 0,
                "in-place recorded-old realloc must not be attributed as a fallback new allocation"
            );
            assert_eq!(delta.realloc_recorded_old_metadata_new_allocation_bytes, 0);
        }

        unsafe {
            alloc.dealloc(new_ptr, new_layout);
        }
        assert_eq!(take_auto_deallocation_metadata(new_ptr, new_layout), None);
        semantic_stats_recording_disable();
    }

    #[cfg(all(feature = "stats", not(feature = "quarantine")))]
    #[test]
    fn fallback_attribution_distinguishes_raw_and_recovered_old_realloc() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE * 2, align_of::<usize>()).unwrap();
        let new_size = layout.size() * 2;
        let new_layout = Layout::from_size_align(new_size, layout.align()).unwrap();
        assert_ne!(
            get_size_class(layout.size()).index(),
            get_size_class(new_size).index(),
            "regression requires raw/recovered-old realloc to cross size classes"
        );

        let raw_ptr = unsafe { alloc.alloc(layout) };
        assert!(!raw_ptr.is_null());
        let raw_before;
        let raw_new = {
            let _stats = SemanticStatsRecordingScope::new();
            raw_before = semantic_fallback_attribution_snapshot();
            unsafe { alloc.realloc(raw_ptr, layout, new_size) }
        };
        assert!(!raw_new.is_null());
        assert_ne!(raw_new, raw_ptr, "cross-size-class raw realloc should move");
        let raw_delta = fallback_delta(raw_before, semantic_fallback_attribution_snapshot());
        assert_eq!(raw_delta.raw_realloc_no_metadata, 1);
        assert_eq!(raw_delta.raw_realloc_no_metadata_bytes, new_size);
        assert_eq!(raw_delta.realloc_recorded_old_metadata_new_allocations, 0);
        unsafe {
            alloc.dealloc(raw_new, new_layout);
        }

        let metadata = AllocationMetadata::for_type(0xFA11_BACC_1001)
            .with_module(0xFA11_BACC_1002)
            .with_callsite(0xFA11_BACC_1003)
            .with_flags(FLAG_TYPE_ISOLATED);
        let recovered_old = unsafe { alloc.alloc_with_recovery_metadata(layout, metadata) };
        assert!(!recovered_old.is_null());
        let recovered_before;
        let recovered_new = {
            let _stats = SemanticStatsRecordingScope::new();
            recovered_before = semantic_fallback_attribution_snapshot();
            unsafe { alloc.realloc(recovered_old, layout, new_size) }
        };
        assert!(!recovered_new.is_null());
        assert_ne!(
            recovered_new, recovered_old,
            "different-size-class recovered-old realloc should allocate a replacement object"
        );
        let recovered_delta =
            fallback_delta(recovered_before, semantic_fallback_attribution_snapshot());
        assert_eq!(recovered_delta.raw_realloc_no_metadata, 0);
        assert_eq!(
            recovered_delta.realloc_recorded_old_metadata_new_allocations,
            1
        );
        assert_eq!(
            recovered_delta.realloc_recorded_old_metadata_new_allocation_bytes,
            new_size
        );
        unsafe {
            alloc.dealloc(recovered_new, new_layout);
        }
    }

    #[test]
    fn over_page_alignment_guard_does_not_break_zero_sized_layouts() {
        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(0, crate::PAGE_SIZE * 2).unwrap();
        let block = alloc
            .allocate(layout)
            .expect("zero-sized over-page alignment remains representable");
        let ptr = block.as_ptr() as *mut u8 as usize;
        assert_ne!(ptr, 0);
        assert_eq!(ptr % layout.align(), 0);
        assert_eq!(block.len(), 0);
    }

    #[test]
    fn rust_allocator_shrink_to_zero_deallocates_and_returns_aligned_empty_slice() {
        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(64, 8).unwrap();
        let new_layout = Layout::from_size_align(0, 256).unwrap();
        let block = alloc.allocate(old_layout).expect("initial allocation");
        let ptr = NonNull::new(block.as_ptr() as *mut u8).unwrap();
        unsafe {
            let shrunk = alloc
                .shrink(ptr, old_layout, new_layout)
                .expect("shrink to zero");
            let shrunk_ptr = shrunk.as_ptr() as *mut u8 as usize;
            assert_ne!(shrunk_ptr, 0);
            assert_eq!(shrunk_ptr % new_layout.align(), 0);
            assert_eq!(shrunk.len(), 0);
            alloc.deallocate(
                NonNull::new(shrunk.as_ptr() as *mut u8).unwrap(),
                new_layout,
            );
        }
    }

    #[test]
    fn checked_realloc_layout_rejects_overflowing_new_size() {
        let old_layout = Layout::from_size_align(32, 8).unwrap();
        assert!(checked_realloc_layout(old_layout, usize::MAX).is_none());
    }

    #[test]
    fn checked_static_heap_range_rejects_invalid_geometry() {
        assert!(checked_static_heap_range(0, crate::PAGE_SIZE, crate::PAGE_SIZE).is_none());
        assert!(checked_static_heap_range(crate::PAGE_SIZE, 0, crate::PAGE_SIZE).is_none());
        assert!(checked_static_heap_range(crate::PAGE_SIZE, crate::PAGE_SIZE, 0).is_none());
        assert!(checked_static_heap_range(crate::PAGE_SIZE, crate::PAGE_SIZE, 3).is_none());
        assert!(checked_static_heap_range(
            crate::PAGE_SIZE + 1,
            crate::PAGE_SIZE,
            crate::PAGE_SIZE
        )
        .is_none());
        assert!(checked_static_heap_range(
            crate::PAGE_SIZE,
            crate::PAGE_SIZE - 1,
            crate::PAGE_SIZE
        )
        .is_none());
        assert!(
            checked_static_heap_range(usize::MAX, crate::PAGE_SIZE, crate::PAGE_SIZE).is_none()
        );
    }

    #[test]
    fn checked_static_heap_range_accepts_page_aligned_span() {
        assert_eq!(
            checked_static_heap_range(crate::PAGE_SIZE, crate::PAGE_SIZE * 2, crate::PAGE_SIZE),
            Some(crate::PAGE_SIZE + crate::PAGE_SIZE * 2)
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn rust_allocator_non_fixed_init_extend_do_not_panic() {
        let alloc = RustAllocator::new();

        unsafe {
            assert!(alloc.try_init(crate::PAGE_SIZE, crate::PAGE_SIZE, crate::PAGE_SIZE));
            assert!(!alloc.try_init(crate::PAGE_SIZE, crate::PAGE_SIZE - 1, crate::PAGE_SIZE));
            alloc.init(crate::PAGE_SIZE, crate::PAGE_SIZE, crate::PAGE_SIZE);
            alloc.extend(crate::PAGE_SIZE, crate::PAGE_SIZE);
        }
    }

    #[test]
    fn realloc_raw_rejects_invalid_new_layout_before_touching_pointer() {
        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(32, 8).unwrap();
        let dangling = NonNull::<u8>::dangling().as_ptr();
        let new_ptr = unsafe { alloc.realloc_raw(dangling, old_layout, usize::MAX) };
        assert!(new_ptr.is_null());
    }

    #[test]
    fn realloc_raw_rejects_null_nonzero_old_layout_before_copying() {
        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(32, 8).unwrap();
        let new_ptr = unsafe { alloc.realloc_raw(core::ptr::null_mut(), old_layout, 96) };
        assert!(new_ptr.is_null());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn realloc_raw_moves_overaligned_allocations_without_size_class_reuse() {
        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(32, crate::PAGE_SIZE * 2).unwrap();
        let ptr = unsafe { alloc.alloc_raw(old_layout) };
        assert!(!ptr.is_null());
        unsafe {
            core::ptr::write_bytes(ptr, 0xA5, old_layout.size());
        }

        let new_ptr = unsafe { alloc.realloc_raw(ptr, old_layout, old_layout.size() * 2) };
        assert!(!new_ptr.is_null());
        assert_eq!(new_ptr as usize % old_layout.align(), 0);
        unsafe {
            for idx in 0..old_layout.size() {
                assert_eq!(*new_ptr.add(idx), 0xA5);
            }
            alloc.dealloc_raw(
                new_ptr,
                Layout::from_size_align(old_layout.size() * 2, old_layout.align()).unwrap(),
            );
        }
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn realloc_raw_moves_overaligned_allocations_with_fixed_heap_backend() {
        let _fixed_heap_guard = crate::sc::fixed_heap_test_guard();
        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(32, crate::PAGE_SIZE * 2).unwrap();
        let ptr = unsafe { alloc.alloc_raw(old_layout) };
        assert!(!ptr.is_null());
        unsafe {
            core::ptr::write_bytes(ptr, 0xA5, old_layout.size());
        }

        let new_ptr = unsafe { alloc.realloc_raw(ptr, old_layout, old_layout.size() * 2) };
        assert!(!new_ptr.is_null());
        assert_eq!(new_ptr as usize % old_layout.align(), 0);
        unsafe {
            for idx in 0..old_layout.size() {
                assert_eq!(*new_ptr.add(idx), 0xA5);
            }
            alloc.dealloc_raw(
                new_ptr,
                Layout::from_size_align(old_layout.size() * 2, old_layout.align()).unwrap(),
            );
        }
    }

    #[test]
    fn allocator_grow_rejects_smaller_layout_without_taking_ownership() {
        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(64, 8).unwrap();
        let smaller_layout = Layout::from_size_align(32, 8).unwrap();
        let block = alloc.allocate(old_layout).expect("initial allocation");
        let ptr = NonNull::new(block.as_ptr() as *mut u8).unwrap();

        unsafe {
            ptr::write_bytes(ptr.as_ptr(), 0xA5, old_layout.size());
            assert!(alloc.grow(ptr, old_layout, smaller_layout).is_err());
            for offset in 0..old_layout.size() {
                assert_eq!(
                    *ptr.as_ptr().add(offset),
                    0xA5,
                    "failed grow must leave the old allocation untouched"
                );
            }
            alloc.deallocate(ptr, old_layout);
        }
    }

    #[test]
    fn allocator_grow_zeroed_rejects_zero_sized_new_layout_without_taking_ownership() {
        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(64, 8).unwrap();
        let zero_layout = Layout::from_size_align(0, 8).unwrap();
        let block = alloc.allocate(old_layout).expect("initial allocation");
        let ptr = NonNull::new(block.as_ptr() as *mut u8).unwrap();

        unsafe {
            ptr::write_bytes(ptr.as_ptr(), 0x5A, old_layout.size());
            assert!(alloc.grow_zeroed(ptr, old_layout, zero_layout).is_err());
            for offset in 0..old_layout.size() {
                assert_eq!(
                    *ptr.as_ptr().add(offset),
                    0x5A,
                    "failed grow_zeroed must leave the old allocation untouched"
                );
            }
            alloc.deallocate(ptr, old_layout);
        }
    }

    #[test]
    fn allocator_shrink_rejects_larger_layout_without_taking_ownership() {
        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(32, 8).unwrap();
        let larger_layout = Layout::from_size_align(64, 8).unwrap();
        let block = alloc.allocate(old_layout).expect("initial allocation");
        let ptr = NonNull::new(block.as_ptr() as *mut u8).unwrap();

        unsafe {
            ptr::write_bytes(ptr.as_ptr(), 0x3C, old_layout.size());
            assert!(alloc.shrink(ptr, old_layout, larger_layout).is_err());
            for offset in 0..old_layout.size() {
                assert_eq!(
                    *ptr.as_ptr().add(offset),
                    0x3C,
                    "failed shrink must leave the old allocation untouched"
                );
            }
            alloc.deallocate(ptr, old_layout);
        }
    }

    #[test]
    fn global_realloc_rejects_null_nonzero_old_layout_even_with_active_metadata() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_DA01)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C000)
            .with_flags(FLAG_TYPE_ISOLATED);

        let previous = unsafe { set_active_metadata(metadata) };
        let new_ptr = unsafe { alloc.realloc(core::ptr::null_mut(), layout, layout.size() * 2) };
        unsafe {
            restore_active_metadata(previous);
        }

        assert!(
            new_ptr.is_null(),
            "GlobalAlloc::realloc must fail closed for invalid null/nonzero old allocation"
        );
    }

    #[test]
    fn active_dealloc_consumes_recovery_record_once_and_keeps_drop_site_metadata() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let alloc_metadata = AllocationMetadata::for_type(0xC003_DA11)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C001)
            .with_flags(FLAG_TYPE_ISOLATED);
        let drop_metadata = alloc_metadata.with_callsite(0xD00D_C001);

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, alloc_metadata) };
        assert!(!ptr.is_null());
        assert_eq!(
            recorded_reallocation_old_metadata(ptr, layout),
            Some(alloc_metadata)
        );

        semantic_stats_reset();
        semantic_stats_recording_disable();
        unsafe {
            dealloc_with_active_or_recorded_metadata(&alloc, ptr, layout, drop_metadata);
        }
        assert_eq!(
            take_auto_deallocation_metadata(ptr, layout),
            None,
            "active dealloc should consume the recovery record exactly once"
        );
        let validation = semantic_metadata_validation_snapshot();
        assert_eq!(
            validation.recovery_identity_matches, 1,
            "same allocation identity with a different drop callsite must be recorded as a validated recovery match"
        );
        assert_eq!(validation.recovery_identity_mismatches, 0);

        let reused = unsafe { alloc.alloc_with_metadata(layout, alloc_metadata) };
        assert_eq!(
            reused, ptr,
            "matching active drop-site metadata should keep the object reusable under the allocation identity"
        );
        unsafe {
            alloc.dealloc_with_metadata(reused, layout, alloc_metadata);
        }
    }

    #[test]
    fn active_dealloc_recovery_record_records_mismatch_and_uses_recorded_identity() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let recorded_metadata = AllocationMetadata::for_type(0xC003_DA51)
            .with_module(0xC0DE_DA51)
            .with_callsite(0xA110_C051)
            .with_flags(FLAG_TYPE_ISOLATED);
        let wrong_active_metadata = AllocationMetadata::for_type(0xC003_DA52)
            .with_module(recorded_metadata.module_id)
            .with_callsite(0xD00D_C052)
            .with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, recorded_metadata) };
        assert!(!ptr.is_null());

        semantic_stats_reset();
        semantic_stats_recording_disable();
        unsafe {
            dealloc_with_active_or_recorded_metadata(&alloc, ptr, layout, wrong_active_metadata);
        }

        let validation = semantic_metadata_validation_snapshot();
        assert_eq!(validation.recovery_identity_matches, 0);
        assert_eq!(
            validation.recovery_identity_mismatches, 1,
            "active-scope recovery mismatches must be observable through the canonical validation counters"
        );
        assert_eq!(
            validation.last_mismatch_requested_type_id,
            wrong_active_metadata.type_id
        );
        assert_eq!(
            validation.last_mismatch_recorded_type_id,
            recorded_metadata.type_id
        );
        assert_eq!(
            validation.last_mismatch_requested_module_id,
            wrong_active_metadata.module_id
        );
        assert_eq!(
            validation.last_mismatch_recorded_module_id,
            recorded_metadata.module_id
        );
        assert_eq!(
            validation.last_mismatch_requested_callsite,
            wrong_active_metadata.callsite
        );
        assert_eq!(
            validation.last_mismatch_recorded_callsite,
            recorded_metadata.callsite
        );

        let reused = unsafe { alloc.alloc_with_metadata(layout, recorded_metadata) };
        assert_eq!(
            reused, ptr,
            "mismatched active dealloc must cache under the recorded allocation identity, not the wrong active identity"
        );
        unsafe {
            alloc.dealloc_with_metadata(reused, layout, recorded_metadata);
        }
    }

    #[test]
    fn reallocated_old_ptr_release_prefers_recorded_metadata_over_fallback() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let recorded_metadata = AllocationMetadata::for_type(0xC003_DA41)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C041)
            .with_flags(FLAG_TYPE_ISOLATED);
        let fallback_metadata = AllocationMetadata::for_type(0xC003_DA42)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C042)
            .with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, recorded_metadata) };
        assert!(!ptr.is_null());

        unsafe {
            dealloc_reallocated_old_ptr(&alloc, ptr, layout, || Some(fallback_metadata));
        }
        assert_eq!(take_auto_deallocation_metadata(ptr, layout), None);

        let reused = unsafe { alloc.alloc_with_metadata(layout, recorded_metadata) };
        assert_eq!(
            reused, ptr,
            "realloc old-pointer release must cache the object under the recorded allocation metadata, not the fallback/current metadata"
        );
        unsafe {
            alloc.dealloc_with_metadata(reused, layout, recorded_metadata);
        }
    }

    #[test]
    fn active_realloc_invalid_new_layout_preserves_recovery_record() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let old_metadata = AllocationMetadata::for_type(0xC003_DA21)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C021)
            .with_flags(FLAG_TYPE_ISOLATED);
        let new_metadata = AllocationMetadata::for_type(0xC003_DA22)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C022)
            .with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, old_metadata) };
        assert!(!ptr.is_null());

        let previous = unsafe { set_active_metadata(new_metadata) };
        let failed = unsafe { alloc.realloc(ptr, layout, usize::MAX) };
        unsafe {
            restore_active_metadata(previous);
        }
        assert!(failed.is_null());
        assert_eq!(
            recorded_reallocation_old_metadata(ptr, layout),
            Some(old_metadata),
            "invalid active realloc must not consume the old object's exact metadata record"
        );

        unsafe {
            alloc.dealloc(ptr, layout);
        }
        assert_eq!(take_auto_deallocation_metadata(ptr, layout), None);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn active_realloc_overaligned_new_allocation_consumes_old_recovery_record() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let overaligned_new_layout = Layout::from_size_align(
            layout.size() + crate::PAGE_SIZE,
            crate::PAGE_SIZE.saturating_mul(2),
        )
        .unwrap();
        let old_metadata = AllocationMetadata::for_type(0xC003_DA31)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C031)
            .with_flags(FLAG_TYPE_ISOLATED);
        let new_metadata = AllocationMetadata::for_type(0xC003_DA32)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C032)
            .with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, old_metadata) };
        assert!(!ptr.is_null());

        let new_ptr = with_auto_allocation_recovery_recording(|| unsafe {
            realloc_with_active_metadata(
                &alloc,
                ptr,
                layout,
                overaligned_new_layout,
                overaligned_new_layout.size(),
                new_metadata,
            )
        });
        assert!(!new_ptr.is_null());
        assert_eq!(new_ptr as usize % overaligned_new_layout.align(), 0);
        assert_eq!(
            recorded_reallocation_old_metadata(ptr, layout),
            None,
            "successful active realloc must consume the old object's recovery record exactly once"
        );

        unsafe {
            alloc.dealloc(new_ptr, overaligned_new_layout);
        }
        assert_eq!(
            take_auto_deallocation_metadata(new_ptr, overaligned_new_layout),
            None
        );
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn active_realloc_overaligned_new_allocation_uses_fixed_heap_backend() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let overaligned_new_layout = Layout::from_size_align(
            layout.size() + crate::PAGE_SIZE,
            crate::PAGE_SIZE.saturating_mul(2),
        )
        .unwrap();
        let old_metadata = AllocationMetadata::for_type(0xC003_DA31)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C031)
            .with_flags(FLAG_TYPE_ISOLATED);
        let new_metadata = AllocationMetadata::for_type(0xC003_DA32)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C032)
            .with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, old_metadata) };
        assert!(!ptr.is_null());

        let new_ptr = with_auto_allocation_recovery_recording(|| unsafe {
            realloc_with_active_metadata(
                &alloc,
                ptr,
                layout,
                overaligned_new_layout,
                overaligned_new_layout.size(),
                new_metadata,
            )
        });
        assert!(!new_ptr.is_null());
        assert_eq!(new_ptr as usize % overaligned_new_layout.align(), 0);
        assert_eq!(
            recorded_reallocation_old_metadata(ptr, layout),
            None,
            "successful fixed-heap active realloc must consume the old object's exact metadata record"
        );

        unsafe {
            alloc.dealloc(new_ptr, overaligned_new_layout);
        }
        assert_eq!(
            take_auto_deallocation_metadata(new_ptr, overaligned_new_layout),
            None
        );
    }

    fn assert_allocator_alignment_change_preserves_scope_ended_recovery_metadata(
        old_layout: Layout,
        new_layout: Layout,
        metadata: AllocationMetadata,
        pattern: u8,
        move_allocation: impl FnOnce(
            &RustAllocator,
            NonNull<u8>,
            Layout,
            Layout,
        ) -> Result<NonNull<[u8]>, AllocError>,
    ) {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        #[cfg(feature = "stats")]
        let _stats = SemanticStatsRecordingScope::new();
        #[cfg(feature = "stats")]
        let fallback_before = semantic_fallback_attribution_snapshot();

        let alloc = RustAllocator::new();
        let previous = unsafe { set_active_metadata(metadata) };
        let old_ptr = unsafe { alloc.alloc(old_layout) };
        unsafe {
            restore_active_metadata(previous);
        }
        assert!(!old_ptr.is_null());
        assert_eq!(
            recorded_reallocation_old_metadata(old_ptr, old_layout),
            Some(metadata)
        );
        unsafe {
            for offset in 0..old_layout.size() {
                old_ptr.add(offset).write((offset as u8) ^ pattern);
            }
        }

        let old_ptr = NonNull::new(old_ptr).unwrap();
        let moved = move_allocation(&alloc, old_ptr, old_layout, new_layout)
            .expect("alignment-changing allocator move");
        let moved_ptr = moved.as_ptr() as *mut u8;
        assert_eq!(moved.len(), new_layout.size());
        assert_eq!(moved_ptr as usize % new_layout.align(), 0);
        unsafe {
            for offset in 0..core::cmp::min(old_layout.size(), new_layout.size()) {
                assert_eq!(*moved_ptr.add(offset), (offset as u8) ^ pattern);
            }
        }
        assert_eq!(
            recorded_reallocation_old_metadata(old_ptr.as_ptr(), old_layout),
            None,
            "successful move must consume the old recovery record"
        );
        assert_eq!(
            recorded_reallocation_old_metadata(moved_ptr, new_layout),
            Some(metadata),
            "replacement allocation must retain the old semantic identity"
        );

        unsafe {
            alloc.deallocate(NonNull::new(moved_ptr).unwrap(), new_layout);
        }
        assert_eq!(take_auto_deallocation_metadata(moved_ptr, new_layout), None);
        #[cfg(feature = "stats")]
        {
            let snapshot = semantic_stats_snapshot();
            assert_eq!(snapshot.typed_allocations, 2);
            assert_eq!(snapshot.fallback_allocations, 0);
            assert_eq!(snapshot.typed_deallocations, 2);
            assert_eq!(snapshot.fallback_deallocations, 0);
            assert_eq!(
                fallback_delta(fallback_before, semantic_fallback_attribution_snapshot()),
                SemanticFallbackAttributionSnapshot {
                    raw_alloc_no_metadata: 0,
                    raw_alloc_no_metadata_bytes: 0,
                    raw_dealloc_no_metadata: 0,
                    raw_realloc_no_metadata: 0,
                    raw_realloc_no_metadata_bytes: 0,
                    raw_realloc_moved_dealloc_no_metadata: 0,
                    realloc_recorded_old_metadata_new_allocations: 0,
                    realloc_recorded_old_metadata_new_allocation_bytes: 0,
                }
            );
        }
    }

    #[test]
    fn allocator_grow_alignment_change_preserves_scope_ended_recovery_metadata() {
        let metadata = AllocationMetadata::for_type(0xC003_DA62)
            .with_module(0xC0DE_DA62)
            .with_callsite(0xA110_C062)
            .with_flags(FLAG_TYPE_ISOLATED);
        assert_allocator_alignment_change_preserves_scope_ended_recovery_metadata(
            Layout::from_size_align(64, 8).unwrap(),
            Layout::from_size_align(128, 64).unwrap(),
            metadata,
            0xA5,
            |alloc, ptr, old_layout, new_layout| unsafe { alloc.grow(ptr, old_layout, new_layout) },
        );
    }

    #[test]
    fn allocator_shrink_alignment_change_preserves_scope_ended_recovery_metadata() {
        let metadata = AllocationMetadata::for_type(0xC003_DA63)
            .with_module(0xC0DE_DA63)
            .with_callsite(0xA110_C063)
            .with_flags(FLAG_TYPE_ISOLATED);
        assert_allocator_alignment_change_preserves_scope_ended_recovery_metadata(
            Layout::from_size_align(128, 8).unwrap(),
            Layout::from_size_align(64, 64).unwrap(),
            metadata,
            0x5A,
            |alloc, ptr, old_layout, new_layout| unsafe {
                alloc.shrink(ptr, old_layout, new_layout)
            },
        );
    }

    fn assert_allocator_alignment_change_splits_recorded_old_and_active_new_metadata(
        old_layout: Layout,
        new_layout: Layout,
        old_metadata: AllocationMetadata,
        new_metadata: AllocationMetadata,
        move_allocation: impl FnOnce(
            &RustAllocator,
            NonNull<u8>,
            Layout,
            Layout,
        ) -> Result<NonNull<[u8]>, AllocError>,
    ) {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_reset();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let old_ptr = unsafe { alloc.alloc_with_recovery_metadata(old_layout, old_metadata) };
        assert!(!old_ptr.is_null());

        let previous = unsafe { set_active_metadata(new_metadata) };
        let moved = move_allocation(
            &alloc,
            NonNull::new(old_ptr).unwrap(),
            old_layout,
            new_layout,
        )
        .expect("active alignment-changing allocator move");
        unsafe {
            restore_active_metadata(previous);
        }
        let moved_ptr = moved.as_ptr() as *mut u8;

        assert_eq!(moved_ptr as usize % new_layout.align(), 0);
        assert_eq!(
            recorded_reallocation_old_metadata(old_ptr, old_layout),
            None,
            "successful split move must consume the old allocation record"
        );
        assert_eq!(
            recorded_reallocation_old_metadata(moved_ptr, new_layout),
            Some(new_metadata),
            "replacement must use the active scope identity"
        );
        let validation = semantic_metadata_validation_snapshot();
        assert_eq!(validation.recovery_identity_mismatches, 0);

        unsafe {
            alloc.deallocate(NonNull::new(moved_ptr).unwrap(), new_layout);
        }
        assert_eq!(take_auto_deallocation_metadata(moved_ptr, new_layout), None);
    }

    #[test]
    fn allocator_grow_alignment_change_splits_recorded_old_and_active_new_metadata() {
        let old_metadata = AllocationMetadata::for_type(0xC003_DA64)
            .with_module(0xC0DE_DA64)
            .with_callsite(0xA110_C064)
            .with_flags(FLAG_TYPE_ISOLATED);
        let new_metadata = AllocationMetadata::for_type(0xC003_DA65)
            .with_module(0xC0DE_DA65)
            .with_callsite(0xA110_C065)
            .with_flags(FLAG_TYPE_ISOLATED);
        assert_allocator_alignment_change_splits_recorded_old_and_active_new_metadata(
            Layout::from_size_align(64, 8).unwrap(),
            Layout::from_size_align(128, 64).unwrap(),
            old_metadata,
            new_metadata,
            |alloc, ptr, old_layout, new_layout| unsafe { alloc.grow(ptr, old_layout, new_layout) },
        );
    }

    #[test]
    fn allocator_shrink_alignment_change_splits_recorded_old_and_active_new_metadata() {
        let old_metadata = AllocationMetadata::for_type(0xC003_DA66)
            .with_module(0xC0DE_DA66)
            .with_callsite(0xA110_C066)
            .with_flags(FLAG_TYPE_ISOLATED);
        let new_metadata = AllocationMetadata::for_type(0xC003_DA67)
            .with_module(0xC0DE_DA67)
            .with_callsite(0xA110_C067)
            .with_flags(FLAG_TYPE_ISOLATED);
        assert_allocator_alignment_change_splits_recorded_old_and_active_new_metadata(
            Layout::from_size_align(128, 8).unwrap(),
            Layout::from_size_align(64, 64).unwrap(),
            old_metadata,
            new_metadata,
            |alloc, ptr, old_layout, new_layout| unsafe {
                alloc.shrink(ptr, old_layout, new_layout)
            },
        );
    }

    #[test]
    fn allocator_alignment_change_active_local_scope_consumes_old_record_without_new_record() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(64, 8).unwrap();
        let new_layout = Layout::from_size_align(128, 64).unwrap();
        let old_metadata = AllocationMetadata::for_type(0xC003_DA68)
            .with_module(0xC0DE_DA68)
            .with_callsite(0xA110_C068)
            .with_flags(FLAG_TYPE_ISOLATED);
        let local_metadata = AllocationMetadata::for_type(0xC003_DA69)
            .with_module(0xC0DE_DA69)
            .with_callsite(0xA110_C069)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_placement_hint(PLACEMENT_HINT_LOCAL_SCOPE_NO_RECOVERY);
        let old_ptr = unsafe { alloc.alloc_with_recovery_metadata(old_layout, old_metadata) };
        assert!(!old_ptr.is_null());

        let previous = unsafe { set_active_metadata(local_metadata) };
        let moved = with_auto_allocation_recovery_recording(|| unsafe {
            alloc.grow(NonNull::new(old_ptr).unwrap(), old_layout, new_layout)
        })
        .expect("local-scope alignment-changing grow");
        unsafe {
            restore_active_metadata(previous);
        }
        let moved_ptr = moved.as_ptr() as *mut u8;

        assert_eq!(
            recorded_reallocation_old_metadata(old_ptr, old_layout),
            None
        );
        assert_eq!(
            recorded_reallocation_old_metadata(moved_ptr, new_layout),
            None,
            "paired local scope must not install a recovery record"
        );
        unsafe {
            alloc.dealloc_with_metadata(moved_ptr, new_layout, local_metadata);
        }
    }

    #[test]
    fn allocator_alignment_change_consumes_one_unknown_compiler_stream_entry() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(64, 8).unwrap();
        let new_layout = Layout::from_size_align(128, 64).unwrap();
        let old_metadata = AllocationMetadata::for_type(0xC003_DA6F)
            .with_module(0xC0DE_DA6F)
            .with_callsite(0xA110_C06F)
            .with_flags(FLAG_TYPE_ISOLATED);
        let old_ptr = unsafe { alloc.alloc_with_recovery_metadata(old_layout, old_metadata) };
        assert!(!old_ptr.is_null());
        assert_eq!(
            recorded_reallocation_old_metadata(old_ptr, old_layout),
            Some(old_metadata)
        );
        unsafe {
            old_ptr.write_bytes(0xD4, old_layout.size());
        }

        let typed_id = 0xC003_DA70;
        let ids = [UNKNOWN_SEMANTIC_ID, typed_id];
        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_enable(
                0xC0DE_DA70,
                FLAG_TYPE_ISOLATED,
                0xA110_C070,
                ids.as_ptr(),
                ids.len(),
            )
        });

        let moved = unsafe { alloc.grow(NonNull::new(old_ptr).unwrap(), old_layout, new_layout) }
            .expect("alignment-changing raw fallback after UNKNOWN stream entry");
        let moved_ptr = moved.as_ptr() as *mut u8;
        assert_eq!(
            recorded_reallocation_old_metadata(old_ptr, old_layout),
            None,
            "the old typed identity must be consumed after the successful raw move"
        );
        assert_eq!(
            recorded_reallocation_old_metadata(moved_ptr, new_layout),
            None,
            "the UNKNOWN site must leave this allocation raw instead of consuming the next typed site"
        );
        unsafe {
            for offset in 0..old_layout.size() {
                assert_eq!(*moved_ptr.add(offset), 0xD4);
            }
        }

        let probe_layout = Layout::from_size_align(96, 8).unwrap();
        let probe = unsafe { alloc.alloc(probe_layout) };
        assert!(!probe.is_null());
        assert_eq!(
            take_auto_deallocation_metadata(probe, probe_layout).map(|metadata| metadata.type_id),
            Some(typed_id),
            "the next allocation event must receive the second compiler stream entry"
        );

        semantic_auto_metadata_disable();
        unsafe {
            alloc.dealloc_raw(moved_ptr, new_layout);
            alloc.dealloc_raw(probe, probe_layout);
        }
    }

    #[test]
    fn active_local_realloc_injected_allocation_failure_preserves_old_record_and_payload() {
        let _semantic_guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(64, 8).unwrap();
        let old_metadata = AllocationMetadata::for_type(0xC003_DA6B)
            .with_module(0xC0DE_DA6B)
            .with_callsite(0xA110_C06B)
            .with_flags(FLAG_TYPE_ISOLATED);
        let local_metadata = AllocationMetadata::for_type(0xC003_DA6C)
            .with_module(0xC0DE_DA6C)
            .with_callsite(0xA110_C06C)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_placement_hint(PLACEMENT_HINT_LOCAL_SCOPE_NO_RECOVERY);
        let old_ptr = unsafe { alloc.alloc_with_recovery_metadata(old_layout, old_metadata) };
        assert!(!old_ptr.is_null());
        unsafe {
            old_ptr.write_bytes(0xC3, old_layout.size());
        }

        let moved_size = crate::size_class::MAX_SIZE.saturating_add(1);
        assert!(Layout::from_size_align(moved_size, old_layout.align()).is_ok());
        let previous = unsafe { set_active_metadata(local_metadata) };
        let failed = unsafe {
            FAIL_NEXT_ACTIVE_LOCAL_REALLOC_ALLOCATION_FOR_TEST = true;
            alloc.realloc(old_ptr, old_layout, moved_size)
        };
        unsafe {
            restore_active_metadata(previous);
        }

        assert!(failed.is_null());
        assert_eq!(
            recorded_reallocation_old_metadata(old_ptr, old_layout),
            Some(old_metadata),
            "failed local moved realloc must retain the old exact record"
        );
        unsafe {
            for offset in 0..old_layout.size() {
                assert_eq!(*old_ptr.add(offset), 0xC3);
            }
            alloc.dealloc(old_ptr, old_layout);
        }
        assert_eq!(take_auto_deallocation_metadata(old_ptr, old_layout), None);
    }

    #[test]
    fn active_local_moved_realloc_ignores_outer_recovery_recording_scope() {
        let _semantic_guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(64, 8).unwrap();
        let new_size = 4096;
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();
        let old_metadata = AllocationMetadata::for_type(0xC003_DA71)
            .with_module(0xC0DE_DA71)
            .with_callsite(0xA110_C071)
            .with_flags(FLAG_TYPE_ISOLATED);
        let local_metadata = AllocationMetadata::for_type(0xC003_DA72)
            .with_module(0xC0DE_DA72)
            .with_callsite(0xA110_C072)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_placement_hint(PLACEMENT_HINT_LOCAL_SCOPE_NO_RECOVERY);
        let old_ptr = unsafe { alloc.alloc_with_recovery_metadata(old_layout, old_metadata) };
        assert!(!old_ptr.is_null());

        let previous = unsafe { set_active_metadata(local_metadata) };
        let moved_ptr = with_auto_allocation_recovery_recording(|| unsafe {
            alloc.realloc(old_ptr, old_layout, new_size)
        });
        unsafe {
            restore_active_metadata(previous);
        }

        assert!(!moved_ptr.is_null());
        assert_ne!(moved_ptr, old_ptr);
        assert_eq!(
            recorded_reallocation_old_metadata(old_ptr, old_layout),
            None
        );
        assert_eq!(
            recorded_reallocation_old_metadata(moved_ptr, new_layout),
            None,
            "paired local moved realloc must suppress an outer conservative recovery scope"
        );
        unsafe {
            alloc.dealloc_with_metadata(moved_ptr, new_layout, local_metadata);
        }
    }

    #[test]
    fn allocator_grow_zeroed_alignment_change_preserves_metadata_and_zeroes_tail() {
        let _guard = semantic_test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(64, 8).unwrap();
        let new_layout = Layout::from_size_align(128, 64).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_DA6A)
            .with_module(0xC0DE_DA6A)
            .with_callsite(0xA110_C06A)
            .with_flags(FLAG_TYPE_ISOLATED);
        let old_ptr = unsafe { alloc.alloc_with_recovery_metadata(old_layout, metadata) };
        assert!(!old_ptr.is_null());
        unsafe {
            old_ptr.write_bytes(0xA5, old_layout.size());
        }

        let moved =
            unsafe { alloc.grow_zeroed(NonNull::new(old_ptr).unwrap(), old_layout, new_layout) }
                .expect("alignment-changing grow_zeroed");
        let moved_ptr = moved.as_ptr() as *mut u8;
        unsafe {
            for offset in 0..old_layout.size() {
                assert_eq!(*moved_ptr.add(offset), 0xA5);
            }
            for offset in old_layout.size()..new_layout.size() {
                assert_eq!(*moved_ptr.add(offset), 0);
            }
        }
        assert_eq!(
            recorded_reallocation_old_metadata(old_ptr, old_layout),
            None
        );
        assert_eq!(
            recorded_reallocation_old_metadata(moved_ptr, new_layout),
            Some(metadata)
        );
        unsafe {
            alloc.deallocate(NonNull::new(moved_ptr).unwrap(), new_layout);
        }
        assert_eq!(take_auto_deallocation_metadata(moved_ptr, new_layout), None);
    }

    #[test]
    fn allocator_grow_zeroed_preserves_prefix_and_zeroes_growth() {
        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(32, 8).unwrap();
        let new_layout = Layout::from_size_align(96, 8).unwrap();
        let block = alloc.allocate(old_layout).expect("initial allocation");
        let ptr = NonNull::new(block.as_ptr() as *mut u8).unwrap();
        unsafe {
            ptr::write_bytes(ptr.as_ptr(), 0xA5, old_layout.size());
            let grown = alloc
                .grow_zeroed(ptr, old_layout, new_layout)
                .expect("grow_zeroed");
            let grown_ptr = grown.as_ptr() as *mut u8;
            for offset in 0..old_layout.size() {
                assert_eq!(*grown_ptr.add(offset), 0xA5);
            }
            for offset in old_layout.size()..new_layout.size() {
                assert_eq!(*grown_ptr.add(offset), 0);
            }
            alloc.deallocate(NonNull::new(grown_ptr).unwrap(), new_layout);
        }
    }

    #[test]
    fn allocator_shrink_preserves_prefix() {
        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(96, 8).unwrap();
        let new_layout = Layout::from_size_align(32, 8).unwrap();
        let block = alloc.allocate(old_layout).expect("initial allocation");
        let ptr = NonNull::new(block.as_ptr() as *mut u8).unwrap();
        unsafe {
            for offset in 0..old_layout.size() {
                *ptr.as_ptr().add(offset) = offset as u8;
            }
            let shrunk = alloc.shrink(ptr, old_layout, new_layout).expect("shrink");
            let shrunk_ptr = shrunk.as_ptr() as *mut u8;
            for offset in 0..new_layout.size() {
                assert_eq!(*shrunk_ptr.add(offset), offset as u8);
            }
            alloc.deallocate(NonNull::new(shrunk_ptr).unwrap(), new_layout);
        }
    }
}
