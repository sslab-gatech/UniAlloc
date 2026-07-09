use crate::error::{AllocError, Result};
use crate::prelude::*;
use crate::sc::SCAllocator;
use crate::sc::META_BUMP;
#[cfg(not(feature = "fixed_heap"))]
use crate::sync::PthreadMutex as Mutex;
use core::alloc::{GlobalAlloc, Layout};
use core::mem::MaybeUninit;
use core::ops::{Deref, DerefMut};
use core::ptr::{null_mut, NonNull};
use core::sync::atomic::{AtomicBool, AtomicPtr, AtomicU64, AtomicUsize, Ordering};
#[cfg(feature = "fixed_heap")]
use spin::Mutex;

/// An allocator holding a bunch of slabs
///
/// It dispatches the allocation request to different slab
/// according to the index of size class
pub struct ZoneAllocator {
    slabs: [Mutex<SCAllocator>; TOTAL_SIZE_CLASS],
    /// Size classes that may currently retain at least one empty slab.
    ///
    /// Retained-empty trimming used to try-lock every size class to discover
    /// candidates.  One bit per class keeps the global trim path focused on
    /// classes that actually reported retained empty slabs; stale set bits are
    /// harmless and are cleared on the next successful class lock.  Class zero
    /// is ignored because it does not represent a normal retained slab class.
    retained_empty_class_bits: AtomicU64,
    retained_empty_trim_active: AtomicBool,
    retained_empty_trim_pending: AtomicBool,
    #[cfg(test)]
    test_pause_retained_empty_trim_after_scan: AtomicBool,
    #[cfg(test)]
    test_retained_empty_trim_scan_paused: AtomicBool,
    #[cfg(test)]
    test_retained_empty_trim_budget: AtomicUsize,
    #[cfg(test)]
    test_retained_empty_trim_invocations: AtomicUsize,
    #[cfg(test)]
    test_retained_empty_trim_forced_recycle_errors: AtomicUsize,
}

pub(crate) const GLOBAL_RETAINED_EMPTY_SLAB_OS_PAGE_BUDGET: usize = 32;
const RETAINED_EMPTY_CLASS_BITS: usize = u64::BITS as usize;

// The retained-empty hint map is intentionally one machine word: it keeps the
// global trim path from probing every size-class lock.  If the size-class table
// grows beyond the bitmap, do not silently leave high classes untracked and let
// retained empty slabs accumulate; fail the build until the bitmap shape is
// widened with matching tests.
const _: [(); 1] = [(); (TOTAL_SIZE_CLASS <= RETAINED_EMPTY_CLASS_BITS) as usize];

#[cfg(feature = "stats")]
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ZoneRetainedEmptySlabSnapshot {
    /// Whether the process-global zone already exists.
    ///
    /// The public snapshot function does not initialize the allocator merely to
    /// answer diagnostics; `false` therefore means the allocator has no retained
    /// zone slabs yet, not an allocation failure.
    pub initialized: bool,
    /// Size classes that the zone currently tracks as possibly retaining empty slabs.
    pub tracked_class_bits: u64,
    /// Number of set bits in `tracked_class_bits`.
    pub tracked_class_count: usize,
    /// Retained empty OS pages observed in size classes whose slab locks were free.
    ///
    /// This is exact when `busy_class_count == 0`.  If some classes are busy,
    /// the value is a lower bound; callers can retry after the allocator is less
    /// contended if they need an exact diagnostic sample.
    pub observed_retained_os_pages: usize,
    /// Number of size classes skipped because their slab lock was busy.
    pub busy_class_count: usize,
    /// Current global retained-empty budget in OS pages.
    pub budget_os_pages: usize,
    /// A trim pass currently owns the zone-level trim guard.
    pub trim_active: bool,
    /// A trim request was deferred because a prior pass could not prove convergence.
    pub trim_pending: bool,
}

impl ZoneAllocator {
    pub fn new() -> Self {
        let mut slabs: [MaybeUninit<Mutex<SCAllocator>>; TOTAL_SIZE_CLASS] =
            unsafe { MaybeUninit::uninit().assume_init() };
        for (idx, item) in slabs.iter_mut().enumerate() {
            item.write(Mutex::new(SCAllocator::new(
                get_rounded_size_by_idx(idx),
                get_num_pages_by_idx(idx),
            )));
        }
        Self {
            slabs: unsafe {
                (&slabs as *const _ as *const [Mutex<SCAllocator>; TOTAL_SIZE_CLASS]).read()
            },
            retained_empty_class_bits: AtomicU64::new(0),
            retained_empty_trim_active: AtomicBool::new(false),
            retained_empty_trim_pending: AtomicBool::new(false),
            #[cfg(test)]
            test_pause_retained_empty_trim_after_scan: AtomicBool::new(false),
            #[cfg(test)]
            test_retained_empty_trim_scan_paused: AtomicBool::new(false),
            #[cfg(test)]
            test_retained_empty_trim_budget: AtomicUsize::new(
                GLOBAL_RETAINED_EMPTY_SLAB_OS_PAGE_BUDGET,
            ),
            #[cfg(test)]
            test_retained_empty_trim_invocations: AtomicUsize::new(0),
            #[cfg(test)]
            test_retained_empty_trim_forced_recycle_errors: AtomicUsize::new(0),
        }
    }

    /// Allocates a batch of chunks from a specific slab described by `idx`
    pub fn allocate_batch_from_slab(
        &self,
        idx: usize,
        align: usize,
    ) -> Result<(*mut u8, usize, Option<usize>)> {
        if idx >= self.slabs.len() {
            return Err(AllocError::ESIZE);
        }
        let sc: &Mutex<SCAllocator> = &self.slabs[idx];
        let (result, retained_after) = {
            let mut sc = sc.lock();
            let result = sc.allocate_batch_v2(align);
            (result, sc.retained_empty_slab_os_pages())
        };
        self.note_retained_empty_class_state(idx, retained_after);
        if self.retained_empty_trim_pending.load(Ordering::Acquire) {
            // Allocation can also be the next safe point after a previous trim
            // was deferred by a busy class or transient recycle error.  The
            // class lock is already released here, so retrying the nonblocking
            // global trim cannot deadlock with the allocation slow path.  In
            // the common case this branch is cold because no trim is pending.
            self.trim_retained_empty_slabs_to_budget();
        }
        result
    }

