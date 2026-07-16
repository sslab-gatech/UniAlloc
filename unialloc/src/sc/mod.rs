#[cfg(feature = "separate_sc_backend")]
mod backend;
mod efficient_sc;
mod separate_sc;

#[cfg(all(feature = "metadata_segregation", not(feature = "separate_sc_backend")))]
compile_error!(
    "metadata_segregation must enable separate_sc_backend so the paper variant \
     exercises the bitmap-backed separated-metadata slab backend."
);

#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sys_alloc as system_alloc;
#[cfg(not(feature = "fixed_heap"))]
use crate::sync::PthreadMutex as Mutex;
#[cfg(feature = "separate_sc_backend")]
pub use backend::SCAllocator;
use core::alloc::{AllocError, Allocator, GlobalAlloc, Layout};
use core::ptr::{null_mut, NonNull};
use core::sync::atomic::{AtomicPtr, AtomicUsize, Ordering};
#[cfg(not(feature = "separate_sc_backend"))]
pub use efficient_sc::*;
#[cfg(feature = "separate_sc_backend")]
pub use separate_sc::align_12k;
#[cfg(feature = "fixed_heap")]
use spin::Mutex;

pub struct BumpAlloc {
    start: usize,
    current: usize,
}

#[cfg(all(test, not(feature = "fixed_heap")))]
static TEST_LAST_BUMP_OS_CHUNK_SIZE: AtomicUsize = AtomicUsize::new(0);

#[cfg(feature = "fixed_heap")]
struct FixedHeapInitPlan {
    meta_end: usize,
    meta_bump_current: usize,
    tcache: *mut u8,
    zone: *mut u8,
    page_bump_start: usize,
    page_bump_end: usize,
    rd_tree: usize,
    page_count: usize,
    #[cfg(feature = "bitmap_page_allocator")]
    bitmap_nodes: *mut crate::bitmap_alloc::RunNode,
    #[cfg(feature = "bitmap_page_allocator")]
    bitmap_nodes_len: usize,
}

#[inline]
pub(crate) fn checked_size_class_geometry(
    size_class: usize,
    num_os_pages: usize,
) -> Option<(usize, usize)> {
    if size_class == 0 || num_os_pages == 0 {
        return None;
    }

    let page_span = crate::PAGE_SIZE.checked_mul(num_os_pages)?;
    let pg_count = page_span.checked_div(size_class)?;
    if pg_count == 0 {
        return None;
    }

    let perfect_align = page_span / pg_count;
    let diff_align = perfect_align.checked_sub(size_class)?;
    let min_align = match diff_align.checked_next_power_of_two()? / 2 {
        0 => 1,
        align => align,
    };
    let rounded_size_class = if size_class % min_align == 0 {
        size_class
    } else {
        size_class
            .checked_div(min_align)?
            .checked_add(1)?
            .checked_mul(min_align)?
    };
    let align = if perfect_align.is_power_of_two() {
        perfect_align
    } else {
        rounded_size_class
    };

    Some((pg_count, align))
}

impl Default for BumpAlloc {
    fn default() -> Self {
        Self::new()
    }
}

impl BumpAlloc {
    const DEFAULT_SIZE: usize = 4 * (1 << 24);
    pub const fn new() -> Self {
        Self {
            start: 0,
            current: 0,
        }
    }

    #[inline]
    fn remaining(&self) -> usize {
        self.current
            .checked_sub(self.start)
            .filter(|remaining| *remaining <= Self::DEFAULT_SIZE)
            .unwrap_or(0)
    }

    #[inline]
    fn chunk_end(start: usize) -> Result<usize, AllocError> {
        Self::chunk_end_for_size(start, Self::DEFAULT_SIZE)
    }

    #[inline]
    fn chunk_end_for_size(start: usize, chunk_size: usize) -> Result<usize, AllocError> {
        start.checked_add(chunk_size).ok_or(AllocError)
    }

    #[inline]
    fn checked_round_up_to_page(size: usize, page_size: usize) -> Result<usize, AllocError> {
        if page_size == 0 || !page_size.is_power_of_two() {
            return Err(AllocError);
        }
        size.checked_add(page_size - 1)
            .map(|rounded| rounded & !(page_size - 1))
            .ok_or(AllocError)
    }

    #[inline]
    fn checked_aligned_reclaim_layout(
        start: usize,
        bytes: usize,
        page_size: usize,
    ) -> Result<Layout, AllocError> {
        if page_size == 0 || !page_size.is_power_of_two() || start & (page_size - 1) != 0 {
            return Err(AllocError);
        }
        let size = Self::checked_round_up_to_page(bytes, page_size)?;
        Layout::from_size_align(size, 1).map_err(|_| AllocError)
    }

    #[inline]
    fn checked_align_down(addr: usize, align: usize) -> Result<usize, AllocError> {
        if align == 0 || !align.is_power_of_two() {
            return Err(AllocError);
        }
        Ok(addr & !(align - 1))
    }

    #[inline]
    fn dangling_ptr_for_align(align: usize) -> Result<*mut u8, AllocError> {
        if align == 0 || !align.is_power_of_two() {
            return Err(AllocError);
        }
        Ok(align as *mut u8)
    }

