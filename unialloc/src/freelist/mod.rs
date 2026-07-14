#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sys_alloc as system_alloc;
mod bump;
use crate::collections::radix_tree::{try_with_rd_tree, RadixTree, TreeNode};
use crate::error::AllocError;
use crate::sc::{align_12k, META_BUMP};
use crate::size_class::BACKEND_MAX_PAGE;
use crate::PAGE_SIZE;
use bump::BumpAlloc;
use core::alloc::{Allocator, GlobalAlloc, Layout};
use core::convert::TryFrom;
use core::mem::align_of;
use core::ptr::{null_mut, NonNull};
use core::sync::atomic::{AtomicPtr, AtomicUsize, Ordering};
use spin::Mutex;

/// Global page-run bump allocator.
///
/// The mutable allocator state is protected by the `spin::Mutex`; the binding
/// itself does not need to be `static mut`.  Keeping it immutable avoids
/// creating references to a mutable static on newer Rust toolchains while
/// preserving the same lock-based synchronization boundary.
pub static BUMP: Mutex<BumpAlloc> = Mutex::new(BumpAlloc::new());

#[inline]
fn dangling_zero_size_slice(layout: Layout) -> NonNull<[u8]> {
    // `Layout::dangling()` was renamed on newer nightly toolchains.  Construct
    // the standard zero-sized allocation sentinel directly so both the
    // paper-pinned 2022 nightly and accepted-newer nightlies compile.
    let ptr = unsafe { NonNull::new_unchecked(layout.align() as *mut u8) };
    NonNull::slice_from_raw_parts(ptr, 0)
}

#[inline]
fn checked_radix_key(addr: usize) -> Result<usize, AllocError> {
    align_12k(addr)
        .checked_mul(1_usize << 16)
        .ok_or(AllocError::EFATAL)
}

#[inline]
fn marker_page_count(marker: i64) -> Option<usize> {
    let pages = (marker >> 48).checked_neg()?;
    if pages > 0 {
        Some(pages as usize)
    } else {
        None
    }
}

#[inline]
fn page_run_index_for_size(size: usize) -> Result<usize, AllocError> {
    if size == 0 {
        return Err(AllocError::ESIZE);
    }
    size.checked_add(PAGE_SIZE - 1)
        .map(|rounded| rounded / PAGE_SIZE)
        .and_then(|pages| pages.checked_sub(1))
        .ok_or(AllocError::ESIZE)
}

#[inline]
fn page_run_count_for_size(size: usize) -> Result<usize, AllocError> {
    page_run_index_for_size(size)?
        .checked_add(1)
        .ok_or(AllocError::ESIZE)
}

#[inline]
fn checked_page_size_from_count(page_count: usize) -> Result<usize, AllocError> {
    page_count.checked_mul(PAGE_SIZE).ok_or(AllocError::ESIZE)
}

#[inline]
fn checked_page_offset(page_count: usize) -> Option<usize> {
    page_count.checked_mul(PAGE_SIZE)
}

#[cfg(not(feature = "fixed_heap"))]
#[inline]
fn hosted_unmap_run_from_idx(ptr: *mut u8, idx: usize) -> Option<(*mut u8, usize)> {
    idx.checked_add(1)
        .and_then(checked_page_offset)
        .map(|size| (ptr, size))
}

#[inline]
fn checked_page_addr(base: usize, page_count: usize) -> Option<usize> {
    checked_page_offset(page_count).and_then(|offset| base.checked_add(offset))
}

#[inline]
fn checked_round_up_addr(addr: usize, align: usize) -> Option<usize> {
    if align == 0 || !align.is_power_of_two() {
        return None;
    }
    let mask = align.checked_sub(1)?;
    addr.checked_add(mask).map(|rounded| rounded & !mask)
}

#[inline]
fn radix_tree_result(res: core::result::Result<(), &'static str>) -> Result<(), AllocError> {
    res.map_err(|_| AllocError::EFATAL)
}

#[inline]
fn run_marker_value(idx: usize) -> Result<i64, AllocError> {
    i64::try_from(idx)
        .map_err(|_| AllocError::EFATAL)?
        .checked_add(1)
        .and_then(|v| v.checked_neg())
        .map(|value| value << 48)
        .ok_or(AllocError::EFATAL)
}

struct DoubleLinkedList {
    prev: Option<*mut DoubleLinkedList>,
    next: Option<*mut DoubleLinkedList>,
}

#[cfg(all(test, not(feature = "fixed_heap")))]
#[derive(Clone, Copy, PartialEq, Eq)]
enum HostedFreeFaultForTest {
    None,
    MetadataUnavailable,
    InsertFailure,
}

#[derive(Clone, Copy)]
struct AlignedRunCandidate {
    ptr: *mut DoubleLinkedList,
    aligned_addr: usize,
    run_pages: usize,
    prefix_pages: usize,
    suffix_pages: usize,
}

impl AlignedRunCandidate {
    #[inline]
    fn fragment_count(&self) -> usize {
        usize::from(self.prefix_pages != 0) + usize::from(self.suffix_pages != 0)
    }

    #[inline]
    fn slack_pages(&self) -> usize {
        self.prefix_pages + self.suffix_pages
    }

    #[inline]
    fn run_idx(&self) -> Result<usize, AllocError> {
        self.run_pages.checked_sub(1).ok_or(AllocError::ESIZE)
    }

    #[inline]
    fn is_less_fragmenting_than(&self, other: &Self) -> bool {
        let self_fragments = self.fragment_count();
        let other_fragments = other.fragment_count();
        if self_fragments != other_fragments {
            return self_fragments < other_fragments;
        }

        // Once the split count is equal, keep the smaller run.  That preserves
        // large contiguous runs for future requests and minimizes the total
        // free slack we have to publish back into the page-run lists.
        let self_slack = self.slack_pages();
        let other_slack = other.slack_pages();
        if self_slack != other_slack {
            return self_slack < other_slack;
        }

        // If two candidates leave the same number of free fragments and pages,
        // prefer the lower aligned payload address.  This keeps selection
        // deterministic without trading away a lower-fragment or tighter run.
        self.aligned_addr < other.aligned_addr
    }
}

// Aligned page-run reuse normally wants the first matching size class to avoid
// over-consuming large runs.  A tiny lookahead is still worthwhile when the
// first candidate would split into both a prefix and suffix but a nearby larger
// candidate would leave only one contiguous remainder.  Keep the window small
// and cap the per-class node walk so over-page-aligned allocation does not
// become a slow full free-list search while the global radix-tree lock is held.
const ALIGNED_RUN_FRAGMENT_LOOKAHEAD_CLASSES: usize = 8;
const ALIGNED_RUN_CANDIDATE_SCAN_NODES: usize = 16;
const ALIGNED_RUN_STALE_SKIP_CAPACITY: usize =
    ALIGNED_RUN_CANDIDATE_SCAN_NODES * (ALIGNED_RUN_FRAGMENT_LOOKAHEAD_CLASSES + 1);
// If stale races fill the normal skip window, allow one more bounded
// per-class scan budget before falling back to bump allocation.  This keeps
// retry work deterministic while avoiding immediate fallback when a valid run
// is just behind a saturated stale window.
const ALIGNED_RUN_STALE_SKIP_RETRY_CAPACITY: usize = ALIGNED_RUN_CANDIDATE_SCAN_NODES;
const ALIGNED_RUN_STALE_SKIP_TOTAL_CAPACITY: usize =
    ALIGNED_RUN_STALE_SKIP_CAPACITY + ALIGNED_RUN_STALE_SKIP_RETRY_CAPACITY;
// Fallback aligned allocation over-reserves a page run and publishes the
// prefix/suffix slack back into this free list.  Hosted tests and long-running
// processes can encounter stale global radix markers when the OS reuses a
// virtual address after another temporary FreeList unmapped it.  Do not return
// an aligned payload after silently losing that slack; restore the raw run and
// retry a bounded number of times instead.
const ALIGNED_FALLBACK_SPLIT_PUBLISH_RETRIES: usize = 4;
// Exact-size allocation normally removes the list head.  If a raced or
// previously failed removal already cleared the radix markers for that head,
// leaving it in place blocks every valid same-size run behind it and pushes the
// caller toward a larger split or bump allocation.  Prune only markerless heads
// and keep the cap small because the list lock is held while this repair runs.
const EXACT_RUN_MARKERLESS_HEAD_PRUNE_LIMIT: usize = 16;
// `remove_spec_one` is a cold coalescing path that removes a known page-run
// pointer.  Radix markers plus local neighbor links are not enough to prove
// that the intrusive node is reachable from the locked size-class list head:
// stale or foreign nodes can still have self-consistent neighbors.  Walk the
// backlink chain to the locked head, but cap the work so a corrupt cycle cannot
// hold the size-class lock indefinitely.  Failing the proof only disables this
// coalescing attempt; the marked run remains available for later exact/larger
// reuse.
const SPEC_RUN_MEMBERSHIP_BACKLINK_SCAN_LIMIT: usize = 1024;

const FREE_LIST_BITMAP_WORDS: usize = 4;
const FREE_LIST_BITMAP_WORD_BITS: usize = core::mem::size_of::<usize>() * 8;
const FREE_LIST_BITMAP_CAPACITY: usize = FREE_LIST_BITMAP_WORDS * FREE_LIST_BITMAP_WORD_BITS;

// The non-empty bitmap is a footprint/lock-traffic optimization: it lets
// page-run allocation skip definitely-empty size classes without taking their
// locks.  If BACKEND_MAX_PAGE outgrows the bitmap, high run classes would fall
// back to the "untracked" path and searches could miss reusable large runs,
// increasing external fragmentation.  Fail closed at compile time instead of
// silently accepting that drift.
const _: [(); 1] = [(); (BACKEND_MAX_PAGE <= FREE_LIST_BITMAP_CAPACITY) as usize];

#[cfg(not(feature = "fixed_heap"))]
// Hosted builds can hand cold page runs back to the OS, unlike `fixed_heap`.
// Keep a bounded warm set for reuse, but cap it below the largest representable
// 128-page free-list class on common 4KiB-page hosts so the policy has a real
// RSS/external-fragmentation effect instead of retaining every free-listable run.
const HOSTED_FREE_RUN_RETAIN_MAX_BYTES: usize = 256 * 1024;

#[cfg(not(feature = "fixed_heap"))]
const fn hosted_free_run_retain_max_pages() -> usize {
    let rounded = (HOSTED_FREE_RUN_RETAIN_MAX_BYTES + PAGE_SIZE - 1) / PAGE_SIZE;
    if rounded < 1 {
        1
    } else if rounded > BACKEND_MAX_PAGE {
        BACKEND_MAX_PAGE
    } else {
        rounded
    }
}

impl DoubleLinkedList {
    const fn new() -> Self {
        Self {
            prev: None,
            next: None,
        }
    }

    fn push_before_head(&mut self, new_node: *mut DoubleLinkedList) -> Result<(), AllocError> {
        let new_ref = Self::try_get_ref(new_node)?;
        self.prev = Some(new_node);
        let self_ptr = self as *const _ as *mut DoubleLinkedList;
        new_ref.next = Some(self_ptr);
        // unsafe {core::ptr::write(&mut new_node.next as * const _ as * mut Option<&'static DoubleLinkedList>, Some(self))}
        new_ref.prev = None;
        Ok(())
    }

    fn try_get_ref(
        ptr: *mut DoubleLinkedList,
    ) -> Result<&'static mut DoubleLinkedList, AllocError> {
        unsafe { ptr.as_mut().ok_or(AllocError::EFATAL) }
    }

    fn remove_current(&mut self) -> Result<Option<*mut DoubleLinkedList>, AllocError> {
        let new_head = if let Some(prev) = self.prev {
            let prev_ref = Self::try_get_ref(prev)?;
            if let Some(next) = self.next {
                let next_ref = Self::try_get_ref(next)?;
                prev_ref.next = Some(next);
                next_ref.prev = Some(prev);
            } else {
                prev_ref.next = None;
            }
            None
        } else if let Some(next) = self.next {
            let next_ref = Self::try_get_ref(next)?;
            next_ref.prev = None;
            Some(next)
        } else {
            None
        };

        self.prev = None;
        self.next = None;
        Ok(new_head)
    }
}

pub struct FreeList {
    lists: AtomicPtr<Mutex<Option<&'static mut DoubleLinkedList>>>,
    // One bit per page-run size class.  The bits are only a hint: the per-list
    // lock and radix markers remain the source of truth.  The bitmap lets the
    // allocator skip definitely-empty run lists without taking every mutex
    // while searching for a larger run to split or an aligned payload window.
    non_empty_word0: AtomicUsize,
    non_empty_word1: AtomicUsize,
    non_empty_word2: AtomicUsize,
    non_empty_word3: AtomicUsize,
}

impl FreeList {
    const fn new() -> Self {
        Self {
            lists: AtomicPtr::new(null_mut()),
            non_empty_word0: AtomicUsize::new(0),
            non_empty_word1: AtomicUsize::new(0),
            non_empty_word2: AtomicUsize::new(0),
            non_empty_word3: AtomicUsize::new(0),
        }
    }

    #[inline]
    fn bitmap_word_bits() -> usize {
        FREE_LIST_BITMAP_WORD_BITS
    }

    #[inline]
    fn bitmap_word(&self, word_idx: usize) -> Option<&AtomicUsize> {
        match word_idx {
            0 => Some(&self.non_empty_word0),
            1 => Some(&self.non_empty_word1),
            2 => Some(&self.non_empty_word2),
            3 => Some(&self.non_empty_word3),
            _ => None,
        }
    }

    #[inline]
    fn bitmap_bit_for_idx(idx: usize) -> Option<(usize, usize)> {
        if idx >= BACKEND_MAX_PAGE {
            return None;
        }
        let bits = Self::bitmap_word_bits();
        let word_idx = idx / bits;
        let bit = idx % bits;
        if word_idx >= FREE_LIST_BITMAP_WORDS {
            return None;
        }
        Some((word_idx, bit))
    }

    #[inline]
    fn mark_list_non_empty(&self, idx: usize) {
        if let Some((word_idx, bit)) = Self::bitmap_bit_for_idx(idx) {
            if let Some(word) = self.bitmap_word(word_idx) {
                word.fetch_or(1usize << bit, Ordering::Release);
            }
        }
    }

    #[inline]
    fn mark_list_empty(&self, idx: usize) {
        if let Some((word_idx, bit)) = Self::bitmap_bit_for_idx(idx) {
            if let Some(word) = self.bitmap_word(word_idx) {
                word.fetch_and(!(1usize << bit), Ordering::AcqRel);
            }
        }
    }

    #[inline]
    fn low_bits_mask(bits: usize) -> usize {
        if bits >= Self::bitmap_word_bits() {
            usize::MAX
        } else if bits == 0 {
            0
        } else {
            (1usize << bits) - 1
        }
    }

    fn next_non_empty_index(&self, start_idx: usize, end_idx: usize) -> Option<usize> {
        let end_idx = core::cmp::min(end_idx, BACKEND_MAX_PAGE);
        if start_idx >= end_idx {
            return None;
        }

        let bits = Self::bitmap_word_bits();
        let mut word_idx = start_idx / bits;
        while let Some(word) = self.bitmap_word(word_idx) {
            let word_start_idx = word_idx.checked_mul(bits)?;
            if word_start_idx >= end_idx {
                break;
            }

            let mut mask = word.load(Ordering::Acquire);
            let first_bit = if start_idx > word_start_idx {
                start_idx - word_start_idx
            } else {
                0
            };
            if first_bit > 0 {
                mask &= usize::MAX << first_bit;
            }

            let last_exclusive = core::cmp::min(end_idx - word_start_idx, bits);
            mask &= Self::low_bits_mask(last_exclusive);

            if mask != 0 {
                return Some(word_start_idx + mask.trailing_zeros() as usize);
            }
            word_idx += 1;
        }
        None
    }

    fn remove_run_markers(
        rd_tree: &mut RadixTree,
        start_addr: usize,
        idx: usize,
    ) -> Result<(), AllocError> {
        let start_key = checked_radix_key(start_addr)?;
        let end_key = match checked_page_addr(start_addr, idx).map(checked_radix_key) {
            Some(Ok(key)) => key,
            _ => return Err(AllocError::EFATAL),
        };
        let expected_value = run_marker_value(idx)?;
        if rd_tree.get_mut(start_key) != expected_value {
            return Err(AllocError::EFATAL);
        }
        if end_key != start_key && rd_tree.get_mut(end_key) != expected_value {
            return Err(AllocError::EFATAL);
        }
        radix_tree_result(rd_tree.remove(start_key, 1))?;
        if end_key != start_key {
            if let Err(err) = radix_tree_result(rd_tree.remove(end_key, 1)) {
                let _ = rd_tree.insert(start_key, expected_value, 1);
                return Err(err);
            }
        }
        Ok(())
    }