    pub fn deallocate_batch_to_slab(&self, idx: usize, ptr: *mut u8) -> Result<()> {
        if idx >= self.slabs.len() {
            return Err(AllocError::ESIZE);
        }
        let sc: &Mutex<SCAllocator> = &self.slabs[idx];
        let should_trim = {
            let mut sc = sc.lock();
            let retained_before = sc.retained_empty_slab_os_pages();
            sc.deallocate_batch(ptr as *mut usize)?;
            let retained_after = sc.retained_empty_slab_os_pages();
            self.note_retained_empty_class_state(idx, retained_after);
            // A global retained-empty scan touches every size class with a
            // try-lock.  Most deallocations only return an object to a partial
            // page and cannot change the global retained-empty RSS budget, so
            // avoid that cross-class work unless this batch actually retained
            // a newly empty slab.  Pending requests are still honored so an
            // earlier over-budget/busy trim converges on the next safe trigger.
            retained_after > retained_before
                || self.retained_empty_trim_pending.load(Ordering::Acquire)
        };
        if should_trim {
            self.trim_retained_empty_slabs_to_budget();
        }
        Ok(())
    }

    #[inline]
    pub fn contains_slab_index(&self, idx: usize) -> bool {
        idx < self.slabs.len()
    }

    #[cfg(feature = "stats")]
    pub fn retained_empty_slab_snapshot(&self) -> ZoneRetainedEmptySlabSnapshot {
        let tracked_class_bits = self.retained_empty_class_bits.load(Ordering::Acquire);
        let mut observed_retained_os_pages = 0usize;
        let mut busy_class_count = 0usize;

        // Snapshot sampling is diagnostic only: use try-locks so a stats read
        // never waits behind allocator work.  Follow the same retained-empty
        // bitset that drives trimming instead of probing every size class:
        // unrelated busy classes should not make the retained-empty diagnostic
        // look inexact, and stats reads should not add cross-class lock
        // pressure when no class has reported retained empty slabs.
        let mut bits = tracked_class_bits;
        while bits != 0 {
            let idx = bits.trailing_zeros() as usize;
            bits &= bits - 1;
            if idx == 0 || idx >= self.slabs.len() {
                continue;
            }

            match self.slabs[idx].try_lock() {
                Some(sc) => {
                    observed_retained_os_pages = observed_retained_os_pages
                        .saturating_add(sc.retained_empty_slab_os_pages());
                }
                None => {
                    busy_class_count = busy_class_count.saturating_add(1);
                }
            }
        }

        ZoneRetainedEmptySlabSnapshot {
            initialized: true,
            tracked_class_bits,
            tracked_class_count: tracked_class_bits.count_ones() as usize,
            observed_retained_os_pages,
            busy_class_count,
            budget_os_pages: self.retained_empty_trim_budget(),
            trim_active: self.retained_empty_trim_active.load(Ordering::Acquire),
            trim_pending: self.retained_empty_trim_pending.load(Ordering::Acquire),
        }
    }

    /// Try to return a batch to the size-class slab without waiting for the
    /// slab lock.
    ///
    /// Thread caches use this on their soft-retention flush path: if another
    /// thread is already mutating the same size class, the caller can keep its
    /// local hot list and retry later instead of spinning or blocking in the
    /// allocator slow path.  `Ok(false)` means "lock busy; no batch ownership
    /// was transferred".  All real validation/corruption errors still return
    /// `Err` after the lock is acquired; callers must treat the submitted batch
    /// as having uncertain ownership on `Err` unless they can prove the backend
    /// failed before mutating allocator state.
    pub fn try_deallocate_batch_to_slab(&self, idx: usize, ptr: *mut u8) -> Result<bool> {
        if idx >= self.slabs.len() {
            return Err(AllocError::ESIZE);
        }
        match self.slabs[idx].try_lock() {
            Some(mut sc) => {
                let retained_before = sc.retained_empty_slab_os_pages();
                sc.deallocate_batch(ptr as *mut usize)?;
                let retained_after = sc.retained_empty_slab_os_pages();
                self.note_retained_empty_class_state(idx, retained_after);
                let should_trim = retained_after > retained_before
                    || self.retained_empty_trim_pending.load(Ordering::Acquire);
                drop(sc);
                if should_trim {
                    self.trim_retained_empty_slabs_to_budget();
                }
                Ok(true)
            }
            None => Ok(false),
        }
    }

    #[inline]
    fn retained_empty_class_mask(idx: usize) -> u64 {
        if idx == 0 || idx >= TOTAL_SIZE_CLASS || idx >= RETAINED_EMPTY_CLASS_BITS {
            0
        } else {
            1u64 << idx
        }
    }

    #[inline]
    fn note_retained_empty_class_state(&self, idx: usize, retained_pages: usize) {
        let mask = Self::retained_empty_class_mask(idx);
        if mask == 0 {
            return;
        }
        if retained_pages == 0 {
            self.retained_empty_class_bits
                .fetch_and(!mask, Ordering::AcqRel);
        } else {
            self.retained_empty_class_bits
                .fetch_or(mask, Ordering::AcqRel);
        }
    }

    fn retained_empty_slab_os_page_candidates_from_try_locked_classes(
        &self,
    ) -> (usize, [(usize, usize); TOTAL_SIZE_CLASS], usize, bool) {
        let mut total = 0usize;
        let mut candidates = [(0usize, 0usize); TOTAL_SIZE_CLASS];
        let mut candidate_count = 0usize;
        let mut skipped_busy_class = false;
        let mut bits = self.retained_empty_class_bits.load(Ordering::Acquire);

        while bits != 0 {
            let idx = bits.trailing_zeros() as usize;
            bits &= bits - 1;
            if idx == 0 || idx >= self.slabs.len() {
                continue;
            }

            let sc = match self.slabs[idx].try_lock() {
                Some(sc) => sc,
                None => {
                    skipped_busy_class = true;
                    continue;
                }
            };
            let pages = sc.retained_empty_slab_os_pages();
            self.note_retained_empty_class_state(idx, pages);
            total = total.saturating_add(pages);
            if pages == 0 {
                continue;
            }

            // Keep the fixed-size stack candidate list ordered by retained OS
            // pages so recycle attempts start with the largest footprint.  The
            // scan drops each class guard before moving to the next class.
            let mut insert_at = candidate_count;
            while insert_at > 0 && candidates[insert_at - 1].1 < pages {
                candidates[insert_at] = candidates[insert_at - 1];
                insert_at -= 1;
            }
            candidates[insert_at] = (idx, pages);
            candidate_count += 1;
        }

        (total, candidates, candidate_count, skipped_busy_class)
    }