    #[inline]
    fn try_alloc_from_current(&mut self, size: usize, align: usize) -> Result<*mut u8, AllocError> {
        let raw_cur = self.current.checked_sub(size).ok_or(AllocError)?;
        let new_cur = Self::checked_align_down(raw_cur, align)?;
        if new_cur < self.start {
            return Err(AllocError);
        }
        self.current = new_cur;
        Ok(new_cur as *mut u8)
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn release_page_aligned_unused_prefix(start: usize, current: usize) {
        if start == 0 || current <= start || crate::PAGE_SIZE == 0 {
            return;
        }
        let reclaim_end = current & !(crate::PAGE_SIZE - 1);
        if reclaim_end <= start {
            return;
        }
        unsafe {
            system_alloc::munmap(start as *mut u8, reclaim_end - start);
        }
    }

    #[allow(unused_unsafe, unused_variables)]
    pub unsafe fn try_extend(&mut self, size: usize, page_size: usize) -> bool {
        #[cfg(all(feature = "fixed_heap", feature = "bitmap_page_allocator"))]
        {
            // The bitmap tree owns caller-provided metadata sized for the
            // initial fixed range. Growing the address range requires a
            // transactional radix/tree metadata replacement. Reject growth
            // before mutating either root until that protocol is available.
            let _ = (size, page_size);
            false
        }
        #[cfg(all(feature = "fixed_heap", not(feature = "bitmap_page_allocator")))]
        {
            if size == 0
                || page_size == 0
                || !page_size.is_power_of_two()
                || !fixed_heap_roots_initialized()
            {
                return false;
            }
            // Compute the candidate heap end without publishing it.  The radix
            // tree owns the authoritative metadata for the object range, so
            // `ArrayNode::extend_with_range` commits the page-run bump's larger
            // checkpoint only if the replacement radix backing is allocated and
            // ready to publish.
            let new_end = match BUMP.lock().checked_extend_end(size) {
                Ok(end) => end,
                Err(_) => return false,
            };
            let (prev_rd_tree, prev_count) = match try_with_rd_tree(|rd_tree| {
                // The global radix root is protected by `RD_TREE_LOCK`; the
                // method itself is unsafe because it reallocates the raw backing
                // array after the fixed heap has grown.
                unsafe { rd_tree.extend_with_range(size, new_end, page_size) }
            }) {
                Ok(Ok(range)) => range,
                _ => return false,
            };
            let prev_bytes = match prev_count.checked_mul(core::mem::size_of::<i64>()) {
                Some(bytes) => bytes,
                None => return true,
            };
            let prev_end = match prev_rd_tree.checked_add(prev_bytes) {
                Some(end) => end,
                None => return true,
            };
            if PG_BUMP
                .lock()
                .try_extend(prev_rd_tree, prev_end, page_size)
                .is_err()
            {
                // Return the replaced radix-tree backing to the allocator when
                // it cannot be reused as an object-page metadata range.
                let layout =
                    match Self::checked_aligned_reclaim_layout(prev_rd_tree, prev_bytes, page_size)
                    {
                        Ok(layout) => layout,
                        Err(_) => return true,
                    };
                GlobalBackend.dealloc(prev_rd_tree as *mut u8, layout);
            }
            true
        }
        #[cfg(not(feature = "fixed_heap"))]
        {
            let _ = (size, page_size);
            true
        }
    }

    #[allow(unused_variables)]
    pub unsafe fn extend(&mut self, size: usize, page_size: usize) {
        let _ = self.try_extend(size, page_size);
    }

    #[cfg(feature = "fixed_heap")]
    fn prepare_fixed_heap_init(
        start: usize,
        end: usize,
        page_size: usize,
    ) -> Option<FixedHeapInitPlan> {
        if start == 0
            || page_size == 0
            || !page_size.is_power_of_two()
            || start % page_size != 0
            || end <= start
        {
            return None;
        }
        let span = end
            .checked_sub(start)
            .filter(|span| span % page_size == 0)?;
        let page_count = span.checked_div(page_size).filter(|count| *count != 0)?;
        let tcache_layout = Layout::new::<ThreadCache>();
        let zone_layout = Layout::new::<ZoneAllocator>();
        let page_bump_bytes = page_count.checked_mul(core::mem::size_of::<EfObjectPage>())?;
        let page_bump_layout =
            Layout::from_size_align(page_bump_bytes, core::mem::align_of::<EfObjectPage>()).ok()?;
        let rd_tree_bytes = page_count.checked_mul(core::mem::size_of::<i64>())?;
        let rd_tree_layout =
            Layout::from_size_align(rd_tree_bytes, core::mem::align_of::<i64>()).ok()?;
        #[cfg(feature = "bitmap_page_allocator")]
        let bitmap_layout = crate::bitmap_alloc::SegmentPageAllocator::required_layout(page_count)?;
        #[cfg(not(feature = "bitmap_page_allocator"))]
        let bitmap_layout = Layout::from_size_align(0, 1).ok()?;
        let total_metadata = [
            tcache_layout,
            zone_layout,
            page_bump_layout,
            rd_tree_layout,
            bitmap_layout,
        ]
        .iter()
        .try_fold(0usize, |acc, layout| {
            acc.checked_add(layout.size())?
                .checked_add(layout.align().saturating_sub(1))
        })?;
        let meta_pages = total_metadata
            .checked_add(page_size - 1)
            .map(|bytes| bytes / page_size)
            .filter(|pages| *pages != 0 && *pages < page_count)?;
        let meta_bytes = page_size.checked_mul(meta_pages)?;
        let meta_end = start
            .checked_add(meta_bytes)
            .filter(|candidate| *candidate <= end)?;

        // Plan all metadata bump allocations against a scratch bump pointer so
        // any geometry/layout failure leaves the live allocator state untouched.
        let mut scratch = BumpAlloc {
            start,
            current: meta_end,
        };
        let tcache = scratch
            .alloc_aligned(tcache_layout.size(), tcache_layout.align())
            .ok()?;
        let zone = scratch
            .alloc_aligned(zone_layout.size(), zone_layout.align())
            .ok()?;
        let page_bump_end = scratch.current;
        let page_bump_start = scratch
            .alloc_aligned(page_bump_layout.size(), page_bump_layout.align())
            .ok()? as usize;
        let rd_tree = scratch
            .alloc_aligned(rd_tree_layout.size(), rd_tree_layout.align())
            .ok()? as usize;
        #[cfg(feature = "bitmap_page_allocator")]
        let bitmap_nodes = scratch
            .alloc_aligned(bitmap_layout.size(), bitmap_layout.align())
            .ok()? as *mut crate::bitmap_alloc::RunNode;

        Some(FixedHeapInitPlan {
            meta_end,
            meta_bump_current: scratch.current,
            tcache,
            zone,
            page_bump_start,
            page_bump_end,
            rd_tree,
            page_count,
            #[cfg(feature = "bitmap_page_allocator")]
            bitmap_nodes,
            #[cfg(feature = "bitmap_page_allocator")]
            bitmap_nodes_len: bitmap_layout.size()
                / core::mem::size_of::<crate::bitmap_alloc::RunNode>(),
        })
    }

    #[allow(unused_variables)]
    pub unsafe fn init_with_range(&mut self, start: usize, end: usize, page_size: usize) -> bool {
        #[cfg(feature = "fixed_heap")]
        {
            let plan = match Self::prepare_fixed_heap_init(start, end, page_size) {
                Some(plan) => plan,
                None => return false,
            };
            #[cfg(feature = "bitmap_page_allocator")]
            {
                let managed_pages = match end
                    .checked_sub(plan.meta_end)
                    .and_then(|bytes| bytes.checked_div(page_size))
                    .filter(|pages| *pages != 0)
                {
                    Some(pages) => pages,
                    None => return false,
                };
                if crate::bitmap_alloc::PAGE_RUN_BITMAP
                    .lock()
                    .initialize(
                        plan.meta_end,
                        managed_pages,
                        page_size,
                        plan.bitmap_nodes,
                        plan.bitmap_nodes_len,
                    )
                    .is_err()
                {
                    return false;
                }
            }
            self.start = start;
            self.current = plan.meta_bump_current;

            // Publish global roots only after all fixed-heap metadata
            // allocations succeeded; callers that race early observe the
            // allocator as not-ready instead of dereferencing half-built roots.
            BUMP.lock().init_with_range(plan.meta_end, end);
            PG_BUMP
                .lock()
                .init_with_range(plan.page_bump_start, plan.page_bump_end);
            if try_with_rd_tree(|rd_tree| {
                rd_tree.init_with_range(start, plan.rd_tree as *mut i64, plan.page_count)
            })
            .is_err()
            {
                return false;
            }
            core::ptr::write(plan.tcache as *mut ThreadCache, ThreadCache::new());
            core::ptr::write(plan.zone as *mut ZoneAllocator, ZoneAllocator::new());
            crate::cache::install_fixed_tcache(plan.tcache as *mut ThreadCache);
            crate::zone::install_fixed_zone(plan.zone as *mut ZoneAllocator);
            true
        }
        #[cfg(not(feature = "fixed_heap"))]
        {
            let _ = (start, end, page_size);
            true
        }
    }

    pub fn alloc_aligned(&mut self, size: usize, align: usize) -> Result<*mut u8, AllocError> {
        self.alloc_aligned_with_chunk(size, align, Self::DEFAULT_SIZE)
    }

    pub fn alloc_aligned_with_chunk(
        &mut self,
        size: usize,
        align: usize,
        chunk_size: usize,
    ) -> Result<*mut u8, AllocError> {
        if size == 0 {
            return Self::dangling_ptr_for_align(align);
        }
        if chunk_size == 0 || size > chunk_size || align == 0 || !align.is_power_of_two() {
            return Err(AllocError);
        }

        if let Ok(ptr) = self.try_alloc_from_current(size, align) {
            return Ok(ptr);
        }

        #[cfg(not(feature = "fixed_heap"))]
        {
            #[cfg(test)]
            TEST_LAST_BUMP_OS_CHUNK_SIZE.store(chunk_size, Ordering::Relaxed);
            let old_start = self.start;
            let old_current = self.current;
            let prot = system_alloc::prots::get_prot(true, true, false);
            let start = unsafe {
                #[cfg(feature = "hugepage")]
                let mut ptr = system_alloc::mmap_huge(chunk_size, prot) as *mut u8;
                #[cfg(not(feature = "hugepage"))]
                let mut ptr = system_alloc::mmap(chunk_size, prot) as *mut u8;
                ptr = system_alloc::normalize_mmap_result(ptr);
                ptr
            };
            let start = NonNull::new(start).ok_or(AllocError)?;
            let new_start = start.as_ptr() as usize;
            let new_current = Self::chunk_end_for_size(new_start, chunk_size)?;
            Self::release_page_aligned_unused_prefix(old_start, old_current);
            self.start = new_start;
            self.current = new_current;
        }
        #[cfg(feature = "fixed_heap")]
        {
            Err(AllocError)
        }
        #[cfg(not(feature = "fixed_heap"))]
        {
            self.try_alloc_from_current(size, align)
        }
    }

    pub fn alloc(&mut self, size: usize) -> Result<*mut u8, AllocError> {
        self.alloc_aligned(size, 1)
    }
}

pub struct MetaBumpAlloc {
    bumper: BumpAlloc,
    backup: *mut usize,
    backup_len: usize,
}

// `backup` is an allocator-internal singly-linked free list head.  The only
// shared global instance is behind `spin::Mutex<META_BUMP>`, so moving the value
// between threads is safe as long as callers keep using that lock boundary.
unsafe impl Send for MetaBumpAlloc {}

impl Default for MetaBumpAlloc {
    fn default() -> Self {
        Self::new()
    }
}

impl MetaBumpAlloc {
    /// Metadata bump chunks are intentionally much smaller than the generic
    /// `BumpAlloc` chunk.  In non-fixed builds this allocator backs allocator
    /// roots such as the thread cache and zone metadata; reserving the generic
    /// 64MiB chunk for those small structures inflated virtual footprint before
    /// the application had allocated many objects.
    const DEFAULT_SIZE: usize = 1 << 21;
    const ALLOC_UNIT: usize = 1 << 11;
    /// Bound backup-list scans so mixed-alignment reuse stays predictable while
    /// still avoiding needless metadata bump growth when an aligned reusable
    /// unit is just behind a lower-alignment head.
    const BACKUP_SCAN_LIMIT: usize = 64;
    /// Keep every retained reusable metadata node within the bounded scan
    /// horizon. This bounds allocator-internal reusable metadata retention; it
    /// does not imply individual bump-allocated objects are returned to the OS.
    const BACKUP_MAX_ENTRIES: usize = Self::BACKUP_SCAN_LIMIT;