    fn insert_run_markers(
        rd_tree: &mut RadixTree,
        start_addr: usize,
        idx: usize,
        rd_value: i64,
    ) -> Result<(), AllocError> {
        let end_addr = checked_page_addr(start_addr, idx).ok_or(AllocError::EFATAL)?;
        let start_key = checked_radix_key(start_addr)?;
        let end_key = checked_radix_key(end_addr)?;
        if rd_tree.get_mut(start_key) != 0 {
            return Err(AllocError::EFATAL);
        }
        if end_key != start_key && rd_tree.get_mut(end_key) != 0 {
            return Err(AllocError::EFATAL);
        }
        radix_tree_result(rd_tree.insert(start_key, rd_value, 1))?;
        if end_key != start_key {
            if let Err(err) = radix_tree_result(rd_tree.insert(end_key, rd_value, 1)) {
                let _ = rd_tree.remove(start_key, 1);
                return Err(err);
            }
        }
        Ok(())
    }

    fn remove_one_locked(&self, idx: usize, rd_tree: &mut RadixTree) -> Option<*mut u8> {
        let cur: &Mutex<Option<&'static mut DoubleLinkedList>> =
            self.try_get_slice().ok()?.get(idx)?;
        let mut locked = cur.lock();
        let expected_pages = idx.checked_add(1)?;
        let mut pruned_markerless_heads = 0usize;

        loop {
            let head_ptr = match Self::locked_head_ptr(&locked) {
                Some(ptr) => ptr,
                None => {
                    self.mark_list_empty(idx);
                    return None;
                }
            };
            let head_addr = head_ptr as usize;
            let key = match checked_radix_key(head_addr) {
                Ok(key) => key,
                Err(_) => {
                    self.mark_list_non_empty(idx);
                    return None;
                }
            };

            match marker_page_count(rd_tree.get_mut(key)) {
                Some(pages) if pages == expected_pages => {
                    if Self::remove_run_markers(rd_tree, head_addr, idx).is_err() {
                        self.mark_list_non_empty(idx);
                        return None;
                    }
                    if let Err(_err) = Self::detach_locked_head(&mut locked) {
                        if let Ok(rd_value) = run_marker_value(idx) {
                            let _ = Self::insert_run_markers(rd_tree, head_addr, idx, rd_value);
                        }
                        self.mark_list_non_empty(idx);
                        return None;
                    }
                    if locked.is_some() {
                        self.mark_list_non_empty(idx);
                    } else {
                        self.mark_list_empty(idx);
                    }
                    return Some(head_addr as *mut u8);
                }
                Some(_) => {
                    // A marker for another size class means the list/radix
                    // relationship is inconsistent.  Do not guess ownership of
                    // that run; fail closed so callers can use the normal
                    // fallback path without corrupting the marker table.
                    self.mark_list_non_empty(idx);
                    return None;
                }
                None => {
                    if pruned_markerless_heads >= EXACT_RUN_MARKERLESS_HEAD_PRUNE_LIMIT {
                        self.mark_list_non_empty(idx);
                        return None;
                    }
                    if Self::detach_locked_head(&mut locked).is_err() {
                        self.mark_list_non_empty(idx);
                        return None;
                    }
                    pruned_markerless_heads += 1;
                    if locked.is_none() {
                        self.mark_list_empty(idx);
                        return None;
                    }
                    self.mark_list_non_empty(idx);
                }
            }
        }
    }

    #[inline]
    fn locked_head_ptr(
        locked: &Option<&'static mut DoubleLinkedList>,
    ) -> Option<*mut DoubleLinkedList> {
        locked
            .as_ref()
            .map(|head| *head as *const DoubleLinkedList as *mut DoubleLinkedList)
    }

    fn detach_locked_head(
        locked: &mut Option<&'static mut DoubleLinkedList>,
    ) -> Result<*mut DoubleLinkedList, AllocError> {
        let head = locked.take().ok_or(AllocError::EFATAL)?;
        let head_ptr = head as *const DoubleLinkedList as *mut DoubleLinkedList;
        match head.remove_current() {
            Ok(Some(next)) => {
                *locked = Some(DoubleLinkedList::try_get_ref(next)?);
            }
            Ok(None) => {
                *locked = None;
            }
            Err(err) => {
                *locked = Some(head);
                return Err(err);
            }
        }
        Ok(head_ptr)
    }

    fn remove_one(&self, idx: usize) -> Option<*mut u8> {
        try_with_rd_tree(|rd_tree| self.remove_one_locked(idx, rd_tree))
            .ok()
            .and_then(|ptr| ptr)
    }

    fn remove_spec_one(
        &self,
        idx: usize,
        ptr: *mut DoubleLinkedList,
        rd_tree: &mut RadixTree,
    ) -> Option<*mut u8> {
        let cur: &Mutex<Option<&'static mut DoubleLinkedList>> =
            self.try_get_slice().ok()?.get(idx)?;
        let mut locked = cur.lock();
        let key = checked_radix_key(ptr as usize).ok()?;
        let expected_pages = idx.checked_add(1)?;
        if marker_page_count(rd_tree.get_mut(key)) != Some(expected_pages) {
            return None;
        }

        let head_ptr = Self::locked_head_ptr(&locked)?;
        let target = DoubleLinkedList::try_get_ref(ptr).ok()?;
        let target_is_head = head_ptr == ptr;
        if !Self::target_topology_allows_removal(target, ptr, head_ptr) {
            return None;
        }

        if Self::remove_run_markers(rd_tree, ptr as usize, idx).is_err() {
            return None;
        }

        match target.remove_current() {
            Ok(Some(new_head)) if target_is_head => match DoubleLinkedList::try_get_ref(new_head) {
                Ok(new_head) => {
                    *locked = Some(new_head);
                    self.mark_list_non_empty(idx);
                }
                Err(_) => {
                    if let Ok(rd_value) = run_marker_value(idx) {
                        let _ = Self::insert_run_markers(rd_tree, ptr as usize, idx, rd_value);
                    }
                    self.mark_list_non_empty(idx);
                    return None;
                }
            },
            Ok(Some(_)) => {
                // A non-head node must have a previous list link.  Returning a
                // replacement head for a node that is not recorded as the head
                // means the free-list topology is corrupt; keep the run
                // unavailable rather than installing an arbitrary head.
                if let Ok(rd_value) = run_marker_value(idx) {
                    let _ = Self::insert_run_markers(rd_tree, ptr as usize, idx, rd_value);
                }
                self.mark_list_non_empty(idx);
                return None;
            }
            Ok(None) if target_is_head => {
                *locked = None;
                self.mark_list_empty(idx);
            }
            Ok(None) => {
                self.mark_list_non_empty(idx);
            }
            Err(_) => {
                if let Ok(rd_value) = run_marker_value(idx) {
                    let _ = Self::insert_run_markers(rd_tree, ptr as usize, idx, rd_value);
                }
                self.mark_list_non_empty(idx);
                return None;
            }
        }
        Some(ptr as *mut u8)
    }

    fn target_topology_allows_removal(
        target: &DoubleLinkedList,
        ptr: *mut DoubleLinkedList,
        head_ptr: *mut DoubleLinkedList,
    ) -> bool {
        let target_is_head = ptr == head_ptr;
        // Radix markers prove that `ptr` names a free run, but not that the
        // intrusive node still belongs to the locked list we are about to
        // mutate.  Validate immediate neighbors, then prove bounded
        // reachability from the locked head before removing markers.
        if target.prev == Some(ptr) || target.next == Some(ptr) {
            return false;
        }

        match (target_is_head, target.prev) {
            (true, Some(_)) | (false, None) => return false,
            _ => {}
        }

        if let Some(prev) = target.prev {
            match DoubleLinkedList::try_get_ref(prev) {
                Ok(prev_ref) if prev_ref.next == Some(ptr) => {}
                _ => return false,
            }
        }

        if let Some(next) = target.next {
            match DoubleLinkedList::try_get_ref(next) {
                Ok(next_ref) if next_ref.prev == Some(ptr) => {}
                _ => return false,
            }
        }

        if !Self::target_reaches_locked_head_by_backlinks(ptr, head_ptr) {
            return false;
        }

        true
    }

    fn target_reaches_locked_head_by_backlinks(
        ptr: *mut DoubleLinkedList,
        head_ptr: *mut DoubleLinkedList,
    ) -> bool {
        if ptr == head_ptr {
            return true;
        }

        let mut cur = ptr;
        for _ in 0..SPEC_RUN_MEMBERSHIP_BACKLINK_SCAN_LIMIT {
            let cur_ref = match DoubleLinkedList::try_get_ref(cur) {
                Ok(node) => node,
                Err(_) => return false,
            };
            let prev = match cur_ref.prev {
                Some(prev) => prev,
                None => return false,
            };
            let prev_ref = match DoubleLinkedList::try_get_ref(prev) {
                Ok(node) => node,
                Err(_) => return false,
            };
            if prev_ref.next != Some(cur) {
                return false;
            }
            if prev == head_ptr {
                return true;
            }
            cur = prev;
        }

        false
    }

