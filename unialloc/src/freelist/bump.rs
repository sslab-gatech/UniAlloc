use crate::error::AllocError;
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sync::general_lock::{
    dynamic_initialize, lock, unlock, OsLock, STATIC_INITIALIZER,
};
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sys_alloc as system_alloc;
use core::alloc::{GlobalAlloc, Layout};
use core::result::Result::{Err, Ok};
use core::sync::atomic::{AtomicPtr, Ordering};
use spin::Mutex;

pub struct BumpAlloc {
    check_point: usize,
    current: usize,
}

impl Default for BumpAlloc {
    fn default() -> Self {
        Self::new()
    }
}

impl BumpAlloc {
    /// Minimum hosted virtual window reserved for ordinary page-run bump allocations.
    ///
    /// A one-page miss should not reserve a 2MiB or 64MiB page-run window.
    /// Start from a compact 512KiB page-aligned chunk, then grow windows
    /// geometrically up to `DEFAULT_SIZE` for sustained bursts.  Direct
    /// allocations larger than `DEFAULT_SIZE` still bypass the pooled bump
    /// window and are returned to the OS on free.
    const COMPACT_MIN_WINDOW_BYTES: usize = 512 * 1024;
    const MIN_WINDOW_SIZE: usize = ((Self::COMPACT_MIN_WINDOW_BYTES + crate::PAGE_SIZE - 1)
        / crate::PAGE_SIZE)
        * crate::PAGE_SIZE;

    /// Maximum hosted virtual window reserved for ordinary page-run bump allocations.
    const DEFAULT_SIZE: usize = 64 * 1024 * 1024;