    pub const fn new() -> Self {
        Self {
            bumper: BumpAlloc::new(),
            backup: null_mut(),
            backup_len: 0,
        }
    }

    pub unsafe fn try_extend(&mut self, size: usize, page_size: usize) -> bool {
        self.bumper.try_extend(size, page_size)
    }

    pub unsafe fn extend(&mut self, size: usize, page_size: usize) {
        let _ = self.try_extend(size, page_size);
    }

    pub unsafe fn init_with_range(&mut self, start: usize, end: usize, page_size: usize) -> bool {
        self.bumper.init_with_range(start, end, page_size)
    }

    #[inline]
    const fn reusable_unit_size() -> usize {
        core::mem::size_of::<ThreadCache>()
    }

    #[inline]
    const fn reusable_unit_align() -> usize {
        core::mem::align_of::<ThreadCache>()
    }

    fn take_backup_aligned(&mut self, align: usize) -> Option<*mut u8> {
        if align == 0 || !align.is_power_of_two() {
            return None;
        }

        let mut prev: *mut usize = null_mut();
        let mut cur = self.backup;
        let mut scanned = 0usize;
        let reusable_align =
            core::cmp::max(Self::reusable_unit_align(), core::mem::align_of::<usize>());

        while !cur.is_null() && scanned < Self::BACKUP_SCAN_LIMIT {
            if (cur as usize) & (reusable_align - 1) != 0 {
                return None;
            }
            let next = unsafe { *cur as *mut usize };
            if (cur as usize) & (align - 1) == 0 {
                if prev.is_null() {
                    self.backup = next;
                } else {
                    unsafe { *prev = next as usize };
                }
                self.backup_len = self.backup_len.saturating_sub(1);
                return Some(cur as *mut u8);
            }
            prev = cur;
            cur = next;
            scanned += 1;
        }

        None
    }

    pub fn alloc_aligned(&mut self, size: usize, align: usize) -> Result<*mut u8, AllocError> {
        if size == 0 {
            return BumpAlloc::dangling_ptr_for_align(align);
        }
        if align == 0 || !align.is_power_of_two() {
            return Err(AllocError);
        }

        if size == Self::reusable_unit_size() {
            if let Some(reused) = self.take_backup_aligned(align) {
                return Ok(reused);
            }
        }
        self.bumper
            .alloc_aligned_with_chunk(size, align, Self::DEFAULT_SIZE)
    }

    pub fn alloc(&mut self, size: usize) -> Result<*mut u8, AllocError> {
        self.alloc_aligned(size, core::mem::align_of::<usize>())
    }