    fn write_list_node(addr: usize) -> Result<&'static mut DoubleLinkedList, AllocError> {
        if addr == 0 || addr % align_of::<DoubleLinkedList>() != 0 {
            return Err(AllocError::EFATAL);
        }
        unsafe {
            let ptr = addr as *mut DoubleLinkedList;
            core::ptr::write(ptr, DoubleLinkedList::new());
            DoubleLinkedList::try_get_ref(ptr)
        }
    }

    fn insert_one_locked(
        &self,
        idx: usize,
        node: &'static mut DoubleLinkedList,
        rd_tree: &mut RadixTree,
    ) -> Result<(), AllocError> {
        let cur: &Mutex<Option<&'static mut DoubleLinkedList>> =
            self.try_get_slice()?.get(idx).ok_or(AllocError::EOOB)?;
        let rd_value = run_marker_value(idx)?;
        Self::insert_run_markers(rd_tree, node as *const _ as usize, idx, rd_value)?;
        let mut locked = cur.lock();
        if let Some(to_remove) = locked.take() {
            if let Err(err) = to_remove.push_before_head(node) {
                *locked = Some(to_remove);
                let _ = Self::remove_run_markers(rd_tree, node as *const _ as usize, idx);
                return Err(err);
            }
        }
        *locked = Some(node);
        self.mark_list_non_empty(idx);
        Ok(())
    }

    fn insert_one(
        &self,
        idx: usize,
        node: &'static mut DoubleLinkedList,
    ) -> Result<(), AllocError> {
        match try_with_rd_tree(move |rd_tree| self.insert_one_locked(idx, node, rd_tree)) {
            Ok(result) => result,
            Err(_) => Err(AllocError::EFATAL),
        }
    }

    fn aligned_payload_within_run(
        run_addr: usize,
        run_pages: usize,
        payload_pages: usize,
        align: usize,
    ) -> Option<AlignedRunCandidate> {
        let aligned_addr = checked_round_up_addr(run_addr, align)?;
        let prefix_size = aligned_addr.checked_sub(run_addr)?;
        if prefix_size % PAGE_SIZE != 0 {
            return None;
        }
        let prefix_pages = prefix_size / PAGE_SIZE;
        let used_pages = prefix_pages.checked_add(payload_pages)?;
        if used_pages > run_pages {
            return None;
        }
        Some(AlignedRunCandidate {
            ptr: run_addr as *mut DoubleLinkedList,
            aligned_addr,
            run_pages,
            prefix_pages,
            suffix_pages: run_pages - used_pages,
        })
    }

    fn stale_skip_contains(
        stale_skips: &[Option<(usize, *mut DoubleLinkedList)>],
        idx: usize,
        ptr: *mut DoubleLinkedList,
    ) -> bool {
        stale_skips.iter().any(|entry| {
            matches!(entry, Some((skip_idx, skip_ptr)) if *skip_idx == idx && *skip_ptr == ptr)
        })
    }

    fn find_aligned_run_candidate_locked(
        &self,
        idx: usize,
        align: usize,
        payload_pages: usize,
        stale_skips: &[Option<(usize, *mut DoubleLinkedList)>],
        max_nodes: usize,
    ) -> Result<Option<AlignedRunCandidate>, AllocError> {
        let cur: &Mutex<Option<&'static mut DoubleLinkedList>> =
            self.try_get_slice()?.get(idx).ok_or(AllocError::EOOB)?;
        let locked = cur.lock();
        let mut next = locked
            .as_ref()
            .map(|head| *head as *const DoubleLinkedList as *mut DoubleLinkedList);
        if next.is_none() {
            self.mark_list_empty(idx);
            return Ok(None);
        }
        let run_pages = idx.checked_add(1).ok_or(AllocError::ESIZE)?;

        let traversal_limit = max_nodes
            .checked_add(stale_skips.len())
            .ok_or(AllocError::EFATAL)?;
        let mut traversed = 0usize;
        let mut inspected = 0usize;
        let mut best = None;
        while let Some(ptr) = next {
            if traversed >= traversal_limit {
                break;
            }
            traversed = traversed.checked_add(1).ok_or(AllocError::EFATAL)?;
            let node = DoubleLinkedList::try_get_ref(ptr)?;
            let addr = ptr as usize;
            next = node.next;
            // Retry after stale best-fit candidates must exclude only the
            // exact failed nodes.  Earlier same-class candidates may have lost
            // the first best-fit comparison to stale nodes and are still valid
            // reuse opportunities; evaluating them avoids unnecessary fallback
            // to larger/new page runs.  Skipped nodes do not count against the
            // useful-candidate budget, but they do count against the separate
            // traversal budget above.  That keeps the documented "one extra
            // linked node per stale pointer" behavior without letting a stale
            // cycle hold this size-class lock indefinitely.
            if Self::stale_skip_contains(stale_skips, idx, ptr) {
                continue;
            }
            if inspected >= max_nodes {
                break;
            }
            inspected = inspected.checked_add(1).ok_or(AllocError::EFATAL)?;
            if let Some(candidate) =
                Self::aligned_payload_within_run(addr, run_pages, payload_pages, align)
            {
                if candidate.fragment_count() == 0 {
                    return Ok(Some(candidate));
                }
                if best
                    .as_ref()
                    .map(|current| candidate.is_less_fragmenting_than(current))
                    .unwrap_or(true)
                {
                    best = Some(candidate);
                }
            }
        }

        Ok(best)
    }

    fn find_best_aligned_run_candidate_locked(
        &self,
        start_idx: usize,
        list_len: usize,
        align: usize,
        payload_pages: usize,
        stale_skips: &[Option<(usize, *mut DoubleLinkedList)>],
    ) -> Result<Option<AlignedRunCandidate>, AllocError> {
        let mut scan_idx = start_idx;
        let mut lookahead_end = None;
        let mut best = None;

        while let Some(idx) = self.next_non_empty_index(scan_idx, list_len) {
            if let Some(end) = lookahead_end {
                if idx >= end {
                    break;
                }
            }

            if let Some(candidate) = self.find_aligned_run_candidate_locked(
                idx,
                align,
                payload_pages,
                stale_skips,
                ALIGNED_RUN_CANDIDATE_SCAN_NODES,
            )? {
                if lookahead_end.is_none() {
                    lookahead_end = Some(
                        idx.saturating_add(ALIGNED_RUN_FRAGMENT_LOOKAHEAD_CLASSES)
                            .saturating_add(1)
                            .min(list_len),
                    );
                }
                if candidate.fragment_count() == 0 {
                    return Ok(Some(candidate));
                }
                if best
                    .as_ref()
                    .map(|current| candidate.is_less_fragmenting_than(current))
                    .unwrap_or(true)
                {
                    best = Some(candidate);
                }
            }

            scan_idx = idx.checked_add(1).ok_or(AllocError::ESIZE)?;
        }

        Ok(best)
    }

    fn insert_split_run_locked(
        &self,
        split_addr: usize,
        split_pages: usize,
        rd_tree: &mut RadixTree,
    ) -> Result<(), AllocError> {
        if split_pages == 0 {
            return Ok(());
        }

        let idx = split_pages.checked_sub(1).ok_or(AllocError::ESIZE)?;
        if idx >= self.try_get_slice()?.len() {
            return Err(AllocError::EOOB);
        }
        let node = Self::write_list_node(split_addr)?;
        self.insert_one_locked(idx, node, rd_tree)
    }

    fn remove_split_run_locked(
        &self,
        split_addr: usize,
        split_pages: usize,
        rd_tree: &mut RadixTree,
    ) {
        if split_pages == 0 {
            return;
        }
        if let Some(idx) = split_pages.checked_sub(1) {
            let _ = self.remove_spec_one(idx, split_addr as *mut DoubleLinkedList, rd_tree);
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn insert_aligned_fallback_split_locked(
        &self,
        split_addr: usize,
        split_pages: usize,
        rd_tree: &mut RadixTree,
        _list_len: usize,
    ) -> Result<(), AllocError> {
        self.insert_split_run_locked(split_addr, split_pages, rd_tree)
    }

    #[cfg(feature = "fixed_heap")]
    fn insert_aligned_fallback_split_locked(
        &self,
        split_addr: usize,
        split_pages: usize,
        rd_tree: &mut RadixTree,
        list_len: usize,
    ) -> Result<(), AllocError> {
        // Fixed heaps cannot hand oversized slack back to the OS.  When a large
        // alignment leaves more pages than one free-list class can represent,
        // publish the slack as max-size chunks plus a tail chunk, matching the
        // normal fixed-heap `free` path.
        self.insert_fixed_heap_oversized_run_chunks_locked(
            split_addr,
            split_pages,
            rd_tree,
            list_len,
        )
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn remove_aligned_fallback_split_locked(
        &self,
        split_addr: usize,
        split_pages: usize,
        rd_tree: &mut RadixTree,
        _list_len: usize,
    ) {
        self.remove_split_run_locked(split_addr, split_pages, rd_tree);
    }

    #[cfg(feature = "fixed_heap")]
    fn remove_aligned_fallback_split_locked(
        &self,
        split_addr: usize,
        split_pages: usize,
        rd_tree: &mut RadixTree,
        list_len: usize,
    ) {
        self.remove_fixed_heap_oversized_run_chunks_locked(
            split_addr,
            split_pages,
            rd_tree,
            list_len,
        );
    }

    fn publish_aligned_fallback_splits(
        &self,
        raw_addr: usize,
        aligned_addr: usize,
        payload_end: usize,
        raw_end: usize,
    ) -> Result<(), AllocError> {
        let prefix_pages = aligned_addr
            .checked_sub(raw_addr)
            .filter(|bytes| bytes % PAGE_SIZE == 0)
            .map(|bytes| bytes / PAGE_SIZE)
            .ok_or(AllocError::EFATAL)?;
        let suffix_pages = raw_end
            .checked_sub(payload_end)
            .filter(|bytes| bytes % PAGE_SIZE == 0)
            .map(|bytes| bytes / PAGE_SIZE)
            .ok_or(AllocError::EFATAL)?;

        if prefix_pages == 0 && suffix_pages == 0 {
            return Ok(());
        }

        match try_with_rd_tree(|rd_tree| {
            let list_len = self.try_get_slice()?.len();
            self.insert_aligned_fallback_split_locked(raw_addr, prefix_pages, rd_tree, list_len)?;
            if let Err(err) = self.insert_aligned_fallback_split_locked(
                payload_end,
                suffix_pages,
                rd_tree,
                list_len,
            ) {
                self.remove_aligned_fallback_split_locked(
                    raw_addr,
                    prefix_pages,
                    rd_tree,
                    list_len,
                );
                return Err(err);
            }
            Ok(())
        }) {
            Ok(result) => result,
            Err(_) => Err(AllocError::EFATAL),
        }
    }

    #[cfg(feature = "fixed_heap")]
    fn remove_fixed_heap_oversized_run_chunks_locked(
        &self,
        start_addr: usize,
        mut pages: usize,
        rd_tree: &mut RadixTree,
        list_len: usize,
    ) {
        if list_len == 0 {
            return;
        }

        let mut addr = start_addr;
        while pages != 0 {
            let chunk_pages = core::cmp::min(pages, list_len);
            let idx = match chunk_pages.checked_sub(1) {
                Some(idx) => idx,
                None => return,
            };
            let _ = self.remove_spec_one(idx, addr as *mut DoubleLinkedList, rd_tree);
            pages -= chunk_pages;
            if pages != 0 {
                addr = match checked_page_addr(addr, chunk_pages) {
                    Some(next_addr) => next_addr,
                    None => return,
                };
            }
        }
    }

    #[cfg(feature = "fixed_heap")]
    fn insert_fixed_heap_oversized_run_chunks_locked(
        &self,
        start_addr: usize,
        pages: usize,
        rd_tree: &mut RadixTree,
        list_len: usize,
    ) -> Result<(), AllocError> {
        self.insert_fixed_heap_oversized_run_chunks_locked_impl(
            start_addr, pages, rd_tree, list_len, None,
        )
    }

    #[cfg(all(test, feature = "fixed_heap"))]
    fn insert_fixed_heap_oversized_run_chunks_locked_with_test_failure(
        &self,
        start_addr: usize,
        pages: usize,
        rd_tree: &mut RadixTree,
        list_len: usize,
        fail_after_inserted_chunks: usize,
    ) -> Result<(), AllocError> {
        self.insert_fixed_heap_oversized_run_chunks_locked_impl(
            start_addr,
            pages,
            rd_tree,
            list_len,
            Some(fail_after_inserted_chunks),
        )
    }

    #[cfg(feature = "fixed_heap")]
    fn insert_fixed_heap_oversized_run_chunks_locked_impl(
        &self,
        start_addr: usize,
        mut pages: usize,
        rd_tree: &mut RadixTree,
        list_len: usize,
        fail_after_inserted_chunks: Option<usize>,
    ) -> Result<(), AllocError> {
        if list_len == 0 || pages == 0 {
            return Ok(());
        }

        let total_pages = pages;
        let mut inserted_chunks = 0usize;
        let mut inserted_pages = 0usize;
        let mut addr = start_addr;
        while pages != 0 {
            let chunk_pages = core::cmp::min(pages, list_len);
            let idx = chunk_pages.checked_sub(1).ok_or(AllocError::ESIZE)?;
            let node = Self::write_list_node(addr)?;
            if let Err(err) = self.insert_one_locked(idx, node, rd_tree) {
                // `free` cannot report an error to its caller.  Avoid leaving a
                // half-published oversized run in the fixed-heap freelist: any
                // chunks inserted before the failure are removed again.  The
                // original run is then conservatively quarantined rather than
                // exposing inconsistent radix/list state.
                self.remove_fixed_heap_oversized_run_chunks_locked(
                    start_addr,
                    inserted_pages,
                    rd_tree,
                    list_len,
                );
                return Err(err);
            }
            pages -= chunk_pages;
            inserted_pages = total_pages - pages;
            inserted_chunks = inserted_chunks.checked_add(1).ok_or(AllocError::EFATAL)?;
            if fail_after_inserted_chunks == Some(inserted_chunks) {
                // Test-only fault injection exercises the same rollback path
                // used by real insertion failures without relying on corrupt
                // radix-tree state.  The helper remains private, so production
                // callers always pass `None`.
                self.remove_fixed_heap_oversized_run_chunks_locked(
                    start_addr,
                    inserted_pages,
                    rd_tree,
                    list_len,
                );
                return Err(AllocError::EFATAL);
            }
            if pages != 0 {
                addr = checked_page_addr(addr, chunk_pages).ok_or_else(|| {
                    self.remove_fixed_heap_oversized_run_chunks_locked(
                        start_addr,
                        inserted_pages,
                        rd_tree,
                        list_len,
                    );
                    AllocError::EFATAL
                })?;
            }
        }
        Ok(())
    }

    fn remove_head_aligned_run_locked(
        &self,
        payload_pages: usize,
        align: usize,
        rd_tree: &mut RadixTree,
    ) -> Result<Option<*mut u8>, AllocError> {
        let first_idx = payload_pages.checked_sub(1).ok_or(AllocError::ESIZE)?;
        let list_len = self.try_get_slice()?.len();
        if first_idx >= list_len {
            return Ok(None);
        }
        let payload_size = checked_page_size_from_count(payload_pages)?;

        // For over-page-aligned large allocations, first consume the smallest
        // free run that contains an aligned payload window.  The older path
        // always requested `payload + align - 1` pages and then split
        // prefix/suffix slack, so it skipped reusable runs where the aligned
        // payload starts inside an existing free run and could grow the page
        // heap unnecessarily.
        //
        // Reusing an interior aligned window has a bounded split shape:
        // optional prefix, aligned payload, optional suffix.  If either split
        // insertion fails, best-effort rollback removes already inserted split
        // pieces and restores the original free run before returning the error.
        let mut stale_skips: [Option<(usize, *mut DoubleLinkedList)>;
            ALIGNED_RUN_STALE_SKIP_TOTAL_CAPACITY] = [None; ALIGNED_RUN_STALE_SKIP_TOTAL_CAPACITY];
        let mut stale_skip_len = 0usize;
        while let Some(candidate_run) = self.find_best_aligned_run_candidate_locked(
            first_idx,
            list_len,
            align,
            payload_pages,
            &stale_skips[..stale_skip_len],
        )? {
            let idx = candidate_run.run_idx()?;
            let suffix_addr = if candidate_run.suffix_pages != 0 {
                Some(
                    candidate_run
                        .aligned_addr
                        .checked_add(payload_size)
                        .ok_or(AllocError::EFATAL)?,
                )
            } else {
                None
            };
            if let Some(ans) = self.remove_spec_one(idx, candidate_run.ptr, rd_tree) {
                let original_addr = ans as usize;
                debug_assert_eq!(original_addr, candidate_run.ptr as usize);
                debug_assert_eq!(
                    original_addr + candidate_run.prefix_pages * PAGE_SIZE,
                    candidate_run.aligned_addr
                );

                let rollback_original =
                    |this: &Self, rd_tree: &mut RadixTree| -> Result<(), AllocError> {
                        let node = Self::write_list_node(original_addr)?;
                        this.insert_one_locked(idx, node, rd_tree)
                    };

                if let Err(err) =
                    self.insert_split_run_locked(original_addr, candidate_run.prefix_pages, rd_tree)
                {
                    let _ = rollback_original(self, rd_tree);
                    return Err(err);
                }

                if let Some(suffix_addr) = suffix_addr {
                    if let Err(err) = self.insert_split_run_locked(
                        suffix_addr,
                        candidate_run.suffix_pages,
                        rd_tree,
                    ) {
                        if candidate_run.prefix_pages != 0 {
                            let _ = self.remove_spec_one(
                                candidate_run.prefix_pages - 1,
                                original_addr as *mut DoubleLinkedList,
                                rd_tree,
                            );
                        }
                        let _ = rollback_original(self, rd_tree);
                        return Err(err);
                    }
                }

                // `ans` may be the start of a split prefix.  Return the
                // aligned payload address, not necessarily the free-run
                // head, so callers can satisfy the requested alignment
                // without allocating a new over-sized run.
                return Ok(Some(candidate_run.aligned_addr as *mut u8));
            }
            // Another thread may have raced us after we dropped the run-list
            // lock inside candidate selection; rescan the bounded window and
            // ignore only recorded stale nodes on the next pass.  The skip set
            // is transient allocation-attempt state derived from the bounded
            // search window.  After the normal skip window fills, keep only one
            // additional per-class scan budget of continuation skips before
            // failing open to the caller fallback, avoiding livelock while
            // still reaching nearby valid candidates in stale-heavy races.
            if stale_skip_len >= stale_skips.len() {
                return Ok(None);
            }
            stale_skips[stale_skip_len] = Some((idx, candidate_run.ptr));
            stale_skip_len = stale_skip_len.checked_add(1).ok_or(AllocError::EFATAL)?;
        }

        Ok(None)
    }

    fn remove_head_aligned_run(
        &self,
        payload_pages: usize,
        align: usize,
    ) -> Result<Option<*mut u8>, AllocError> {
        if !self.free_list_metadata_initialized() {
            return Ok(None);
        }
        match try_with_rd_tree(|rd_tree| {
            self.remove_head_aligned_run_locked(payload_pages, align, rd_tree)
        }) {
            Ok(result) => result,
            Err(_) => Err(AllocError::EFATAL),
        }
    }

    pub fn alloc(&self, size: usize) -> Result<*mut u8, AllocError> {
        let origin_size = page_run_index_for_size(size)?;
        let list_len = self
            .try_get_existing_slice()
            .map(|slice| slice.len())
            .unwrap_or(0);
        if origin_size < list_len {
            if let Some(ans) = self.remove_one(origin_size) {
                return Ok(ans);
            }
            // Try larger free runs only when the free-list metadata already
            // exists.  A cold allocator with no published free runs should not
            // allocate the whole metadata slice just to discover an empty list.
            let mut parent_scan = self.next_non_empty_index(origin_size + 1, list_len);
            while let Some(parent_idx) = parent_scan {
                if let Some(parent) = self.remove_one(parent_idx) {
                    let remain = checked_page_addr(parent as usize, origin_size + 1)
                        .ok_or(AllocError::EFATAL)?;
                    let node = match Self::write_list_node(remain) {
                        Ok(node) => node,
                        Err(err) => {
                            if let Ok(parent_node) = Self::write_list_node(parent as usize) {
                                let _ = self.insert_one(parent_idx, parent_node);
                            }
                            return Err(err);
                        }
                    };
                    if let Err(err) = self.insert_one(parent_idx - origin_size - 1, node) {
                        if let Ok(parent_node) = Self::write_list_node(parent as usize) {
                            let _ = self.insert_one(parent_idx, parent_node);
                        }
                        return Err(err);
                    }
                    return Ok(parent);
                }
                parent_scan = self.next_non_empty_index(
                    parent_idx.checked_add(1).ok_or(AllocError::ESIZE)?,
                    list_len,
                );
            }
        }
        BUMP.lock().alloc(size)
    }

    pub fn alloc_aligned(&self, size: usize, align: usize) -> Result<*mut u8, AllocError> {
        if align <= PAGE_SIZE {
            return self.alloc(size);
        }
        if align % PAGE_SIZE != 0 || !align.is_power_of_two() {
            return Err(AllocError::ESIZE);
        }

        let payload_pages = page_run_count_for_size(size)?;
        let align_pages = align.checked_div(PAGE_SIZE).ok_or(AllocError::ESIZE)?;
        let total_pages = payload_pages
            .checked_add(align_pages.checked_sub(1).ok_or(AllocError::ESIZE)?)
            .ok_or(AllocError::ESIZE)?;
        let total_size = checked_page_size_from_count(total_pages)?;
        let payload_size = checked_page_size_from_count(payload_pages)?;

        if let Some(reused) = self.remove_head_aligned_run(payload_pages, align)? {
            return Ok(reused);
        }

        for _ in 0..ALIGNED_FALLBACK_SPLIT_PUBLISH_RETRIES {
            let raw = self.alloc(total_size)?;
            let raw_addr = raw as usize;
            let aligned_addr = match checked_round_up_addr(raw_addr, align) {
                Some(addr) => addr,
                None => {
                    self.free(raw, total_size);
                    return Err(AllocError::EFATAL);
                }
            };
            let raw_end = match raw_addr.checked_add(total_size) {
                Some(end) => end,
                None => {
                    self.free(raw, total_size);
                    return Err(AllocError::EFATAL);
                }
            };
            let payload_end = match aligned_addr.checked_add(payload_size) {
                Some(end) => end,
                None => {
                    self.free(raw, total_size);
                    return Err(AllocError::EFATAL);
                }
            };
            if aligned_addr < raw_addr
                || payload_end > raw_end
                || (aligned_addr - raw_addr) % PAGE_SIZE != 0
                || (raw_end - payload_end) % PAGE_SIZE != 0
            {
                self.free(raw, total_size);
                return Err(AllocError::EFATAL);
            }

            let split_result =
                self.publish_aligned_fallback_splits(raw_addr, aligned_addr, payload_end, raw_end);
            match split_result {
                Ok(()) => return Ok(aligned_addr as *mut u8),
                Err(_) => self.free(raw, total_size),
            }
        }

        Err(AllocError::EFATAL)
    }

    pub fn free(&self, ptr: *mut u8, size: usize) {
        #[cfg(all(test, not(feature = "fixed_heap")))]
        self.free_impl(ptr, size, HostedFreeFaultForTest::None);
        #[cfg(not(all(test, not(feature = "fixed_heap"))))]
        self.free_impl(ptr, size);
    }

    #[cfg(all(test, not(feature = "fixed_heap")))]
    fn free_with_hosted_metadata_failure_for_test(&self, ptr: *mut u8, size: usize) {
        // Hosted-only fault injection for the metadata-allocation failure path.
        // The test still allocates and probes a real OS mapping; only the
        // freelist metadata publication step is forced to fail.
        self.free_impl(ptr, size, HostedFreeFaultForTest::MetadataUnavailable);
    }

    #[cfg(all(test, not(feature = "fixed_heap")))]
    fn free_with_hosted_insert_failure_for_test(&self, ptr: *mut u8, size: usize) {
        // Hosted-only fault injection for rollback coverage.  The production
        // `free_impl` signature has no test flag and compiles without this branch.
        self.free_impl(ptr, size, HostedFreeFaultForTest::InsertFailure);
    }

    fn free_impl(
        &self,
        ptr: *mut u8,
        size: usize,
        #[cfg(all(test, not(feature = "fixed_heap")))]
        hosted_fault_for_test: HostedFreeFaultForTest,
    ) {
        if ptr.is_null() {
            return;
        }
        let origin_size = match page_run_index_for_size(size) {
            Ok(size) => size,
            Err(_) => return,
        };

        let mut final_ptr = ptr;
        let mut final_idx = origin_size;
        #[cfg(all(
            feature = "adaptive_bitmap_page_allocator",
            not(feature = "fixed_heap")
        ))]
        let mut merged_previous = false;
        #[cfg(all(
            feature = "adaptive_bitmap_page_allocator",
            not(feature = "fixed_heap")
        ))]
        let mut merged_next = false;
        #[cfg(not(feature = "fixed_heap"))]
        let mut unmap: Option<(*mut u8, usize)> = None;
        #[cfg(feature = "fixed_heap")]
        let unmap: Option<(*mut u8, usize)> = None;
        let rd_tree_result = try_with_rd_tree(|rd_tree| {
            #[cfg(all(test, not(feature = "fixed_heap")))]
            if hosted_fault_for_test == HostedFreeFaultForTest::MetadataUnavailable {
                unmap = hosted_unmap_run_from_idx(final_ptr, final_idx);
                return;
            }
            let list_len = match self.try_get_slice() {
                Ok(slice) => slice.len(),
                Err(_) => {
                    #[cfg(not(feature = "fixed_heap"))]
                    {
                        unmap = hosted_unmap_run_from_idx(final_ptr, final_idx);
                    }
                    return;
                }
            };
            //check prev
            if let Some(prev) = (ptr as usize).checked_sub(PAGE_SIZE) {
                if let Some(pflag_pages) = checked_radix_key(prev)
                    .ok()
                    .and_then(|key| marker_page_count(rd_tree.get_mut(key)))
                {
                    let start = match checked_page_offset(pflag_pages)
                        .and_then(|offset| (ptr as usize).checked_sub(offset))
                    {
                        Some(start) => start,
                        None => return,
                    };
                    if let Some(prev_ptr) = self.remove_spec_one(
                        pflag_pages - 1,
                        start as *mut DoubleLinkedList,
                        rd_tree,
                    ) {
                        //successfully combine with previous
                        final_ptr = prev_ptr;
                        #[cfg(all(
                            feature = "adaptive_bitmap_page_allocator",
                            not(feature = "fixed_heap")
                        ))]
                        {
                            merged_previous = true;
                        }
                        final_idx = match final_idx.checked_add(pflag_pages) {
                            Some(idx) => idx,
                            None => return,
                        };
                    }
                }
            }
            //check next
            if let Some(next) = checked_page_addr(ptr as usize, origin_size + 1) {
                if let Some(nflag_pages) = checked_radix_key(next)
                    .ok()
                    .and_then(|key| marker_page_count(rd_tree.get_mut(key)))
                {
                    let start = next;
                    if self
                        .remove_spec_one(nflag_pages - 1, start as *mut DoubleLinkedList, rd_tree)
                        .is_some()
                    {
                        //successfully combine with next
                        #[cfg(all(
                            feature = "adaptive_bitmap_page_allocator",
                            not(feature = "fixed_heap")
                        ))]
                        {
                            merged_next = true;
                        }
                        final_idx = match final_idx.checked_add(nflag_pages) {
                            Some(idx) => idx,
                            None => return,
                        };
                    }
                }
            }
            if final_idx < list_len {
                #[cfg(not(feature = "fixed_heap"))]
                {
                    let final_pages = match final_idx.checked_add(1) {
                        Some(pages) => pages,
                        None => return,
                    };
                    if final_pages > hosted_free_run_retain_max_pages() {
                        unmap = hosted_unmap_run_from_idx(final_ptr, final_idx);
                        return;
                    }
                }

                let node = match Self::write_list_node(final_ptr as usize) {
                    Ok(node) => node,
                    Err(_) => {
                        #[cfg(not(feature = "fixed_heap"))]
                        {
                            unmap = hosted_unmap_run_from_idx(final_ptr, final_idx);
                        }
                        return;
                    }
                };
                #[cfg(all(test, not(feature = "fixed_heap")))]
                if hosted_fault_for_test == HostedFreeFaultForTest::InsertFailure {
                    unmap = hosted_unmap_run_from_idx(final_ptr, final_idx);
                    return;
                }

                if self.insert_one_locked(final_idx, node, rd_tree).is_err() {
                    #[cfg(not(feature = "fixed_heap"))]
                    {
                        unmap = hosted_unmap_run_from_idx(final_ptr, final_idx);
                    }
                }
            } else {
                #[cfg(feature = "fixed_heap")]
                {
                    // A fixed heap cannot return pages to the OS.  Do not lose
                    // an oversized freed run just because it is larger than one
                    // free-list size class; split it into reusable maximum-size
                    // chunks plus a tail chunk so smaller/later allocations can
                    // consume the memory again.
                    if let Some(pages) = final_idx.checked_add(1) {
                        let result = self.insert_fixed_heap_oversized_run_chunks_locked(
                            final_ptr as usize,
                            pages,
                            rd_tree,
                            list_len,
                        );
                        debug_assert!(
                            result.is_ok(),
                            "fixed-heap oversized free split failed; run was quarantined"
                        );
                    }
                }
                #[cfg(not(feature = "fixed_heap"))]
                {
                    unmap = hosted_unmap_run_from_idx(final_ptr, final_idx);
                }
            }
        });
        #[cfg(not(feature = "fixed_heap"))]
        if rd_tree_result.is_err() && unmap.is_none() {
            // Hosted free-list publication depends on global radix-tree
            // metadata.  If that metadata is unavailable, the safe policy is
            // to return the just-freed run to the OS instead of silently
            // retaining unreachable pages in this allocator.
            unmap = hosted_unmap_run_from_idx(final_ptr, final_idx);
        }
        #[cfg(feature = "fixed_heap")]
        let _ = rd_tree_result;
        if let Some((ptr, size)) = unmap {
            #[cfg(not(feature = "fixed_heap"))]
            unsafe {
                system_alloc::munmap(ptr, size)
            };
            #[cfg(feature = "fixed_heap")]
            let _ = (ptr, size);
        }
        #[cfg(all(
            feature = "adaptive_bitmap_page_allocator",
            not(feature = "fixed_heap")
        ))]
        if merged_previous && merged_next && unmap.is_none() {
            crate::adaptive_bitmap_alloc::note_freelist_bridge_merge();
        }
    }

    fn try_get_slice(&self) -> Result<&[Mutex<Option<&'static mut DoubleLinkedList>>], AllocError> {
        let mut ptr_val = self.lists.load(Ordering::Acquire);
        if ptr_val.is_null() {
            unsafe {
                let layout = Layout::new::<
                    [Mutex<Option<&'static mut DoubleLinkedList>>; BACKEND_MAX_PAGE],
                >();
                let new_ptr = META_BUMP
                    .lock()
                    .alloc_aligned(layout.size(), layout.align())
                    .map_err(|_| AllocError::ENOMEM)?;
                let slice = core::slice::from_raw_parts_mut(
                    new_ptr as *mut Mutex<Option<&'static mut DoubleLinkedList>>,
                    BACKEND_MAX_PAGE,
                );
                #[allow(clippy::declare_interior_mutable_const)]
                const VAL: Mutex<Option<&'static mut DoubleLinkedList>> = Mutex::new(None);
                for s in slice {
                    *s = VAL;
                }
                if let Err(real_ptr) = self.lists.compare_exchange(
                    null_mut(),
                    new_ptr as *mut Mutex<Option<&'static mut DoubleLinkedList>>,
                    Ordering::AcqRel,
                    Ordering::Relaxed,
                ) {
                    // CAS losers cannot individually release bump metadata, but
                    // they must report the actual slice size so undersized
                    // freelist metadata never enters the ThreadCache-sized
                    // reusable backup list.
                    META_BUMP
                        .lock()
                        .dealloc(new_ptr as *mut usize, layout.size());
                    ptr_val = real_ptr;
                } else {
                    ptr_val = new_ptr as *mut Mutex<Option<&'static mut DoubleLinkedList>>;
                }
            }
        }
        if ptr_val.is_null() {
            return Err(AllocError::ENOMEM);
        }
        Ok(unsafe { core::slice::from_raw_parts(ptr_val, BACKEND_MAX_PAGE) })
    }

    #[inline]
    fn free_list_metadata_initialized(&self) -> bool {
        !self.lists.load(Ordering::Acquire).is_null()
    }

    #[inline]
    fn try_get_existing_slice(&self) -> Option<&[Mutex<Option<&'static mut DoubleLinkedList>>]> {
        let ptr_val = self.lists.load(Ordering::Acquire);
        if ptr_val.is_null() {
            None
        } else {
            Some(unsafe { core::slice::from_raw_parts(ptr_val, BACKEND_MAX_PAGE) })
        }
    }
}