    #[cfg(test)]
    fn retained_empty_trim_budget(&self) -> usize {
        self.test_retained_empty_trim_budget.load(Ordering::Acquire)
    }

    #[cfg(not(test))]
    fn retained_empty_trim_budget(&self) -> usize {
        GLOBAL_RETAINED_EMPTY_SLAB_OS_PAGE_BUDGET
    }

    fn trim_retained_empty_slabs_to_budget(&self) {
        #[cfg(test)]
        self.test_retained_empty_trim_invocations
            .fetch_add(1, Ordering::AcqRel);

        self.retained_empty_trim_pending
            .store(true, Ordering::Release);
        if self
            .retained_empty_trim_active
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            return;
        }

        loop {
            self.retained_empty_trim_pending
                .store(false, Ordering::Release);

            let mut defer_pending_retry = false;
            loop {
                let (total, candidates, candidate_count, skipped_busy_class) =
                    self.retained_empty_slab_os_page_candidates_from_try_locked_classes();
                if total <= self.retained_empty_trim_budget() || candidate_count == 0 {
                    if skipped_busy_class {
                        // A try-lock scan that skips a busy class cannot prove
                        // the global retained-empty OS-page budget is satisfied:
                        // the skipped class may contain the over-budget retained
                        // slab(s).  Keep a pending nonblocking trim request for
                        // the next safe trigger instead of terminally consuming
                        // this request while every candidate is temporarily busy.
                        defer_pending_retry = true;
                        self.retained_empty_trim_pending
                            .store(true, Ordering::Release);
                    }
                    break;
                }

                #[cfg(test)]
                {
                    self.test_retained_empty_trim_scan_paused
                        .store(true, Ordering::Release);
                    while self
                        .test_pause_retained_empty_trim_after_scan
                        .load(Ordering::Acquire)
                    {
                        core::hint::spin_loop();
                    }
                    self.test_retained_empty_trim_scan_paused
                        .store(false, Ordering::Release);
                }

                let mut recycled = false;
                let mut stop_on_error = false;
                let mut recycle_blocked_by_busy_class = false;
                for &(idx, _) in candidates[..candidate_count].iter() {
                    let mut sc = match self.slabs[idx].try_lock() {
                        Some(sc) => sc,
                        None => {
                            // The class was lockable during the scan but is
                            // busy now.  Skip this candidate and keep trimming
                            // other retained empty slabs so contention in one
                            // class does not pin unrelated empty-slab RSS.
                            recycle_blocked_by_busy_class = true;
                            continue;
                        }
                    };
                    match self.recycle_one_retained_empty_slab_for_trim(&mut sc) {
                        Ok(true) => {
                            self.note_retained_empty_class_state(
                                idx,
                                sc.retained_empty_slab_os_pages(),
                            );
                            recycled = true;
                            break;
                        }
                        Ok(false) => {
                            self.note_retained_empty_class_state(
                                idx,
                                sc.retained_empty_slab_os_pages(),
                            );
                            continue;
                        }
                        Err(_) => {
                            // A recycle error can be transient: lower layers
                            // try to restore the retained empty slab before
                            // returning an error.  Preserve both the class bit
                            // and the pending trim request, but do not spin in
                            // this pass; a later allocation/deallocation or
                            // explicit trim can retry when the backend state is
                            // safe again.
                            self.note_retained_empty_class_state(
                                idx,
                                sc.retained_empty_slab_os_pages(),
                            );
                            self.retained_empty_trim_pending
                                .store(true, Ordering::Release);
                            defer_pending_retry = true;
                            stop_on_error = true;
                            break;
                        }
                    }
                }

                if stop_on_error || !recycled {
                    if !stop_on_error && recycle_blocked_by_busy_class {
                        defer_pending_retry = true;
                        self.retained_empty_trim_pending
                            .store(true, Ordering::Release);
                    }
                    break;
                }
            }

            #[cfg(test)]
            {
                self.test_retained_empty_trim_scan_paused
                    .store(true, Ordering::Release);
                while self
                    .test_pause_retained_empty_trim_after_scan
                    .load(Ordering::Acquire)
                {
                    core::hint::spin_loop();
                }
                self.test_retained_empty_trim_scan_paused
                    .store(false, Ordering::Release);
            }

            self.retained_empty_trim_active
                .store(false, Ordering::Release);

            if self.retained_empty_trim_pending.load(Ordering::Acquire)
                && !defer_pending_retry
                && self
                    .retained_empty_trim_active
                    .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
                    .is_ok()
            {
                continue;
            }

            return;
        }
    }

    fn recycle_one_retained_empty_slab_for_trim(&self, sc: &mut SCAllocator) -> Result<bool> {
        #[cfg(test)]
        {
            if self
                .test_retained_empty_trim_forced_recycle_errors
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |remaining| {
                    remaining.checked_sub(1)
                })
                .is_ok()
            {
                return Err(AllocError::EFATAL);
            }
        }

        sc.recycle_one_retained_empty_slab()
    }

    #[cfg(test)]
    pub(crate) fn recycle_all_retained_empty_slabs_for_tests(&self) {
        for (idx, slab) in self.slabs.iter().enumerate() {
            let mut sc = slab.lock();
            while sc.recycle_one_retained_empty_slab().unwrap_or(false) {}
            self.note_retained_empty_class_state(idx, sc.retained_empty_slab_os_pages());
        }
    }

    #[cfg(test)]
    pub(crate) fn retained_empty_class_bits_for_tests(&self) -> u64 {
        self.retained_empty_class_bits.load(Ordering::Acquire)
    }

    #[cfg(test)]
    pub(crate) fn retained_empty_slab_os_pages_for_tests(&self) -> usize {
        self.slabs
            .iter()
            .map(|slab| slab.lock().retained_empty_slab_os_pages())
            .fold(0usize, |total, pages| total.saturating_add(pages))
    }

    #[cfg(test)]
    pub(crate) fn retained_empty_slab_count_for_tests(&self, idx: usize) -> usize {
        self.slabs[idx].lock().retained_empty_slab_count_for_tests()
    }

    #[cfg(test)]
    pub(crate) fn retained_empty_trim_invocations_for_tests(&self) -> usize {
        self.test_retained_empty_trim_invocations
            .load(Ordering::Acquire)
    }

    #[cfg(test)]
    pub(crate) fn force_retained_empty_trim_recycle_errors_for_tests(&self, count: usize) {
        self.test_retained_empty_trim_forced_recycle_errors
            .store(count, Ordering::Release);
    }

    /// Test-only hook for exercising callers that must make forward progress
    /// when a real slab lock is already held.
    #[cfg(test)]
    pub(crate) fn test_lock_slab_for_contention(
        &self,
        idx: usize,
    ) -> Option<impl DerefMut<Target = SCAllocator> + '_> {
        self.slabs.get(idx).map(|slab| slab.lock())
    }
}