    pub fn dealloc(&mut self, ptr: *mut usize, size: usize) {
        if ptr.is_null() {
            return;
        }
        // The backup list is intentionally only for reusable ThreadCache-sized
        // metadata units.  Variable-sized metadata allocations stay bump-only:
        // writing a next pointer into a smaller object would corrupt it.  This
        // bounds reusable metadata retention and scan cost; ignored bump
        // allocations are not individually returned to the OS.
        if size != Self::reusable_unit_size() {
            return;
        }
        let align = core::cmp::max(Self::reusable_unit_align(), core::mem::align_of::<usize>());
        if (ptr as usize) & (align - 1) != 0 {
            return;
        }
        // Retention is capped to the scan horizon, so mixed-alignment churn
        // cannot build an indefinitely deep practically unreachable tail.
        let head = self.backup;
        unsafe { *ptr = head as usize };
        self.backup = ptr;
        if self.backup_len < Self::BACKUP_MAX_ENTRIES {
            self.backup_len += 1;
            return;
        }

        if Self::BACKUP_MAX_ENTRIES == 0 {
            self.backup = null_mut();
            return;
        }

        let mut cur = self.backup;
        for _ in 1..Self::BACKUP_MAX_ENTRIES {
            let next = unsafe { *cur as *mut usize };
            if next.is_null() {
                self.backup_len = self.backup_len.min(Self::BACKUP_MAX_ENTRIES);
                return;
            }
            cur = next;
        }
        unsafe { *cur = 0 };
        self.backup_len = Self::BACKUP_MAX_ENTRIES;
    }
}

/// Global metadata bump/reuse allocator.
///
/// The mutable fields live behind the mutex.  This intentionally is not
/// `static mut`, avoiding UB-prone shared references to mutable statics on
/// modern Rust while preserving the existing allocator synchronization model.
pub static META_BUMP: Mutex<MetaBumpAlloc> = Mutex::new(MetaBumpAlloc::new());

#[derive(Copy, Clone, Default)]
pub struct MetaAllocator {}

unsafe impl Allocator for MetaAllocator {
    fn allocate(&self, layout: Layout) -> Result<NonNull<[u8]>, AllocError> {
        if layout.size() == 0 {
            let ptr = NonNull::new(layout.align() as *mut u8).ok_or(AllocError)?;
            return Ok(NonNull::slice_from_raw_parts(ptr, 0));
        }

        let raw_ptr = META_BUMP
            .lock()
            .alloc_aligned(layout.size(), layout.align())? as *mut u8;
        let ptr = NonNull::new(raw_ptr).ok_or(core::alloc::AllocError)?;
        Ok(NonNull::slice_from_raw_parts(ptr, layout.size()))
    }

    unsafe fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
        if layout.size() == 0 {
            return;
        }

        if layout.size() == MetaBumpAlloc::reusable_unit_size() {
            META_BUMP
                .lock()
                .dealloc(ptr.as_ptr() as *mut usize, layout.size())
        }
    }
}
use crate::cache::ThreadCache;
#[cfg(feature = "fixed_heap")]
use crate::collections::radix_tree::try_with_rd_tree;
use crate::freelist::BUMP;
use crate::page::{EfObjectPage, PG_BUMP};
use crate::prelude::GlobalBackend;
#[cfg(feature = "fixed_heap")]
use crate::zone::ZoneAllocator;
pub use MetaAllocator as MetadataAllocator;

#[cfg(feature = "fixed_heap")]
#[inline]
pub(crate) fn fixed_heap_roots_initialized() -> bool {
    crate::cache::fixed_tcache_initialized() && crate::zone::fixed_zone_initialized()
}

#[cfg(all(test, feature = "fixed_heap"))]
const TEST_FIXED_HEAP_BYTES: usize = 256 * 1024 * 1024;

#[cfg(all(test, feature = "fixed_heap"))]
#[repr(align(16384))]
struct FixedHeapTestHeap([u8; TEST_FIXED_HEAP_BYTES]);

#[cfg(all(test, feature = "fixed_heap"))]
static FIXED_HEAP_TEST_INIT_STATE: AtomicUsize = AtomicUsize::new(0);
#[cfg(all(test, feature = "fixed_heap"))]
static FIXED_HEAP_TEST_SERIAL_LOCK: spin::Mutex<()> = spin::Mutex::new(());
#[cfg(all(test, feature = "fixed_heap"))]
extern crate std;
#[cfg(all(test, feature = "fixed_heap"))]
std::thread_local! {
    static FIXED_HEAP_TEST_INIT_OWNER: core::cell::Cell<bool> = core::cell::Cell::new(false);
}
#[cfg(all(test, feature = "fixed_heap"))]
const FIXED_HEAP_TEST_INIT_UNINITIALIZED: usize = 0;
#[cfg(all(test, feature = "fixed_heap"))]
const FIXED_HEAP_TEST_INIT_INITIALIZING: usize = 1;
#[cfg(all(test, feature = "fixed_heap"))]
const FIXED_HEAP_TEST_INIT_INITIALIZED: usize = 2;

#[cfg(all(test, feature = "fixed_heap"))]
static mut FIXED_HEAP_TEST_HEAP: FixedHeapTestHeap = FixedHeapTestHeap([0; TEST_FIXED_HEAP_BYTES]);

#[cfg(all(test, feature = "fixed_heap"))]
struct FixedHeapTestInitOwnerGuard;

#[cfg(all(test, feature = "fixed_heap"))]
impl FixedHeapTestInitOwnerGuard {
    fn enter() -> Self {
        FIXED_HEAP_TEST_INIT_OWNER.with(|owner| owner.set(true));
        Self
    }
}

#[cfg(all(test, feature = "fixed_heap"))]
impl Drop for FixedHeapTestInitOwnerGuard {
    fn drop(&mut self) {
        FIXED_HEAP_TEST_INIT_OWNER.with(|owner| owner.set(false));
    }
}

#[cfg(all(test, feature = "fixed_heap"))]
pub(crate) fn ensure_fixed_heap_test_initialized() {
    loop {
        match FIXED_HEAP_TEST_INIT_STATE.compare_exchange(
            FIXED_HEAP_TEST_INIT_UNINITIALIZED,
            FIXED_HEAP_TEST_INIT_INITIALIZING,
            Ordering::AcqRel,
            Ordering::Acquire,
        ) {
            Ok(_) => break,
            Err(FIXED_HEAP_TEST_INIT_INITIALIZED) => return,
            Err(FIXED_HEAP_TEST_INIT_INITIALIZING) => {
                // `ZoneAllocator::new` constructs every size-class allocator;
                // the optional separate backend re-enters this helper on the
                // initializing thread.  That owner must return to finish the
                // bootstrap, while unrelated test threads must wait until the
                // global roots are actually published.
                if FIXED_HEAP_TEST_INIT_OWNER.with(|owner| owner.get()) {
                    return;
                }
                while FIXED_HEAP_TEST_INIT_STATE.load(Ordering::Acquire)
                    == FIXED_HEAP_TEST_INIT_INITIALIZING
                {
                    core::hint::spin_loop();
                }
            }
            Err(_) => {}
        }
    }

    if fixed_heap_roots_initialized() {
        FIXED_HEAP_TEST_INIT_STATE.store(FIXED_HEAP_TEST_INIT_INITIALIZED, Ordering::Release);
        return;
    }

    let _owner = FixedHeapTestInitOwnerGuard::enter();
    let initialized = unsafe {
        // The init-state transition above grants one-time access to the test
        // heap.  Keep that access as a raw pointer so we never create a mutable
        // reference to a `static mut` backing range.
        let heap = core::ptr::addr_of_mut!(FIXED_HEAP_TEST_HEAP);
        let start = core::ptr::addr_of_mut!((*heap).0).cast::<u8>() as usize;
        start
            .checked_add(TEST_FIXED_HEAP_BYTES)
            .map(|end| {
                META_BUMP
                    .lock()
                    .init_with_range(start, end, crate::PAGE_SIZE)
            })
            .unwrap_or(false)
    };

    FIXED_HEAP_TEST_INIT_STATE.store(
        if initialized {
            FIXED_HEAP_TEST_INIT_INITIALIZED
        } else {
            FIXED_HEAP_TEST_INIT_UNINITIALIZED
        },
        Ordering::Release,
    );
}