#[cfg(test)]
mod tests {
    #[cfg(feature = "fixed_heap")]
    extern crate std;

    use super::*;
    use alloc::boxed::Box;
    #[cfg(not(feature = "fixed_heap"))]
    use alloc::vec::Vec;

    #[cfg(feature = "fixed_heap")]
    fn run_fixed_heap_capacity_test_in_fresh_process(child_env: &str, test_name: &str) -> bool {
        if std::env::var_os(child_env).is_some() {
            return false;
        }
        let output = std::process::Command::new(
            std::env::current_exe().expect("current allocator test executable"),
        )
        .args(["--exact", test_name, "--nocapture", "--test-threads=1"])
        .env(child_env, "1")
        .env("RUST_BACKTRACE", "0")
        .output()
        .expect("spawn isolated fixed-heap capacity test");
        assert!(
            output.status.success(),
            "isolated fixed-heap capacity test failed: {}",
            std::string::String::from_utf8_lossy(&output.stderr)
        );
        true
    }

    #[test]
    fn page_run_index_rejects_zero_and_rounding_overflow() {
        assert!(page_run_index_for_size(0).is_err());
        assert!(page_run_index_for_size(usize::MAX).is_err());
    }

    #[test]
    fn page_run_index_preserves_page_run_geometry() {
        assert_eq!(page_run_index_for_size(1).expect("one byte"), 0);
        assert_eq!(page_run_index_for_size(PAGE_SIZE).expect("one page"), 0);
        assert_eq!(
            page_run_index_for_size(PAGE_SIZE + 1).expect("two pages"),
            1
        );
        assert_eq!(page_run_count_for_size(1).expect("one byte count"), 1);
        assert_eq!(
            checked_page_size_from_count(2).expect("two page bytes"),
            PAGE_SIZE * 2
        );
    }

    #[test]
    fn freelist_radix_key_rejects_overflow_without_wrapping() {
        assert!(checked_radix_key(usize::MAX).is_err());
    }