impl ZoneAllocator {
    pub fn allocate_large(&self, layout: Layout) -> Result<NonNull<u8>> {
        unsafe {
            let ptr = GlobalBackend.alloc(layout);
            NonNull::new(ptr).ok_or(AllocError::ENOMEM)
        }
    }

    pub fn deallocate_large(&self, page_ptr: NonNull<u8>, layout: Layout) {
        unsafe {
            GlobalBackend.dealloc(page_ptr.as_ptr(), layout);
        }
    }
}
#[cfg(not(feature = "fixed_heap"))]
static GLOBAL_ZONE_PTR: AtomicPtr<ZoneAllocator> = AtomicPtr::new(null_mut());
#[cfg(not(feature = "fixed_heap"))]
static GLOBAL_ZONE_INIT_LOCK: Mutex<()> = Mutex::new(());

#[cfg(not(feature = "fixed_heap"))]
#[allow(non_camel_case_types)]
pub struct GLOBAL_ZONE;

#[cfg(not(feature = "fixed_heap"))]
impl GLOBAL_ZONE {
    fn try_get() -> Result<&'static ZoneAllocator> {
        let mut ptr = GLOBAL_ZONE_PTR.load(Ordering::Acquire);
        if ptr.is_null() {
            let _guard = GLOBAL_ZONE_INIT_LOCK.lock();
            ptr = GLOBAL_ZONE_PTR.load(Ordering::Acquire);
            if ptr.is_null() {
                let layout = Layout::new::<ZoneAllocator>();
                let init_ptr = META_BUMP
                    .lock()
                    .alloc_aligned(layout.size(), layout.align())
                    .map_err(|_| AllocError::ENOMEM)?
                    as *mut ZoneAllocator;
                if init_ptr.is_null() {
                    return Err(AllocError::ENOMEM);
                }
                unsafe {
                    core::ptr::write(init_ptr, ZoneAllocator::new());
                }
                GLOBAL_ZONE_PTR.store(init_ptr, Ordering::Release);
                ptr = init_ptr;
            }
        }
        unsafe { ptr.as_ref().ok_or(AllocError::ENOMEM) }
    }

    fn get() -> &'static ZoneAllocator {
        Self::try_get().expect("zone allocator init failed")
    }
}

#[cfg(not(feature = "fixed_heap"))]
pub fn try_global_zone() -> Result<&'static ZoneAllocator> {
    GLOBAL_ZONE::try_get()
}

#[cfg(all(feature = "stats", not(feature = "fixed_heap")))]
pub fn zone_retained_empty_slab_snapshot() -> ZoneRetainedEmptySlabSnapshot {
    let ptr = GLOBAL_ZONE_PTR.load(Ordering::Acquire);
    unsafe {
        match ptr.as_ref() {
            Some(zone) => zone.retained_empty_slab_snapshot(),
            None => ZoneRetainedEmptySlabSnapshot {
                initialized: false,
                budget_os_pages: GLOBAL_RETAINED_EMPTY_SLAB_OS_PAGE_BUDGET,
                ..ZoneRetainedEmptySlabSnapshot::default()
            },
        }
    }
}

#[cfg(not(feature = "fixed_heap"))]
impl Deref for GLOBAL_ZONE {
    type Target = ZoneAllocator;

    fn deref(&self) -> &Self::Target {
        Self::get()
    }
}

#[cfg(feature = "fixed_heap")]
pub mod fixed_zone {
    use crate::error::{AllocError, Result};
    use crate::zone::ZoneAllocator;
    use core::ptr::null_mut;
    use core::sync::atomic::{AtomicPtr, Ordering};

    pub struct GlobalZone;

    pub static GLOBAL_ZONE_PTR: AtomicPtr<ZoneAllocator> = AtomicPtr::new(null_mut());

    /// Publish a fully initialized fixed-heap zone root.
    ///
    /// # Safety
    ///
    /// `ptr` must remain valid for the allocator lifetime and point to a fully
    /// initialized `ZoneAllocator` before publication.
    pub(crate) unsafe fn install_fixed_zone(ptr: *mut ZoneAllocator) {
        GLOBAL_ZONE_PTR.store(ptr, Ordering::Release);
    }

    #[inline]
    pub(crate) fn fixed_zone_initialized() -> bool {
        !GLOBAL_ZONE_PTR.load(Ordering::Acquire).is_null()
    }

    impl core::ops::Deref for GlobalZone {
        type Target = ZoneAllocator;

        fn deref(&self) -> &Self::Target {
            let ptr = GLOBAL_ZONE_PTR.load(Ordering::Acquire);
            unsafe {
                assert!(!ptr.is_null());
                ptr.as_ref()
                    .expect("fixed-heap global zone must be initialized before use")
            }
        }
    }

    pub fn try_global_zone() -> Result<&'static ZoneAllocator> {
        let ptr = GLOBAL_ZONE_PTR.load(Ordering::Acquire);
        unsafe { ptr.as_ref().ok_or(AllocError::ENOMEM) }
    }
}
#[cfg(feature = "fixed_heap")]
pub use fixed_zone as Fixed_Zone;
#[cfg(feature = "fixed_heap")]
pub use fixed_zone::GlobalZone as GLOBAL_ZONE;
#[cfg(feature = "fixed_heap")]
pub use fixed_zone::GLOBAL_ZONE_PTR as GLOBAL_ZONE_ptr;
#[cfg(feature = "fixed_heap")]
pub use fixed_zone::*;

#[cfg(all(feature = "stats", feature = "fixed_heap"))]
pub fn zone_retained_empty_slab_snapshot() -> ZoneRetainedEmptySlabSnapshot {
    let ptr = fixed_zone::GLOBAL_ZONE_PTR.load(Ordering::Acquire);
    unsafe {
        match ptr.as_ref() {
            Some(zone) => zone.retained_empty_slab_snapshot(),
            None => ZoneRetainedEmptySlabSnapshot {
                initialized: false,
                budget_os_pages: GLOBAL_RETAINED_EMPTY_SLAB_OS_PAGE_BUDGET,
                ..ZoneRetainedEmptySlabSnapshot::default()
            },
        }
    }
}