/// Serialize unit tests that inspect or mutate the process-wide fixed heap.
///
/// The fixed-heap backend deliberately has one global radix tree, zone, page
/// bump, and thread cache. Libtest runs modules in parallel, so tests with
/// exact retained-list/accounting assertions must share this guard rather than
/// using unrelated module-local locks.
#[cfg(all(test, feature = "fixed_heap"))]
pub(crate) fn fixed_heap_test_guard() -> spin::MutexGuard<'static, ()> {
    let guard = FIXED_HEAP_TEST_SERIAL_LOCK.lock();
    ensure_fixed_heap_test_initialized();
    guard
}

#[cfg(test)]
mod tests {
    extern crate std;

    use super::*;
    use crate::PAGE_SIZE;

    #[test]
    fn bump_alloc_rejects_oversized_request_without_underflow() {
        let mut bump = BumpAlloc::new();
        assert!(bump.alloc(BumpAlloc::DEFAULT_SIZE + 1).is_err());
        assert_eq!(bump.start, 0);
        assert_eq!(bump.current, 0);
    }

    #[test]
    fn bump_alloc_existing_capacity_uses_checked_subtraction() {
        let mut bump = BumpAlloc {
            start: 0x1000,
            current: 0x1100,
        };

        let ptr = bump.alloc(0x40).expect("existing capacity");

        assert_eq!(ptr as usize, 0x10c0);
        assert_eq!(bump.current, 0x10c0);
    }

    #[test]
    fn bump_alloc_aligned_respects_requested_alignment() {
        let mut bump = BumpAlloc {
            start: 0x1000,
            current: 0x1100,
        };

        let ptr = bump.alloc_aligned(24, 64).expect("aligned bump allocation");

        assert_eq!((ptr as usize) % 64, 0);
        assert_eq!(ptr as usize, 0x10c0);
        assert_eq!(bump.current, 0x10c0);
        assert!(bump.current >= bump.start);
    }

    #[test]
    fn bump_alloc_aligned_rejects_invalid_alignment() {
        let mut bump = BumpAlloc {
            start: 0x1000,
            current: 0x1100,
        };

        assert!(bump.alloc_aligned(16, 0).is_err());
        assert!(bump.alloc_aligned(16, 3).is_err());
    }

    #[test]
    fn bump_alloc_zero_sized_request_returns_non_null_without_state_change() {
        let mut bump = BumpAlloc {
            start: 0x1000,
            current: 0x1100,
        };

        let ptr = bump.alloc(0).expect("zero-size allocation");

        assert!(!ptr.is_null());
        assert_eq!(bump.start, 0x1000);
        assert_eq!(bump.current, 0x1100);
    }

    #[test]
    fn bump_alloc_chunk_end_rejects_overflow_before_state_commit() {
        assert!(BumpAlloc::chunk_end(usize::MAX - BumpAlloc::DEFAULT_SIZE + 1).is_err());
    }

    #[test]
    fn bump_alloc_page_rounding_rejects_bad_page_size() {
        assert!(BumpAlloc::checked_round_up_to_page(1, 0).is_err());
        assert!(BumpAlloc::checked_round_up_to_page(1, 3).is_err());
    }

    #[test]
    fn bump_alloc_page_rounding_rejects_overflow() {
        assert!(BumpAlloc::checked_round_up_to_page(usize::MAX, PAGE_SIZE).is_err());
    }

    #[test]
    fn bump_alloc_page_rounding_preserves_aligned_size() {
        assert_eq!(
            BumpAlloc::checked_round_up_to_page(PAGE_SIZE, PAGE_SIZE).expect("rounding"),
            PAGE_SIZE
        );
    }

    #[test]
    fn bump_alloc_reclaim_layout_rejects_misaligned_start_without_panic() {
        assert!(BumpAlloc::checked_aligned_reclaim_layout(1, PAGE_SIZE, PAGE_SIZE).is_err());
    }

    #[test]
    fn bump_alloc_reclaim_layout_rejects_bad_page_size_without_panic() {
        assert!(BumpAlloc::checked_aligned_reclaim_layout(PAGE_SIZE, PAGE_SIZE, 0).is_err());
        assert!(BumpAlloc::checked_aligned_reclaim_layout(PAGE_SIZE, PAGE_SIZE, 3).is_err());
    }

