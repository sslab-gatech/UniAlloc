mod efficient_page;
mod separate_page;

use crate::error::AllocError;
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sys_alloc as system_alloc;
use crate::prelude::GlobalBackend;
use crate::PAGE_SIZE;
use core::alloc::{GlobalAlloc, Layout};
use core::ptr::null_mut;
use core::result::Result::Err;
use core::sync::atomic::{AtomicPtr, AtomicUsize, Ordering};
pub use efficient_page::ObjectPage as EfObjectPage;
pub(crate) use efficient_page::ObjectPageAllocationState as EfObjectPageAllocationState;
pub(crate) use efficient_page::ObjectPageDeallocation as EfObjectPageDeallocation;
pub(crate) use efficient_page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT;
pub use separate_page::ObjectPage;
pub(crate) use separate_page::ObjectPageAllocationState;
use spin::Mutex;

#[inline]
pub(crate) fn checked_object_page_span_bytes(pg_num: usize) -> Result<usize, AllocError> {
    if pg_num == 0 {
        return Err(AllocError::ESIZE);
    }
    PAGE_SIZE.checked_mul(pg_num).ok_or(AllocError::ESIZE)
}

#[inline]
pub(crate) fn try_object_page_layout(pg_num: usize) -> Result<Layout, AllocError> {
    let span = checked_object_page_span_bytes(pg_num)?;
    Layout::from_size_align(span, PAGE_SIZE).map_err(|_| AllocError::ELAYOUT)
}

#[inline]
fn checked_page_bump_range_end(start: usize, span: usize) -> Result<usize, AllocError> {
    start.checked_add(span).ok_or(AllocError::ENOMEM)
}

pub struct PageBumpAlloc {
    start: usize,
    current: usize,
}

impl Default for PageBumpAlloc {
    fn default() -> Self {
        Self::new()
    }
}

impl PageBumpAlloc {
    #[cfg(feature = "hugepage")]
    const DEFAULT_SIZE: usize = 2 * 1024 * 1024;
    #[cfg(not(feature = "hugepage"))]
    const DEFAULT_SIZE: usize = page_bump_default_chunk_size();
    /// Number of object-page descriptors to reserve per metadata bump chunk.
    ///
    /// Older code multiplied descriptor size by `PAGE_SIZE`, accidentally using
    /// the OS page size as an object count.  That over-reserved metadata by
    /// hundreds of KiB on 16KiB-page platforms before the application had many
    /// object pages.  A 256-descriptor chunk keeps the allocator's first-touch
    /// metadata footprint small while still amortizing mmap/sys-alloc overhead.
    const DEFAULT_OBJECT_COUNT: usize = 256;
    pub const fn new() -> Self {
        Self {
            start: 0,
            current: 0,
        }
    }

    pub fn init_with_range(&mut self, start: usize, end: usize) {
        self.current = end;
        self.start = start;
    }

    pub fn try_extend(&mut self, start: usize, end: usize, page_size: usize) -> Result<(), ()> {
        if page_size == 0
            || !page_size.is_power_of_two()
            || end < start
            || start & (page_size - 1) != 0
            || end & (page_size - 1) != 0
        {
            return Err(());
        }
        if end == self.start {
            self.start = start;
            return Ok(());
        }
        Err(())
    }

    #[inline]
    fn needs_new_chunk(&self, requested_size: usize) -> bool {
        if self.current <= self.start {
            return true;
        }

        let remaining = self.current - self.start;
        #[cfg(not(feature = "fixed_heap"))]
        {
            // Non-fixed-heap ranges are always created with DEFAULT_SIZE.  A
            // larger live range means the bump state is not one of our own
            // chunks, so discard it instead of underflowing later.
            if remaining > Self::DEFAULT_SIZE {
                return true;
            }
        }

        // Fixed-heap initialization can hand PG_BUMP an exact metadata range
        // for the target heap.  That pre-reserved range may legitimately be
        // larger than DEFAULT_SIZE, so use it until exhausted instead of
        // throwing it away and consuming object memory for another metadata
        // chunk.
        remaining < requested_size
    }