    pub const fn new() -> Self {
        Self {
            check_point: 0,
            current: 0,
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn window_size_for_alloc(alloc_size: usize) -> Result<usize, AllocError> {
        if alloc_size > Self::DEFAULT_SIZE {
            return Err(AllocError::ESIZE);
        }

        let min_window = core::cmp::max(Self::MIN_WINDOW_SIZE, crate::PAGE_SIZE);
        let requested = core::cmp::max(alloc_size, min_window);
        let geometric = requested
            .checked_next_power_of_two()
            .ok_or(AllocError::ESIZE)?;
        let capped = core::cmp::min(geometric, Self::DEFAULT_SIZE);
        capped
            .checked_add(crate::PAGE_SIZE - 1)
            .and_then(|size| size.checked_div(crate::PAGE_SIZE))
            .and_then(|pages| pages.checked_mul(crate::PAGE_SIZE))
            .ok_or(AllocError::ESIZE)
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn init(&mut self, window_size: usize) -> Result<(), AllocError> {
        // Clean up the still-unallocated tail of the previous bump window.
        // Allocated blocks are either live or are returned through FreeList::free.
        if self.check_point != self.current {
            let size = self
                .current
                .checked_sub(self.check_point)
                .ok_or(AllocError::EFATAL)?;
            if size > 0 {
                unsafe { system_alloc::munmap(self.check_point as *mut u8, size) };
            }
        }

        let prot = system_alloc::prots::get_prot(true, true, false);
        let start = unsafe { system_alloc::mmap(window_size, prot) as *mut u8 };
        if start.is_null() || start as usize == usize::MAX {
            self.check_point = 0;
            self.current = 0;
            return Err(AllocError::ENOMEM);
        }

        let end = (start as usize)
            .checked_add(window_size)
            .ok_or(AllocError::EFATAL)?;
        self.check_point = start as usize;
        self.current = end;
        Ok(())
    }

    #[cfg(feature = "fixed_heap")]
    fn init(&mut self, _window_size: usize) -> Result<(), AllocError> {
        Err(AllocError::ENOMEM)
    }

    pub fn init_with_range(&mut self, start: usize, end: usize) {
        self.check_point = end as usize;
        self.current = start as usize;
    }

    pub fn checked_extend_end(&self, size: usize) -> Result<usize, AllocError> {
        self.check_point.checked_add(size).ok_or(AllocError::EFATAL)
    }

    fn checked_page_rounded_size(size: usize) -> Result<usize, AllocError> {
        size.checked_add(crate::PAGE_SIZE - 1)
            .ok_or(AllocError::ESIZE)?
            .checked_div(crate::PAGE_SIZE)
            .and_then(|pages| pages.checked_mul(crate::PAGE_SIZE))
            .ok_or(AllocError::ESIZE)
    }

    pub fn extend_with_range(&mut self, size: usize) -> Result<usize, AllocError> {
        let end = self.checked_extend_end(size)?;
        self.check_point = end;
        Ok(end)
    }

    #[cfg(feature = "fixed_heap")]
    pub fn alloc_with_extended_checkpoint(
        &mut self,
        size: usize,
        new_checkpoint: usize,
    ) -> Result<*mut u8, AllocError> {
        if new_checkpoint < self.check_point {
            return Err(AllocError::EFATAL);
        }
        let alloc_size = Self::checked_page_rounded_size(size)?;
        let new_current = self
            .current
            .checked_add(alloc_size)
            .ok_or(AllocError::EFATAL)?;
        if new_current > new_checkpoint {
            return Err(AllocError::ENOMEM);
        }

        let ptr = self.current;
        self.current = new_current;
        self.check_point = new_checkpoint;
        Ok(ptr as *mut u8)
    }

    #[cfg(test)]
    pub(crate) fn range_for_tests(&self) -> (usize, usize) {
        (self.current, self.check_point)
    }

    pub fn alloc(&mut self, size: usize) -> Result<*mut u8, AllocError> {
        let alloc_size = Self::checked_page_rounded_size(size)?;
        #[cfg(not(feature = "fixed_heap"))]
        if alloc_size > Self::DEFAULT_SIZE {
            let prot = system_alloc::prots::get_prot(true, true, false);
            let start = unsafe { system_alloc::mmap(alloc_size, prot) as *mut u8 };
            if start.is_null() || start as usize == usize::MAX {
                return Err(AllocError::ENOMEM);
            }
            return Ok(start);
        }
        let current_ptr = self.current as usize;
        #[cfg(not(feature = "fixed_heap"))]
        if self
            .check_point
            .checked_add(alloc_size)
            .ok_or(AllocError::EFATAL)?
            > current_ptr
        {
            self.init(Self::window_size_for_alloc(alloc_size)?)?;
        }
        #[cfg(feature = "fixed_heap")]
        if self
            .check_point
            .checked_sub(alloc_size)
            .ok_or(AllocError::EFATAL)?
            < current_ptr
        {
            return Err(AllocError::ENOMEM);
        }
        let ptr = self.current as usize;
        #[cfg(not(feature = "fixed_heap"))]
        if let Some(new_ptr) = ptr.checked_sub(alloc_size) {
            // Round down to the requested alignment.
            // let new_ptr = new_ptr & !(align - 1);
            self.current = new_ptr;
            Ok(new_ptr as *mut u8)
        } else {
            Err(AllocError::EFATAL)
        }
        #[cfg(feature = "fixed_heap")]
        if let Some(new_ptr) = ptr.checked_add(alloc_size) {
            // Round down to the requested alignment.
            let ans = self.current;
            self.current = new_ptr;
            Ok(ans as *mut u8)
        } else {
            Err(AllocError::EFATAL)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(not(feature = "fixed_heap"))]
    struct MappingGuard {
        start: usize,
        size: usize,
    }

    #[cfg(not(feature = "fixed_heap"))]
    impl MappingGuard {
        fn new(start: usize, size: usize) -> Self {
            Self { start, size }
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    impl Drop for MappingGuard {
        fn drop(&mut self) {
            if self.start != 0 && self.size != 0 {
                unsafe {
                    system_alloc::munmap(self.start as *mut u8, self.size);
                }
            }
        }
    }

    #[test]
    fn default_bump_window_is_footprint_bounded_and_page_aligned() {
        assert_eq!(BumpAlloc::DEFAULT_SIZE % crate::PAGE_SIZE, 0);
        assert_eq!(BumpAlloc::MIN_WINDOW_SIZE % crate::PAGE_SIZE, 0);
        assert!(BumpAlloc::DEFAULT_SIZE >= 2 * 1024 * 1024);
        assert!(BumpAlloc::DEFAULT_SIZE <= 64 * 1024 * 1024);
        assert!(BumpAlloc::MIN_WINDOW_SIZE <= BumpAlloc::DEFAULT_SIZE);
        assert!(BumpAlloc::MIN_WINDOW_SIZE >= BumpAlloc::COMPACT_MIN_WINDOW_BYTES);
        if crate::PAGE_SIZE <= BumpAlloc::COMPACT_MIN_WINDOW_BYTES {
            assert_eq!(BumpAlloc::MIN_WINDOW_SIZE, 512 * 1024);
            assert!(BumpAlloc::MIN_WINDOW_SIZE < 2 * 1024 * 1024);
        } else {
            assert_eq!(BumpAlloc::MIN_WINDOW_SIZE, crate::PAGE_SIZE);
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hosted_bump_window_scales_with_requested_page_run() {
        assert_eq!(
            BumpAlloc::window_size_for_alloc(crate::PAGE_SIZE).expect("one-page window"),
            BumpAlloc::MIN_WINDOW_SIZE
        );
        assert_eq!(
            BumpAlloc::window_size_for_alloc(BumpAlloc::MIN_WINDOW_SIZE - crate::PAGE_SIZE)
                .expect("below-minimum window"),
            BumpAlloc::MIN_WINDOW_SIZE
        );
        assert_eq!(
            BumpAlloc::window_size_for_alloc(BumpAlloc::MIN_WINDOW_SIZE + crate::PAGE_SIZE)
                .expect("geometric growth after compact minimum"),
            BumpAlloc::MIN_WINDOW_SIZE * 2
        );
        assert_eq!(
            BumpAlloc::window_size_for_alloc(3 * 1024 * 1024).expect("3MiB window"),
            4 * 1024 * 1024
        );
        assert_eq!(
            BumpAlloc::window_size_for_alloc(BumpAlloc::DEFAULT_SIZE).expect("max pooled window"),
            BumpAlloc::DEFAULT_SIZE
        );
        assert!(
            BumpAlloc::window_size_for_alloc(BumpAlloc::DEFAULT_SIZE + crate::PAGE_SIZE).is_err(),
            "larger requests should use the direct mmap path instead of a pooled window"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn fresh_bump_window_reserves_only_adaptive_window_size() {
        let mut bump = BumpAlloc::new();
        let alloc_size = crate::PAGE_SIZE;
        let expected_window =
            BumpAlloc::window_size_for_alloc(alloc_size).expect("adaptive bump window");
        let ptr = bump
            .alloc(alloc_size)
            .expect("fresh page-run bump allocation");
        let _guard = MappingGuard::new(bump.check_point, expected_window);

        assert!(!ptr.is_null());
        assert_ne!(bump.check_point, 0);
        assert_eq!(
            bump.current
                .checked_sub(bump.check_point)
                .expect("current should stay inside fresh window"),
            expected_window - alloc_size
        );
        assert_eq!(
            ptr as usize, bump.current,
            "hosted downward bump allocation must return the reserved page-run start, not the previous one-past-end cursor"
        );
        let ptr_end = (ptr as usize)
            .checked_add(alloc_size)
            .expect("returned page run end should not overflow");
        assert!(
            (ptr as usize) >= bump.check_point && ptr_end <= bump.check_point + expected_window,
            "returned page run must lie inside the mmap window"
        );
    }

    #[cfg(all(
        not(feature = "fixed_heap"),
        any(target_os = "linux", target_os = "macos")
    ))]
    #[test]
    fn fresh_bump_window_maps_expected_compact_window_in_os() {
        #[cfg(target_os = "linux")]
        unsafe fn os_page_is_mapped(page_addr: usize) -> bool {
            debug_assert_eq!(page_addr % crate::PAGE_SIZE, 0);
            let mut residency = 0u8;
            libc::mincore(
                page_addr as *mut libc::c_void,
                crate::PAGE_SIZE,
                &mut residency as *mut u8,
            ) == 0
        }

        #[cfg(target_os = "macos")]
        unsafe fn os_page_is_mapped(page_addr: usize) -> bool {
            debug_assert_eq!(page_addr % crate::PAGE_SIZE, 0);
            let mut residency = 0 as libc::c_char;
            libc::mincore(
                page_addr as *mut libc::c_void,
                crate::PAGE_SIZE as libc::vm_size_t,
                &mut residency as *mut libc::c_char,
            ) == 0
        }

        let mut bump = BumpAlloc::new();
        let alloc_size = crate::PAGE_SIZE;
        let expected_window =
            BumpAlloc::window_size_for_alloc(alloc_size).expect("adaptive bump window");
        if expected_window >= BumpAlloc::DEFAULT_SIZE {
            return;
        }

        let ptr = bump
            .alloc(alloc_size)
            .expect("fresh page-run bump allocation");
        assert!(!ptr.is_null());
        let _guard = MappingGuard::new(bump.check_point, expected_window);

        let compact_end = bump
            .check_point
            .checked_add(expected_window)
            .expect("compact window end");
        let compact_last_page = compact_end
            .checked_sub(crate::PAGE_SIZE)
            .expect("compact window contains at least one page");
        assert_eq!(
            bump.current,
            compact_end - alloc_size,
            "hosted bump state must account for the compact mmap window, not the historical fixed 64MiB window"
        );
        assert_eq!(
            ptr as usize, bump.current,
            "downward bump allocation should return the low address of the allocated page run"
        );
        assert!(
            unsafe { os_page_is_mapped(bump.check_point) },
            "first page of the compact bump window must be mapped"
        );
        assert!(
            unsafe { os_page_is_mapped(compact_last_page) },
            "last page of the compact bump window must be mapped"
        );
    }

    #[cfg(all(not(feature = "fixed_heap"), target_os = "linux"))]
    #[test]
    fn fresh_bump_window_does_not_reserve_old_64m_tail_in_os() {
        unsafe fn os_can_map_exact_page(page_addr: usize) -> bool {
            debug_assert_eq!(page_addr % crate::PAGE_SIZE, 0);
            let ptr = libc::mmap(
                page_addr as *mut libc::c_void,
                crate::PAGE_SIZE,
                libc::PROT_NONE,
                libc::MAP_PRIVATE | libc::MAP_ANONYMOUS | libc::MAP_FIXED_NOREPLACE,
                -1,
                0,
            );
            if ptr == libc::MAP_FAILED {
                return false;
            }

            let mapped_exactly = ptr as usize == page_addr;
            let _ = libc::munmap(ptr, crate::PAGE_SIZE);
            mapped_exactly
        }

        let mut bump = BumpAlloc::new();
        let alloc_size = crate::PAGE_SIZE;
        let expected_window =
            BumpAlloc::window_size_for_alloc(alloc_size).expect("adaptive bump window");
        if expected_window >= BumpAlloc::DEFAULT_SIZE {
            return;
        }

        let ptr = bump
            .alloc(alloc_size)
            .expect("fresh page-run bump allocation");
        assert!(!ptr.is_null());
        let _guard = MappingGuard::new(bump.check_point, expected_window);

        let compact_end = bump
            .check_point
            .checked_add(expected_window)
            .expect("compact window end");
        let generic_end = bump
            .check_point
            .checked_add(BumpAlloc::DEFAULT_SIZE)
            .expect("old generic window end");
        let tail_pages = (BumpAlloc::DEFAULT_SIZE - expected_window).checked_div(crate::PAGE_SIZE);
        let last_tail_page = tail_pages
            .and_then(|pages| pages.checked_sub(1))
            .expect("old generic tail should contain pages");
        assert!(
            !unsafe { os_can_map_exact_page(bump.check_point) },
            "Linux MAP_FIXED_NOREPLACE should reject an address inside the live compact bump window"
        );

        let mut exact_mappable_tail_probe = None;
        for page_offset in [0usize, 1, 16, 256, 1024, last_tail_page] {
            let probe = compact_end
                .checked_add(
                    page_offset
                        .checked_mul(crate::PAGE_SIZE)
                        .expect("probe offset"),
                )
                .expect("probe address");
            if probe < generic_end && unsafe { os_can_map_exact_page(probe) } {
                exact_mappable_tail_probe = Some(probe);
                break;
            }
        }
        assert!(
            exact_mappable_tail_probe.is_some(),
            "no old 64MiB bump-window tail page was exact-mappable; a stale fixed-size mapping would keep [{:#x}, {:#x}) reserved",
            compact_end,
            generic_end
        );
    }

    #[test]
    fn extend_with_range_rejects_checkpoint_overflow() {
        let mut bump = BumpAlloc {
            check_point: usize::MAX - 7,
            current: 0,
        };
        assert!(bump.extend_with_range(8).is_err());
        assert_eq!(bump.check_point, usize::MAX - 7);
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn fixed_heap_alloc_with_extended_checkpoint_is_transactional_on_failure() {
        let mut bump = BumpAlloc {
            check_point: crate::PAGE_SIZE * 4,
            current: crate::PAGE_SIZE * 3,
        };
        let before = bump.range_for_tests();

        assert!(
            bump.alloc_with_extended_checkpoint(crate::PAGE_SIZE * 3, crate::PAGE_SIZE * 5)
                .is_err(),
            "allocation larger than the candidate range must fail"
        );

        assert_eq!(
            bump.range_for_tests(),
            before,
            "failed transactional allocation must not publish a larger checkpoint"
        );
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn fixed_heap_alloc_with_extended_checkpoint_commits_pointer_and_checkpoint_together() {
        let mut bump = BumpAlloc {
            check_point: crate::PAGE_SIZE * 4,
            current: crate::PAGE_SIZE * 3,
        };
        let ptr = bump
            .alloc_with_extended_checkpoint(crate::PAGE_SIZE, crate::PAGE_SIZE * 5)
            .expect("candidate range has room for one page");

        assert_eq!(ptr as usize, crate::PAGE_SIZE * 3);
        assert_eq!(
            bump.range_for_tests(),
            (crate::PAGE_SIZE * 4, crate::PAGE_SIZE * 5)
        );
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn fixed_heap_alloc_rejects_checkpoint_underflow() {
        let mut bump = BumpAlloc {
            check_point: 16,
            current: 0,
        };
        assert!(bump.alloc(crate::PAGE_SIZE).is_err());
        assert_eq!(bump.current, 0);
        assert_eq!(bump.check_point, 16);
    }
}