    #[test]
    fn bump_alloc_reclaim_layout_rounds_to_page() {
        let layout = BumpAlloc::checked_aligned_reclaim_layout(PAGE_SIZE, 1, PAGE_SIZE)
            .expect("aligned reclaim layout");

        assert_eq!(layout.size(), PAGE_SIZE);
        assert_eq!(layout.align(), 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn bump_alloc_non_fixed_init_with_range_is_noop_without_panic() {
        let mut bump = BumpAlloc::new();

        unsafe {
            bump.init_with_range(0x1000, 0x2000, PAGE_SIZE);
        }

        assert_eq!(bump.start, 0);
        assert_eq!(bump.current, 0);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn bump_alloc_non_fixed_extend_is_noop_without_panic() {
        let mut bump = BumpAlloc::new();

        unsafe {
            bump.extend(PAGE_SIZE, PAGE_SIZE);
        }

        assert_eq!(bump.start, 0);
        assert_eq!(bump.current, 0);
    }

    #[test]
    fn meta_bump_alloc_rejects_rounding_overflow() {
        let mut meta = MetaBumpAlloc::new();
        assert!(meta.alloc(usize::MAX).is_err());
    }

    #[test]
    fn meta_bump_alloc_rejects_requests_larger_than_compact_chunk() {
        let mut meta = MetaBumpAlloc::new();

        assert!(meta.alloc(MetaBumpAlloc::DEFAULT_SIZE + 1).is_err());
        assert_eq!(meta.bumper.start, 0);
        assert_eq!(meta.bumper.current, 0);
    }

    #[test]
    fn meta_bump_default_chunk_covers_allocator_roots_without_generic_reservation() {
        let allocator_roots = core::mem::size_of::<ThreadCache>()
            .checked_add(core::mem::size_of::<crate::zone::ZoneAllocator>())
            .expect("allocator root metadata size should not overflow");

        assert!(MetaBumpAlloc::DEFAULT_SIZE >= allocator_roots);
        assert!(MetaBumpAlloc::DEFAULT_SIZE < BumpAlloc::DEFAULT_SIZE);
        assert_eq!(MetaBumpAlloc::DEFAULT_SIZE % MetaBumpAlloc::ALLOC_UNIT, 0);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn meta_bump_alloc_uses_compact_chunk_for_fresh_mapping() {
        let mut meta = MetaBumpAlloc::new();
        let ptr = meta
            .alloc_aligned(128, 64)
            .expect("fresh metadata bump allocation");

        assert_ne!(meta.bumper.start, 0);
        assert_ne!(ptr as usize, 0);
        assert!(meta.bumper.current >= meta.bumper.start);
        assert!(
            meta.bumper.current
                < BumpAlloc::chunk_end_for_size(meta.bumper.start, MetaBumpAlloc::DEFAULT_SIZE)
                    .expect("compact chunk end")
        );
        assert!(
            BumpAlloc::chunk_end_for_size(meta.bumper.start, BumpAlloc::DEFAULT_SIZE)
                .expect("generic chunk end")
                > BumpAlloc::chunk_end_for_size(meta.bumper.start, MetaBumpAlloc::DEFAULT_SIZE)
                    .expect("compact chunk end")
        );
    }

    #[cfg(all(
        not(feature = "fixed_heap"),
        any(target_os = "linux", target_os = "macos")
    ))]
    #[test]
    fn meta_bump_alloc_maps_compact_chunk_in_os() {
        struct MappingGuard {
            start: usize,
            size: usize,
        }

        impl Drop for MappingGuard {
            fn drop(&mut self) {
                if self.start != 0 && self.size != 0 {
                    unsafe {
                        system_alloc::munmap(self.start as *mut u8, self.size);
                    }
                }
            }
        }

        unsafe fn os_page_is_mapped(page_addr: usize) -> bool {
            debug_assert_eq!(page_addr % PAGE_SIZE, 0);
            let mut residency = 0_u8;
            libc::mincore(
                // Linux and Darwin disagree on the signedness of the residency
                // byte. Let the final pointer cast infer the target libc ABI;
                // the kernel still writes the same one-byte bit vector.
                page_addr as *mut libc::c_void,
                PAGE_SIZE,
                (&mut residency as *mut u8).cast(),
            ) == 0
        }

        let mut meta = MetaBumpAlloc::new();
        let ptr = meta
            .alloc_aligned(128, 64)
            .expect("fresh metadata bump allocation");
        let _guard = MappingGuard {
            start: meta.bumper.start,
            size: MetaBumpAlloc::DEFAULT_SIZE,
        };

        assert_ne!(ptr as usize, 0);
        assert_eq!(meta.bumper.start % PAGE_SIZE, 0);
        assert_eq!(MetaBumpAlloc::DEFAULT_SIZE % PAGE_SIZE, 0);
        assert!(MetaBumpAlloc::DEFAULT_SIZE < BumpAlloc::DEFAULT_SIZE);

        let compact_end =
            BumpAlloc::chunk_end_for_size(meta.bumper.start, MetaBumpAlloc::DEFAULT_SIZE)
                .expect("compact chunk end");
        let compact_last_page = compact_end
            .checked_sub(PAGE_SIZE)
            .expect("compact chunk contains at least one page");

        assert!(
            unsafe { os_page_is_mapped(meta.bumper.start) },
            "first page of the compact metadata chunk must be backed by the OS mapping"
        );
        assert!(
            unsafe { os_page_is_mapped(compact_last_page) },
            "last page of the compact metadata chunk must be backed by the OS mapping"
        );
        assert!(
            meta.bumper.current < compact_end,
            "metadata bump cursor must stay inside the compact chunk, not a historical generic chunk"
        );
    }

    #[cfg(all(not(feature = "fixed_heap"), target_os = "linux"))]
    #[test]
    fn meta_bump_alloc_requests_compact_os_chunk() {
        const CHILD_ENV: &str = "UNIALLOC_META_BUMP_CHUNK_SIZE_CHILD";
        const TEST_NAME: &str = "sc::tests::meta_bump_alloc_requests_compact_os_chunk";

        if std::env::var_os(CHILD_ENV).is_none() {
            let output = std::process::Command::new(
                std::env::current_exe().expect("current allocator test executable"),
            )
            .args(["--exact", TEST_NAME, "--nocapture", "--test-threads=1"])
            .env(CHILD_ENV, "1")
            .env("RUST_BACKTRACE", "0")
            .output()
            .expect("spawn isolated metadata bump mapping test");
            assert!(
                output.status.success(),
                "isolated metadata bump mapping test failed: {}",
                std::string::String::from_utf8_lossy(&output.stderr)
            );
            return;
        }

        struct MappingGuard {
            start: usize,
            size: usize,
        }

        impl Drop for MappingGuard {
            fn drop(&mut self) {
                if self.start != 0 && self.size != 0 {
                    unsafe {
                        system_alloc::munmap(self.start as *mut u8, self.size);
                    }
                }
            }
        }

        TEST_LAST_BUMP_OS_CHUNK_SIZE.store(0, Ordering::Relaxed);
        let mut meta = MetaBumpAlloc::new();
        let ptr = meta
            .alloc_aligned(128, 64)
            .expect("fresh metadata bump allocation");
        let _guard = MappingGuard {
            start: meta.bumper.start,
            size: MetaBumpAlloc::DEFAULT_SIZE,
        };

        assert_ne!(ptr as usize, 0);
        assert_eq!(meta.bumper.start % PAGE_SIZE, 0);
        assert!(MetaBumpAlloc::DEFAULT_SIZE < BumpAlloc::DEFAULT_SIZE);
        assert_eq!(
            TEST_LAST_BUMP_OS_CHUNK_SIZE.load(Ordering::Relaxed),
            MetaBumpAlloc::DEFAULT_SIZE,
            "metadata allocation must request the compact chunk size from the OS mapping path"
        );
    }

    #[cfg(all(
        not(feature = "fixed_heap"),
        any(target_os = "linux", target_os = "macos")
    ))]
    #[test]
    fn bump_alloc_releases_page_aligned_unused_prefix_when_replacing_chunk() {
        const CHILD_ENV: &str = "UNIALLOC_BUMP_RELEASED_PREFIX_CHILD";
        const TEST_NAME: &str =
            "sc::tests::bump_alloc_releases_page_aligned_unused_prefix_when_replacing_chunk";

        if crate::test_support::run_test_in_fresh_process(CHILD_ENV, TEST_NAME) {
            return;
        }

        #[cfg(target_os = "linux")]
        unsafe fn os_can_map_exact_page(page_addr: usize) -> bool {
            debug_assert_eq!(page_addr % PAGE_SIZE, 0);
            let mapped = libc::mmap(
                page_addr as *mut libc::c_void,
                PAGE_SIZE,
                libc::PROT_NONE,
                libc::MAP_PRIVATE | libc::MAP_ANONYMOUS | libc::MAP_FIXED_NOREPLACE,
                -1,
                0,
            );
            if mapped == libc::MAP_FAILED || mapped as usize != page_addr {
                return false;
            }
            libc::munmap(mapped, PAGE_SIZE);
            true
        }

        #[cfg(target_os = "macos")]
        unsafe fn os_can_map_exact_page(page_addr: usize) -> bool {
            extern "C" {
                static mach_task_self_: libc::mach_port_t;
            }

            debug_assert_eq!(page_addr % PAGE_SIZE, 0);
            let mut address = page_addr as libc::vm_address_t;
            let result = libc::vm_allocate(
                mach_task_self_,
                &mut address,
                PAGE_SIZE as libc::vm_size_t,
                0,
            );
            if result != 0 || address as usize != page_addr {
                if result == 0 {
                    let _ =
                        libc::vm_deallocate(mach_task_self_, address, PAGE_SIZE as libc::vm_size_t);
                }
                return false;
            }
            let _ = libc::vm_deallocate(mach_task_self_, address, PAGE_SIZE as libc::vm_size_t);
            true
        }

        let chunk_size = PAGE_SIZE * 2;
        let prot = system_alloc::prots::get_prot(true, true, false);
        let old_mapping = unsafe { system_alloc::mmap(chunk_size, prot) };
        assert!(!system_alloc::mmap_failed(old_mapping));
        let old_start = old_mapping as usize;
        let old_current = old_start + PAGE_SIZE;

        let mut bump = BumpAlloc {
            start: old_start,
            current: old_current,
        };
        let ptr = bump
            .alloc_aligned_with_chunk(PAGE_SIZE + 1, 1, chunk_size)
            .expect("larger request should replace the exhausted old chunk");

        assert!(!ptr.is_null());
        assert_ne!(bump.start, old_start);
        assert!(
            unsafe { os_can_map_exact_page(old_start) },
            "the page-aligned unused prefix of the old chunk should be released"
        );

        unsafe {
            system_alloc::munmap((old_start + PAGE_SIZE) as *mut u8, PAGE_SIZE);
            system_alloc::munmap(bump.start as *mut u8, chunk_size);
        }
    }

    #[test]
    fn meta_allocator_zero_sized_layout_is_non_null_and_dealloc_noop() {
        let alloc = MetaAllocator::default();
        let layout = Layout::from_size_align(0, 256).expect("zero-size layout");

        let ptr = alloc.allocate(layout).expect("zero-size allocation");
        let data = ptr.as_ptr() as *mut u8;

        assert!(!data.is_null());
        assert_eq!((data as usize) % layout.align(), 0);

        unsafe {
            alloc.deallocate(
                NonNull::new(data).expect("non-null zero-size pointer"),
                layout,
            );
        }
    }

    fn seeded_meta_bump_for_alignment_tests() -> MetaBumpAlloc {
        MetaBumpAlloc {
            bumper: BumpAlloc {
                start: 0x1000,
                current: 0x4000,
            },
            backup: null_mut(),
            backup_len: 0,
        }
    }

    const BACKUP_TEST_WORDS_PER_UNIT: usize =
        (MetaBumpAlloc::reusable_unit_size() + core::mem::size_of::<usize>() - 1)
            / core::mem::size_of::<usize>();
    const BACKUP_TEST_ENTRY_COUNT: usize = MetaBumpAlloc::BACKUP_MAX_ENTRIES + 4;
    const BACKUP_TEST_WORDS: usize = BACKUP_TEST_WORDS_PER_UNIT * BACKUP_TEST_ENTRY_COUNT;

    #[repr(align(4096))]
    struct BackupTestStorage([usize; BACKUP_TEST_WORDS]);

    impl BackupTestStorage {
        fn new() -> Self {
            Self([0usize; BACKUP_TEST_WORDS])
        }

        fn slot(&mut self, index: usize) -> *mut usize {
            assert!(index < BACKUP_TEST_ENTRY_COUNT);
            unsafe { self.0.as_mut_ptr().add(index * BACKUP_TEST_WORDS_PER_UNIT) }
        }
    }

    fn meta_bump_backup_list_len(meta: &MetaBumpAlloc) -> usize {
        let mut len = 0usize;
        let mut cur = meta.backup;
        while !cur.is_null() {
            len += 1;
            assert!(
                len <= MetaBumpAlloc::BACKUP_MAX_ENTRIES,
                "backup list exceeded cap"
            );
            cur = unsafe { *cur as *mut usize };
        }
        len
    }

    fn meta_bump_backup_contains(meta: &MetaBumpAlloc, needle: *mut usize) -> bool {
        let mut cur = meta.backup;
        while !cur.is_null() {
            if cur == needle {
                return true;
            }
            cur = unsafe { *cur as *mut usize };
        }
        false
    }

    #[test]
    fn meta_bump_backup_ignores_undersized_metadata_unit() {
        let mut meta = seeded_meta_bump_for_alignment_tests();
        let mut storage = BackupTestStorage::new();
        let undersized = storage.slot(0);
        let undersized_len = MetaBumpAlloc::reusable_unit_size() - core::mem::size_of::<usize>();

        meta.dealloc(undersized, undersized_len);

        assert!(meta.backup.is_null());
        assert_eq!(meta.backup_len, 0);

        let allocated = meta
            .alloc_aligned(
                MetaBumpAlloc::reusable_unit_size(),
                core::mem::align_of::<usize>(),
            )
            .expect("fresh reusable-unit allocation should come from bump");
        assert_ne!(allocated as *mut usize, undersized);
        assert_eq!(meta.backup_len, 0);
    }

    #[test]
    fn meta_bump_backup_reuses_exact_sized_metadata_unit() {
        let mut meta = seeded_meta_bump_for_alignment_tests();
        let mut storage = BackupTestStorage::new();
        let exact = storage.slot(0);

        meta.dealloc(exact, MetaBumpAlloc::reusable_unit_size());

        assert_eq!(meta.backup, exact);
        assert_eq!(meta.backup_len, 1);

        let reused = meta
            .alloc_aligned(
                MetaBumpAlloc::reusable_unit_size(),
                core::mem::align_of::<usize>(),
            )
            .expect("exact-sized backup entry should be reusable");
        assert_eq!(reused as *mut usize, exact);
        assert_eq!(meta.backup_len, 0);
    }

    #[test]
    fn meta_bump_backup_cap_holds_after_more_than_cap_deallocs() {
        let mut meta = seeded_meta_bump_for_alignment_tests();
        let mut storage = BackupTestStorage::new();

        for index in 0..BACKUP_TEST_ENTRY_COUNT {
            meta.dealloc(storage.slot(index), MetaBumpAlloc::reusable_unit_size());
        }

        assert_eq!(meta.backup_len, MetaBumpAlloc::BACKUP_MAX_ENTRIES);
        assert_eq!(
            meta_bump_backup_list_len(&meta),
            MetaBumpAlloc::BACKUP_MAX_ENTRIES
        );
    }

    #[test]
    fn meta_bump_backup_when_full_keeps_newest_and_evicts_old_tail() {
        let mut meta = seeded_meta_bump_for_alignment_tests();
        let mut storage = BackupTestStorage::new();
        let oldest = storage.slot(0);
        let newest_index = MetaBumpAlloc::BACKUP_MAX_ENTRIES;
        let newest = storage.slot(newest_index);

        for index in 0..=newest_index {
            meta.dealloc(storage.slot(index), MetaBumpAlloc::reusable_unit_size());
        }

        assert_eq!(meta.backup_len, MetaBumpAlloc::BACKUP_MAX_ENTRIES);
        assert_eq!(meta.backup, newest);
        assert!(meta_bump_backup_contains(&meta, newest));
        assert!(!meta_bump_backup_contains(&meta, oldest));
        assert_eq!(
            meta_bump_backup_list_len(&meta),
            MetaBumpAlloc::BACKUP_MAX_ENTRIES
        );

        let reused = meta
            .alloc_aligned(
                MetaBumpAlloc::reusable_unit_size(),
                core::mem::align_of::<usize>(),
            )
            .expect("newest backup entry should remain reusable");
        assert_eq!(reused as *mut usize, newest);
        assert_eq!(meta.backup_len, MetaBumpAlloc::BACKUP_MAX_ENTRIES - 1);
    }

    #[test]
    fn meta_bump_alloc_aligned_respects_layout_alignment() {
        let mut meta = seeded_meta_bump_for_alignment_tests();
        let ptr = meta
            .alloc_aligned(128, 256)
            .expect("aligned metadata bump allocation");
        assert_eq!((ptr as usize) % 256, 0);
    }

    #[test]
    fn meta_bump_alloc_aligned_uses_requested_size_without_thread_cache_rounding() {
        let mut meta = seeded_meta_bump_for_alignment_tests();
        let ptr = meta
            .alloc_aligned(128, 64)
            .expect("aligned metadata bump allocation");

        assert_eq!(ptr as usize, 0x3f80);
        assert_eq!(meta.bumper.current, 0x3f80);
    }

    #[test]
    fn meta_bump_alloc_aligned_skips_misaligned_backup_entry() {
        let mut meta = seeded_meta_bump_for_alignment_tests();
        let mut storage = [0usize; 256];
        let misaligned = unsafe { storage.as_mut_ptr().cast::<u8>().add(1) as *mut usize };
        meta.dealloc(misaligned, MetaBumpAlloc::reusable_unit_size());
        assert!(meta.backup.is_null());

        let ptr = meta
            .alloc_aligned(128, 256)
            .expect("aligned metadata bump allocation");
        assert_ne!(ptr as *mut usize, misaligned);
        assert_eq!((ptr as usize) % 256, 0);
    }
    #[test]
    fn meta_bump_alloc_aligned_finds_aligned_backup_behind_lower_aligned_head() {
        const WORDS: usize = MetaBumpAlloc::reusable_unit_size() / core::mem::size_of::<usize>();
        #[repr(align(256))]
        struct AlignedReusableUnit([usize; WORDS + 32]);

        let mut meta = seeded_meta_bump_for_alignment_tests();
        let mut storage = AlignedReusableUnit([0usize; WORDS + 32]);
        let aligned = storage.0.as_mut_ptr();
        assert_eq!((aligned as usize) % 256, 0);
        let lower_aligned_head =
            unsafe { aligned.cast::<u8>().add(core::mem::align_of::<usize>()) } as *mut usize;
        assert_eq!(
            (lower_aligned_head as usize) % core::mem::align_of::<usize>(),
            0
        );
        assert_ne!((lower_aligned_head as usize) % 256, 0);

        // Insert the 256-byte-aligned reusable unit first, then a merely
        // word-aligned unit as the head.  Older code only inspected the head
        // and would grow the metadata bump even though a valid aligned unit was
        // one link away.
        meta.dealloc(aligned, MetaBumpAlloc::reusable_unit_size());
        meta.dealloc(lower_aligned_head, MetaBumpAlloc::reusable_unit_size());
        assert_eq!(meta.backup_len, 2);

        let reused = meta
            .alloc_aligned(MetaBumpAlloc::reusable_unit_size(), 256)
            .expect("aligned backup entry behind the head should be reusable");

        assert_eq!(reused as *mut usize, aligned);
        assert_eq!(meta.backup, lower_aligned_head);
        assert_eq!(meta.backup_len, 1);
        assert_eq!(unsafe { *lower_aligned_head }, 0);
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn fixed_heap_init_plan_rejects_invalid_heap_geometry() {
        assert!(BumpAlloc::prepare_fixed_heap_init(0, PAGE_SIZE * 64, PAGE_SIZE).is_none());
        assert!(
            BumpAlloc::prepare_fixed_heap_init(PAGE_SIZE + 1, PAGE_SIZE * 65 + 1, PAGE_SIZE)
                .is_none()
        );
        assert!(
            BumpAlloc::prepare_fixed_heap_init(PAGE_SIZE, PAGE_SIZE * 65 - 1, PAGE_SIZE).is_none()
        );
        assert!(BumpAlloc::prepare_fixed_heap_init(PAGE_SIZE, PAGE_SIZE * 65, 3).is_none());
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn fixed_heap_test_initializer_sets_global_roots() {
        let _fixed_heap_guard = fixed_heap_test_guard();
        assert!(crate::cache::fixed_tcache_initialized());
        assert!(crate::zone::fixed_zone_initialized());
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn fixed_heap_try_extend_rejects_unaligned_growth_without_advancing_page_run_bump() {
        let _fixed_heap_guard = fixed_heap_test_guard();

        let before = BUMP.lock().range_for_tests();
        let extended = unsafe { META_BUMP.lock().try_extend(PAGE_SIZE / 2, PAGE_SIZE) };
        let after = BUMP.lock().range_for_tests();

        assert!(
            !extended,
            "a non-page-multiple heap growth cannot be represented in page metadata"
        );
        assert_eq!(
            after, before,
            "failed radix-tree metadata growth must not publish a larger fixed-heap page-run range"
        );
    }

    #[test]
    fn checked_size_class_geometry_rejects_zero_pages() {
        assert_eq!(checked_size_class_geometry(64, 0), None);
    }

    #[test]
    fn checked_size_class_geometry_rejects_oversized_class_without_dividing_by_zero() {
        assert_eq!(checked_size_class_geometry(PAGE_SIZE + 1, 1), None);
    }

    #[test]
    fn checked_size_class_geometry_rejects_page_span_overflow() {
        assert_eq!(
            checked_size_class_geometry(64, usize::MAX / PAGE_SIZE + 1),
            None
        );
    }

    #[test]
    fn checked_size_class_geometry_preserves_normal_layout() {
        assert_eq!(
            checked_size_class_geometry(64, 1),
            Some((PAGE_SIZE / 64, 64))
        );
    }
}