    pub fn alloc(&mut self, size: usize) -> Result<*mut u8, AllocError> {
        if size != core::mem::size_of::<EfObjectPage>() {
            return Err(AllocError::ESIZE);
        }
        if self.needs_new_chunk(size) {
            #[cfg(not(feature = "fixed_heap"))]
            {
                let prot = system_alloc::prots::get_prot(true, true, false);
                let start = unsafe {
                    #[cfg(feature = "hugepage")]
                    let mut ptr = system_alloc::mmap_huge(Self::DEFAULT_SIZE, prot) as *mut u8;
                    #[cfg(not(feature = "hugepage"))]
                    let mut ptr = system_alloc::mmap(Self::DEFAULT_SIZE, prot) as *mut u8;
                    ptr = system_alloc::normalize_mmap_result(ptr);
                    ptr
                };
                if start.is_null() {
                    return Err(AllocError::ENOMEM);
                }
                let start = start as usize;
                let end = checked_page_bump_range_end(start, Self::DEFAULT_SIZE)?;
                self.current = end;
                self.start = start;
            }
            #[cfg(feature = "fixed_heap")]
            {
                if !crate::sc::fixed_heap_roots_initialized() {
                    return Err(AllocError::ENOMEM);
                }
                let layout = Layout::from_size_align(Self::DEFAULT_SIZE, 1)
                    .map_err(|_| AllocError::ENOMEM)?;
                let start = unsafe { GlobalBackend.alloc(layout) } as usize;
                if start == 0 {
                    return Err(AllocError::ENOMEM);
                }
                let end = checked_page_bump_range_end(start, Self::DEFAULT_SIZE)?;
                self.current = end;
                self.start = start;
            }
        }
        let new_cur = self
            .current
            .checked_sub(size)
            .filter(|new_cur| *new_cur >= self.start)
            .ok_or(AllocError::ENOMEM)?;
        self.current = new_cur;
        Ok(new_cur as *mut u8)
    }
}

#[inline]
const fn page_bump_default_chunk_size() -> usize {
    let descriptor_bytes =
        core::mem::size_of::<EfObjectPage>() * PageBumpAlloc::DEFAULT_OBJECT_COUNT;
    let minimum_bytes = if descriptor_bytes < PAGE_SIZE {
        PAGE_SIZE
    } else {
        descriptor_bytes
    };
    round_up_to_page_bytes(minimum_bytes)
}

#[inline]
const fn round_up_to_page_bytes(bytes: usize) -> usize {
    if bytes == 0 {
        0
    } else {
        ((bytes - 1) / PAGE_SIZE + 1) * PAGE_SIZE
    }
}