#[cfg(test)]
mod test {
    use super::*;
    #[cfg(not(feature = "fixed_heap"))]
    use crate::pal::thread::linux::thread;
    #[cfg(not(feature = "fixed_heap"))]
    use alloc::boxed::Box;

    #[test]
    fn zone_retained_empty_class_mask_covers_all_base_size_classes() {
        assert_eq!(
            ZoneAllocator::retained_empty_class_mask(0),
            0,
            "class zero is the internal sentinel and must stay untracked"
        );

        let mut seen = 0u64;
        for idx in 1..TOTAL_SIZE_CLASS {
            let mask = ZoneAllocator::retained_empty_class_mask(idx);
            assert_ne!(
                mask, 0,
                "retained-empty trim must be able to track size class {}",
                idx
            );
            assert_eq!(
                mask.count_ones(),
                1,
                "size class {} must map to exactly one retained-empty bit",
                idx
            );
            assert_eq!(
                mask.trailing_zeros() as usize,
                idx,
                "size class {} must keep a stable one-bit mapping",
                idx
            );
            assert_eq!(
                seen & mask,
                0,
                "size class {} must not alias another retained-empty bit",
                idx
            );
            seen |= mask;
        }

        assert_eq!(
            seen.count_ones() as usize,
            TOTAL_SIZE_CLASS.saturating_sub(1),
            "every non-sentinel size class should be represented exactly once"
        );
        assert_eq!(
            ZoneAllocator::retained_empty_class_mask(TOTAL_SIZE_CLASS),
            0,
            "out-of-range classes remain explicitly untracked"
        );
    }

    #[test]
    fn zone_rejects_out_of_range_slab_index_without_panic() {
        let zone = ZoneAllocator::new();
        let invalid_idx = TOTAL_SIZE_CLASS;

        let alloc_err = zone
            .allocate_batch_from_slab(invalid_idx, 1)
            .expect_err("out-of-range slab allocation must fail closed");
        assert_eq!(alloc_err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());

        let dealloc_err = zone
            .deallocate_batch_to_slab(invalid_idx, null_mut())
            .expect_err("out-of-range slab deallocation must fail closed");
        assert_eq!(dealloc_err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());

        let try_dealloc_err = zone
            .try_deallocate_batch_to_slab(invalid_idx, null_mut())
            .expect_err("out-of-range nonblocking slab deallocation must fail closed");
        assert_eq!(
            try_dealloc_err.to_raw_errno(),
            AllocError::ESIZE.to_raw_errno()
        );
    }

    #[test]
    fn zone_try_deallocate_batch_reports_busy_without_touching_batch() {
        let zone = ZoneAllocator::new();
        let _guard = zone.slabs[1].lock();

        assert_eq!(
            zone.try_deallocate_batch_to_slab(1, null_mut())
                .expect("busy lock is not a validation failure"),
            false,
            "try_deallocate_batch_to_slab must not block or consume a batch while the slab lock is held"
        );
    }

    #[cfg(feature = "fixed_heap")]
    fn ensure_zone_test_backend_initialized() -> spin::MutexGuard<'static, ()> {
        crate::sc::fixed_heap_test_guard()
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn ensure_zone_test_backend_initialized() {}

    unsafe fn link_allocation_batch(ptr: *mut u8, count: usize, stride: usize) {
        for idx in 0..count {
            let next = if idx + 1 == count {
                0
            } else {
                ptr.add((idx + 1) * stride) as usize
            };
            (ptr.add(idx * stride) as *mut usize).write(next);
        }
    }

    fn recycle_all_test_zone_retained_empty_slabs(zone: &ZoneAllocator) {
        zone.recycle_all_retained_empty_slabs_for_tests();
    }

    #[cfg(not(feature = "fixed_heap"))]
    struct TestZoneThreadPtr(*const ZoneAllocator);

    #[cfg(not(feature = "fixed_heap"))]
    unsafe impl Send for TestZoneThreadPtr {}

    fn retain_one_zone_test_batch_without_zone_trim(zone: &ZoneAllocator, class_idx: usize) {
        assert_eq!(get_num_pages_by_idx(class_idx), 1);
        let (ptr, count, stride) =
            zone.allocate_batch_from_slab(class_idx, 8)
                .unwrap_or_else(|err| {
                    recycle_all_test_zone_retained_empty_slabs(zone);
                    panic!(
                        "real size-class batch allocation should succeed without ENOMEM/skip: {:?}",
                        err
                    )
                });
        unsafe {
            link_allocation_batch(
                ptr,
                count,
                stride.expect("new slab batch should report object stride"),
            )
        };
        let retained_after = {
            let mut slab = zone.slabs[class_idx].lock();
            slab.deallocate_batch(ptr as *mut usize)
                .expect("real size-class batch deallocation should succeed");
            slab.retained_empty_slab_os_pages()
        };
        zone.note_retained_empty_class_state(class_idx, retained_after);
    }

    fn allocate_zone_test_batches<const TOTAL_BATCHES: usize>(
        zone: &ZoneAllocator,
        class_count: usize,
        batches_per_class: usize,
    ) -> [(usize, *mut u8, usize, usize); TOTAL_BATCHES] {
        let mut batches = [(0usize, core::ptr::null_mut::<u8>(), 0usize, 0usize); TOTAL_BATCHES];
        let mut write_idx = 0usize;

        for class_idx in 1..=class_count {
            assert_eq!(get_num_pages_by_idx(class_idx), 1);
            for _ in 0..batches_per_class {
                let (ptr, count, stride) = zone
                    .allocate_batch_from_slab(class_idx, 8)
                    .unwrap_or_else(|err| {
                        recycle_all_test_zone_retained_empty_slabs(zone);
                        panic!(
                            "real size-class batch allocation should succeed without ENOMEM/skip: {:?}",
                            err
                        )
                    });
                batches[write_idx] = (
                    class_idx,
                    ptr,
                    count,
                    stride.expect("new slab batch should report object stride"),
                );
                write_idx += 1;
            }
        }

        batches
    }