    #[test]
    fn run_marker_value_rejects_index_overflow_without_wrapping() {
        assert_eq!(run_marker_value(0).expect("first marker"), -1_i64 << 48);
        assert!(run_marker_value(i64::MAX as usize).is_err());
        #[cfg(target_pointer_width = "64")]
        assert!(run_marker_value(usize::MAX).is_err());
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn empty_radix_tree() -> &'static mut RadixTree {
        unsafe {
            crate::collections::radix_tree::allocate_node::<RadixTree>()
                .as_mut()
                .expect("radix tree")
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    struct MmapNodeArena {
        ptr: *mut u8,
        size: usize,
    }

    #[cfg(not(feature = "fixed_heap"))]
    impl MmapNodeArena {
        fn new(pages: usize) -> Self {
            let size = pages.checked_mul(PAGE_SIZE).expect("test mapping size");
            let prot = system_alloc::prots::get_prot(true, true, false);
            let ptr = unsafe { system_alloc::mmap(size, prot) };
            assert!(!system_alloc::mmap_failed(ptr));
            Self { ptr, size }
        }

        fn node(&self, page_offset: usize) -> &'static mut DoubleLinkedList {
            let offset = page_offset.checked_mul(PAGE_SIZE).expect("node offset");
            let addr = (self.ptr as usize).checked_add(offset).expect("node addr");
            FreeList::write_list_node(addr).expect("page-aligned test node")
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    impl Drop for MmapNodeArena {
        fn drop(&mut self) {
            unsafe { system_alloc::munmap(self.ptr, self.size) };
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn remove_run_markers_rolls_back_start_marker_when_end_overflows() {
        let rd_tree = empty_radix_tree();
        let start_addr = PAGE_SIZE;
        let start_key = checked_radix_key(start_addr).expect("start key");
        let marker = -1i64 << 48;

        rd_tree
            .insert(start_key, marker, 1)
            .expect("insert start marker");

        let err = FreeList::remove_run_markers(rd_tree, start_addr, usize::MAX)
            .expect_err("overflowing end marker must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(rd_tree.get_mut(start_key), marker);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_remove_spec_one_handles_single_page_run_marker_alias() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(2);
        let node = arena.node(0);
        let node_ptr = node as *mut DoubleLinkedList;
        const RUN_IDX: usize = 0;

        freelist
            .insert_one_locked(RUN_IDX, node, rd_tree)
            .expect("insert one-page free run marker");
        let key = checked_radix_key(node_ptr as usize).expect("node key");
        assert_eq!(marker_page_count(rd_tree.get_mut(key)), Some(1));

        let removed = freelist
            .remove_spec_one(RUN_IDX, node_ptr, rd_tree)
            .expect("one-page run should be removable even when start/end marker alias");

        assert_eq!(removed, node_ptr as *mut u8);
        assert!(marker_page_count(rd_tree.get_mut(key)).is_none());
        assert!(freelist.try_get_slice().expect("slice")[RUN_IDX]
            .lock()
            .is_none());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_remove_spec_one_removes_singleton_run_for_coalescing() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(4);
        let node = arena.node(0);
        let node_ptr = node as *mut DoubleLinkedList;
        const RUN_IDX: usize = 1;

        freelist
            .insert_one_locked(RUN_IDX, node, rd_tree)
            .expect("insert free run marker");
        assert!(marker_page_count(
            rd_tree.get_mut(checked_radix_key(node_ptr as usize).expect("node key"))
        )
        .is_some());

        let removed = freelist
            .remove_spec_one(RUN_IDX, node_ptr, rd_tree)
            .expect("singleton free run should be removable for coalescing");

        assert_eq!(removed, node_ptr as *mut u8);
        assert!(marker_page_count(
            rd_tree.get_mut(checked_radix_key(node_ptr as usize).expect("node key"))
        )
        .is_none());
        assert!(freelist.try_get_slice().expect("slice")[RUN_IDX]
            .lock()
            .is_none());
        let removed_node = DoubleLinkedList::try_get_ref(node_ptr).expect("removed node");
        assert!(removed_node.prev.is_none());
        assert!(removed_node.next.is_none());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_remove_spec_one_rejects_wrong_sized_marker() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(4);
        let node = arena.node(0);
        let node_ptr = node as *mut DoubleLinkedList;
        const RUN_IDX: usize = 1;
        const WRONG_IDX: usize = 0;

        {
            let slice = freelist.try_get_slice().expect("slice");
            *slice[RUN_IDX].lock() = Some(node);
        }
        FreeList::insert_run_markers(
            rd_tree,
            node_ptr as usize,
            WRONG_IDX,
            run_marker_value(WRONG_IDX).expect("wrong marker value"),
        )
        .expect("insert intentionally wrong marker");

        let removed = freelist.remove_spec_one(RUN_IDX, node_ptr, rd_tree);

        assert!(
            removed.is_none(),
            "a marker for a different run length must not validate this list entry"
        );
        assert_eq!(
            marker_page_count(
                rd_tree.get_mut(checked_radix_key(node_ptr as usize).expect("node key"))
            ),
            Some(WRONG_IDX + 1)
        );
        assert!(freelist.try_get_slice().expect("slice")[RUN_IDX]
            .lock()
            .is_some());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_remove_spec_one_rejects_missing_end_marker_without_detach() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(4);
        let node = arena.node(0);
        let node_ptr = node as *mut DoubleLinkedList;
        const RUN_IDX: usize = 1;

        freelist
            .insert_one_locked(RUN_IDX, node, rd_tree)
            .expect("insert two-page free run marker");
        let start_key = checked_radix_key(node_ptr as usize).expect("start key");
        let end_key = checked_radix_key(node_ptr as usize + PAGE_SIZE).expect("end key");
        rd_tree
            .remove(end_key, 1)
            .expect("simulate a missing tail marker");

        let removed = freelist.remove_spec_one(RUN_IDX, node_ptr, rd_tree);

        assert!(
            removed.is_none(),
            "a run missing its tail marker must not be handed out"
        );
        assert_eq!(
            marker_page_count(rd_tree.get_mut(start_key)),
            Some(RUN_IDX + 1),
            "failed removal must preserve the start marker for diagnostics"
        );
        assert!(
            marker_page_count(rd_tree.get_mut(end_key)).is_none(),
            "test corruption should remain visible"
        );
        assert!(freelist.try_get_slice().expect("slice")[RUN_IDX]
            .lock()
            .is_some());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_insert_one_rejects_existing_end_marker_without_overwrite() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(4);
        let node = arena.node(0);
        let node_ptr = node as *mut DoubleLinkedList;
        const RUN_IDX: usize = 1;
        const EXISTING_MARKER: i64 = 0x1234_5678;

        let start_key = checked_radix_key(node_ptr as usize).expect("start key");
        let end_key = checked_radix_key(node_ptr as usize + PAGE_SIZE).expect("end key");
        rd_tree
            .insert(end_key, EXISTING_MARKER, 1)
            .expect("simulate an already-owned tail boundary");

        let err = freelist
            .insert_one_locked(RUN_IDX, node, rd_tree)
            .expect_err("inserting an overlapping free run must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(
            rd_tree.get_mut(start_key),
            0,
            "failed insertion must not publish a start marker"
        );
        assert_eq!(
            rd_tree.get_mut(end_key),
            EXISTING_MARKER,
            "failed insertion must not overwrite existing ownership metadata"
        );
        assert!(freelist.try_get_slice().expect("slice")[RUN_IDX]
            .lock()
            .is_none());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_remove_spec_one_unlinks_tail_without_losing_head() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(8);
        let tail = arena.node(0);
        let tail_ptr = tail as *mut DoubleLinkedList;
        let head = arena.node(4);
        let head_ptr = head as *mut DoubleLinkedList;
        const RUN_IDX: usize = 1;

        freelist
            .insert_one_locked(RUN_IDX, tail, rd_tree)
            .expect("insert tail run");
        freelist
            .insert_one_locked(RUN_IDX, head, rd_tree)
            .expect("insert head run");

        let removed = freelist
            .remove_spec_one(RUN_IDX, tail_ptr, rd_tree)
            .expect("non-head free run should be removable for coalescing");

        assert_eq!(removed, tail_ptr as *mut u8);
        let slice = freelist.try_get_slice().expect("slice");
        let locked = slice[RUN_IDX].lock();
        let current_head = locked
            .as_ref()
            .map(|node| *node as *const DoubleLinkedList as *mut DoubleLinkedList);
        assert_eq!(current_head, Some(head_ptr));
        drop(locked);

        let head_ref = DoubleLinkedList::try_get_ref(head_ptr).expect("head node");
        assert!(head_ref.prev.is_none());
        assert!(head_ref.next.is_none());
        let tail_ref = DoubleLinkedList::try_get_ref(tail_ptr).expect("tail node");
        assert!(tail_ref.prev.is_none());
        assert!(tail_ref.next.is_none());

        assert!(freelist
            .remove_spec_one(RUN_IDX, head_ptr, rd_tree)
            .is_some());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_remove_spec_one_rejects_non_head_without_backlink() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(12);
        let tail = arena.node(0);
        let tail_ptr = tail as *mut DoubleLinkedList;
        let head = arena.node(4);
        let head_ptr = head as *mut DoubleLinkedList;
        let foreign_prev = arena.node(8);
        let foreign_prev_ptr = foreign_prev as *mut DoubleLinkedList;
        const RUN_IDX: usize = 1;

        freelist
            .insert_one_locked(RUN_IDX, tail, rd_tree)
            .expect("insert tail run");
        freelist
            .insert_one_locked(RUN_IDX, head, rd_tree)
            .expect("insert head run");

        {
            let tail_ref = DoubleLinkedList::try_get_ref(tail_ptr).expect("tail node");
            tail_ref.prev = Some(foreign_prev_ptr);
        }

        let start_key = checked_radix_key(tail_ptr as usize).expect("tail start key");
        let end_key = checked_radix_key(tail_ptr as usize + PAGE_SIZE).expect("tail end key");
        let removed = freelist.remove_spec_one(RUN_IDX, tail_ptr, rd_tree);

        assert!(
            removed.is_none(),
            "non-head removal must reject a node whose prev link does not point back"
        );
        assert_eq!(marker_page_count(rd_tree.get_mut(start_key)), Some(2));
        assert_eq!(marker_page_count(rd_tree.get_mut(end_key)), Some(2));

        let slice = freelist.try_get_slice().expect("slice");
        let locked = slice[RUN_IDX].lock();
        let current_head = locked
            .as_ref()
            .map(|node| *node as *const DoubleLinkedList as *mut DoubleLinkedList);
        assert_eq!(current_head, Some(head_ptr));
        drop(locked);

        let head_ref = DoubleLinkedList::try_get_ref(head_ptr).expect("head node");
        assert_eq!(head_ref.next, Some(tail_ptr));
        let foreign_prev_ref =
            DoubleLinkedList::try_get_ref(foreign_prev_ptr).expect("foreign prev node");
        assert!(foreign_prev_ref.next.is_none());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_remove_spec_one_rejects_unreachable_same_class_node() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(16);
        let reachable = arena.node(0);
        let reachable_ptr = reachable as *mut DoubleLinkedList;
        let foreign_prev = arena.node(4);
        let foreign_prev_ptr = foreign_prev as *mut DoubleLinkedList;
        let foreign = arena.node(8);
        let foreign_ptr = foreign as *mut DoubleLinkedList;
        let foreign_next = arena.node(12);
        let foreign_next_ptr = foreign_next as *mut DoubleLinkedList;
        const RUN_IDX: usize = 1;

        freelist
            .insert_one_locked(RUN_IDX, reachable, rd_tree)
            .expect("insert reachable free run");
        FreeList::insert_run_markers(
            rd_tree,
            foreign_ptr as usize,
            RUN_IDX,
            run_marker_value(RUN_IDX).expect("marker value"),
        )
        .expect("publish markers for a foreign same-sized run");

        {
            let foreign_ref = DoubleLinkedList::try_get_ref(foreign_ptr).expect("foreign node");
            foreign_ref.prev = Some(foreign_prev_ptr);
            foreign_ref.next = Some(foreign_next_ptr);
            let prev_ref = DoubleLinkedList::try_get_ref(foreign_prev_ptr).expect("foreign prev");
            prev_ref.next = Some(foreign_ptr);
            let next_ref = DoubleLinkedList::try_get_ref(foreign_next_ptr).expect("foreign next");
            next_ref.prev = Some(foreign_ptr);
        }

        let foreign_start_key = checked_radix_key(foreign_ptr as usize).expect("foreign start key");
        let foreign_end_key =
            checked_radix_key(foreign_ptr as usize + PAGE_SIZE).expect("foreign end key");
        let removed = freelist.remove_spec_one(RUN_IDX, foreign_ptr, rd_tree);

        assert!(
            removed.is_none(),
            "same-sized markers plus locally consistent links must not prove list membership"
        );
        assert_eq!(
            marker_page_count(rd_tree.get_mut(foreign_start_key)),
            Some(RUN_IDX + 1),
            "failed removal must preserve foreign start marker"
        );
        assert_eq!(
            marker_page_count(rd_tree.get_mut(foreign_end_key)),
            Some(RUN_IDX + 1),
            "failed removal must preserve foreign end marker"
        );

        let slice = freelist.try_get_slice().expect("slice");
        let locked = slice[RUN_IDX].lock();
        let current_head = locked
            .as_ref()
            .map(|node| *node as *const DoubleLinkedList as *mut DoubleLinkedList);
        assert_eq!(current_head, Some(reachable_ptr));
        drop(locked);

        let foreign_ref = DoubleLinkedList::try_get_ref(foreign_ptr).expect("foreign node");
        assert_eq!(foreign_ref.prev, Some(foreign_prev_ptr));
        assert_eq!(foreign_ref.next, Some(foreign_next_ptr));
        assert!(freelist
            .remove_spec_one(RUN_IDX, reachable_ptr, rd_tree)
            .is_some());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_remove_spec_one_updates_head_to_successor() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(8);
        let successor = arena.node(0);
        let successor_ptr = successor as *mut DoubleLinkedList;
        let head = arena.node(4);
        let head_ptr = head as *mut DoubleLinkedList;
        const RUN_IDX: usize = 1;

        freelist
            .insert_one_locked(RUN_IDX, successor, rd_tree)
            .expect("insert successor run");
        freelist
            .insert_one_locked(RUN_IDX, head, rd_tree)
            .expect("insert head run");

        let removed = freelist
            .remove_spec_one(RUN_IDX, head_ptr, rd_tree)
            .expect("head free run should move list head to successor");

        assert_eq!(removed, head_ptr as *mut u8);
        let slice = freelist.try_get_slice().expect("slice");
        let locked = slice[RUN_IDX].lock();
        let current_head = locked
            .as_ref()
            .map(|node| *node as *const DoubleLinkedList as *mut DoubleLinkedList);
        assert_eq!(current_head, Some(successor_ptr));
        drop(locked);

        let successor_ref = DoubleLinkedList::try_get_ref(successor_ptr).expect("successor node");
        assert!(successor_ref.prev.is_none());
        assert!(successor_ref.next.is_none());
        let removed_head = DoubleLinkedList::try_get_ref(head_ptr).expect("removed head");
        assert!(removed_head.prev.is_none());
        assert!(removed_head.next.is_none());

        assert!(freelist
            .remove_spec_one(RUN_IDX, successor_ptr, rd_tree)
            .is_some());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_remove_one_skips_markerless_stale_head() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(8);
        let valid = arena.node(0);
        let valid_ptr = valid as *mut DoubleLinkedList;
        let stale_head = arena.node(4);
        let stale_head_ptr = stale_head as *mut DoubleLinkedList;
        const RUN_IDX: usize = 1;

        freelist
            .insert_one_locked(RUN_IDX, valid, rd_tree)
            .expect("insert valid tail run");
        freelist
            .insert_one_locked(RUN_IDX, stale_head, rd_tree)
            .expect("insert markerless stale head candidate");
        FreeList::remove_run_markers(rd_tree, stale_head_ptr as usize, RUN_IDX)
            .expect("remove stale head markers to model a raced/stale free-list node");

        let removed = freelist
            .remove_one_locked(RUN_IDX, rd_tree)
            .expect("exact-size removal should skip stale head and reuse valid tail");

        assert_eq!(removed, valid_ptr as *mut u8);
        assert!(marker_page_count(
            rd_tree.get_mut(checked_radix_key(valid_ptr as usize).expect("valid key"))
        )
        .is_none());
        assert!(marker_page_count(
            rd_tree.get_mut(checked_radix_key(stale_head_ptr as usize).expect("stale key"))
        )
        .is_none());
        assert!(freelist.try_get_slice().expect("slice")[RUN_IDX]
            .lock()
            .is_none());

        let stale_ref = DoubleLinkedList::try_get_ref(stale_head_ptr).expect("stale head node");
        assert!(stale_ref.prev.is_none());
        assert!(stale_ref.next.is_none());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_non_empty_bitmap_tracks_insert_and_last_remove() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(8);
        let node = arena.node(0);
        let node_ptr = node as *mut DoubleLinkedList;
        const RUN_IDX: usize = 3;

        assert_eq!(freelist.next_non_empty_index(0, BACKEND_MAX_PAGE), None);
        freelist
            .insert_one_locked(RUN_IDX, node, rd_tree)
            .expect("insert free run");
        assert_eq!(
            freelist.next_non_empty_index(0, BACKEND_MAX_PAGE),
            Some(RUN_IDX)
        );
        assert_eq!(
            freelist.next_non_empty_index(RUN_IDX + 1, BACKEND_MAX_PAGE),
            None
        );

        let removed = freelist
            .remove_one_locked(RUN_IDX, rd_tree)
            .expect("bitmap-tracked free run should be removable");

        assert_eq!(removed, node_ptr as *mut u8);
        assert_eq!(freelist.next_non_empty_index(0, BACKEND_MAX_PAGE), None);
    }

    #[test]
    fn freelist_non_empty_bitmap_covers_every_backend_run_class() {
        for idx in 0..BACKEND_MAX_PAGE {
            let (word_idx, bit) = FreeList::bitmap_bit_for_idx(idx)
                .unwrap_or_else(|| panic!("run class {} must be bitmap-trackable", idx));
            assert!(
                word_idx < FREE_LIST_BITMAP_WORDS,
                "run class {} selected bitmap word {} outside the bitmap",
                idx,
                word_idx
            );
            assert!(
                bit < FreeList::bitmap_word_bits(),
                "run class {} selected bitmap bit {} outside a word",
                idx,
                bit
            );
            assert_eq!(
                word_idx * FreeList::bitmap_word_bits() + bit,
                idx,
                "run class {} must keep a stable one-bit mapping",
                idx
            );
        }

        assert_eq!(
            FreeList::bitmap_bit_for_idx(BACKEND_MAX_PAGE),
            None,
            "out-of-range run classes remain explicitly untracked"
        );
    }

    #[test]
    fn freelist_rejects_impossible_request_before_touching_global_state() {
        let freelist = FreeList::new();
        assert!(freelist.alloc(usize::MAX).is_err());
        assert!(freelist.lists.load(Ordering::Relaxed).is_null());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_successful_bump_alloc_defers_list_metadata_until_free() {
        let freelist = FreeList::new();
        assert!(freelist.lists.load(Ordering::Relaxed).is_null());

        let ptr = freelist
            .alloc(PAGE_SIZE)
            .expect("first hosted page-run allocation should come from bump");
        assert!(
            freelist.lists.load(Ordering::Relaxed).is_null(),
            "a cold bump allocation should not allocate FreeList metadata before any free-run exists"
        );

        freelist.free(ptr, PAGE_SIZE);
        assert!(
            !freelist.lists.load(Ordering::Relaxed).is_null(),
            "publishing the first free run should still initialize FreeList metadata"
        );
    }

    #[test]
    fn freelist_free_ignores_null_or_zero_sized_inputs_without_underflow() {
        let freelist = FreeList::new();
        freelist.free(core::ptr::null_mut(), PAGE_SIZE);
        freelist.free(PAGE_SIZE as *mut u8, 0);
        assert!(freelist.lists.load(Ordering::Relaxed).is_null());
    }

    #[test]
    fn double_linked_list_rejects_null_links_without_panic() {
        assert!(DoubleLinkedList::try_get_ref(core::ptr::null_mut()).is_err());

        let head: &'static mut DoubleLinkedList = Box::leak(Box::new(DoubleLinkedList::new()));
        assert!(head.push_before_head(core::ptr::null_mut()).is_err());
        assert!(head.prev.is_none());
        assert!(head.next.is_none());
    }

    #[test]
    fn double_linked_list_remove_rejects_corrupt_prev_without_panic() {
        let node: &'static mut DoubleLinkedList = Box::leak(Box::new(DoubleLinkedList::new()));
        node.prev = Some(core::ptr::null_mut());

        assert!(node.remove_current().is_err());
        assert_eq!(node.prev, Some(core::ptr::null_mut()));
        assert!(node.next.is_none());
    }

    #[test]
    fn freelist_write_list_node_rejects_invalid_addresses_without_panic() {
        assert!(FreeList::write_list_node(0).is_err());

        let mut backing = [0usize; 4];
        let misaligned = backing.as_mut_ptr() as usize + 1;
        assert!(FreeList::write_list_node(misaligned).is_err());
    }

    #[test]
    fn freelist_alloc_aligned_splits_over_page_runs() {
        #[cfg(feature = "fixed_heap")]
        if run_fixed_heap_capacity_test_in_fresh_process(
            "UNIALLOC_FIXED_FREELIST_ALIGNED_SPLIT_CHILD",
            "freelist::tests::freelist_alloc_aligned_splits_over_page_runs",
        ) {
            return;
        }
        #[cfg(feature = "fixed_heap")]
        let _fixed_heap_guard = crate::sc::fixed_heap_test_guard();

        let freelist = FreeList::new();
        let align = PAGE_SIZE * 4;
        let ptr = freelist
            .alloc_aligned(PAGE_SIZE + 17, align)
            .expect("over-page aligned freelist allocation");

        assert_eq!(ptr as usize % align, 0);
        freelist.free(ptr, PAGE_SIZE + 17);
    }

    #[test]
    fn freelist_alloc_aligned_fallback_reclaims_overreserved_slack() {
        #[cfg(feature = "fixed_heap")]
        if run_fixed_heap_capacity_test_in_fresh_process(
            "UNIALLOC_FIXED_FREELIST_ALIGNED_FALLBACK_CHILD",
            "freelist::tests::freelist_alloc_aligned_fallback_reclaims_overreserved_slack",
        ) {
            return;
        }
        #[cfg(feature = "fixed_heap")]
        let _fixed_heap_guard = crate::sc::fixed_heap_test_guard();

        let freelist = FreeList::new();
        let align = PAGE_SIZE * 4;
        let payload_size = PAGE_SIZE * 2;
        let total_size = PAGE_SIZE * 5;
        // Hosted freelist tests share the global bump/radix metadata.  Keep
        // real allocated guards around the overreserved run so this regression
        // proves prefix/payload/suffix coalescing for the fallback allocation
        // itself, instead of accidentally coalescing with a neighboring free
        // run left by an earlier test and tripping the hosted large-run unmap
        // policy.
        let leading_guard = freelist.alloc(PAGE_SIZE).expect("leading guard page");

        let aligned = freelist
            .alloc_aligned(payload_size, align)
            .expect("fallback over-page-aligned allocation");
        assert_eq!(aligned as usize % align, 0);

        let trailing_guard_size = total_size + PAGE_SIZE;
        let trailing_guard = freelist
            .alloc(trailing_guard_size)
            .expect("trailing guard run");

        freelist.free(aligned, payload_size);
        let reclaimed = freelist
            .alloc(total_size)
            .expect("freeing the payload should coalesce prefix/payload/suffix slack");
        let reclaimed_start = reclaimed as usize;
        let reclaimed_end = reclaimed_start + total_size;
        let payload_start = aligned as usize;
        let payload_end = payload_start + payload_size;

        assert!(
            reclaimed_start <= payload_start && payload_end <= reclaimed_end,
            "the next total-size allocation should reuse the whole overreserved run"
        );

        freelist.free(reclaimed, total_size);
        freelist.free(trailing_guard, trailing_guard_size);
        freelist.free(leading_guard, PAGE_SIZE);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hosted_free_run_retain_max_pages_is_clamped() {
        let retain_pages = hosted_free_run_retain_max_pages();
        let rounded = (HOSTED_FREE_RUN_RETAIN_MAX_BYTES + PAGE_SIZE - 1) / PAGE_SIZE;
        assert_eq!(HOSTED_FREE_RUN_RETAIN_MAX_BYTES, 256 * 1024);
        assert!(retain_pages >= 1);
        assert!(retain_pages <= BACKEND_MAX_PAGE);
        if rounded > BACKEND_MAX_PAGE {
            assert_eq!(retain_pages, BACKEND_MAX_PAGE);
        } else {
            assert!(retain_pages * PAGE_SIZE >= HOSTED_FREE_RUN_RETAIN_MAX_BYTES);
            if retain_pages > 1 {
                assert!((retain_pages - 1) * PAGE_SIZE < HOSTED_FREE_RUN_RETAIN_MAX_BYTES);
            }
        }
        if PAGE_SIZE == 4096 {
            assert!(
                retain_pages < BACKEND_MAX_PAGE,
                "hosted 4KiB-page cap must be below the largest representable run"
            );
            assert_eq!(retain_pages, 64);
        }
    }

    #[cfg(all(
        not(feature = "fixed_heap"),
        any(target_os = "linux", target_os = "macos")
    ))]
    fn os_page_is_mapped_for_test(ptr: *mut u8) -> bool {
        let mut residency = 0_u8;
        let mincore_ok = unsafe {
            libc::mincore(
                // Linux and Darwin disagree on the signedness of the residency
                // byte. Let the final pointer cast infer the target libc ABI;
                // the kernel still writes the same one-byte bit vector.
                ptr as *mut libc::c_void,
                PAGE_SIZE,
                (&mut residency as *mut u8).cast(),
            ) == 0
        };
        mincore_ok && (residency & 1) != 0
    }

    #[cfg(all(not(feature = "fixed_heap"), target_os = "linux"))]
    fn assert_page_unmapped_for_test(ptr: *mut u8) {
        let mapped = unsafe {
            libc::mmap(
                ptr as *mut libc::c_void,
                PAGE_SIZE,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_PRIVATE | libc::MAP_ANONYMOUS | libc::MAP_FIXED_NOREPLACE,
                -1,
                0,
            ) as *mut u8
        };
        assert!(!system_alloc::mmap_failed(mapped));
        assert_eq!(mapped, ptr);
        unsafe { system_alloc::munmap(mapped, PAGE_SIZE) };
    }

    #[cfg(all(not(feature = "fixed_heap"), target_os = "macos"))]
    fn assert_page_unmapped_for_test(ptr: *mut u8) {
        assert!(
            unsafe { system_alloc::fixed_address_mapping_available_for_test(ptr, PAGE_SIZE) },
            "freed hosted page should accept a fixed-address test mapping"
        );
    }

    #[cfg(all(
        not(feature = "fixed_heap"),
        any(target_os = "linux", target_os = "macos")
    ))]
    #[test]
    fn hosted_free_above_representable_retention_cap_is_unmapped() {
        let retain_pages = hosted_free_run_retain_max_pages();
        if retain_pages >= BACKEND_MAX_PAGE {
            // If a target page size ever rounds the hosted cap up to the largest
            // representable class, there is no above-cap-but-representable run.
            return;
        }

        let freelist = FreeList::new();
        let run_pages = retain_pages + 1;
        let run_size = checked_page_size_from_count(run_pages).expect("above-cap run size");
        let _guard_before = freelist.alloc(PAGE_SIZE).expect("leading guard page");
        let ptr = freelist.alloc(run_size).expect("above-cap page run");
        let _guard_after = freelist.alloc(PAGE_SIZE).expect("trailing guard page");

        unsafe { core::ptr::write_volatile(ptr, 0xA5) };
        assert!(os_page_is_mapped_for_test(ptr));

        freelist.free(ptr, run_size);

        assert_page_unmapped_for_test(ptr);
    }

    #[cfg(all(
        not(feature = "fixed_heap"),
        any(target_os = "linux", target_os = "macos")
    ))]
    #[test]
    fn hosted_free_unmaps_when_freelist_metadata_unavailable() {
        let freelist = FreeList::new();
        let _guard_before = freelist.alloc(PAGE_SIZE).expect("leading guard page");
        let ptr = freelist.alloc(PAGE_SIZE).expect("hosted page run");
        let _guard_after = freelist.alloc(PAGE_SIZE).expect("trailing guard page");

        unsafe { core::ptr::write_volatile(ptr, 0xA7) };
        assert!(os_page_is_mapped_for_test(ptr));

        freelist.free_with_hosted_metadata_failure_for_test(ptr, PAGE_SIZE);

        assert_page_unmapped_for_test(ptr);
        assert!(
            !freelist.free_list_metadata_initialized(),
            "metadata-failure fallback must not initialize reusable freelist state"
        );
    }

    #[cfg(all(
        not(feature = "fixed_heap"),
        any(target_os = "linux", target_os = "macos")
    ))]
    #[test]
    fn hosted_free_retainable_coalesced_run_unmaps_when_insert_fails() {
        let freelist = FreeList::new();
        let retain_pages = hosted_free_run_retain_max_pages();
        if retain_pages < 2 {
            return;
        }

        let prev_size = PAGE_SIZE;
        let current_pages = retain_pages - 1;
        let current_size = checked_page_size_from_count(current_pages).expect("current run size");
        let coalesced_size = checked_page_size_from_count(retain_pages).expect("cap run size");
        let _guard_before = freelist.alloc(PAGE_SIZE).expect("leading guard page");
        let prev = freelist.alloc(coalesced_size).expect("coalesced page run");
        let current = prev.wrapping_add(prev_size);
        let _guard_after = freelist.alloc(PAGE_SIZE).expect("trailing guard page");
        unsafe {
            core::ptr::write_volatile(prev, 0xC3);
            core::ptr::write_volatile(current, 0x3C);
        }

        freelist.free(prev, prev_size);
        freelist.free_with_hosted_insert_failure_for_test(current, current_size);

        try_with_rd_tree(|rd_tree| {
            assert_eq!(
                marker_page_count(
                    rd_tree.get_mut(checked_radix_key(prev as usize).expect("coalesced key"))
                ),
                None,
                "failed hosted retention insertion must remove the coalesced run marker"
            );
        })
        .expect("global radix tree should be available");
        assert!(
            freelist.try_get_slice().expect("slice")[retain_pages - 1]
                .lock()
                .is_none(),
            "failed hosted retention insertion must not leave the coalesced run reachable"
        );

        assert_page_unmapped_for_test(prev);
    }

    #[cfg(all(not(feature = "fixed_heap"), unix))]
    #[test]
    fn hosted_free_at_retention_cap_is_retained_and_reused() {
        let freelist = FreeList::new();
        let run_pages = hosted_free_run_retain_max_pages();
        let run_size = checked_page_size_from_count(run_pages).expect("cap run size");
        let _guard_before = freelist.alloc(PAGE_SIZE).expect("leading guard page");
        let ptr = freelist.alloc(run_size).expect("at-cap page run");
        let _guard_after = freelist.alloc(PAGE_SIZE).expect("trailing guard page");

        unsafe { core::ptr::write_volatile(ptr, 0x5A) };
        freelist.free(ptr, run_size);

        #[cfg(any(target_os = "linux", target_os = "macos"))]
        assert!(
            os_page_is_mapped_for_test(ptr),
            "at-cap hosted free run should remain mapped for freelist reuse"
        );
        let reused = freelist
            .alloc(run_size)
            .expect("retained at-cap page run should be reusable");
        assert_eq!(reused, ptr);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_head_aligned_run_fast_path_reuses_existing_exact_free_run() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(8);
        let align = PAGE_SIZE * 4;
        let payload_pages = 2;
        let offset = (0..=6)
            .find(|page| {
                (arena.ptr as usize + page * PAGE_SIZE) % align == 0 && page + payload_pages <= 8
            })
            .expect("arena should contain a 4-page-aligned two-page window");
        let node = arena.node(offset);
        let node_ptr = node as *mut DoubleLinkedList;

        freelist
            .insert_one_locked(payload_pages - 1, node, rd_tree)
            .expect("insert exact aligned free run");

        let reused = freelist
            .remove_head_aligned_run_locked(payload_pages, align, rd_tree)
            .expect("head aligned scan should not fail")
            .expect("exact aligned free run should be reused");

        assert_eq!(reused, node_ptr as *mut u8);
        assert!(marker_page_count(
            rd_tree.get_mut(checked_radix_key(node_ptr as usize).expect("node key"))
        )
        .is_none());
        assert!(freelist.try_get_slice().expect("slice")[payload_pages - 1]
            .lock()
            .is_none());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_head_aligned_run_fast_path_splits_larger_suffix() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(8);
        let align = PAGE_SIZE * 4;
        let run_pages = 4;
        let payload_pages = 2;
        let offset = (0..=4)
            .find(|page| {
                (arena.ptr as usize + page * PAGE_SIZE) % align == 0 && page + run_pages <= 8
            })
            .expect("arena should contain a 4-page-aligned four-page window");
        let node = arena.node(offset);
        let node_ptr = node as *mut DoubleLinkedList;
        let suffix_addr = node_ptr as usize + payload_pages * PAGE_SIZE;

        freelist
            .insert_one_locked(run_pages - 1, node, rd_tree)
            .expect("insert larger aligned free run");

        let reused = freelist
            .remove_head_aligned_run_locked(payload_pages, align, rd_tree)
            .expect("head aligned scan should not fail")
            .expect("larger aligned free run should be split and reused");

        assert_eq!(reused, node_ptr as *mut u8);
        assert!(marker_page_count(
            rd_tree.get_mut(checked_radix_key(node_ptr as usize).expect("node key"))
        )
        .is_none());
        assert_eq!(
            marker_page_count(rd_tree.get_mut(checked_radix_key(suffix_addr).expect("suffix key"))),
            Some(run_pages - payload_pages)
        );
        let slice = freelist.try_get_slice().expect("slice");
        assert!(slice[run_pages - 1].lock().is_none());
        assert!(slice[run_pages - payload_pages - 1].lock().is_some());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_aligned_run_fast_path_splits_prefix_and_suffix() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(10);
        let align = PAGE_SIZE * 4;
        let run_pages = 6;
        let payload_pages = 2;
        let offset = (0..=4)
            .find(|page| {
                let start = arena.ptr as usize + page * PAGE_SIZE;
                let aligned = match checked_round_up_addr(start, align) {
                    Some(aligned) => aligned,
                    None => return false,
                };
                let prefix_pages = (aligned - start) / PAGE_SIZE;
                start % align != 0
                    && prefix_pages > 0
                    && prefix_pages + payload_pages < run_pages
                    && page + run_pages <= 10
            })
            .expect("arena should contain an interior aligned two-page window");
        let node = arena.node(offset);
        let node_ptr = node as *mut DoubleLinkedList;
        let start_addr = node_ptr as usize;
        let aligned_addr = checked_round_up_addr(start_addr, align).expect("aligned payload addr");
        let prefix_pages = (aligned_addr - start_addr) / PAGE_SIZE;
        let suffix_pages = run_pages - prefix_pages - payload_pages;
        let suffix_addr = aligned_addr + payload_pages * PAGE_SIZE;

        assert_ne!(
            start_addr % align,
            0,
            "test must start with an unaligned run head"
        );
        assert!(prefix_pages > 0, "test must split a prefix");
        assert!(suffix_pages > 0, "test must split a suffix");

        freelist
            .insert_one_locked(run_pages - 1, node, rd_tree)
            .expect("insert larger unaligned free run containing an aligned payload window");

        let reused = freelist
            .remove_head_aligned_run_locked(payload_pages, align, rd_tree)
            .expect("interior aligned scan should not fail")
            .expect("interior aligned payload window should be reused");

        assert_eq!(reused as usize, aligned_addr);
        assert_eq!(reused as usize % align, 0);
        assert_eq!(
            marker_page_count(rd_tree.get_mut(checked_radix_key(start_addr).expect("prefix key"))),
            Some(prefix_pages)
        );
        assert_ne!(
            marker_page_count(rd_tree.get_mut(checked_radix_key(start_addr).expect("prefix key"))),
            Some(run_pages),
            "the original full run marker must be replaced by the prefix marker"
        );
        assert_eq!(
            marker_page_count(rd_tree.get_mut(checked_radix_key(suffix_addr).expect("suffix key"))),
            Some(suffix_pages)
        );
        let slice = freelist.try_get_slice().expect("slice");
        assert!(slice[run_pages - 1].lock().is_none());
        assert!(slice[prefix_pages - 1].lock().is_some());
        assert!(slice[suffix_pages - 1].lock().is_some());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_aligned_run_fast_path_prefers_less_fragmenting_same_size_run() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(32);
        let align = PAGE_SIZE * 4;
        let run_pages = 4;
        let payload_pages = 2;

        let one_fragment_offset = (0..=28)
            .find(|page| {
                let start = arena.ptr as usize + page * PAGE_SIZE;
                start % align == 0 && page + run_pages <= 32
            })
            .expect("arena should contain an aligned four-page run");
        let two_fragment_offset = (0..=28)
            .find(|page| {
                let does_not_overlap = page + run_pages <= one_fragment_offset
                    || *page >= one_fragment_offset + run_pages;
                if !does_not_overlap {
                    return false;
                }
                let start = arena.ptr as usize + page * PAGE_SIZE;
                let aligned = match checked_round_up_addr(start, align) {
                    Some(aligned) => aligned,
                    None => return false,
                };
                let prefix_pages = (aligned - start) / PAGE_SIZE;
                start % align != 0
                    && prefix_pages > 0
                    && prefix_pages + payload_pages < run_pages
                    && page + run_pages <= 32
            })
            .expect("arena should contain a same-size run that would split into two fragments");
        assert_ne!(one_fragment_offset, two_fragment_offset);

        let one_fragment_node = arena.node(one_fragment_offset);
        let one_fragment_addr = one_fragment_node as *mut DoubleLinkedList as usize;
        let two_fragment_node = arena.node(two_fragment_offset);
        let two_fragment_addr = two_fragment_node as *mut DoubleLinkedList as usize;
        let one_fragment_suffix = one_fragment_addr + payload_pages * PAGE_SIZE;

        freelist
            .insert_one_locked(run_pages - 1, one_fragment_node, rd_tree)
            .expect("insert the less-fragmenting candidate first");
        freelist
            .insert_one_locked(run_pages - 1, two_fragment_node, rd_tree)
            .expect("insert the more-fragmenting candidate last so it becomes list head");

        let reused = freelist
            .remove_head_aligned_run_locked(payload_pages, align, rd_tree)
            .expect("aligned scan should not fail")
            .expect("same-size aligned run should be reusable");

        assert_eq!(
            reused as usize, one_fragment_addr,
            "candidate selection should prefer one suffix fragment over prefix+suffix splitting"
        );
        assert_eq!(reused as usize % align, 0);
        assert_eq!(
            marker_page_count(
                rd_tree.get_mut(checked_radix_key(two_fragment_addr).expect("two-fragment key"))
            ),
            Some(run_pages),
            "the more-fragmenting run should remain available as a whole run"
        );
        assert_eq!(
            marker_page_count(
                rd_tree.get_mut(checked_radix_key(one_fragment_suffix).expect("suffix key"))
            ),
            Some(run_pages - payload_pages),
            "the selected run should leave only one suffix fragment"
        );

        let slice = freelist.try_get_slice().expect("slice");
        assert!(
            slice[run_pages - 1].lock().is_some(),
            "the same-size list should still contain the untouched two-fragment candidate"
        );
        assert!(slice[run_pages - payload_pages - 1].lock().is_some());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_aligned_run_stale_retry_keeps_earlier_same_class_candidates() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(32);
        let align = PAGE_SIZE * 4;
        let run_pages = 4;
        let payload_pages = 2;
        let idx = run_pages - 1;

        let one_fragment_offset = (0..=28)
            .find(|&page| {
                let start = arena.ptr as usize + page * PAGE_SIZE;
                start % align == 0 && page + run_pages <= 32
            })
            .expect("arena should contain an aligned four-page run");
        let two_fragment_offset = (0..=28)
            .find(|&page| {
                let does_not_overlap = page + run_pages <= one_fragment_offset
                    || page >= one_fragment_offset + run_pages;
                if !does_not_overlap {
                    return false;
                }
                let start = arena.ptr as usize + page * PAGE_SIZE;
                let aligned = match checked_round_up_addr(start, align) {
                    Some(aligned) => aligned,
                    None => return false,
                };
                let prefix_pages = (aligned - start) / PAGE_SIZE;
                start % align != 0
                    && prefix_pages > 0
                    && prefix_pages + payload_pages < run_pages
                    && page + run_pages <= 32
            })
            .expect("arena should contain a non-overlapping two-fragment run");

        let one_fragment_node = arena.node(one_fragment_offset);
        let one_fragment_addr = one_fragment_node as *mut DoubleLinkedList as usize;
        let two_fragment_node = arena.node(two_fragment_offset);
        let two_fragment_addr = two_fragment_node as *mut DoubleLinkedList as usize;
        let two_fragment_candidate = FreeList::aligned_payload_within_run(
            two_fragment_addr,
            run_pages,
            payload_pages,
            align,
        )
        .expect("two-fragment run should contain an aligned payload");
        assert_eq!(
            two_fragment_candidate.fragment_count(),
            2,
            "test needs a prefix+suffix candidate at the list head"
        );

        freelist
            .insert_one_locked(idx, one_fragment_node, rd_tree)
            .expect("insert stale better candidate first so it is list tail");
        freelist
            .insert_one_locked(idx, two_fragment_node, rd_tree)
            .expect("insert valid two-fragment candidate last so it is list head");

        FreeList::remove_run_markers(rd_tree, one_fragment_addr, idx)
            .expect("remove only radix markers for stale candidate");
        assert!(
            marker_page_count(
                rd_tree.get_mut(checked_radix_key(one_fragment_addr).expect("stale key"))
            )
            .is_none(),
            "stale candidate marker should be absent before retry"
        );

        let reused = freelist
            .remove_head_aligned_run_locked(payload_pages, align, rd_tree)
            .expect("stale retry aligned scan should not fail")
            .expect("retry should still reuse the earlier valid same-class candidate");

        assert_eq!(
            reused as usize, two_fragment_candidate.aligned_addr,
            "retry should return the aligned payload inside the valid head candidate"
        );
        assert_eq!(reused as usize % align, 0);
        assert_eq!(
            marker_page_count(
                rd_tree.get_mut(checked_radix_key(two_fragment_addr).expect("prefix key"))
            ),
            Some(two_fragment_candidate.prefix_pages),
            "selected two-fragment run should publish its prefix"
        );
        let suffix_addr = two_fragment_candidate.aligned_addr + payload_pages * PAGE_SIZE;
        assert_eq!(
            marker_page_count(rd_tree.get_mut(checked_radix_key(suffix_addr).expect("suffix key"))),
            Some(two_fragment_candidate.suffix_pages),
            "selected two-fragment run should publish its suffix"
        );
        assert!(
            marker_page_count(
                rd_tree.get_mut(checked_radix_key(one_fragment_addr).expect("stale key"))
            )
            .is_none(),
            "stale candidate marker should remain absent"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_aligned_run_stale_retry_skips_multiple_stale_candidates() {
        fn overlaps(page: usize, pages: usize, used: &[(usize, usize)]) -> bool {
            used.iter().any(|&(used_page, used_pages)| {
                page < used_page + used_pages && used_page < page + pages
            })
        }

        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena_pages = 64;
        let arena = MmapNodeArena::new(arena_pages);
        let align = PAGE_SIZE * 4;
        let run_pages = 4;
        let payload_pages = 2;
        let idx = run_pages - 1;

        let mut used = Vec::new();
        let mut stale_offsets = Vec::new();
        for page in 0..=arena_pages - run_pages {
            if overlaps(page, run_pages, &used) {
                continue;
            }
            let start = arena.ptr as usize + page * PAGE_SIZE;
            if start % align == 0 {
                stale_offsets.push(page);
                used.push((page, run_pages));
                if stale_offsets.len() == 2 {
                    break;
                }
            }
        }
        assert_eq!(
            stale_offsets.len(),
            2,
            "arena should contain two non-overlapping aligned one-fragment runs"
        );

        let two_fragment_offset = (0..=arena_pages - run_pages)
            .find(|&page| {
                if overlaps(page, run_pages, &used) {
                    return false;
                }
                let start = arena.ptr as usize + page * PAGE_SIZE;
                let aligned = match checked_round_up_addr(start, align) {
                    Some(aligned) => aligned,
                    None => return false,
                };
                let prefix_pages = (aligned - start) / PAGE_SIZE;
                start % align != 0 && prefix_pages > 0 && prefix_pages + payload_pages < run_pages
            })
            .expect("arena should contain a non-overlapping two-fragment run");

        let first_stale_node = arena.node(stale_offsets[0]);
        let first_stale_addr = first_stale_node as *mut DoubleLinkedList as usize;
        let second_stale_node = arena.node(stale_offsets[1]);
        let second_stale_addr = second_stale_node as *mut DoubleLinkedList as usize;
        let two_fragment_node = arena.node(two_fragment_offset);
        let two_fragment_addr = two_fragment_node as *mut DoubleLinkedList as usize;
        let two_fragment_candidate = FreeList::aligned_payload_within_run(
            two_fragment_addr,
            run_pages,
            payload_pages,
            align,
        )
        .expect("two-fragment run should contain an aligned payload");
        assert_eq!(
            two_fragment_candidate.fragment_count(),
            2,
            "test needs a valid prefix+suffix candidate at the list head"
        );

        freelist
            .insert_one_locked(idx, first_stale_node, rd_tree)
            .expect("insert first stale one-fragment candidate");
        freelist
            .insert_one_locked(idx, second_stale_node, rd_tree)
            .expect("insert second stale one-fragment candidate");
        freelist
            .insert_one_locked(idx, two_fragment_node, rd_tree)
            .expect("insert valid two-fragment candidate last so it is list head");

        FreeList::remove_run_markers(rd_tree, first_stale_addr, idx)
            .expect("remove first stale candidate markers");
        FreeList::remove_run_markers(rd_tree, second_stale_addr, idx)
            .expect("remove second stale candidate markers");

        let reused = freelist
            .remove_head_aligned_run_locked(payload_pages, align, rd_tree)
            .expect("multi-stale retry aligned scan should not fail")
            .expect("retry should skip both stale candidates and reuse the valid candidate");

        assert_eq!(
            reused as usize, two_fragment_candidate.aligned_addr,
            "retry should return the aligned payload inside the valid head candidate"
        );
        let suffix_addr = two_fragment_candidate.aligned_addr + payload_pages * PAGE_SIZE;
        assert_eq!(
            marker_page_count(
                rd_tree.get_mut(checked_radix_key(two_fragment_addr).expect("prefix key"))
            ),
            Some(two_fragment_candidate.prefix_pages)
        );
        assert_eq!(
            marker_page_count(rd_tree.get_mut(checked_radix_key(suffix_addr).expect("suffix key"))),
            Some(two_fragment_candidate.suffix_pages)
        );
        assert!(marker_page_count(
            rd_tree.get_mut(checked_radix_key(first_stale_addr).expect("first stale key"))
        )
        .is_none());
        assert!(marker_page_count(
            rd_tree.get_mut(checked_radix_key(second_stale_addr).expect("second stale key"))
        )
        .is_none());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_aligned_run_stale_retry_continues_after_skip_cap_exhaustion() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let align = PAGE_SIZE * 4;
        let run_pages = 2;
        let payload_pages = 2;
        let idx = run_pages - 1;
        let stale_count = ALIGNED_RUN_STALE_SKIP_CAPACITY + 1;
        let needed_aligned_runs = stale_count + 1;
        let arena_pages = (needed_aligned_runs + 2) * 4;
        let arena = MmapNodeArena::new(arena_pages);

        let mut aligned_offsets = Vec::new();
        for page in 0..=arena_pages - run_pages {
            let start = arena.ptr as usize + page * PAGE_SIZE;
            if start % align == 0 {
                aligned_offsets.push(page);
                if aligned_offsets.len() == needed_aligned_runs {
                    break;
                }
            }
        }
        assert_eq!(
            aligned_offsets.len(),
            needed_aligned_runs,
            "arena should contain enough aligned runs to overflow the stale skip cap"
        );

        let valid_node = arena.node(aligned_offsets[0]);
        let valid_addr = valid_node as *mut DoubleLinkedList as usize;
        freelist
            .insert_one_locked(idx, valid_node, rd_tree)
            .expect("insert valid tail candidate");

        let mut stale_addrs = Vec::new();
        for &offset in &aligned_offsets[1..] {
            let stale_node = arena.node(offset);
            let stale_addr = stale_node as *mut DoubleLinkedList as usize;
            freelist
                .insert_one_locked(idx, stale_node, rd_tree)
                .expect("insert stale candidate ahead of valid tail");
            stale_addrs.push(stale_addr);
        }
        assert_eq!(
            stale_addrs.len(),
            ALIGNED_RUN_STALE_SKIP_CAPACITY + 1,
            "test must fill the normal skip cap and require continuation"
        );

        for &stale_addr in &stale_addrs {
            FreeList::remove_run_markers(rd_tree, stale_addr, idx)
                .expect("remove stale candidate markers");
        }

        let reused = freelist
            .remove_head_aligned_run_locked(payload_pages, align, rd_tree)
            .expect("stale-cap continuation aligned scan should not fail")
            .expect("continuation should reach the valid candidate instead of falling back");

        assert_eq!(
            reused as usize, valid_addr,
            "candidate behind a saturated stale window should be reused"
        );
        assert_eq!(reused as usize % align, 0);
        assert!(
            marker_page_count(rd_tree.get_mut(checked_radix_key(valid_addr).expect("valid key")))
                .is_none(),
            "reused valid exact run should be removed from the radix tree"
        );
        for &stale_addr in &stale_addrs {
            assert!(
                marker_page_count(
                    rd_tree.get_mut(checked_radix_key(stale_addr).expect("stale key"))
                )
                .is_none(),
                "stale candidate markers should remain absent"
            );
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_aligned_run_fast_path_looks_ahead_for_less_fragmenting_larger_run() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(96);
        let align = PAGE_SIZE * 4;
        let compact_run_pages = 4;
        let roomy_run_pages = 5;
        let payload_pages = 2;

        let compact_two_fragment_offset = (0..=92)
            .find(|&page| {
                let start = arena.ptr as usize + page * PAGE_SIZE;
                let aligned = match checked_round_up_addr(start, align) {
                    Some(aligned) => aligned,
                    None => return false,
                };
                let prefix_pages = (aligned - start) / PAGE_SIZE;
                start % align != 0
                    && prefix_pages > 0
                    && prefix_pages + payload_pages < compact_run_pages
                    && page + compact_run_pages <= 96
            })
            .expect("arena should contain a compact run that splits into prefix+suffix");
        let roomy_one_fragment_offset = (0..=91)
            .find(|&page| {
                let no_overlap = page + roomy_run_pages <= compact_two_fragment_offset
                    || page >= compact_two_fragment_offset + compact_run_pages;
                let start = arena.ptr as usize + page * PAGE_SIZE;
                no_overlap && start % align == 0 && page + roomy_run_pages <= 96
            })
            .expect("arena should contain a nearby aligned larger run");

        let compact_node = arena.node(compact_two_fragment_offset);
        let compact_addr = compact_node as *mut DoubleLinkedList as usize;
        let roomy_node = arena.node(roomy_one_fragment_offset);
        let roomy_addr = roomy_node as *mut DoubleLinkedList as usize;
        let roomy_suffix_addr = roomy_addr + payload_pages * PAGE_SIZE;
        let compact_candidate = FreeList::aligned_payload_within_run(
            compact_addr,
            compact_run_pages,
            payload_pages,
            align,
        )
        .expect("compact candidate should contain an aligned payload");
        let roomy_candidate =
            FreeList::aligned_payload_within_run(roomy_addr, roomy_run_pages, payload_pages, align)
                .expect("roomy candidate should contain an aligned payload");
        assert_eq!(
            compact_candidate.fragment_count(),
            2,
            "a first-candidate size-class scan would split the compact run twice"
        );
        assert_eq!(
            roomy_candidate.fragment_count(),
            1,
            "the roomy candidate should leave one contiguous suffix"
        );
        assert!(
            roomy_candidate.is_less_fragmenting_than(&compact_candidate),
            "the bounded-lookahead policy should rank the roomy run ahead of the first compact candidate"
        );

        freelist
            .insert_one_locked(compact_run_pages - 1, compact_node, rd_tree)
            .expect("insert compact but two-fragment candidate");
        freelist
            .insert_one_locked(roomy_run_pages - 1, roomy_node, rd_tree)
            .expect("insert larger one-fragment candidate");

        let reused = freelist
            .remove_head_aligned_run_locked(payload_pages, align, rd_tree)
            .expect("aligned lookahead scan should not fail")
            .expect("larger one-fragment run should be reusable");

        assert_eq!(
            reused as usize, roomy_addr,
            "bounded lookahead should prefer one suffix fragment over prefix+suffix splitting"
        );
        assert_eq!(reused as usize % align, 0);
        assert_eq!(
            marker_page_count(
                rd_tree.get_mut(checked_radix_key(compact_addr).expect("compact key"))
            ),
            Some(compact_run_pages),
            "the smaller but more fragmenting run should remain available as a whole run"
        );
        assert_eq!(
            marker_page_count(
                rd_tree.get_mut(checked_radix_key(roomy_suffix_addr).expect("roomy suffix key"))
            ),
            Some(roomy_run_pages - payload_pages),
            "the selected larger run should leave one contiguous suffix"
        );

        let slice = freelist.try_get_slice().expect("slice");
        assert!(
            slice[compact_run_pages - 1].lock().is_some(),
            "compact two-fragment candidate should remain untouched"
        );
        assert!(slice[roomy_run_pages - 1].lock().is_none());
        assert!(slice[roomy_run_pages - payload_pages - 1].lock().is_some());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_aligned_run_candidate_scan_bounds_stale_skip_cycle() {
        let freelist = FreeList::new();
        let arena = MmapNodeArena::new(4);
        let idx = 1;
        let align = PAGE_SIZE * 2;
        let payload_pages = 1;

        let first = arena.node(0);
        let first_ptr = first as *mut DoubleLinkedList;
        let second = arena.node(1);
        let second_ptr = second as *mut DoubleLinkedList;

        // Model the smallest stale/corrupt shape that used to defeat the
        // useful-candidate cap: every reachable node is in the transient
        // stale-skip set, and the intrusive `next` links form a cycle.  The
        // candidate scanner must count traversal steps separately from useful
        // candidates so the size-class lock is released after bounded work.
        first.prev = Some(second_ptr);
        first.next = Some(second_ptr);
        second.prev = Some(first_ptr);
        second.next = Some(first_ptr);

        {
            let slice = freelist.try_get_slice().expect("free-list slice");
            let mut locked = slice[idx].lock();
            *locked = Some(first);
        }

        let stale_skips = [Some((idx, first_ptr)), Some((idx, second_ptr))];
        let candidate = freelist
            .find_aligned_run_candidate_locked(idx, align, payload_pages, &stale_skips, 1)
            .expect("bounded stale-skip scan should not fail");

        assert!(
            candidate.is_none(),
            "all candidates were stale; the scan should stop instead of looping"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_aligned_run_candidate_scan_cap_bounds_per_class_walk() {
        fn overlaps(page: usize, pages: usize, used: &[(usize, usize)]) -> bool {
            used.iter().any(|&(used_page, used_pages)| {
                page < used_page + used_pages && used_page < page + pages
            })
        }

        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena_pages = 192;
        let arena = MmapNodeArena::new(arena_pages);
        let align = PAGE_SIZE * 4;
        let run_pages = 4;
        let payload_pages = 2;
        let idx = run_pages - 1;

        let aligned_one_fragment_offset = (0..=arena_pages - run_pages)
            .find(|&page| {
                let start = arena.ptr as usize + page * PAGE_SIZE;
                start % align == 0 && page + run_pages <= arena_pages
            })
            .expect("arena should contain an aligned one-fragment run");
        let aligned_node = arena.node(aligned_one_fragment_offset);
        let aligned_addr = aligned_node as *mut DoubleLinkedList as usize;

        let mut used = Vec::new();
        used.push((aligned_one_fragment_offset, run_pages));
        let mut two_fragment_offsets = Vec::new();
        for page in 0..=arena_pages - run_pages {
            if overlaps(page, run_pages, &used) {
                continue;
            }
            let start = arena.ptr as usize + page * PAGE_SIZE;
            let aligned = match checked_round_up_addr(start, align) {
                Some(aligned) => aligned,
                None => continue,
            };
            let prefix_pages = (aligned - start) / PAGE_SIZE;
            if start % align != 0 && prefix_pages > 0 && prefix_pages + payload_pages < run_pages {
                two_fragment_offsets.push(page);
                used.push((page, run_pages));
                if two_fragment_offsets.len() == ALIGNED_RUN_CANDIDATE_SCAN_NODES {
                    break;
                }
            }
        }
        assert_eq!(
            two_fragment_offsets.len(),
            ALIGNED_RUN_CANDIDATE_SCAN_NODES,
            "test needs enough same-class prefix+suffix candidates to fill the scan cap"
        );

        freelist
            .insert_one_locked(idx, aligned_node, rd_tree)
            .expect("insert one-fragment tail candidate");
        for offset in two_fragment_offsets {
            freelist
                .insert_one_locked(idx, arena.node(offset), rd_tree)
                .expect("insert two-fragment capped-scan candidate");
        }

        let capped = freelist
            .find_aligned_run_candidate_locked(
                idx,
                align,
                payload_pages,
                &[],
                ALIGNED_RUN_CANDIDATE_SCAN_NODES,
            )
            .expect("capped candidate scan should not fail")
            .expect("capped scan should still find a same-class candidate");
        assert_eq!(
            capped.fragment_count(),
            2,
            "the per-class cap should stop before the better tail candidate"
        );
        assert_ne!(capped.aligned_addr, aligned_addr);

        let uncapped_by_one = freelist
            .find_aligned_run_candidate_locked(
                idx,
                align,
                payload_pages,
                &[],
                ALIGNED_RUN_CANDIDATE_SCAN_NODES + 1,
            )
            .expect("one-more-node candidate scan should not fail")
            .expect("one-more-node scan should reach the tail candidate");
        assert_eq!(
            uncapped_by_one.aligned_addr, aligned_addr,
            "scanning one extra node proves the better candidate exists past the cap"
        );
        assert_eq!(uncapped_by_one.fragment_count(), 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn freelist_head_aligned_run_fast_path_skips_unaligned_run() {
        let freelist = FreeList::new();
        let rd_tree = empty_radix_tree();
        let arena = MmapNodeArena::new(8);
        let align = PAGE_SIZE * 4;
        let payload_pages = 2;
        let offset = (0..=6)
            .find(|page| (arena.ptr as usize + page * PAGE_SIZE) % align != 0)
            .expect("arena should contain an unaligned page-run start");
        let node = arena.node(offset);
        let node_ptr = node as *mut DoubleLinkedList;

        freelist
            .insert_one_locked(payload_pages - 1, node, rd_tree)
            .expect("insert exact-size but unaligned run");

        let reused = freelist
            .remove_head_aligned_run_locked(payload_pages, align, rd_tree)
            .expect("head aligned scan should not fail");

        assert!(reused.is_none());
        assert_eq!(
            marker_page_count(
                rd_tree.get_mut(checked_radix_key(node_ptr as usize).expect("node key"))
            ),
            Some(payload_pages)
        );
        assert!(freelist.try_get_slice().expect("slice")[payload_pages - 1]
            .lock()
            .is_some());
    }

    #[test]
    fn freelist_backend_slice_allocation_respects_array_alignment() {
        #[cfg(feature = "fixed_heap")]
        if run_fixed_heap_capacity_test_in_fresh_process(
            "UNIALLOC_FIXED_FREELIST_BACKEND_SLICE_CHILD",
            "freelist::tests::freelist_backend_slice_allocation_respects_array_alignment",
        ) {
            return;
        }
        #[cfg(feature = "fixed_heap")]
        let _fixed_heap_guard = crate::sc::fixed_heap_test_guard();

        let freelist = FreeList::new();
        let slice = freelist
            .try_get_slice()
            .expect("metadata slice allocation should succeed");
        let ptr = slice.as_ptr() as usize;
        assert_eq!(
            ptr % align_of::<[Mutex<Option<&'static mut DoubleLinkedList>>; BACKEND_MAX_PAGE]>(),
            0
        );
    }

    #[test]
    fn freelist_write_list_node_resets_valid_node() {
        let node = Box::leak(Box::new(DoubleLinkedList {
            prev: Some(core::ptr::null_mut()),
            next: Some(core::ptr::null_mut()),
        }));
        let rewritten = FreeList::write_list_node(node as *mut DoubleLinkedList as usize)
            .expect("valid leaked node");

        assert!(rewritten.prev.is_none());
        assert!(rewritten.next.is_none());
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn fixed_heap_free_splits_oversized_run_into_reusable_chunks() {
        let _fixed_heap_guard = crate::sc::fixed_heap_test_guard();
        let freelist = FreeList::new();
        let total_pages = BACKEND_MAX_PAGE + 3;
        let total_size = total_pages
            .checked_mul(PAGE_SIZE)
            .expect("oversized test run size");
        let max_chunk_size = BACKEND_MAX_PAGE
            .checked_mul(PAGE_SIZE)
            .expect("max chunk size");
        let tail_size = 3usize.checked_mul(PAGE_SIZE).expect("tail chunk size");

        let guarded_total_size = total_size
            .checked_add(PAGE_SIZE * 2)
            .expect("guarded oversized test run size");
        let raw = BUMP
            .lock()
            .alloc(guarded_total_size)
            .expect("fixed heap should provide a guarded oversized page run");
        let ptr = raw.wrapping_add(PAGE_SIZE);

        // The fixed-heap radix tree is global across tests.  Keep guard pages
        // allocated on both sides of this run so `free` cannot coalesce this
        // test-local run with unrelated free chunks left by earlier tests.
        freelist.free(ptr, total_size);

        let reused_head = freelist
            .alloc(max_chunk_size)
            .expect("oversized fixed-heap free should expose a max-size chunk");
        assert_eq!(reused_head, ptr);

        let reused_tail = freelist
            .alloc(tail_size)
            .expect("oversized fixed-heap free should expose the tail chunk");
        assert_eq!(
            reused_tail as usize,
            (ptr as usize) + max_chunk_size,
            "tail chunk should start immediately after the max-size chunk"
        );

        freelist.free(reused_head, max_chunk_size);
        freelist.free(reused_tail, tail_size);
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn fixed_heap_oversized_run_split_rolls_back_on_insert_failure() {
        let _fixed_heap_guard = crate::sc::fixed_heap_test_guard();
        let freelist = FreeList::new();
        let total_pages = BACKEND_MAX_PAGE + 1;
        let total_size = total_pages
            .checked_mul(PAGE_SIZE)
            .expect("oversized test run size");
        let ptr = BUMP
            .lock()
            .alloc(total_size)
            .expect("fixed heap should provide an oversized page run");

        try_with_rd_tree(|rd_tree| {
            let err = freelist
                .insert_fixed_heap_oversized_run_chunks_locked_with_test_failure(
                    ptr as usize,
                    total_pages,
                    rd_tree,
                    BACKEND_MAX_PAGE,
                    1,
                )
                .expect_err("injected failure after first chunk should fail insertion");

            assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
            assert_eq!(
                marker_page_count(
                    rd_tree.get_mut(checked_radix_key(ptr as usize).expect("first chunk key"))
                ),
                None,
                "rollback must remove the first inserted max-size chunk marker"
            );
            assert!(
                freelist.try_get_slice().expect("slice")[BACKEND_MAX_PAGE - 1]
                    .lock()
                    .is_none(),
                "rollback must remove the first inserted max-size chunk list node"
            );
        })
        .expect("global fixed-heap radix tree should be available");
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn fixed_heap_alloc_aligned_chunks_oversized_fallback_slack_for_reuse() {
        let _fixed_heap_guard = crate::sc::fixed_heap_test_guard();
        let freelist = FreeList::new();
        let align_pages = 512usize;
        let align = checked_page_size_from_count(align_pages).expect("large alignment bytes");
        let payload_size = PAGE_SIZE;

        let ptr = freelist
            .alloc_aligned(payload_size, align)
            .expect("fixed-heap alloc_aligned should publish oversized slack");
        assert_eq!(ptr as usize % align, 0);

        let mut reusable_pages = 0usize;
        let mut reusable_chunks = 0usize;
        let mut saw_max_chunk = false;
        while let Some(idx) = freelist.next_non_empty_index(0, BACKEND_MAX_PAGE) {
            let run = freelist
                .remove_one(idx)
                .expect("non-empty bitmap entry should name a removable split run");
            assert!(!run.is_null());
            let pages = idx + 1;
            reusable_pages = reusable_pages
                .checked_add(pages)
                .expect("reusable page count");
            reusable_chunks += 1;
            saw_max_chunk |= pages == BACKEND_MAX_PAGE;
        }

        assert_eq!(
            reusable_pages,
            align_pages - 1,
            "all alignment slack pages should remain reachable through freelist chunks"
        );
        assert!(
            saw_max_chunk,
            "PAGE_SIZE*512 alignment should create at least one max-size fixed-heap slack chunk"
        );
        assert!(
            reusable_chunks >= 2,
            "oversized slack should be split into multiple reusable chunks"
        );
    }
}

/// Global buddy-style free-list front end.
///
/// `FreeList` publishes its page-run lists through an `AtomicPtr` and protects
/// each run-list with a `spin::Mutex`, so callers only need shared access to the
/// global root.  Keeping the binding immutable avoids creating `&mut` references
/// to a mutable static on Rust 2024-compatible toolchains.
pub static FREELIST: FreeList = FreeList::new();

/// # Experimental
///
/// It can be very hard to use if we do not have `Copy` trait.
/// Thus, we sadly add another global variable :(
#[derive(Copy, Clone)]
pub struct BuddySystemAllocator;

impl Default for BuddySystemAllocator {
    fn default() -> Self {
        BuddySystemAllocator
    }
}

unsafe impl GlobalAlloc for BuddySystemAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if layout.size() == 0 || layout.size() > isize::MAX as usize {
            return core::ptr::null_mut::<u8>();
        }
        #[cfg(all(
            feature = "adaptive_bitmap_page_allocator",
            not(feature = "fixed_heap")
        ))]
        {
            crate::adaptive_bitmap_alloc::allocate_layout(layout)
        }
        #[cfg(feature = "bitmap_page_allocator")]
        {
            crate::bitmap_alloc::PAGE_RUN_BITMAP
                .lock()
                .allocate_bytes(layout.size(), layout.align())
                .unwrap_or(null_mut())
        }
        #[cfg(all(
            feature = "hosted_bitmap_page_allocator",
            not(feature = "adaptive_bitmap_page_allocator"),
            not(feature = "fixed_heap")
        ))]
        {
            crate::hosted_bitmap_alloc::HOSTED_PAGE_RUN_BITMAP
                .allocate_layout(layout)
                .unwrap_or(null_mut())
        }
        #[cfg(not(any(
            feature = "bitmap_page_allocator",
            all(feature = "hosted_bitmap_page_allocator", not(feature = "fixed_heap"))
        )))]
        if layout.align() > crate::PAGE_SIZE {
            if layout.align() % crate::PAGE_SIZE != 0 {
                return core::ptr::null_mut::<u8>();
            }
            return FREELIST
                .alloc_aligned(layout.size(), layout.align())
                .unwrap_or(null_mut());
        }
        #[cfg(not(any(
            feature = "bitmap_page_allocator",
            all(feature = "hosted_bitmap_page_allocator", not(feature = "fixed_heap"))
        )))]
        FREELIST.alloc(layout.size()).unwrap_or(null_mut())
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        if !ptr.is_null() && layout.size() != 0 {
            #[cfg(all(
                feature = "adaptive_bitmap_page_allocator",
                not(feature = "fixed_heap")
            ))]
            {
                crate::adaptive_bitmap_alloc::deallocate_layout(ptr, layout);
            }
            #[cfg(feature = "bitmap_page_allocator")]
            {
                let result = crate::bitmap_alloc::PAGE_RUN_BITMAP
                    .lock()
                    .deallocate_bytes(ptr, layout.size());
                debug_assert!(result.is_ok(), "bitmap page-run deallocation rejected");
            }
            #[cfg(all(
                feature = "hosted_bitmap_page_allocator",
                not(feature = "adaptive_bitmap_page_allocator"),
                not(feature = "fixed_heap")
            ))]
            {
                let result = crate::hosted_bitmap_alloc::HOSTED_PAGE_RUN_BITMAP
                    .deallocate_layout(ptr, layout);
                debug_assert!(
                    result.is_ok(),
                    "hosted bitmap page-run deallocation rejected"
                );
            }
            #[cfg(not(any(
                feature = "bitmap_page_allocator",
                all(feature = "hosted_bitmap_page_allocator", not(feature = "fixed_heap"))
            )))]
            FREELIST.free(ptr, layout.size())
        }
    }
}