/// Global object-page metadata bump allocator.
///
/// All mutation goes through the `spin::Mutex`; using an immutable static keeps
/// the global root compatible with Rust's stricter `static_mut_refs` checking.
pub static PG_BUMP: Mutex<PageBumpAlloc> = Mutex::new(PageBumpAlloc::new());

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn object_page_layout_rejects_zero_pages() {
        let err = try_object_page_layout(0).expect_err("zero pages must fail closed");
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
    }

    #[test]
    fn object_page_layout_rejects_span_overflow() {
        let overflowing_pages = usize::MAX / PAGE_SIZE + 1;
        let err = try_object_page_layout(overflowing_pages)
            .expect_err("overflowing page span must fail closed");
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
    }

    #[test]
    fn object_page_layout_requests_page_alignment() {
        let layout = try_object_page_layout(2).expect("valid two-page object page layout");
        assert_eq!(layout.size(), PAGE_SIZE * 2);
        assert_eq!(layout.align(), PAGE_SIZE);
    }

    #[test]
    fn page_bump_range_end_rejects_overflow() {
        let err = checked_page_bump_range_end(usize::MAX - 1, PAGE_SIZE)
            .expect_err("metadata page-bump range overflow must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::ENOMEM.to_raw_errno());
    }

    #[cfg(not(feature = "hugepage"))]
    #[test]
    fn page_bump_default_chunk_is_page_aligned_and_footprint_bounded() {
        assert_eq!(PageBumpAlloc::DEFAULT_SIZE % PAGE_SIZE, 0);
        assert!(PageBumpAlloc::DEFAULT_SIZE >= PAGE_SIZE);
        assert!(
            PageBumpAlloc::DEFAULT_SIZE >= core::mem::size_of::<EfObjectPage>() * 256,
            "metadata chunk must hold the intended warm descriptor set"
        );
        assert!(
            PageBumpAlloc::DEFAULT_SIZE <= core::cmp::max(PAGE_SIZE, 16 * 1024),
            "default metadata chunk should stay small on first touch"
        );
    }

    #[test]
    fn page_bump_non_empty_range_is_reusable_without_default_size_assumption() {
        let mut bump = PageBumpAlloc {
            start: 0x1000,
            current: 0x1000 + core::mem::size_of::<EfObjectPage>(),
        };

        let ptr = bump
            .alloc(core::mem::size_of::<EfObjectPage>())
            .expect("existing descriptor range should be consumed before a new chunk");

        assert_eq!(ptr as usize, 0x1000);
        assert_eq!(bump.current, bump.start);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn page_bump_alloc_renews_chunk_when_tail_fragment_is_too_small() {
        let descriptor_size = core::mem::size_of::<EfObjectPage>();
        let old_start = PAGE_SIZE * 4;
        let mut bump = PageBumpAlloc {
            start: old_start,
            current: old_start + descriptor_size - 1,
        };

        let ptr = bump
            .alloc(descriptor_size)
            .expect("tail fragment smaller than a descriptor should allocate a fresh chunk");

        assert!(!ptr.is_null());
        assert_ne!(
            bump.start, old_start,
            "allocator must not keep an unusable tail fragment as the active chunk"
        );
        assert_eq!(
            bump.current - bump.start,
            PageBumpAlloc::DEFAULT_SIZE - descriptor_size
        );

        unsafe { system_alloc::munmap(bump.start as *mut u8, PageBumpAlloc::DEFAULT_SIZE) };
    }

    #[test]
    fn page_bump_alloc_exhausted_range_does_not_underflow() {
        let mut bump = PageBumpAlloc {
            start: 0x1000,
            current: 0x1000,
        };
        let result = bump.alloc(core::mem::size_of::<EfObjectPage>());
        if result.is_ok() {
            assert!(bump.current >= bump.start);
        } else {
            assert_eq!(bump.start, 0x1000);
            assert_eq!(bump.current, 0x1000);
        }
    }

    #[test]
    fn page_bump_alloc_rejects_wrong_size_without_panic() {
        let mut bump = PageBumpAlloc {
            start: 0x1000,
            current: 0x2000,
        };
        let err = bump
            .alloc(core::mem::size_of::<EfObjectPage>() - 1)
            .expect_err("wrong metadata object size must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(bump.start, 0x1000);
        assert_eq!(bump.current, 0x2000);
    }

    #[test]
    fn page_bump_try_extend_rejects_invalid_page_size_without_mutation() {
        let start = PAGE_SIZE * 4;
        let current = start + PAGE_SIZE;
        let mut bump = PageBumpAlloc { start, current };

        assert!(bump.try_extend(start - PAGE_SIZE, start, 0).is_err());
        assert!(bump.try_extend(start - PAGE_SIZE, start, 3).is_err());
        assert!(bump.try_extend(current, start, PAGE_SIZE).is_err());
        assert_eq!(bump.start, start);
        assert_eq!(bump.current, current);
    }

    #[test]
    fn page_bump_try_extend_rejects_unaligned_range_without_claiming_extra_memory() {
        let start = PAGE_SIZE * 4;
        let current = start + PAGE_SIZE;
        let mut bump = PageBumpAlloc { start, current };

        assert!(bump
            .try_extend(start - PAGE_SIZE + 1, start, PAGE_SIZE)
            .is_err());
        assert!(bump
            .try_extend(start - PAGE_SIZE, start + 1, PAGE_SIZE)
            .is_err());
        assert_eq!(bump.start, start);
        assert_eq!(bump.current, current);
    }

    #[test]
    fn page_bump_try_extend_accepts_exact_contiguous_aligned_range() {
        let start = PAGE_SIZE * 4;
        let current = start + PAGE_SIZE;
        let mut bump = PageBumpAlloc { start, current };
        let extension_start = start - PAGE_SIZE;

        bump.try_extend(extension_start, start, PAGE_SIZE)
            .expect("contiguous aligned page range should extend");

        assert_eq!(bump.start, extension_start);
        assert_eq!(bump.current, current);
    }
}