    fn run_zone_global_empty_slab_budget_trims_cross_class_bursts() {
        const CLASS_COUNT: usize = 5;
        const BATCHES_PER_CLASS: usize = 7;
        const OLD_PER_CLASS_AGGREGATE_OS_PAGES: usize = CLASS_COUNT * BATCHES_PER_CLASS;

        let _fixed_heap_guard = ensure_zone_test_backend_initialized();
        let zone = ZoneAllocator::new();
        let batches = allocate_zone_test_batches::<OLD_PER_CLASS_AGGREGATE_OS_PAGES>(
            &zone,
            CLASS_COUNT,
            BATCHES_PER_CLASS,
        );

        for (class_idx, ptr, count, stride) in batches {
            unsafe { link_allocation_batch(ptr, count, stride) };
            zone.deallocate_batch_to_slab(class_idx, ptr)
                .expect("real size-class batch deallocation should succeed");
        }

        let retained_pages = zone.retained_empty_slab_os_pages_for_tests();
        assert!(
            retained_pages <= GLOBAL_RETAINED_EMPTY_SLAB_OS_PAGE_BUDGET,
            "retained empty slabs should be trimmed to the zone/global budget; retained {}",
            retained_pages
        );
        assert!(
            retained_pages < OLD_PER_CLASS_AGGREGATE_OS_PAGES,
            "global trim should retain fewer OS pages than independent per-class warm caches"
        );

        recycle_all_test_zone_retained_empty_slabs(&zone);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn zone_global_empty_slab_budget_trims_cross_class_bursts() {
        run_zone_global_empty_slab_budget_trims_cross_class_bursts();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn zone_partial_deallocation_does_not_scan_global_empty_slab_budget() {
        const CLASS_IDX: usize = 1;

        let _fixed_heap_guard = ensure_zone_test_backend_initialized();
        let zone = ZoneAllocator::new();
        let (ptr, count, stride) = zone
            .allocate_batch_from_slab(CLASS_IDX, 8)
            .expect("real size-class batch allocation should succeed");
        let stride = stride.expect("new slab batch should report object stride");
        assert!(count > 1);
        assert_eq!(zone.retained_empty_trim_invocations_for_tests(), 0);

        unsafe {
            // Return one object from a full page.  The page becomes partial, so
            // retained-empty RSS cannot have increased and the zone should not
            // scan every size class.
            *(ptr as *mut usize) = 0;
        }
        zone.deallocate_batch_to_slab(CLASS_IDX, ptr)
            .expect("single-object deallocation should succeed");
        assert_eq!(
            zone.retained_empty_trim_invocations_for_tests(),
            0,
            "full-to-partial deallocation must not pay the global trim scan cost"
        );

        unsafe {
            link_allocation_batch(ptr.add(stride), count - 1, stride);
        }
        zone.deallocate_batch_to_slab(CLASS_IDX, unsafe { ptr.add(stride) })
            .expect("remaining objects should deallocate cleanly");
        assert!(
            zone.retained_empty_trim_invocations_for_tests() > 0,
            "page-empty deallocation should still trigger retained-empty convergence"
        );

        recycle_all_test_zone_retained_empty_slabs(&zone);
    }

    #[cfg(feature = "stats")]
    #[test]
    fn zone_retained_empty_snapshot_reports_real_retained_pages_and_busy_lower_bound() {
        const CLASS_IDX: usize = 1;

        let _fixed_heap_guard = ensure_zone_test_backend_initialized();
        let zone = ZoneAllocator::new();
        retain_one_zone_test_batch_without_zone_trim(&zone, CLASS_IDX);

        let exact = zone.retained_empty_slab_snapshot();
        assert!(exact.initialized);
        assert_eq!(exact.busy_class_count, 0);
        assert_eq!(
            exact.observed_retained_os_pages, 1,
            "uncontended snapshot should report the real retained empty slab page"
        );
        assert_eq!(
            exact.tracked_class_bits & ZoneAllocator::retained_empty_class_mask(CLASS_IDX),
            ZoneAllocator::retained_empty_class_mask(CLASS_IDX),
            "snapshot should expose the retained-empty class bit used by the trim path"
        );
        assert_eq!(exact.budget_os_pages, zone.retained_empty_trim_budget());

        let busy_guard = zone
            .test_lock_slab_for_contention(CLASS_IDX)
            .expect("test class should exist");
        let contended = zone.retained_empty_slab_snapshot();
        assert_eq!(
            contended.busy_class_count, 1,
            "snapshot must not block on a contended slab lock"
        );
        assert_eq!(
            contended.observed_retained_os_pages, 0,
            "busy retained classes are intentionally excluded from the trusted observed total"
        );
        assert_eq!(
            contended.tracked_class_bits & ZoneAllocator::retained_empty_class_mask(CLASS_IDX),
            ZoneAllocator::retained_empty_class_mask(CLASS_IDX),
            "the bitset still tells diagnostics which class needs a later exact sample"
        );
        drop(busy_guard);

        recycle_all_test_zone_retained_empty_slabs(&zone);
    }

    #[cfg(feature = "stats")]
    #[test]
    fn zone_retained_empty_snapshot_ignores_busy_untracked_classes() {
        const RETAINED_CLASS_IDX: usize = 1;
        const BUSY_EMPTY_CLASS_IDX: usize = 2;

        let _fixed_heap_guard = ensure_zone_test_backend_initialized();
        let zone = ZoneAllocator::new();
        retain_one_zone_test_batch_without_zone_trim(&zone, RETAINED_CLASS_IDX);
        assert_eq!(
            zone.retained_empty_slab_count_for_tests(BUSY_EMPTY_CLASS_IDX),
            0,
            "the contended class starts empty and should not be in the retained-empty bitset"
        );
        assert_eq!(
            zone.retained_empty_class_bits_for_tests()
                & ZoneAllocator::retained_empty_class_mask(BUSY_EMPTY_CLASS_IDX),
            0,
            "untracked classes should not be sampled by the retained-empty snapshot"
        );

        let busy_guard = zone
            .test_lock_slab_for_contention(BUSY_EMPTY_CLASS_IDX)
            .expect("test class should exist");
        let snapshot = zone.retained_empty_slab_snapshot();

        assert_eq!(
            snapshot.busy_class_count, 0,
            "a busy untracked class must not make the retained-empty diagnostic inexact"
        );
        assert_eq!(
            snapshot.observed_retained_os_pages, 1,
            "tracked retained class remains exactly observable while an unrelated class is busy"
        );
        assert_eq!(
            snapshot.tracked_class_count, 1,
            "only the retained class should be tracked"
        );
        drop(busy_guard);

        recycle_all_test_zone_retained_empty_slabs(&zone);
    }

    #[test]
    fn zone_retained_empty_trim_ignores_busy_class_without_retained_pages() {
        const RETAINED_CLASS_IDX: usize = 1;
        const BUSY_EMPTY_CLASS_IDX: usize = 2;

        let _fixed_heap_guard = ensure_zone_test_backend_initialized();
        let zone = ZoneAllocator::new();
        zone.test_retained_empty_trim_budget
            .store(0, Ordering::Release);

        retain_one_zone_test_batch_without_zone_trim(&zone, RETAINED_CLASS_IDX);
        assert_ne!(
            zone.retained_empty_class_bits_for_tests()
                & ZoneAllocator::retained_empty_class_mask(RETAINED_CLASS_IDX),
            0,
            "retained class should be discoverable through the zone bitset"
        );
        assert_eq!(
            zone.retained_empty_slab_count_for_tests(BUSY_EMPTY_CLASS_IDX),
            0,
            "the contended class starts empty and should not be probed by trim"
        );

        let busy_guard = zone
            .test_lock_slab_for_contention(BUSY_EMPTY_CLASS_IDX)
            .expect("test class should exist");
        zone.trim_retained_empty_slabs_to_budget();

        assert!(
            !zone.retained_empty_trim_pending.load(Ordering::Acquire),
            "a busy class with no retained slabs must not create a false pending trim"
        );
        drop(busy_guard);

        assert_eq!(
            zone.retained_empty_slab_os_pages_for_tests(),
            0,
            "trim should reclaim the only retained empty slab without waiting on unrelated classes"
        );
        assert_eq!(
            zone.retained_empty_class_bits_for_tests()
                & ZoneAllocator::retained_empty_class_mask(RETAINED_CLASS_IDX),
            0,
            "reclaimed class should clear its retained-empty bit"
        );

        recycle_all_test_zone_retained_empty_slabs(&zone);
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn fixed_heap_zone_global_empty_slab_budget_trims_cross_class_bursts() {
        run_zone_global_empty_slab_budget_trims_cross_class_bursts();
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn fixed_heap_zone_empty_slab_global_budget_trims_cross_class_bursts() {
        run_zone_global_empty_slab_budget_trims_cross_class_bursts();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn zone_empty_slab_trim_skips_busy_candidate_and_reclaims_next_class() {
        const BUSY_CLASS_IDX: usize = 1;
        const RECLAIMABLE_CLASS_IDX: usize = 2;

        let _fixed_heap_guard = ensure_zone_test_backend_initialized();
        let zone: &'static ZoneAllocator = Box::leak(Box::new(ZoneAllocator::new()));
        zone.test_retained_empty_trim_budget
            .store(0, Ordering::Release);

        retain_one_zone_test_batch_without_zone_trim(zone, BUSY_CLASS_IDX);
        retain_one_zone_test_batch_without_zone_trim(zone, RECLAIMABLE_CLASS_IDX);
        assert_eq!(zone.retained_empty_slab_count_for_tests(BUSY_CLASS_IDX), 1);
        assert_eq!(
            zone.retained_empty_slab_count_for_tests(RECLAIMABLE_CLASS_IDX),
            1
        );
        assert_eq!(zone.retained_empty_slab_os_pages_for_tests(), 2);

        zone.test_pause_retained_empty_trim_after_scan
            .store(true, Ordering::Release);
        let trimmer_zone = TestZoneThreadPtr(zone as *const ZoneAllocator);
        let trimmer = thread::spawn(move || unsafe {
            (*trimmer_zone.0).trim_retained_empty_slabs_to_budget();
        });

        while !zone
            .test_retained_empty_trim_scan_paused
            .load(Ordering::Acquire)
        {
            thread::yield_now();
        }

        let busy_guard = zone
            .test_lock_slab_for_contention(BUSY_CLASS_IDX)
            .expect("test class should exist");
        zone.test_pause_retained_empty_trim_after_scan
            .store(false, Ordering::Release);
        trimmer
            .join()
            .expect("trim thread should skip busy class and finish");

        assert_eq!(
            zone.retained_empty_slab_count_for_tests(RECLAIMABLE_CLASS_IDX),
            0,
            "a busy largest candidate must not prevent reclaiming another lockable class"
        );
        drop(busy_guard);
        assert_eq!(
            zone.retained_empty_slab_os_pages_for_tests(),
            1,
            "global retained empty-slab pages should drop after skipping the contended class"
        );
        assert!(
            !zone.retained_empty_trim_active.load(Ordering::Acquire),
            "trim guard should be released after busy-candidate skip"
        );

        recycle_all_test_zone_retained_empty_slabs(zone);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn zone_empty_slab_trim_all_busy_candidates_leave_pending_for_later_trigger() {
        const BUSY_CLASS_IDX: usize = 1;

        let _fixed_heap_guard = ensure_zone_test_backend_initialized();
        let zone: &'static ZoneAllocator = Box::leak(Box::new(ZoneAllocator::new()));
        zone.test_retained_empty_trim_budget
            .store(0, Ordering::Release);

        retain_one_zone_test_batch_without_zone_trim(zone, BUSY_CLASS_IDX);
        assert_eq!(zone.retained_empty_slab_count_for_tests(BUSY_CLASS_IDX), 1);
        assert_eq!(zone.retained_empty_slab_os_pages_for_tests(), 1);

        let busy_guard = zone
            .test_lock_slab_for_contention(BUSY_CLASS_IDX)
            .expect("test class should exist");
        zone.test_pause_retained_empty_trim_after_scan
            .store(true, Ordering::Release);

        let trimmer_zone = TestZoneThreadPtr(zone as *const ZoneAllocator);
        let trimmer = thread::spawn(move || unsafe {
            (*trimmer_zone.0).trim_retained_empty_slabs_to_budget();
        });

        while !zone
            .test_retained_empty_trim_scan_paused
            .load(Ordering::Acquire)
        {
            thread::yield_now();
        }

        assert!(
            zone.retained_empty_trim_active.load(Ordering::Acquire),
            "test trimmer should still own the trim guard after observing only busy classes"
        );

        let pending_zone = TestZoneThreadPtr(zone as *const ZoneAllocator);
        let pending_trimmer = thread::spawn(move || unsafe {
            (*pending_zone.0).trim_retained_empty_slabs_to_budget();
        });
        pending_trimmer
            .join()
            .expect("concurrent trim request should record pending and return");
        assert!(
            zone.retained_empty_trim_pending.load(Ordering::Acquire),
            "all-busy trim must not consume the only pending convergence request"
        );

        zone.test_pause_retained_empty_trim_after_scan
            .store(false, Ordering::Release);
        trimmer
            .join()
            .expect("all-busy trim should release the active guard without blocking");

        assert!(
            !zone.retained_empty_trim_active.load(Ordering::Acquire),
            "all-busy trim should not spin while the candidate remains locked"
        );
        assert!(
            zone.retained_empty_trim_pending.load(Ordering::Acquire),
            "all-busy trim should leave a pending request for a later safe trigger"
        );

        drop(busy_guard);
        zone.trim_retained_empty_slabs_to_budget();

        assert_eq!(
            zone.retained_empty_slab_os_pages_for_tests(),
            0,
            "later trim trigger should converge after the busy class becomes lockable"
        );
        assert!(
            !zone.retained_empty_trim_pending.load(Ordering::Acquire),
            "successful later convergence should consume the pending request"
        );

        recycle_all_test_zone_retained_empty_slabs(zone);
    }

    #[test]
    fn zone_empty_slab_trim_preserves_pending_after_recycle_error() {
        const CLASS_IDX: usize = 1;

        let _fixed_heap_guard = ensure_zone_test_backend_initialized();
        let zone = ZoneAllocator::new();
        zone.test_retained_empty_trim_budget
            .store(0, Ordering::Release);

        retain_one_zone_test_batch_without_zone_trim(&zone, CLASS_IDX);
        assert_eq!(zone.retained_empty_slab_count_for_tests(CLASS_IDX), 1);
        assert_eq!(zone.retained_empty_slab_os_pages_for_tests(), 1);

        zone.force_retained_empty_trim_recycle_errors_for_tests(1);
        zone.trim_retained_empty_slabs_to_budget();

        assert_eq!(
            zone.retained_empty_slab_os_pages_for_tests(),
            1,
            "injected recycle error should leave the retained empty slab in place"
        );
        assert_ne!(
            zone.retained_empty_class_bits_for_tests()
                & ZoneAllocator::retained_empty_class_mask(CLASS_IDX),
            0,
            "the retained class bit must keep pointing future trims at the unrecycled class"
        );
        assert!(
            zone.retained_empty_trim_pending.load(Ordering::Acquire),
            "a recycle error must preserve the trim request instead of consuming it"
        );

        zone.trim_retained_empty_slabs_to_budget();
        assert_eq!(
            zone.retained_empty_slab_os_pages_for_tests(),
            0,
            "a later trim should converge once the transient recycle error is gone"
        );
        assert!(
            !zone.retained_empty_trim_pending.load(Ordering::Acquire),
            "successful later convergence should consume the preserved pending request"
        );

        recycle_all_test_zone_retained_empty_slabs(&zone);
    }

    #[test]
    fn zone_pending_empty_slab_trim_converges_on_later_allocation() {
        const CLASS_IDX: usize = 1;

        let _fixed_heap_guard = ensure_zone_test_backend_initialized();
        let zone = ZoneAllocator::new();
        zone.test_retained_empty_trim_budget
            .store(0, Ordering::Release);

        retain_one_zone_test_batch_without_zone_trim(&zone, CLASS_IDX);
        zone.force_retained_empty_trim_recycle_errors_for_tests(1);
        zone.trim_retained_empty_slabs_to_budget();
        assert!(
            zone.retained_empty_trim_pending.load(Ordering::Acquire),
            "the injected recycle error should leave a pending trim request"
        );

        let (ptr, count, stride) = zone
            .allocate_batch_from_slab(CLASS_IDX, 8)
            .expect("later allocation should be able to reuse the retained empty slab");
        assert_eq!(
            zone.retained_empty_slab_os_pages_for_tests(),
            0,
            "allocating from the retained empty slab removes its retained-page footprint"
        );
        assert!(
            !zone.retained_empty_trim_pending.load(Ordering::Acquire),
            "allocation slow path should consume stale pending trim once retained pages are gone"
        );

        unsafe {
            link_allocation_batch(
                ptr,
                count,
                stride.expect("reused slab batch should report object stride"),
            )
        };
        zone.deallocate_batch_to_slab(CLASS_IDX, ptr)
            .expect("cleanup deallocation should succeed");

        recycle_all_test_zone_retained_empty_slabs(&zone);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn zone_empty_slab_trim_pending_request_converges_before_guard_release() {
        const CLASS_IDX: usize = 1;

        let _fixed_heap_guard = ensure_zone_test_backend_initialized();
        let zone: &'static ZoneAllocator = Box::leak(Box::new(ZoneAllocator::new()));
        zone.test_retained_empty_trim_budget
            .store(0, Ordering::Release);

        zone.test_pause_retained_empty_trim_after_scan
            .store(true, Ordering::Release);
        let trimmer_zone = TestZoneThreadPtr(zone as *const ZoneAllocator);
        let trimmer = thread::spawn(move || unsafe {
            (*trimmer_zone.0).trim_retained_empty_slabs_to_budget();
        });

        while !zone
            .test_retained_empty_trim_scan_paused
            .load(Ordering::Acquire)
        {
            thread::yield_now();
        }

        assert!(
            zone.retained_empty_trim_active.load(Ordering::Acquire),
            "test trimmer should still own the trim guard while skipped frees arrive"
        );

        assert_eq!(get_num_pages_by_idx(CLASS_IDX), 1);
        let (ptr, count, stride) =
            zone.allocate_batch_from_slab(CLASS_IDX, 8)
                .unwrap_or_else(|err| {
                    recycle_all_test_zone_retained_empty_slabs(zone);
                    panic!(
                        "real size-class batch allocation should succeed without ENOMEM/skip: {:?}",
                        err
                    )
                });
        unsafe {
            link_allocation_batch(
                ptr,
                count,
                stride.expect("new slab batch should report object stride"),
            )
        };
        zone.deallocate_batch_to_slab(CLASS_IDX, ptr)
            .expect("real size-class batch deallocation should succeed while trim is active");

        assert!(
            zone.retained_empty_slab_os_pages_for_tests() > zone.retained_empty_trim_budget(),
            "skipped trim request should temporarily leave retained pages over the test budget"
        );
        assert!(
            zone.retained_empty_trim_pending.load(Ordering::Acquire),
            "busy callers must record a pending trim request instead of dropping it"
        );

        zone.test_pause_retained_empty_trim_after_scan
            .store(false, Ordering::Release);
        trimmer.join().expect("trim thread should finish");

        assert_eq!(
            zone.retained_empty_slab_os_pages_for_tests(),
            0,
            "active trimmer should rescan pending skipped requests before final release"
        );
        assert!(
            !zone.retained_empty_trim_active.load(Ordering::Acquire),
            "trim guard should be released after pending convergence"
        );
        assert!(
            !zone.retained_empty_trim_pending.load(Ordering::Acquire),
            "pending trim request should be consumed by the active trimmer"
        );

        recycle_all_test_zone_retained_empty_slabs(zone);
    }
}