unsafe impl Allocator for BuddySystemAllocator {
    /// Follow the implementation in
    /// https://github.com/rust-lang/rust/blob/master/library/alloc/src/alloc.rs#L161
    fn allocate(&self, layout: Layout) -> Result<NonNull<[u8]>, core::alloc::AllocError> {
        match layout.size() {
            0 => Ok(dangling_zero_size_slice(layout)),
            // SAFETY: `layout` is non-zero in size,
            size => unsafe {
                let raw_ptr = self.alloc(layout);
                let ptr = NonNull::new(raw_ptr).ok_or(core::alloc::AllocError)?;
                Ok(NonNull::slice_from_raw_parts(ptr, size))
            },
        }
    }

    unsafe fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
        if layout.size() != 0 {
            self.dealloc(ptr.as_ptr(), layout);
        }
    }
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn buddy_system_allocator_allocates_over_page_alignment_with_backend_split() {
        #[cfg(feature = "fixed_heap")]
        let _fixed_heap_guard = crate::sc::fixed_heap_test_guard();

        let layout = Layout::from_size_align(8, crate::PAGE_SIZE * 2).unwrap();
        unsafe {
            let ptr = BuddySystemAllocator.alloc(layout);
            assert!(!ptr.is_null());
            assert_eq!(ptr as usize % layout.align(), 0);
            BuddySystemAllocator.dealloc(ptr, layout);
        }
        let block = BuddySystemAllocator
            .allocate(layout)
            .expect("Allocator trait over-page allocation");
        let ptr = block.as_ptr() as *mut u8;
        assert_eq!(ptr as usize % layout.align(), 0);
        unsafe {
            BuddySystemAllocator.deallocate(NonNull::new(ptr).unwrap(), layout);
        }
    }

    #[test]
    fn buddy_system_allocator_keeps_zero_sized_allocator_contract() {
        let layout = Layout::from_size_align(0, crate::PAGE_SIZE * 2).unwrap();
        unsafe {
            assert!(BuddySystemAllocator.alloc(layout).is_null());
        }
        let block = BuddySystemAllocator
            .allocate(layout)
            .expect("Allocator trait still represents zero-sized layouts");
        let ptr = block.as_ptr() as *mut u8 as usize;
        assert_ne!(ptr, 0);
        assert_eq!(ptr % layout.align(), 0);
        assert_eq!(block.len(), 0);
    }

    #[cfg(all(target_arch = "x86_64", target_os = "linux"))]
    #[test]
    fn it_works() {
        let lay = Layout::from_size_align(4095, 1).expect("err");
        unsafe {
            let ptr = BuddySystemAllocator.alloc(lay);
            let ptr2 = BuddySystemAllocator.alloc(lay);
            BuddySystemAllocator.dealloc(ptr, lay);
            BuddySystemAllocator.dealloc(ptr2, lay);
            let ptr3 = BuddySystemAllocator.alloc(lay);
            let ptr4 = BuddySystemAllocator.alloc(lay);
            assert_ne!(ptr3 as usize, ptr4 as usize)
        }
    }
}
