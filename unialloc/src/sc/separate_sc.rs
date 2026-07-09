use crate::collections::linklist::*;
use crate::collections::radix_tree::RadixTree;
use crate::collections::radix_tree::{allocate_node, RadixBottomNode, TreeNode};
use crate::error::{AllocError, Result};
use crate::page::{ObjectPage, ObjectPageAllocationState};
use crate::prelude::*;
use crate::*;
use alloc::vec::Vec;
use core::alloc::Layout;
use core::borrow::BorrowMut;
use core::cmp::Ordering;
use core::ptr::{self, NonNull};

/// Maximum number of fully empty one-OS-page object pages retained per size class.
///
/// This bitmap-backed slab implementation is not the currently exported slab
/// path, but keeping its retention policy aligned with `efficient_sc` prevents
/// future feature switches from reintroducing the old >2048-empty-pages RSS
/// cliff. Empty pages beyond the warm cache are recycled to the uninitialized
/// list so their backing memory can be released.
///
/// The live policy scales this cap by the span size (`pg_num`) and disables
/// warm retention for single-slot spans.  Tiny multi-slot classes keep 8
/// one-page spans, while multi-page classes keep fewer or no empty spans so
/// external fragmentation is bounded in OS pages rather than object-page
/// descriptors.
const EMPTY_SLAB_RETAIN_LIMIT: usize = 8;
const EMPTY_SLAB_RETAIN_OS_PAGE_BUDGET: usize = 8;
/// Number of bitmap-backed object-page descriptors reserved on first growth.
///
/// `separate_sc` is the alternate bitmap-backed slab implementation.  It used
/// to reserve 512 page descriptors and 1024 `u32` bitmap words the first time a
/// size class needed a page.  That hidden fixed cost is at odds with UniAlloc's
/// memory-footprint goals and made this path expensive to enable for small
/// heaps.  Keep amortized growth, but reserve a small descriptor batch and size
/// the bitfield reservation from the actual words needed per object page.
const SEPARATE_SC_METADATA_RESERVE_PAGES: usize = 16;

/// Tiny stack-local cache used while draining a thread-cache batch.
///
/// `ObjectPage::deallocate_batch` already handles a contiguous same-page prefix
/// in one page-local pass.  Real thread-cache lists can still interleave pages
/// (A, B, A, ...), so a few set-associative entries avoid repeated radix-tree
/// lookups without changing batch order or stale-pointer semantics.  This is
/// stack-local: the alternate bitmap slab gets the same conflict resistance as
/// `efficient_sc`'s batch-dealloc cache without persistent allocator metadata.
/// The cache is invalidated when a page is recycled and its radix mapping is
/// removed.
const DEALLOCATE_BATCH_PAGE_INDEX_CACHE_SETS: usize = 4;
const DEALLOCATE_BATCH_PAGE_INDEX_CACHE_WAYS: usize = 2;
const DEALLOCATE_BATCH_PAGE_INDEX_CACHE_ENTRIES: usize =
    DEALLOCATE_BATCH_PAGE_INDEX_CACHE_SETS * DEALLOCATE_BATCH_PAGE_INDEX_CACHE_WAYS;
const DEALLOCATE_BATCH_PAGE_INDEX_CACHE_EMPTY: usize = usize::MAX;

#[inline]
fn empty_slab_retain_limit_for_pg_num(pg_num: usize) -> usize {
    if pg_num == 0 || pg_num > EMPTY_SLAB_RETAIN_OS_PAGE_BUDGET {
        return 0;
    }
    core::cmp::min(
        EMPTY_SLAB_RETAIN_LIMIT,
        EMPTY_SLAB_RETAIN_OS_PAGE_BUDGET / pg_num,
    )
}

#[inline]
fn empty_slab_retain_limit_for_geometry(pg_num: usize, pg_count: usize) -> usize {
    if pg_count <= 1 {
        0
    } else {
        empty_slab_retain_limit_for_pg_num(pg_num)
    }
}

#[inline]
fn strict_alignment_batch_request_limit(align: usize, pg_align: usize) -> usize {
    if align <= pg_align {
        return usize::MAX;
    }

    let object_limit = crate::page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT.max(1);
    let page_payload_limit = if pg_align == 0 {
        1
    } else {
        (PAGE_SIZE / pg_align).max(1)
    };
    core::cmp::min(object_limit, page_payload_limit)
}

pub fn align_12k(ptr: usize) -> usize {
    let mut page_vaddr = ptr & !((PAGE_SIZE - 1) as usize);
    match 12_u32.cmp(&PAGE_SIZE.trailing_zeros()) {
        Ordering::Greater => page_vaddr <<= 12 - PAGE_SIZE.trailing_zeros(),
        Ordering::Less => page_vaddr >>= PAGE_SIZE.trailing_zeros() - 12,
        Ordering::Equal => {}
    };
    page_vaddr
}

/// A slab allocator allocates elements of a fixed size.
///
/// It maintains three internal lists of `ObjectPage8k`
/// from which it can allocate memory.
///
///  * `empty_slabs`: Is a list of pages that the SCAllocator maintains, but
///    has 0 allocations in them.
///  * `slabs`: A list of pages partially allocated and still have room for more.
///  * `full_slabs`: A list of pages that are completely allocated.
///
/// On allocation we allocate memory from `slabs`, however if the list is empty
/// we try to reclaim a page from `empty_slabs` before we return with an out-of-memory
/// error. If a page becomes full after the allocation we move it from `slabs` to
/// `full_slabs`.
///
/// Similarly, on dealloaction we might move a page from `full_slabs` to `slabs`
/// or from `slabs` to `empty_slabs` after we deallocated an object.
pub struct SCAllocator {
    /// Tracks the start of full slab. It is meaningful only if `full_count >= 1`
    full_start: usize,
    /// Tracks the start of partial slab. It is meaningful only if `partial_count >= 1`
    partial_start: usize,
    /// Tracks the start of empty slab. It is meaningful only if `empty_count >= 1`
    empty_start: usize,
    /// Tracks the start of uninitialized slab. It is meaningful only if `uninit_count >= 1`
    uninit_start: usize,
    full_count: usize,
    empty_count: usize,
    partial_count: usize,
    uninit_count: usize,

    // start, count
    // full: (usize, usize),
    // partial: (usize, usize),
    // empty: (usize, usize),
    // uninit: (usize, usize),

    // page_num: how many OS pages in our page
    pg_num: usize,
    // pg_count: how many chunks of current size class in one "page"
    pg_count: usize,
    // when allocating, what align between chunks shall we take
    pg_align: usize,
    bitfields: Vec<u32, GlobalBackend>,
    pages: ArrayLinkedList<ObjectPage>,
}

#[derive(Clone, Copy)]
struct RadixPageChunk {
    key: usize,
    len: usize,
}

#[derive(Clone, Copy)]
struct RadixPageChunks {
    first: RadixPageChunk,
    second: Option<RadixPageChunk>,
}

impl SCAllocator {
    fn empty() -> Self {
        Self {
            full_start: 0,
            partial_start: 0,
            empty_start: 0,
            uninit_start: 0,
            full_count: 0,
            empty_count: 0,
            partial_count: 0,
            uninit_count: 0,
            pg_count: 0,
            pg_num: 0,
            pg_align: 0,
            bitfields: Vec::with_capacity_in(0, GlobalBackend),
            pages: ArrayLinkedList::new(),
        }
    }

    // The new "new" function takes three parameters:
    // current size class, current size class's idx, how many OS pages are combined into one page.rs
    pub fn new(size_class: usize, num_os_pages: usize) -> Self {
        #[cfg(all(test, feature = "fixed_heap"))]
        crate::sc::ensure_fixed_heap_test_initialized();

        let (pg_count, align) = match super::checked_size_class_geometry(size_class, num_os_pages) {
            Some(geometry) => geometry,
            None => return Self::empty(),
        };

        Self {
            full_start: 0,
            partial_start: 0,
            empty_start: 0,
            uninit_start: 0,
            full_count: 0,
            empty_count: 0,
            partial_count: 0,
            uninit_count: 0,
            pg_count,
            pg_num: num_os_pages,
            pg_align: align,
            bitfields: Vec::with_capacity_in(0, GlobalBackend),
            pages: ArrayLinkedList::new(),
        }
    }

    pub fn remove_full(&mut self, idx: usize) -> Result<()> {
        if self.full_count == 0 || idx >= self.pages.len() {
            return Err(AllocError::EFATAL);
        }
        let next = self.pages.get_next(idx)?;
        self.pages.try_remove_node(idx)?;
        if idx == self.full_start {
            self.full_start = next;
        }
        self.full_count = self.full_count.checked_sub(1).ok_or(AllocError::EFATAL)?;
        Ok(())
    }

    pub fn insert_full(&mut self, idx: usize) -> Result<()> {
        if idx >= self.pages.len() {
            return Err(AllocError::EFATAL);
        }
        if self.full_count == 0 {
            self.full_start = idx;
            self.pages.try_reset_links(idx)?;
        } else {
            if self.full_start >= self.pages.len() {
                return Err(AllocError::EFATAL);
            }
            self.pages.try_insert_to_prev(self.full_start, idx)?;
        }
        self.full_count = self.full_count.checked_add(1).ok_or(AllocError::EFATAL)?;
        Ok(())
    }

    pub fn insert_partial(&mut self, idx: usize) -> Result<()> {
        if idx >= self.pages.len() {
            return Err(AllocError::EFATAL);
        }
        if self.partial_count == 0 {
            self.partial_start = idx;
            self.pages.try_reset_links(idx)?;
        } else {
            if self.partial_start >= self.pages.len() {
                return Err(AllocError::EFATAL);
            }
            self.pages.try_insert_to_prev(self.partial_start, idx)?;
        }
        self.partial_count = self
            .partial_count
            .checked_add(1)
            .ok_or(AllocError::EFATAL)?;
        Ok(())
    }

    pub fn remove_partial(&mut self, idx: usize) -> Result<()> {
        if self.partial_count == 0 || idx >= self.pages.len() {
            return Err(AllocError::EFATAL);
        }
        let next = self.pages.get_next(idx)?;
        self.pages.try_remove_node(idx)?;
        if idx == self.partial_start {
            self.partial_start = next;
        }
        self.partial_count = self
            .partial_count
            .checked_sub(1)
            .ok_or(AllocError::EFATAL)?;
        Ok(())
    }

    pub fn remove_uninit(&mut self, idx: usize) -> Result<()> {
        if self.uninit_count == 0 || idx >= self.pages.len() {
            return Err(AllocError::EFATAL);
        }
        let next = self.pages.get_next(idx)?;
        self.pages.try_remove_node(idx)?;
        if idx == self.uninit_start {
            self.uninit_start = next;
        }
        self.uninit_count = self.uninit_count.checked_sub(1).ok_or(AllocError::EFATAL)?;
        Ok(())
    }

    pub fn get_uninit(&mut self) -> Result<(usize, usize)> {
        if self.pg_num == 0 || self.pg_count == 0 {
            return Err(AllocError::ESIZE);
        }
        if self.uninit_count == 0 {
            let n = Self::bitfield_words_per_page(self.pg_count)?;
            self.ensure_uninit_metadata_capacity(n)?;

            // The linklist's algorithm sets its prev and next to itself.
            self.pages.push(ObjectPage::new());
            for _ in 0..n {
                self.bitfields.push(0);
            }
            let index = self.pages.len() - 1;

            self.uninit_start = index;
            self.uninit_count += 1;
        }

        let res = self.uninit_start;
        self.remove_uninit(self.uninit_start)?;

        let obj = self.pages.get_mut(res as usize).ok_or(AllocError::EFATAL)?;
        let ptr = match obj.allocate_page(self.pg_num) {
            Ok(ptr) => ptr as usize,
            Err(err) => {
                // The descriptor and its bitfield metadata have already been
                // created and removed from the uninitialized ring.  If backing
                // page allocation/layout validation fails, put the descriptor
                // back instead of leaking metadata and growing another
                // descriptor on every retry.
                return Err(self.back_uninit(res).err().unwrap_or(err));
            }
        };

        Ok((res, ptr))
    }

    #[inline]
    fn bitfield_words_per_page(pg_count: usize) -> Result<usize> {
        if pg_count == 0 {
            return Err(AllocError::ESIZE);
        }
        pg_count
            .checked_add(31)
            .map(|rounded| rounded / 32)
            .ok_or(AllocError::EFATAL)
    }

    #[inline]
    fn bitfield_words_for_current_page(&self) -> Result<usize> {
        Self::bitfield_words_per_page(self.pg_count)
    }

    #[inline]
    fn bitfield_reserve_words_for_descriptor_spare(
        bitfield_spare_words: usize,
        bitfield_words_per_page: usize,
        descriptor_spare_pages: usize,
    ) -> Result<usize> {
        if bitfield_words_per_page == 0 || descriptor_spare_pages == 0 {
            return Err(AllocError::ESIZE);
        }
        let target_spare_words = bitfield_words_per_page
            .checked_mul(descriptor_spare_pages)
            .ok_or(AllocError::EFATAL)?;
        Ok(target_spare_words.saturating_sub(bitfield_spare_words))
    }

    /// Keep bitmap metadata capacity aligned with descriptor capacity.
    ///
    /// `pages.try_reserve()` controls how many future object-page descriptors can be
    /// appended without reallocating.  The bitmap vector needs exactly
    /// `bitfield_words_per_page` words for each of those spare descriptors.
    /// Matching that spare capacity preserves amortized growth without the old
    /// fixed "reserve 16 pages worth" behavior when only one descriptor slot is
    /// missing bitmap words.
    fn ensure_uninit_metadata_capacity(&mut self, bitfield_words_per_page: usize) -> Result<()> {
        if bitfield_words_per_page == 0 {
            return Err(AllocError::ESIZE);
        }
        if self.pages.capacity().saturating_sub(self.pages.len()) == 0 {
            self.pages.try_reserve(SEPARATE_SC_METADATA_RESERVE_PAGES)?;
        }

        let descriptor_spare_pages = self.pages.capacity().saturating_sub(self.pages.len());
        let bitfield_spare_words = self
            .bitfields
            .capacity()
            .saturating_sub(self.bitfields.len());
        let additional_words = Self::bitfield_reserve_words_for_descriptor_spare(
            bitfield_spare_words,
            bitfield_words_per_page,
            descriptor_spare_pages,
        )?;
        if additional_words != 0 {
            self.bitfields
                .try_reserve(additional_words)
                .map_err(|_| AllocError::ENOMEM)?;
        }
        Ok(())
    }

    pub fn get_empty(&mut self) -> Result<(usize, Option<usize>)> {
        if self.empty_count > 0 {
            let res = self.empty_start;
            if res >= self.pages.len() {
                return Err(AllocError::EFATAL);
            }
            let next = self.pages.get_next(self.empty_start)?;
            self.pages.try_remove_node(res)?;
            self.empty_start = next;

            self.empty_count = self.empty_count.checked_sub(1).ok_or(AllocError::EFATAL)?;
            Ok((res, None))
        } else {
            let ans = self.get_uninit()?;
            Ok((ans.0, Some(ans.1)))
        }
    }

    pub fn back_uninit(&mut self, idx: usize) -> Result<Option<*mut u8>> {
        if idx >= self.pages.len() {
            return Err(AllocError::EFATAL);
        }
        if self.uninit_count == 0 {
            self.uninit_start = idx;
            self.pages.try_reset_links(idx)?;
        } else {
            if self.uninit_start >= self.pages.len() {
                return Err(AllocError::EFATAL);
            }
            self.pages.try_insert_to_prev(self.uninit_start, idx)?;
        }
        let obj = self.pages.get_mut(idx as usize).ok_or(AllocError::EFATAL)?;
        self.uninit_count = self.uninit_count.checked_add(1).ok_or(AllocError::EFATAL)?;
        Ok(obj.destroy_page(self.pg_num))
    }

    // This function is only used in deallocate, for more info, refer to that function
    pub fn try_back_ety(&mut self, idx: usize) -> Result<Option<usize>> {
        if self.empty_count >= self.empty_slab_retain_limit() {
            let ptr = self
                .pages
                .get_mut(idx as usize)
                .ok_or(AllocError::EFATAL)?
                .get_data_ptr();
            Ok(ptr.map(|ptr| ptr as usize))
        } else {
            Ok(None)
        }
    }

    pub fn back_ety(&mut self, idx: usize) -> Result<Option<usize>> {
        if idx >= self.pages.len() {
            return Err(AllocError::EFATAL);
        }
        if self.empty_count >= self.empty_slab_retain_limit() {
            let ptr = self.back_uninit(idx)?;
            Ok(ptr.map(|ptr| ptr as usize))
        } else {
            if self.empty_count == 0 {
                self.empty_start = idx;
                self.pages.try_reset_links(idx)?;
            } else {
                if self.empty_start >= self.pages.len() {
                    return Err(AllocError::EFATAL);
                }
                self.pages.try_insert_to_prev(self.empty_start, idx)?;
            }
            self.empty_count = self.empty_count.checked_add(1).ok_or(AllocError::EFATAL)?;
            Ok(None)
        }
    }

    #[inline]
    pub(crate) fn empty_slab_retain_limit(&self) -> usize {
        empty_slab_retain_limit_for_geometry(self.pg_num, self.pg_count)
    }

    pub(crate) fn retained_empty_slab_os_pages(&self) -> usize {
        self.empty_count.saturating_mul(self.pg_num)
    }

    #[cfg(test)]
    pub(crate) fn retained_empty_slab_count_for_tests(&self) -> usize {
        self.empty_count
    }

    pub(crate) fn recycle_one_retained_empty_slab(
        &mut self,
        ptr_map: &mut RadixTree,
    ) -> Result<bool> {
        if self.empty_count == 0 {
            return Ok(false);
        }
        let evicted_idx = self.remove_empty_index()?;
        let evicted_addr = match self
            .pages
            .get_mut(evicted_idx)
            .and_then(|page| page.get_data_ptr())
            .map(|ptr| ptr as usize)
        {
            Some(addr) => addr,
            None => {
                let _ = self.back_ety(evicted_idx);
                return Ok(false);
            }
        };

        if let Err(err) = self.handle_rd_tree_remove(ptr_map, evicted_addr) {
            let _ = self.back_ety(evicted_idx);
            return Err(err);
        }

        if let Err(err) = self.back_uninit(evicted_idx) {
            let _ = self.handle_rd_tree_insert(ptr_map, evicted_idx, evicted_addr);
            let _ = self.back_ety(evicted_idx);
            return Err(err);
        }

        Ok(true)
    }

    /// Allocates a block of memory described by `layout`.
    ///
    /// Returns a pointer to a valid region of memory or an
    /// Error.
    ///
    /// The function may also move around pages between lists
    /// (empty -> partial or partial -> full).
    pub fn allocate(&mut self, align: usize, ptr_map: &mut RadixTree) -> Result<NonNull<u8>> {
        let mut ptr: *mut u8;
        let n = self.bitfield_words_for_current_page()?;
        if self.partial_count > 0 {
            let mut head = self.partial_start;
            loop {
                let (bit_start, bit_end) = self.bitfield_range(head, n)?;
                let (allocation_state, became_full) = {
                    let obj = self
                        .pages
                        .get_mut(head as usize)
                        .ok_or(AllocError::EFATAL)?;
                    let allocation_state = obj.allocation_state_snapshot();
                    ptr = obj.allocate(
                        align,
                        &mut self.bitfields[bit_start..bit_end],
                        self.pg_count,
                        self.pg_align,
                        self.pg_num,
                    );
                    (
                        allocation_state,
                        !ptr.is_null() && obj.is_full(self.pg_count),
                    )
                };
                if ptr.is_null() {
                    head = self.pages.get_next(head)?;
                } else {
                    if became_full {
                        let ptr_addr = ptr as usize;
                        let transition_result = self.remove_partial(head).and_then(|()| {
                            if let Err(err) = self.insert_full(head) {
                                let _ = self.insert_partial(head);
                                Err(err)
                            } else {
                                Ok(())
                            }
                        });
                        if let Err(err) = transition_result {
                            let restore_result = self.restore_allocated_prefix(
                                head,
                                n,
                                allocation_state,
                                core::slice::from_ref(&ptr_addr),
                            );
                            return Err(restore_result.err().unwrap_or(err));
                        }
                    } else if align > self.pg_align {
                        // A strict-alignment request may skip earlier partial
                        // pages whose free slots do not satisfy `align`.
                        // Promote the successful still-partial page to the
                        // scan cursor so the next strict request does not
                        // re-walk the same failing prefix before reaching the
                        // page that just proved useful.  Normal first-fit
                        // allocations keep the historical cursor policy.
                        self.partial_start = head;
                    }
                    break;
                }
                if head == self.partial_start {
                    break;
                }
            }
            if !ptr.is_null() {
                return NonNull::new(ptr).ok_or(AllocError::ENOMEM);
            }
        } //final case, try to get a new one
        let idx = self.get_empty()?;
        ptr = {
            let (bit_start, bit_end) = self.bitfield_range(idx.0, n)?;
            let obj = self
                .pages
                .get_mut(idx.0 as usize)
                .ok_or(AllocError::EFATAL)?;
            obj.allocate(
                align,
                &mut self.bitfields[bit_start..bit_end],
                self.pg_count,
                self.pg_align,
                self.pg_num,
            )
        };
        if ptr.is_null() {
            self.back_ety(idx.0)?;
            return Err(AllocError::EFATAL);
        }
        if let Some(addr) = idx.1 {
            if let Err(err) = self.handle_rd_tree_insert(ptr_map, idx.0, addr) {
                let clear_res = self.clear_page_allocations(idx.0, n);
                let back_res = self.back_uninit(idx.0);
                return Err(back_res.err().or_else(|| clear_res.err()).unwrap_or(err));
            }
        }
        let obj = self
            .pages
            .get_mut(idx.0 as usize)
            .ok_or(AllocError::EFATAL)?;
        let insert_result = if obj.is_full(self.pg_count) {
            self.insert_full(idx.0)
        } else {
            self.insert_partial(idx.0)
        };
        if let Err(err) = insert_result {
            let rollback_result = self.rollback_new_page_allocation(idx.0, n, ptr_map, idx.1);
            return Err(rollback_result.err().unwrap_or(err));
        }

        NonNull::new(ptr).ok_or(AllocError::ENOMEM)
    }

    fn clear_page_allocations(&mut self, idx: usize, n: usize) -> Result<()> {
        let (bit_start, bit_end) = self.bitfield_range(idx, n)?;
        let bitfield = self
            .bitfields
            .get_mut(bit_start..bit_end)
            .ok_or(AllocError::EFATAL)?;
        let obj = self.pages.get_mut(idx).ok_or(AllocError::EFATAL)?;
        obj.clear_allocations(bitfield);
        Ok(())
    }

    fn restore_allocated_prefix(
        &mut self,
        idx: usize,
        n: usize,
        allocation_state: ObjectPageAllocationState,
        allocated_ptrs: &[usize],
    ) -> Result<()> {
        let (bit_start, bit_end) = self.bitfield_range(idx, n)?;
        let bitfield = self
            .bitfields
            .get_mut(bit_start..bit_end)
            .ok_or(AllocError::EFATAL)?;
        let obj = self.pages.get_mut(idx).ok_or(AllocError::EFATAL)?;
        obj.restore_allocated_prefix(
            allocation_state,
            allocated_ptrs,
            bitfield,
            self.pg_count,
            self.pg_align,
            self.pg_num,
        )
    }

    fn rollback_new_page_allocation(
        &mut self,
        idx: usize,
        n: usize,
        ptr_map: &mut RadixTree,
        inserted_radix_addr: Option<usize>,
    ) -> Result<()> {
        let mut radix_remove_err = None;
        let radix_removed = if let Some(addr) = inserted_radix_addr {
            match self.handle_rd_tree_remove(ptr_map, addr) {
                Ok(()) => true,
                Err(err) => {
                    radix_remove_err = Some(err);
                    false
                }
            }
        } else {
            false
        };
        let clear_res = self.clear_page_allocations(idx, n);
        let recycle_res = if inserted_radix_addr.is_some() && radix_removed {
            self.back_uninit(idx).map(|_| ())
        } else {
            self.back_ety(idx).map(|_| ())
        };

        recycle_res
            .err()
            .or_else(|| clear_res.err())
            .or(radix_remove_err)
            .map_or(Ok(()), Err)
    }

    fn restore_unallocated_empty_candidate(
        &mut self,
        idx: usize,
        new_page_addr: Option<usize>,
    ) -> Result<()> {
        if new_page_addr.is_some() {
            self.back_uninit(idx).map(|_| ())
        } else {
            self.back_ety(idx).map(|_| ())
        }
    }

    fn rollback_successful_batch_allocations(
        &mut self,
        ptr_map: &mut RadixTree,
        res_array: &[usize],
        allocated: usize,
        reserved_head: usize,
    ) -> Result<()> {
        let mut rollback_err = None;

        // `allocate_batch_v2` stores the first reserved object outside the
        // prefix it fills later (`res_array[count - 1]`).  Roll the prefix back
        // first, then the reserved head, so an error never leaves objects that
        // were not successfully returned to the caller.  Deallocation is the
        // safest repair primitive here because it updates the same bitmap,
        // page-list, empty-slab, and radix-map state as normal object frees.
        for addr in res_array.iter().take(allocated).copied() {
            if addr == 0 {
                continue;
            }
            let ptr = match NonNull::new(addr as *mut u8) {
                Some(ptr) => ptr,
                None => {
                    rollback_err = Some(AllocError::ENOMEM);
                    break;
                }
            };
            if let Err(err) = self.deallocate(ptr, ptr_map) {
                rollback_err = Some(err);
                break;
            }
        }

        if rollback_err.is_none() && reserved_head != 0 {
            match NonNull::new(reserved_head as *mut u8) {
                Some(ptr) => {
                    if let Err(err) = self.deallocate(ptr, ptr_map) {
                        rollback_err = Some(err);
                    }
                }
                None => rollback_err = Some(AllocError::ENOMEM),
            }
        }

        rollback_err.map_or(Ok(()), Err)
    }

    fn batch_allocation_err_after_rollback(
        &mut self,
        ptr_map: &mut RadixTree,
        res_array: &[usize],
        allocated: usize,
        reserved_head: usize,
        err: AllocError,
    ) -> AllocError {
        self.rollback_successful_batch_allocations(ptr_map, res_array, allocated, reserved_head)
            .err()
            .unwrap_or(err)
    }

    fn bitfield_range(&self, idx: usize, n: usize) -> Result<(usize, usize)> {
        let bit_start = idx.checked_mul(n).ok_or(AllocError::EFATAL)?;
        let bit_end = bit_start.checked_add(n).ok_or(AllocError::EFATAL)?;
        if bit_end > self.bitfields.len() {
            return Err(AllocError::EFATAL);
        }
        Ok((bit_start, bit_end))
    }

    fn checked_radix_key(page_addr: usize) -> Result<usize> {
        page_addr
            .checked_mul(1_usize << 16)
            .ok_or(AllocError::EFATAL)
    }

    fn checked_radix_value(idx: usize) -> Result<i64> {
        let value = idx.checked_add(1).ok_or(AllocError::EFATAL)?;
        if value > i64::MAX as usize {
            return Err(AllocError::EFATAL);
        }
        Ok(value as i64)
    }

    fn radix_page_chunks(&self, addr: usize) -> Result<RadixPageChunks> {
        if self.pg_num == 0 {
            return Err(AllocError::ESIZE);
        }
        let num = 1_usize << 16;
        let rem = num - ((addr >> PAGE_SIZE.trailing_zeros()) & (num - 1));
        let ptr = align_12k(addr);
        let first_len = core::cmp::min(rem, self.pg_num);
        let first = RadixPageChunk {
            key: Self::checked_radix_key(ptr)?,
            len: first_len,
        };
        let second = if rem >= self.pg_num {
            None
        } else {
            let next_delta = 4096_usize.checked_mul(rem).ok_or(AllocError::EFATAL)?;
            let next_ptr = ptr.checked_add(next_delta).ok_or(AllocError::EFATAL)?;
            Some(RadixPageChunk {
                key: Self::checked_radix_key(next_ptr)?,
                len: self.pg_num - rem,
            })
        };
        Ok(RadixPageChunks { first, second })
    }

    fn handle_rd_tree_insert(
        &self,
        ptr_map: &mut RadixTree,
        idx: usize,
        addr: usize,
    ) -> Result<()> {
        let value = Self::checked_radix_value(idx)?;
        let chunks = self.radix_page_chunks(addr)?;
        ptr_map
            .insert(chunks.first.key, value, chunks.first.len)
            .map_err(|_| AllocError::EFATAL)?;
        if let Some(second) = chunks.second {
            if ptr_map.insert(second.key, value, second.len).is_err() {
                let _ = ptr_map.remove(chunks.first.key, chunks.first.len);
                return Err(AllocError::EFATAL);
            }
        }
        Ok(())
    }

    fn retire_zero_progress_partial(&mut self, idx: usize, n: usize) -> Result<()> {
        let bit_start = idx.checked_mul(n).ok_or(AllocError::EFATAL)?;
        let bit_end = bit_start.checked_add(n).ok_or(AllocError::EFATAL)?;
        let used = {
            let bitfield = self
                .bitfields
                .get(bit_start..bit_end)
                .ok_or(AllocError::EFATAL)?;
            let obj = self.pages.get_mut(idx).ok_or(AllocError::EFATAL)?;
            if obj.is_inited() {
                Some(obj.repair_counter_from_bitfield(bitfield, self.pg_count)?)
            } else {
                None
            }
        };

        self.remove_partial(idx)?;
        match used {
            None => {
                self.back_uninit(idx)?;
                Ok(())
            }
            Some(used) if used >= self.pg_count => {
                self.insert_full(idx)?;
                Ok(())
            }
            Some(_) => {
                self.insert_partial(idx)?;
                Err(AllocError::EFATAL)
            }
        }
    }

    // We use the same strategy from tcmalloc:
    // We first try to alloc from partial, then create empty page
    pub fn allocate_batch_v2(
        &mut self,
        ptr_map: &mut RadixTree,
        res_array: &mut [usize],
        count_p: usize,
        align: usize,
    ) -> Result<NonNull<u8>> {
        let requested_count = count_p.min(res_array.len());
        if requested_count == 0
            || align == 0
            || !align.is_power_of_two()
            || align > PAGE_SIZE
            || self.pg_count == 0
            || self.pg_align == 0
            || self.pg_num == 0
        {
            return Err(AllocError::ESIZE);
        }
        let n = self.bitfield_words_for_current_page()?;
        let strict_alignment = align > self.pg_align;
        // `allocate_batch_v2` reserves one strict-alignment result up front via
        // `allocate()`. Clamp the effective request before placing that tail
        // object so the up-front object plus the page-local batch never exceeds
        // the strict split cap. This keeps excess aligned slots on the slab/page
        // for reuse instead of privatizing them into one thread cache.
        let count = requested_count.min(strict_alignment_batch_request_limit(align, self.pg_align));
        let res: usize = self.allocate(align, ptr_map)?.as_ptr() as usize;
        res_array[count - 1] = res;
        let mut allocated = 0_usize;
        let count = count - 1;
        let mut strict_cur_idx = self.partial_start;
        let mut strict_scanned = 0_usize;
        while self.partial_count > 0 && allocated < count {
            let cur_idx = if strict_alignment {
                strict_cur_idx
            } else {
                self.partial_start
            };
            let batch_start = allocated;
            let (allocation_state, made) = {
                let (bit_start, bit_end) = self.bitfield_range(cur_idx, n)?;
                let obj = self
                    .pages
                    .get_mut(cur_idx as usize)
                    .ok_or(AllocError::EFATAL)?;
                let allocation_state = obj.allocation_state_snapshot();
                let made = obj.allocate_batch(
                    align,
                    res_array,
                    allocated,
                    count - allocated,
                    &mut self.bitfields[bit_start..bit_end],
                    self.pg_count,
                    self.pg_align,
                    self.pg_num,
                );
                (allocation_state, made)
            };
            if made == 0 {
                if strict_alignment {
                    strict_scanned += 1;
                    if strict_scanned >= self.partial_count {
                        break;
                    }
                    strict_cur_idx = self.pages.get_next(cur_idx)?;
                    continue;
                }
                if let Err(err) = self.retire_zero_progress_partial(cur_idx, n) {
                    return Err(self.batch_allocation_err_after_rollback(
                        ptr_map, res_array, allocated, res, err,
                    ));
                }
                continue;
            }
            let obj = self
                .pages
                .get_mut(cur_idx as usize)
                .ok_or(AllocError::EFATAL)?;
            if obj.is_full(self.pg_count) {
                let transition_result = self.remove_partial(cur_idx).and_then(|()| {
                    if let Err(err) = self.insert_full(cur_idx) {
                        let _ = self.insert_partial(cur_idx);
                        Err(err)
                    } else {
                        Ok(())
                    }
                });
                if let Err(err) = transition_result {
                    let restore_result = self.restore_allocated_prefix(
                        cur_idx,
                        n,
                        allocation_state,
                        &res_array[batch_start..batch_start + made],
                    );
                    let err = restore_result.err().unwrap_or(err);
                    return Err(self.batch_allocation_err_after_rollback(
                        ptr_map, res_array, allocated, res, err,
                    ));
                }
                if strict_alignment {
                    strict_cur_idx = self.partial_start;
                    strict_scanned = 0;
                }
            } else if strict_alignment {
                self.partial_start = cur_idx;
                strict_cur_idx = cur_idx;
                strict_scanned = 0;
            }
            allocated += made;
            if allocated == count {
                return NonNull::new(res as *mut u8).ok_or(AllocError::ENOMEM);
            }
        }
        while allocated < count {
            let idx = match self.get_empty() {
                Ok(idx) => idx,
                Err(err) => {
                    return Err(self.batch_allocation_err_after_rollback(
                        ptr_map, res_array, allocated, res, err,
                    ));
                }
            };
            let allocated_before_page = allocated;

            let whole_page_satisfies_alignment = self.pg_align % align == 0;
            let insert_as_full =
                if whole_page_satisfies_alignment && count - allocated >= self.pg_count {
                    let (bit_start, bit_end) = match self.bitfield_range(idx.0, n) {
                        Ok(range) => range,
                        Err(err) => {
                            let err = self
                                .restore_unallocated_empty_candidate(idx.0, idx.1)
                                .err()
                                .unwrap_or(err);
                            return Err(self.batch_allocation_err_after_rollback(
                                ptr_map,
                                res_array,
                                allocated_before_page,
                                res,
                                err,
                            ));
                        }
                    };
                    let ptr = {
                        let obj = match self.pages.get_mut(idx.0 as usize) {
                            Some(obj) => obj,
                            None => {
                                let err = self
                                    .restore_unallocated_empty_candidate(idx.0, idx.1)
                                    .err()
                                    .unwrap_or(AllocError::EFATAL);
                                return Err(self.batch_allocation_err_after_rollback(
                                    ptr_map,
                                    res_array,
                                    allocated_before_page,
                                    res,
                                    err,
                                ));
                            }
                        };
                        obj.allocate_all(
                            &mut self.bitfields[bit_start..bit_end],
                            self.pg_count,
                            self.pg_align,
                            self.pg_num,
                        )
                    };
                    if ptr.is_null() {
                        let err = self.back_ety(idx.0).err().unwrap_or(AllocError::EFATAL);
                        return Err(self.batch_allocation_err_after_rollback(
                            ptr_map,
                            res_array,
                            allocated_before_page,
                            res,
                            err,
                        ));
                    }
                    for i in 0..self.pg_count {
                        res_array[allocated + i] = (ptr as usize) + i * self.pg_align;
                    }
                    allocated += self.pg_count;
                    true
                } else {
                    let (bit_start, bit_end) = match self.bitfield_range(idx.0, n) {
                        Ok(range) => range,
                        Err(err) => {
                            let err = self
                                .restore_unallocated_empty_candidate(idx.0, idx.1)
                                .err()
                                .unwrap_or(err);
                            return Err(self.batch_allocation_err_after_rollback(
                                ptr_map,
                                res_array,
                                allocated_before_page,
                                res,
                                err,
                            ));
                        }
                    };
                    let made = {
                        let obj = match self.pages.get_mut(idx.0 as usize) {
                            Some(obj) => obj,
                            None => {
                                let err = self
                                    .restore_unallocated_empty_candidate(idx.0, idx.1)
                                    .err()
                                    .unwrap_or(AllocError::EFATAL);
                                return Err(self.batch_allocation_err_after_rollback(
                                    ptr_map,
                                    res_array,
                                    allocated_before_page,
                                    res,
                                    err,
                                ));
                            }
                        };
                        obj.allocate_batch(
                            align,
                            res_array,
                            allocated,
                            count - allocated,
                            &mut self.bitfields[bit_start..bit_end],
                            self.pg_count,
                            self.pg_align,
                            self.pg_num,
                        )
                    };
                    if made == 0 {
                        let err = self.back_ety(idx.0).err().unwrap_or(AllocError::EFATAL);
                        return Err(self.batch_allocation_err_after_rollback(
                            ptr_map,
                            res_array,
                            allocated_before_page,
                            res,
                            err,
                        ));
                    }
                    allocated += made;
                    false
                };
            if let Some(addr) = idx.1 {
                if let Err(err) = self.handle_rd_tree_insert(ptr_map, idx.0, addr) {
                    let clear_res = self.clear_page_allocations(idx.0, n);
                    let back_res = self.back_uninit(idx.0);
                    let err = back_res.err().or_else(|| clear_res.err()).unwrap_or(err);
                    return Err(self.batch_allocation_err_after_rollback(
                        ptr_map,
                        res_array,
                        allocated_before_page,
                        res,
                        err,
                    ));
                }
            }
            let insert_result = if insert_as_full {
                self.insert_full(idx.0)
            } else {
                self.insert_partial(idx.0)
            };
            if let Err(err) = insert_result {
                let rollback_result = self.rollback_new_page_allocation(idx.0, n, ptr_map, idx.1);
                let err = rollback_result.err().unwrap_or(err);
                return Err(self.batch_allocation_err_after_rollback(
                    ptr_map,
                    res_array,
                    allocated_before_page,
                    res,
                    err,
                ));
            }
        }
        if allocated != count {
            return Err(self.batch_allocation_err_after_rollback(
                ptr_map,
                res_array,
                allocated,
                res,
                AllocError::EFATAL,
            ));
        }
        NonNull::new(res as *mut u8).ok_or(AllocError::ENOMEM)
    }

    pub fn allocate_batch(
        &mut self,
        align: usize,
        ptr_map: &mut RadixTree,
    ) -> (Result<NonNull<u8>>, usize, usize) {
        let mut ptr: *mut u8;
        let n = match self.bitfield_words_for_current_page() {
            Ok(words) => words,
            Err(err) => return (Err(err), self.pg_align, 0),
        };
        if self.partial_count > 0 {
            let mut head = self.partial_start;
            loop {
                let (bit_start, bit_end) = match self.bitfield_range(head, n) {
                    Ok(range) => range,
                    Err(err) => return (Err(err), self.pg_align, 0),
                };
                let (allocation_state, became_full) = {
                    let obj = match self.pages.get_mut(head as usize) {
                        Some(obj) => obj,
                        None => return (Err(AllocError::EFATAL), self.pg_align, 0),
                    };

                    let allocation_state = obj.allocation_state_snapshot();
                    ptr = obj.allocate(
                        align,
                        &mut self.bitfields[bit_start..bit_end],
                        self.pg_count,
                        self.pg_align,
                        self.pg_num,
                    );
                    (
                        allocation_state,
                        !ptr.is_null() && obj.is_full(self.pg_count),
                    )
                };
                if ptr.is_null() {
                    // head = obj.get_next() as i32;
                    head = match self.pages.get_next(head) {
                        Ok(next) => next,
                        Err(err) => return (Err(err), self.pg_align, 0),
                    };
                } else {
                    if became_full {
                        let ptr_addr = ptr as usize;
                        let transition_result = self.remove_partial(head).and_then(|()| {
                            if let Err(err) = self.insert_full(head) {
                                let _ = self.insert_partial(head);
                                Err(err)
                            } else {
                                Ok(())
                            }
                        });
                        if let Err(err) = transition_result {
                            let restore_result = self.restore_allocated_prefix(
                                head,
                                n,
                                allocation_state,
                                core::slice::from_ref(&ptr_addr),
                            );
                            return (Err(restore_result.err().unwrap_or(err)), self.pg_align, 0);
                        }
                    }
                    break;
                }
                if head == self.partial_start {
                    break;
                }
            }
            if !ptr.is_null() {
                return match NonNull::new(ptr) {
                    Some(ptr) => (Ok(ptr), self.pg_align, 0),
                    None => (Err(AllocError::ENOMEM), self.pg_align, 0),
                };
            }
        } //final case, try to get a new one
        let idx = match self.get_empty() {
            Ok(idx) => idx,
            Err(err) => return (Err(err), self.pg_align, 0),
        };
        ptr = {
            let (bit_start, bit_end) = match self.bitfield_range(idx.0, n) {
                Ok(range) => range,
                Err(err) => return (Err(err), self.pg_align, 0),
            };
            let obj = match self.pages.get_mut(idx.0 as usize) {
                Some(obj) => obj,
                None => return (Err(AllocError::EFATAL), self.pg_align, 0),
            };
            obj.allocate_all(
                &mut self.bitfields[bit_start..bit_end],
                self.pg_count,
                self.pg_align,
                self.pg_num,
            )
        };
        if ptr.is_null() {
            if let Err(err) = self.back_ety(idx.0) {
                return (Err(err), self.pg_align, 0);
            }
            return (Err(AllocError::EFATAL), self.pg_align, 0);
        }
        if let Some(addr) = idx.1 {
            if let Err(err) = self.handle_rd_tree_insert(ptr_map, idx.0, addr) {
                let clear_res = self.clear_page_allocations(idx.0, n);
                let back_res = self.back_uninit(idx.0);
                return (
                    Err(back_res.err().or_else(|| clear_res.err()).unwrap_or(err)),
                    self.pg_align,
                    0,
                );
            }
        }
        let obj = match self.pages.get_mut(idx.0 as usize) {
            Some(obj) => obj,
            None => return (Err(AllocError::EFATAL), self.pg_align, 0),
        };
        let insert_result = if obj.is_full(self.pg_count) {
            self.insert_full(idx.0)
        } else {
            self.insert_partial(idx.0)
        };
        if let Err(err) = insert_result {
            let rollback_result = self.rollback_new_page_allocation(idx.0, n, ptr_map, idx.1);
            return (Err(rollback_result.err().unwrap_or(err)), self.pg_align, 0);
        }

        (
            NonNull::new(ptr).ok_or(AllocError::ENOMEM),
            self.pg_align,
            self.pg_count,
        )
    }

    fn handle_rd_tree_remove(&self, ptr_map: &mut RadixTree, addr: usize) -> Result<()> {
        let chunks = self.radix_page_chunks(addr)?;
        let first_value = ptr_map.get_mut(chunks.first.key);
        ptr_map
            .remove(chunks.first.key, chunks.first.len)
            .map_err(|_| AllocError::EFATAL)?;
        if let Some(second) = chunks.second {
            if ptr_map.remove(second.key, second.len).is_err() {
                if first_value != 0 {
                    let _ = ptr_map.insert(chunks.first.key, first_value, chunks.first.len);
                }
                return Err(AllocError::EFATAL);
            }
        }
        Ok(())
    }

    fn restore_page_to_allocation_list(&mut self, idx: usize, was_full: bool) -> Result<()> {
        if was_full {
            self.insert_full(idx)
        } else {
            self.insert_partial(idx)
        }
    }

    fn remove_from_allocation_list(&mut self, idx: usize, was_full: bool) -> Result<()> {
        if was_full {
            self.remove_full(idx)
        } else {
            self.remove_partial(idx)
        }
    }

    fn move_full_page_to_partial_list(&mut self, idx: usize) -> Result<()> {
        self.remove_full(idx)?;
        if let Err(err) = self.insert_partial(idx) {
            let _ = self.insert_full(idx);
            return Err(err);
        }
        Ok(())
    }

    #[inline]
    fn promote_partial_scan_start(&mut self, idx: usize) {
        if self.partial_count != 0 && idx < self.pages.len() {
            self.partial_start = idx;
        }
    }

    #[inline]
    fn should_evict_retained_empty_slab_for_new_empty(&self) -> bool {
        let limit = self.empty_slab_retain_limit();
        limit > 0 && self.empty_count >= limit && self.empty_start < self.pages.len()
    }

    fn recycle_current_empty_page(
        &mut self,
        idx: usize,
        was_full: bool,
        ptr_map: &mut RadixTree,
    ) -> Result<()> {
        let mut removed_radix_addr = None;
        if let Some(p) = self.try_back_ety(idx)? {
            if let Err(err) = self.handle_rd_tree_remove(ptr_map, p) {
                let _ = self.restore_page_to_allocation_list(idx, was_full);
                return Err(err);
            }
            removed_radix_addr = Some(p);
        }

        if let Err(err) = self.back_ety(idx) {
            if let Some(p) = removed_radix_addr {
                // `back_ety` can fail after the page-to-object radix mapping
                // has been removed (for example a corrupt uninit list).  Put
                // the mapping back before restoring the page to the allocation
                // list so a retry can still locate this still-live page.
                let _ = self.handle_rd_tree_insert(ptr_map, idx, p);
            }
            let _ = self.restore_page_to_allocation_list(idx, was_full);
            return Err(err);
        }
        Ok(())
    }

    fn remove_empty_index(&mut self) -> Result<usize> {
        if self.empty_count == 0 || self.empty_start >= self.pages.len() {
            return Err(AllocError::EFATAL);
        }
        let res = self.empty_start;
        let next = self.pages.get_next(res)?;
        self.pages.try_remove_node(res)?;
        self.empty_start = next;
        self.empty_count = self.empty_count.checked_sub(1).ok_or(AllocError::EFATAL)?;
        Ok(res)
    }

    fn evict_retained_empty_slab_for_new_empty(
        &mut self,
        idx: usize,
        was_full: bool,
        ptr_map: &mut RadixTree,
    ) -> Result<()> {
        let evicted_idx = match self.remove_empty_index() {
            Ok(evicted_idx) => evicted_idx,
            Err(_) => return self.recycle_current_empty_page(idx, was_full, ptr_map),
        };
        let evicted_addr = match self
            .pages
            .get_mut(evicted_idx)
            .and_then(|page| page.get_data_ptr())
            .map(|ptr| ptr as usize)
        {
            Some(addr) => addr,
            None => {
                let _ = self.back_ety(evicted_idx);
                return self.recycle_current_empty_page(idx, was_full, ptr_map);
            }
        };

        if let Err(err) = self.handle_rd_tree_remove(ptr_map, evicted_addr) {
            let _ = self.back_ety(evicted_idx);
            let _ = self.restore_page_to_allocation_list(idx, was_full);
            return Err(err);
        }

        if let Err(err) = self.back_uninit(evicted_idx) {
            let _ = self.handle_rd_tree_insert(ptr_map, evicted_idx, evicted_addr);
            let _ = self.back_ety(evicted_idx);
            let _ = self.restore_page_to_allocation_list(idx, was_full);
            return Err(err);
        }

        if let Err(err) = self.back_ety(idx) {
            let _ = self.restore_page_to_allocation_list(idx, was_full);
            return Err(err);
        }
        self.empty_start = idx;
        Ok(())
    }

    fn move_empty_page_to_recycle_list(
        &mut self,
        idx: usize,
        was_full: bool,
        ptr_map: &mut RadixTree,
    ) -> Result<()> {
        self.remove_from_allocation_list(idx, was_full)?;
        if self.should_evict_retained_empty_slab_for_new_empty() {
            self.evict_retained_empty_slab_for_new_empty(idx, was_full, ptr_map)
        } else {
            self.recycle_current_empty_page(idx, was_full, ptr_map)
        }
    }

    fn lookup_page_index_for_page(
        &self,
        ptr_map: &mut RadixTree,
        page_vaddr: usize,
    ) -> Result<usize> {
        // Rd tree stores indices plus one so that 0 remains an invalid/miss value.
        let idx = ptr_map.get_mut(Self::checked_radix_key(page_vaddr)?) - 1;
        if idx < 0 {
            return Err(AllocError::EUAF);
        }
        let idx = idx as usize;
        if idx >= self.pages.len() {
            return Err(AllocError::EFATAL);
        }
        Ok(idx)
    }

    fn lookup_page_index(&self, ptr_map: &mut RadixTree, ptr: usize) -> Result<usize> {
        self.lookup_page_index_for_page(ptr_map, align_12k(ptr))
    }

    #[inline]
    fn deallocate_batch_page_cache_set(page_vaddr: usize) -> usize {
        debug_assert!(DEALLOCATE_BATCH_PAGE_INDEX_CACHE_SETS.is_power_of_two());
        (page_vaddr >> PAGE_SIZE.trailing_zeros()) & (DEALLOCATE_BATCH_PAGE_INDEX_CACHE_SETS - 1)
    }

    #[inline]
    fn deallocate_batch_page_cache_set_range(page_vaddr: usize) -> core::ops::Range<usize> {
        let set = Self::deallocate_batch_page_cache_set(page_vaddr);
        let start = set * DEALLOCATE_BATCH_PAGE_INDEX_CACHE_WAYS;
        start..start + DEALLOCATE_BATCH_PAGE_INDEX_CACHE_WAYS
    }

    #[inline]
    fn deallocate_batch_page_cache_replacement_way(page_vaddr: usize) -> usize {
        debug_assert!(DEALLOCATE_BATCH_PAGE_INDEX_CACHE_WAYS.is_power_of_two());
        (page_vaddr
            >> (PAGE_SIZE.trailing_zeros()
                + DEALLOCATE_BATCH_PAGE_INDEX_CACHE_SETS.trailing_zeros()))
            & (DEALLOCATE_BATCH_PAGE_INDEX_CACHE_WAYS - 1)
    }

    #[inline]
    fn fill_deallocate_batch_page_cache(
        page_vaddr: usize,
        idx: usize,
        cached_pages: &mut [usize; DEALLOCATE_BATCH_PAGE_INDEX_CACHE_ENTRIES],
        cached_indices: &mut [usize; DEALLOCATE_BATCH_PAGE_INDEX_CACHE_ENTRIES],
    ) {
        let set_range = Self::deallocate_batch_page_cache_set_range(page_vaddr);
        let slot = set_range
            .clone()
            .find(|&slot| cached_indices[slot] == DEALLOCATE_BATCH_PAGE_INDEX_CACHE_EMPTY)
            .unwrap_or_else(|| {
                set_range.start + Self::deallocate_batch_page_cache_replacement_way(page_vaddr)
            });
        cached_pages[slot] = page_vaddr;
        cached_indices[slot] = idx;
    }

    fn lookup_page_index_cached(
        &self,
        ptr_map: &mut RadixTree,
        ptr: usize,
        cached_pages: &mut [usize; DEALLOCATE_BATCH_PAGE_INDEX_CACHE_ENTRIES],
        cached_indices: &mut [usize; DEALLOCATE_BATCH_PAGE_INDEX_CACHE_ENTRIES],
    ) -> Result<(usize, usize)> {
        let page_vaddr = align_12k(ptr);
        for slot in Self::deallocate_batch_page_cache_set_range(page_vaddr) {
            if cached_indices[slot] != DEALLOCATE_BATCH_PAGE_INDEX_CACHE_EMPTY
                && cached_pages[slot] == page_vaddr
            {
                let idx = cached_indices[slot];
                if idx < self.pages.len() {
                    return Ok((idx, page_vaddr));
                }
                return Err(AllocError::EFATAL);
            }
        }

        let idx = self.lookup_page_index_for_page(ptr_map, page_vaddr)?;
        Self::fill_deallocate_batch_page_cache(page_vaddr, idx, cached_pages, cached_indices);
        Ok((idx, page_vaddr))
    }

    #[inline]
    fn invalidate_deallocate_batch_page_cache(
        page_vaddr: usize,
        cached_pages: &mut [usize; DEALLOCATE_BATCH_PAGE_INDEX_CACHE_ENTRIES],
        cached_indices: &mut [usize; DEALLOCATE_BATCH_PAGE_INDEX_CACHE_ENTRIES],
    ) {
        for slot in Self::deallocate_batch_page_cache_set_range(page_vaddr) {
            if cached_pages[slot] == page_vaddr {
                cached_indices[slot] = DEALLOCATE_BATCH_PAGE_INDEX_CACHE_EMPTY;
            }
        }
    }

    /// Deallocating a previously allocated `ptr` described by `Layout`.
    ///
    /// May return an error in case an invalid `layout` is provided.
    /// The function may also move internal slab pages between lists partial -> empty
    /// or full -> partial lists.
    pub fn deallocate(&mut self, ptr: NonNull<u8>, ptr_map: &mut RadixTree) -> Result<()> {
        let idx = self.lookup_page_index(ptr_map, ptr.as_ptr() as usize)?;
        let n = self.bitfield_words_for_current_page()?;
        let (bit_start, bit_end) = self.bitfield_range(idx, n)?;
        let ptr_addr = ptr.as_ptr() as usize;
        let (back_partial, page_is_empty, allocation_state) = {
            let obj_pge = self.pages.get_mut(idx).ok_or(AllocError::EFATAL)?;
            let back_partial = obj_pge.is_full(self.pg_count);
            let allocation_state = obj_pge.allocation_state_snapshot();
            obj_pge.deallocate(
                ptr,
                &mut self.bitfields[bit_start..bit_end],
                self.pg_count,
                self.pg_align,
            )?;
            (back_partial, obj_pge.is_empty(), allocation_state)
        };

        let transition_result = if page_is_empty {
            self.move_empty_page_to_recycle_list(idx, back_partial, ptr_map)
        } else if back_partial {
            self.move_full_page_to_partial_list(idx)
        } else {
            Ok(())
        };

        if let Err(err) = transition_result {
            let restore_result = {
                let obj_pge = self.pages.get_mut(idx).ok_or(AllocError::EFATAL)?;
                obj_pge.restore_deallocated_prefix(
                    allocation_state,
                    core::slice::from_ref(&ptr_addr),
                    &mut self.bitfields[bit_start..bit_end],
                    self.pg_count,
                    self.pg_align,
                    self.pg_num,
                )
            };
            return Err(restore_result.err().unwrap_or(err));
        }
        if !page_is_empty {
            // Prefer the page that just received returned objects.  This keeps
            // thread-cache trim surplus promptly reusable by other caches
            // instead of letting older partial pages hide it behind the scan
            // cursor.
            self.promote_partial_scan_start(idx);
        }
        Ok(())
    }

    pub fn deallocate_batch(
        &mut self,
        res_array: &mut [usize],
        count_p: usize,
        ptr_map: &mut RadixTree,
    ) -> Result<()> {
        self.deallocate_batch_with_policy(res_array, count_p, ptr_map, false)
    }

    pub(crate) fn deallocate_batch_promoting_batch_head(
        &mut self,
        res_array: &mut [usize],
        count_p: usize,
        ptr_map: &mut RadixTree,
    ) -> Result<()> {
        self.deallocate_batch_with_policy(res_array, count_p, ptr_map, true)
    }

    /// Deallocate a batch while preserving pointer order.
    ///
    /// `ObjectPage::deallocate_batch` consumes each contiguous same-page prefix.
    /// The small stack page-index cache below only avoids repeated radix lookups
    /// when the same page reappears later in an interleaved batch; it never
    /// reorders pointers or hides stale mappings for recycled pages.
    ///
    /// The thread-cache adapter can request `promote_batch_head` so a returned
    /// suffix becomes promptly reusable by other caches.  The default backend
    /// API keeps historical partial-ring ordering for lower-level tests and
    /// direct callers.
    fn deallocate_batch_with_policy(
        &mut self,
        res_array: &mut [usize],
        count_p: usize,
        ptr_map: &mut RadixTree,
        promote_batch_head: bool,
    ) -> Result<()> {
        let count = count_p.min(res_array.len());
        if count == 0 {
            return Ok(());
        }
        let n = self.bitfield_words_for_current_page()?;
        let mut cached_pages = [0usize; DEALLOCATE_BATCH_PAGE_INDEX_CACHE_ENTRIES];
        let mut cached_indices =
            [DEALLOCATE_BATCH_PAGE_INDEX_CACHE_EMPTY; DEALLOCATE_BATCH_PAGE_INDEX_CACHE_ENTRIES];
        let mut deallocated = 0;
        let mut preferred_partial_idx = None;
        while deallocated < count {
            let ptr = res_array[deallocated];
            let (idx, page_vaddr) = self.lookup_page_index_cached(
                ptr_map,
                ptr,
                &mut cached_pages,
                &mut cached_indices,
            )?;
            let (bit_start, bit_end) = self.bitfield_range(idx, n)?;
            let (back_partial, page_is_empty, allocation_state, t) = {
                let obj_pge = self.pages.get_mut(idx).ok_or(AllocError::EFATAL)?;
                let back_partial = obj_pge.is_full(self.pg_count);
                let allocation_state = obj_pge.allocation_state_snapshot();
                let t = obj_pge.deallocate_batch(
                    &mut res_array[deallocated..],
                    &mut self.bitfields[bit_start..bit_end],
                    self.pg_count,
                    self.pg_align,
                    self.pg_num,
                )?;
                (back_partial, obj_pge.is_empty(), allocation_state, t)
            };
            if t == 0 {
                return Err(AllocError::EUAF);
            }

            let transition_result = if page_is_empty {
                self.move_empty_page_to_recycle_list(idx, back_partial, ptr_map)
            } else if back_partial {
                self.move_full_page_to_partial_list(idx)
            } else {
                Ok(())
            };

            if let Err(err) = transition_result {
                let restore_result = {
                    let obj_pge = self.pages.get_mut(idx).ok_or(AllocError::EFATAL)?;
                    obj_pge.restore_deallocated_prefix(
                        allocation_state,
                        &res_array[deallocated..deallocated + t],
                        &mut self.bitfields[bit_start..bit_end],
                        self.pg_count,
                        self.pg_align,
                        self.pg_num,
                    )
                };
                return Err(restore_result.err().unwrap_or(err));
            }
            if page_is_empty {
                Self::invalidate_deallocate_batch_page_cache(
                    page_vaddr,
                    &mut cached_pages,
                    &mut cached_indices,
                );
                if preferred_partial_idx == Some(idx) {
                    preferred_partial_idx = None;
                }
            } else if promote_batch_head {
                // Batch deallocation preserves pointer order.  Remember the
                // first returned page that remains partial and promote it after
                // the whole batch succeeds, rather than letting later old pages
                // hide the batch head behind stale partial-list order.
                if preferred_partial_idx.is_none() {
                    preferred_partial_idx = Some(idx);
                }
            }
            deallocated += t;
        }
        if let Some(idx) = preferred_partial_idx {
            self.promote_partial_scan_start(idx);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn new_rejects_zero_pages_without_dividing_by_zero() {
        let alloc = SCAllocator::new(64, 0);
        assert_eq!(alloc.pg_count, 0);
        assert_eq!(alloc.pg_num, 0);
        assert_eq!(alloc.pg_align, 0);
    }

    #[test]
    fn empty_slab_retain_limit_scales_by_span_pages_for_multi_slot_classes() {
        assert_eq!(empty_slab_retain_limit_for_geometry(1, 1), 0);
        assert_eq!(empty_slab_retain_limit_for_geometry(8, 1), 0);
        assert_eq!(empty_slab_retain_limit_for_geometry(1, 2), 8);
        assert_eq!(empty_slab_retain_limit_for_geometry(2, 2), 4);
        assert_eq!(empty_slab_retain_limit_for_geometry(4, 2), 2);
        assert_eq!(empty_slab_retain_limit_for_geometry(8, 2), 1);
        assert_eq!(empty_slab_retain_limit_for_geometry(9, 2), 0);
        assert_eq!(empty_slab_retain_limit_for_geometry(0, 2), 0);

        assert_eq!(SCAllocator::new(PAGE_SIZE, 1).empty_slab_retain_limit(), 0);
        assert_eq!(SCAllocator::new(64, 1).empty_slab_retain_limit(), 8);
        assert_eq!(SCAllocator::new(64, 2).empty_slab_retain_limit(), 4);
        assert_eq!(SCAllocator::new(64, 4).empty_slab_retain_limit(), 2);
        assert_eq!(SCAllocator::new(64, 8).empty_slab_retain_limit(), 1);
        assert_eq!(SCAllocator::new(64, 9).empty_slab_retain_limit(), 0);
        assert_eq!(SCAllocator::new(64, 0).empty_slab_retain_limit(), 0);
    }

    #[test]
    fn get_empty_rejects_uninitialized_geometry_without_page_alloc_panic() {
        let mut alloc = SCAllocator::new(64, 0);
        let err = alloc
            .get_empty()
            .expect_err("zero-page separate size class must fail closed");
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
    }

    #[test]
    fn get_empty_rejects_corrupt_empty_start_without_panic() {
        let mut alloc = SCAllocator::new(64, 1);
        alloc.empty_count = 1;
        alloc.empty_start = 99;

        let err = alloc
            .get_empty()
            .expect_err("corrupt empty list head must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(alloc.empty_count, 1);
    }

    #[test]
    fn bitfield_words_per_page_rounds_up_and_fails_closed() {
        assert_eq!(SCAllocator::bitfield_words_per_page(1).unwrap(), 1);
        assert_eq!(SCAllocator::bitfield_words_per_page(32).unwrap(), 1);
        assert_eq!(SCAllocator::bitfield_words_per_page(33).unwrap(), 2);

        let zero_err = SCAllocator::bitfield_words_per_page(0)
            .expect_err("zero-slot geometry must fail closed");
        assert_eq!(zero_err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());

        let overflow_err = SCAllocator::bitfield_words_per_page(usize::MAX)
            .expect_err("overflowing bitmap-word rounding must fail closed");
        assert_eq!(
            overflow_err.to_raw_errno(),
            AllocError::EFATAL.to_raw_errno()
        );
    }

    #[test]
    fn bitfield_reserve_words_tracks_descriptor_spare_not_fixed_batch() {
        assert_eq!(
            SCAllocator::bitfield_reserve_words_for_descriptor_spare(0, 2, 1).unwrap(),
            2,
            "one spare descriptor only needs one descriptor worth of bitmap words"
        );
        assert_eq!(
            SCAllocator::bitfield_reserve_words_for_descriptor_spare(1, 2, 1).unwrap(),
            1
        );
        assert_eq!(
            SCAllocator::bitfield_reserve_words_for_descriptor_spare(2, 2, 1).unwrap(),
            0
        );
        assert_eq!(
            SCAllocator::bitfield_reserve_words_for_descriptor_spare(
                0,
                2,
                SEPARATE_SC_METADATA_RESERVE_PAGES
            )
            .unwrap(),
            2 * SEPARATE_SC_METADATA_RESERVE_PAGES,
            "first growth still reserves bitmap words for the descriptor batch"
        );

        let zero_err = SCAllocator::bitfield_reserve_words_for_descriptor_spare(0, 0, 1)
            .expect_err("zero bitmap words per descriptor must fail closed");
        assert_eq!(zero_err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());

        let overflow_err =
            SCAllocator::bitfield_reserve_words_for_descriptor_spare(0, usize::MAX, 2)
                .expect_err("overflowing reserve calculation must fail closed");
        assert_eq!(
            overflow_err.to_raw_errno(),
            AllocError::EFATAL.to_raw_errno()
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn get_uninit_reserves_bounded_metadata_batch() {
        let mut alloc = SCAllocator::new(64, 1);
        let words_per_page =
            SCAllocator::bitfield_words_per_page(alloc.pg_count).expect("valid bitfield words");
        assert!(words_per_page > 0);

        let (idx, _addr) = alloc
            .get_uninit()
            .expect("real separate object-page backing should allocate");

        assert!(
            alloc.pages.capacity() < 512,
            "first separate_sc growth should not reserve the old 512 page descriptors"
        );
        assert!(
            alloc.bitfields.capacity() < PAGE_SIZE / core::mem::size_of::<u32>(),
            "first separate_sc growth should not reserve the old page-sized bitfield block"
        );
        assert!(
            alloc.pages.capacity() >= SEPARATE_SC_METADATA_RESERVE_PAGES,
            "the small batch should still amortize descriptor growth"
        );
        assert!(
            alloc.bitfields.capacity() >= words_per_page * SEPARATE_SC_METADATA_RESERVE_PAGES,
            "bitfield reservation should scale with the actual words per object page"
        );

        alloc
            .back_uninit(idx)
            .expect("test must release real page backing after capacity check");
        assert!(
            !alloc
                .pages
                .get(idx)
                .expect("tracked page metadata")
                .is_inited(),
            "cleanup should destroy the temporary real page backing"
        );
    }

    #[test]
    fn get_uninit_rolls_back_descriptor_when_page_backing_fails() {
        let mut alloc = SCAllocator::new(64, 1);
        alloc.pg_num = usize::MAX;

        let err = alloc
            .get_uninit()
            .expect_err("overflowing page backing layout must fail closed");
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(alloc.pages.len(), 1);
        assert_eq!(alloc.uninit_count, 1);
        assert_eq!(alloc.uninit_start, 0);
        assert!(
            !alloc
                .pages
                .get(0)
                .expect("rolled-back descriptor")
                .is_inited(),
            "failed page-backing allocation must not leave a fake initialized page"
        );

        let err = alloc
            .get_uninit()
            .expect_err("retrying the same invalid geometry should reuse the descriptor");
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(
            alloc.pages.len(),
            1,
            "repeated page-backing failures must not grow descriptor metadata"
        );
        assert_eq!(alloc.uninit_count, 1);
    }

    #[test]
    fn remove_partial_rejects_empty_list_without_underflow() {
        let mut alloc = SCAllocator::new(64, 1);

        let err = alloc
            .remove_partial(0)
            .expect_err("removing from an empty partial list must not underflow");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(alloc.partial_count, 0);
    }

    #[test]
    fn back_ety_rejects_invalid_page_index_without_panic() {
        let mut alloc = SCAllocator::new(64, 1);

        let err = alloc
            .back_ety(1)
            .expect_err("invalid page index must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(alloc.empty_count, 0);
    }

    #[test]
    fn back_ety_retains_empty_page_below_warm_cache_limit() {
        let mut alloc = SCAllocator::new(64, 1);
        alloc.pages.push(ObjectPage::new());
        alloc.pages.push(ObjectPage::new());
        alloc.empty_count = EMPTY_SLAB_RETAIN_LIMIT - 1;
        alloc.empty_start = 0;

        assert_eq!(
            alloc
                .back_ety(1)
                .expect("below-limit separate empty page should stay cached"),
            None
        );

        assert_eq!(alloc.empty_count, EMPTY_SLAB_RETAIN_LIMIT);
        assert_eq!(alloc.uninit_count, 0);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn back_ety_recycles_empty_page_at_warm_cache_limit() {
        let mut alloc = SCAllocator::new(64, 1);
        alloc.pages.push(ObjectPage::new());
        let page_addr = alloc
            .pages
            .get_mut(0)
            .expect("test page")
            .allocate_page(alloc.pg_num)
            .expect("real separate object-page backing should allocate");
        alloc.empty_count = EMPTY_SLAB_RETAIN_LIMIT;

        assert_eq!(
            alloc
                .back_ety(0)
                .expect("at-limit separate empty page should recycle backing"),
            Some(page_addr as usize)
        );

        assert_eq!(alloc.empty_count, EMPTY_SLAB_RETAIN_LIMIT);
        assert_eq!(alloc.uninit_count, 1);
        assert_eq!(alloc.uninit_start, 0);
        assert!(
            !alloc.pages.get(0).expect("test page").is_inited(),
            "recycled empty slab must release backing memory instead of retaining RSS"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn empty_slab_retention_is_per_class_and_reuses_retained_real_slabs() {
        let mut alloc64 = SCAllocator::new(64, 1);
        let mut alloc128 = SCAllocator::new(128, 1);
        let ptr_map64 = empty_radix_tree();
        let ptr_map128 = empty_radix_tree();
        let mut batches = [(0usize, 0usize, 0usize); EMPTY_SLAB_RETAIN_LIMIT + 1];

        for slot in batches.iter_mut() {
            let (ptr, stride, count) = alloc64.allocate_batch(8, ptr_map64);
            let ptr = ptr.expect("64-byte class should allocate a real slab batch");
            *slot = (ptr.as_ptr() as usize, stride, count);
        }

        let mut linked_batch = alloc::vec![0usize; alloc64.pg_count.max(alloc128.pg_count)];
        for (base, stride, count) in batches {
            assert!(count <= linked_batch.len());
            fill_page_batch(&mut linked_batch, base, stride, count);
            alloc64
                .deallocate_batch(&mut linked_batch[..count], count, ptr_map64)
                .expect("real 64-byte batch should deallocate");
        }

        assert_eq!(
            alloc64.empty_count, EMPTY_SLAB_RETAIN_LIMIT,
            "64-byte class should cap retained empty slabs at its own warm-cache limit"
        );
        assert_eq!(alloc64.uninit_count, 1);
        let recycled_idx = alloc64.uninit_start;
        assert!(
            !alloc64
                .pages
                .get(recycled_idx)
                .expect("overflow page should stay tracked as metadata")
                .is_inited(),
            "overflow empty slab must release backing memory"
        );
        assert_eq!(
            alloc128.empty_count, 0,
            "another size class must not share the 64-byte class retention counter"
        );

        let (ptr128, stride128, count128) = alloc128.allocate_batch(8, ptr_map128);
        let ptr128 = ptr128.expect("128-byte class should allocate independently");
        fill_page_batch(
            &mut linked_batch,
            ptr128.as_ptr() as usize,
            stride128,
            count128,
        );
        alloc128
            .deallocate_batch(&mut linked_batch[..count128], count128, ptr_map128)
            .expect("128-byte class should retain its own empty slab");
        assert_eq!(alloc128.empty_count, 1);

        let (reused, reused_stride, reused_count) = alloc64.allocate_batch(8, ptr_map64);
        let reused =
            reused.expect("64-byte class should reuse a retained empty slab before uninit");
        assert_eq!(
            alloc64.empty_count,
            EMPTY_SLAB_RETAIN_LIMIT - 1,
            "hot empty-slab reuse should pop from the retained warm cache"
        );
        fill_page_batch(
            &mut linked_batch,
            reused.as_ptr() as usize,
            reused_stride,
            reused_count,
        );
        alloc64
            .deallocate_batch(&mut linked_batch[..reused_count], reused_count, ptr_map64)
            .expect("cleanup after retained-slab reuse should succeed");
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn empty_slab_rotation_evicts_old_retained_slab_and_keeps_new_hot_slab() {
        let mut alloc = SCAllocator::new(64, 1);
        let ptr_map = empty_radix_tree();
        let mut batches = [(0usize, 0usize, 0usize); EMPTY_SLAB_RETAIN_LIMIT + 1];

        for slot in batches.iter_mut() {
            let (ptr, stride, count) = alloc.allocate_batch(8, ptr_map);
            let ptr = ptr.expect("64-byte class should allocate a real slab batch");
            *slot = (ptr.as_ptr() as usize, stride, count);
        }

        let oldest_ptr = batches[0].0;
        let newest_ptr = batches[EMPTY_SLAB_RETAIN_LIMIT].0;
        let mut linked_batch = alloc::vec![0usize; alloc.pg_count];
        for (base, stride, count) in batches {
            fill_page_batch(&mut linked_batch, base, stride, count);
            alloc
                .deallocate_batch(&mut linked_batch[..count], count, ptr_map)
                .expect("real 64-byte batch should deallocate");
        }

        assert_eq!(alloc.empty_count, EMPTY_SLAB_RETAIN_LIMIT);
        assert_eq!(alloc.uninit_count, 1);
        assert!(!alloc
            .pages
            .get(alloc.uninit_start)
            .expect("evicted descriptor")
            .is_inited());
        assert_eq!(
            alloc
                .pages
                .get(alloc.empty_start)
                .expect("hot empty slab")
                .get_data_ptr()
                .map(|ptr| ptr as usize),
            Some(newest_ptr),
            "the newest freed slab should become the hot retained empty head"
        );

        let err = alloc
            .deallocate(
                NonNull::new(oldest_ptr as *mut u8).expect("oldest pointer should be non-null"),
                ptr_map,
            )
            .expect_err("evicted retained slab mapping should be removed");
        assert_eq!(err.to_raw_errno(), AllocError::EUAF.to_raw_errno());

        let (reused, reused_stride, reused_count) = alloc.allocate_batch(8, ptr_map);
        let reused = reused.expect("hot retained empty slab should be reusable");
        assert_eq!(reused.as_ptr() as usize, newest_ptr);
        fill_page_batch(
            &mut linked_batch,
            reused.as_ptr() as usize,
            reused_stride,
            reused_count,
        );
        alloc
            .deallocate_batch(&mut linked_batch[..reused_count], reused_count, ptr_map)
            .expect("cleanup after hot retained-slab reuse should succeed");
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn back_ety_recycles_large_span_without_warm_cache() {
        let mut alloc = SCAllocator::new(1024, EMPTY_SLAB_RETAIN_OS_PAGE_BUDGET + 1);
        alloc.pages.push(ObjectPage::new());
        let page_addr = alloc
            .pages
            .get_mut(0)
            .expect("test page")
            .allocate_page(alloc.pg_num)
            .expect("real large-span separate backing should allocate");

        assert_eq!(alloc.empty_slab_retain_limit(), 0);
        assert_eq!(
            alloc
                .back_ety(0)
                .expect("huge empty spans should be recycled immediately"),
            Some(page_addr as usize)
        );

        assert_eq!(alloc.empty_count, 0);
        assert_eq!(alloc.uninit_count, 1);
        assert_eq!(alloc.uninit_start, 0);
        assert!(
            !alloc.pages.get(0).expect("test page").is_inited(),
            "large-span recycling must release backing memory instead of retaining RSS"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn one_slot_size_class_recycles_empty_real_span_immediately() {
        let (class, rounded) = crate::size_class::get_size_class_tuple(8193);
        let idx = match class {
            crate::size_class::SizeClass::Base(idx) => idx,
            crate::size_class::SizeClass::Large(size) => {
                panic!(
                    "8193-byte request unexpectedly classified as large: {}",
                    size
                )
            }
        };
        let pages = crate::size_class::get_num_pages_by_idx(idx);
        let ptr_map = empty_radix_tree();
        let mut alloc = SCAllocator::new(rounded, pages);
        assert_eq!(alloc.pg_count, 1);
        assert_eq!(alloc.empty_slab_retain_limit(), 0);

        let (ptr_res, stride, count) = alloc.allocate_batch(1, ptr_map);
        let ptr = ptr_res.expect("real separate one-slot allocation should succeed");
        assert_eq!(count, 1);
        assert!(stride >= rounded);
        assert_eq!(alloc.full_count, 1);

        let mut batch = [ptr.as_ptr() as usize];
        alloc
            .deallocate_batch(&mut batch, 1, ptr_map)
            .expect("one-slot separate span should deallocate cleanly");
        assert_eq!(alloc.full_count, 0);
        assert_eq!(alloc.partial_count, 0);
        assert_eq!(alloc.empty_count, 0);
        assert_eq!(alloc.empty_start, 0);
        assert_eq!(alloc.uninit_count, 1);
        assert!(
            !alloc
                .pages
                .get(alloc.uninit_start)
                .expect("recycled one-slot descriptor")
                .is_inited(),
            "single-slot separate span should release backing immediately"
        );

        let (reallocated_res, reallocated_stride, reallocated_count) =
            alloc.allocate_batch(1, ptr_map);
        let reallocated =
            reallocated_res.expect("recycled separate descriptor should remain reusable");
        assert_eq!(reallocated_count, 1);
        assert!(reallocated_stride >= rounded);
        let mut reallocated_batch = [reallocated.as_ptr() as usize];
        alloc
            .deallocate_batch(&mut reallocated_batch, 1, ptr_map)
            .expect("cleanup of reallocated one-slot separate span should succeed");
        assert_eq!(alloc.empty_count, 0);
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn empty_radix_tree() -> &'static mut RadixTree {
        unsafe { allocate_node::<RadixTree>().as_mut().expect("radix tree") }
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn fill_page_batch(batch: &mut [usize], base: usize, stride: usize, count: usize) {
        for (idx, slot) in batch.iter_mut().take(count).enumerate() {
            *slot = base + idx * stride;
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn split_radix_second_key_overflow_addr() -> usize {
        let normalized_first_ptr = (1_usize << 48) - 4096;
        let page_shift = PAGE_SIZE.trailing_zeros().saturating_sub(12);
        normalized_first_ptr << page_shift
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn deallocate_rejects_unmapped_pointer_without_panic() {
        let mut alloc = SCAllocator::new(64, 1);
        let ptr = NonNull::new(0x1000usize as *mut u8).expect("non-null test ptr");
        let err = alloc
            .deallocate(ptr, empty_radix_tree())
            .expect_err("unmapped separate dealloc must fail closed");
        assert_eq!(err.to_raw_errno(), AllocError::EUAF.to_raw_errno());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn deallocate_batch_rejects_unmapped_pointer_without_panic() {
        let mut alloc = SCAllocator::new(64, 1);
        let mut batch = [0x1000usize];
        let err = alloc
            .deallocate_batch(&mut batch, 1, empty_radix_tree())
            .expect_err("unmapped separate batch dealloc must fail closed");
        assert_eq!(err.to_raw_errno(), AllocError::EUAF.to_raw_errno());
        assert_eq!(batch[0], 0x1000usize);
    }

    #[test]
    fn deallocate_batch_page_cache_keeps_two_conflicting_pages_per_set() {
        let page_a = PAGE_SIZE;
        let page_b = page_a + PAGE_SIZE * DEALLOCATE_BATCH_PAGE_INDEX_CACHE_SETS;
        assert_eq!(
            SCAllocator::deallocate_batch_page_cache_set(page_a),
            SCAllocator::deallocate_batch_page_cache_set(page_b),
            "test pages should collide in the same cache set"
        );

        let mut cached_pages = [0usize; DEALLOCATE_BATCH_PAGE_INDEX_CACHE_ENTRIES];
        let mut cached_indices =
            [DEALLOCATE_BATCH_PAGE_INDEX_CACHE_EMPTY; DEALLOCATE_BATCH_PAGE_INDEX_CACHE_ENTRIES];

        SCAllocator::fill_deallocate_batch_page_cache(
            page_a,
            11,
            &mut cached_pages,
            &mut cached_indices,
        );
        SCAllocator::fill_deallocate_batch_page_cache(
            page_b,
            22,
            &mut cached_pages,
            &mut cached_indices,
        );

        let set_range = SCAllocator::deallocate_batch_page_cache_set_range(page_a);
        assert!(
            set_range
                .clone()
                .any(|slot| cached_pages[slot] == page_a && cached_indices[slot] == 11),
            "first page should remain cached after a same-set second page is inserted"
        );
        assert!(
            set_range
                .clone()
                .any(|slot| cached_pages[slot] == page_b && cached_indices[slot] == 22),
            "second same-set page should use the other way instead of replacing the first"
        );

        SCAllocator::invalidate_deallocate_batch_page_cache(
            page_a,
            &mut cached_pages,
            &mut cached_indices,
        );
        assert!(
            set_range.clone().any(|slot| cached_pages[slot] == page_a
                && cached_indices[slot] == DEALLOCATE_BATCH_PAGE_INDEX_CACHE_EMPTY),
            "invalidating one recycled page must clear its way"
        );
        assert!(
            set_range
                .clone()
                .any(|slot| cached_pages[slot] == page_b && cached_indices[slot] == 22),
            "invalidating one way must not evict the other same-set live page"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn deallocate_batch_page_cache_real_lookup_retains_same_set_pair() {
        let mut alloc = SCAllocator::new(64, 1);
        alloc.pages.push(ObjectPage::new());
        alloc.pages.push(ObjectPage::new());

        let page_a_ptr = PAGE_SIZE * 32;
        let page_a = align_12k(page_a_ptr);
        let mut page_b_ptr = page_a_ptr + PAGE_SIZE;
        while SCAllocator::deallocate_batch_page_cache_set(align_12k(page_b_ptr))
            != SCAllocator::deallocate_batch_page_cache_set(page_a)
        {
            page_b_ptr += PAGE_SIZE;
        }
        let page_b = align_12k(page_b_ptr);
        assert_ne!(page_a, page_b);

        let ptr_map = empty_radix_tree();
        ptr_map
            .insert(SCAllocator::checked_radix_key(page_a).unwrap(), 1, 1)
            .expect("insert page A mapping");
        ptr_map
            .insert(SCAllocator::checked_radix_key(page_b).unwrap(), 2, 1)
            .expect("insert page B mapping");

        let mut cached_pages = [0usize; DEALLOCATE_BATCH_PAGE_INDEX_CACHE_ENTRIES];
        let mut cached_indices =
            [DEALLOCATE_BATCH_PAGE_INDEX_CACHE_EMPTY; DEALLOCATE_BATCH_PAGE_INDEX_CACHE_ENTRIES];

        assert_eq!(
            alloc
                .lookup_page_index_cached(
                    ptr_map,
                    page_a_ptr,
                    &mut cached_pages,
                    &mut cached_indices
                )
                .expect("page A should resolve through the real radix tree"),
            (0, page_a)
        );
        assert_eq!(
            alloc
                .lookup_page_index_cached(
                    ptr_map,
                    page_b_ptr,
                    &mut cached_pages,
                    &mut cached_indices
                )
                .expect("page B should resolve through the real radix tree"),
            (1, page_b)
        );

        let set_range = SCAllocator::deallocate_batch_page_cache_set_range(page_a);
        assert!(
            set_range
                .clone()
                .any(|slot| cached_pages[slot] == page_a && cached_indices[slot] == 0),
            "real lookup should keep the first same-set page cached"
        );
        assert!(
            set_range
                .clone()
                .any(|slot| cached_pages[slot] == page_b && cached_indices[slot] == 1),
            "real lookup should keep the second same-set page cached"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn deallocate_batch_page_cache_invalidates_recycled_page_mapping() {
        let mut alloc = SCAllocator::new(1024, 1);
        let ptr_map = empty_radix_tree();
        let (first_res, first_stride, first_count) = alloc.allocate_batch(1, ptr_map);
        let first = first_res.expect("first real full-page allocation should succeed");
        let first_base = first.as_ptr() as usize;
        let (second_res, _second_stride, _second_count) = alloc.allocate_batch(1, ptr_map);
        let second = second_res.expect("second real full-page allocation should succeed");
        let second_base = second.as_ptr() as usize;
        assert!(first_count > 1);
        assert_ne!(align_12k(first_base), align_12k(second_base));

        let mut batch = [0usize; 129];
        assert!(first_count + 2 <= batch.len());
        fill_page_batch(&mut batch, first_base, first_stride, first_count);
        batch[first_count] = second_base;
        batch[first_count + 1] = first_base;

        alloc.empty_count = EMPTY_SLAB_RETAIN_LIMIT;
        let err = alloc
            .deallocate_batch(&mut batch[..first_count + 2], first_count + 2, ptr_map)
            .expect_err("stale pointer after page recycle must be reported as unmapped");

        assert_eq!(err.to_raw_errno(), AllocError::EUAF.to_raw_errno());
        assert_eq!(alloc.uninit_count, 1);
        assert!(
            alloc.lookup_page_index(ptr_map, first_base).is_err(),
            "recycled page must not remain reachable through the batch-local cache or radix map"
        );

        let mut cleanup = [0usize; 129];
        let second_idx = alloc
            .lookup_page_index(ptr_map, second_base)
            .expect("second page should still be mapped for cleanup");
        let second_page = alloc.pages.get(second_idx).expect("second page metadata");
        let second_data = second_page
            .get_data_ptr()
            .expect("second page should still have backing") as usize;
        fill_page_batch(&mut cleanup, second_data, alloc.pg_align, alloc.pg_count);
        cleanup[0] = second_base;
        alloc.empty_count = 0;
        alloc
            .deallocate_batch(&mut cleanup[1..alloc.pg_count], alloc.pg_count - 1, ptr_map)
            .expect("cleanup should free the rest of the second real page");
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_rejects_corrupt_partial_start_without_panic() {
        let mut alloc = SCAllocator::new(64, 1);
        alloc.partial_count = 1;
        alloc.partial_start = 7;

        let err = alloc
            .allocate(1, empty_radix_tree())
            .expect_err("corrupt partial list head must not panic");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(alloc.partial_count, 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_rejects_missing_bitfield_metadata_without_panic() {
        let mut alloc = SCAllocator::new(64, 1);
        alloc.pages.push(ObjectPage::new());
        alloc.partial_start = 0;
        alloc.partial_count = 1;

        let err = alloc
            .allocate(1, empty_radix_tree())
            .expect_err("missing bitfield metadata must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(alloc.partial_count, 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_batch_v2_quarantines_zero_progress_partial_without_spinning() {
        let mut alloc = SCAllocator::new(64, 1);
        let bitfield_words = alloc
            .bitfield_words_for_current_page()
            .expect("valid bitmap word count");
        alloc.pages.push(ObjectPage::new());
        for _ in 0..bitfield_words {
            alloc.bitfields.push(u32::MAX);
        }
        alloc.partial_start = 0;
        alloc.partial_count = 1;

        let mut batch = [0usize; 3];
        let res = alloc
            .allocate_batch_v2(empty_radix_tree(), &mut batch, 3, 1)
            .expect("corrupt partial page should be quarantined, not spin or poison batch alloc");

        assert_ne!(res.as_ptr() as usize, 0);
        assert!(batch.iter().all(|ptr| *ptr != 0));
        assert_eq!(alloc.uninit_count, 1);
        assert_eq!(alloc.partial_count, 1);
        assert_ne!(alloc.partial_start, 0);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_batch_v2_respects_requested_alignment_on_partial_first_hit() {
        let mut alloc = SCAllocator::new(8, 1);
        let ptr_map = empty_radix_tree();
        let mut batch = [0usize; 4];

        alloc
            .allocate_batch_v2(ptr_map, &mut batch, 4, 1)
            .expect("initial real page-backed allocation should succeed");
        let unaligned_hole = batch[0];
        assert_eq!(unaligned_hole & 15, 8);
        alloc
            .deallocate(
                NonNull::new(unaligned_hole as *mut u8).expect("non-null allocated slot"),
                ptr_map,
            )
            .expect("test deallocation should create a partial-page hole");

        let mut aligned_batch = [0usize; 1];
        let aligned = alloc
            .allocate_batch_v2(ptr_map, &mut aligned_batch, 1, 16)
            .expect("aligned refill should skip the unaligned partial-page hole");

        assert_ne!(aligned.as_ptr() as usize, unaligned_hole);
        assert_eq!((aligned.as_ptr() as usize) & 15, 0);
        assert_eq!(aligned_batch[0], aligned.as_ptr() as usize);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_batch_v2_rolls_back_reserved_head_when_later_transition_fails() {
        let mut alloc = SCAllocator::new(64, 1);
        let ptr_map = empty_radix_tree();

        let (page, stride, count) = alloc.allocate_batch(1, ptr_map);
        let page = page.expect("real separate page allocation should succeed");
        assert!(
            count >= 4,
            "test needs a multi-slot page so two freed objects leave a partial slab"
        );

        let mut freed = [page.as_ptr() as usize, page.as_ptr() as usize + stride];
        alloc
            .deallocate_batch(&mut freed, 2, ptr_map)
            .expect("test deallocation should create two partial-page holes");
        assert_eq!(alloc.partial_count, 1);

        let partial_idx = alloc.partial_start;
        let words_per_page = alloc
            .bitfield_words_for_current_page()
            .expect("valid test geometry");
        let (bit_start, bit_end) = alloc
            .bitfield_range(partial_idx, words_per_page)
            .expect("partial page bitfield range");
        let before_bits = alloc::vec::Vec::from(&alloc.bitfields[bit_start..bit_end]);
        let before_page_count = alloc.pages.len();

        // Corrupt only the full-list head. The first reserved object in
        // allocate_batch_v2 can still be taken from the partial page, but the
        // later attempt to move that same page back to the full list must fail.
        // The error path must roll back the reserved batch head too; otherwise
        // the failed batch silently consumes one of the two holes and the retry
        // has to grow another object page.
        alloc.full_count = 1;
        alloc.full_start = usize::MAX;

        let mut failed_batch = [0usize; 2];
        let err = alloc
            .allocate_batch_v2(ptr_map, &mut failed_batch, 2, 1)
            .expect_err("corrupt full-list transition should fail the batch");
        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());

        let (after_bit_start, after_bit_end) = alloc
            .bitfield_range(partial_idx, words_per_page)
            .expect("partial page bitfield range after failed batch");
        assert_eq!(
            &alloc.bitfields[after_bit_start..after_bit_end],
            before_bits.as_slice(),
            "failed batch allocation must not consume the up-front reserved head"
        );
        assert_eq!(
            alloc.pages.len(),
            before_page_count,
            "failed batch allocation must not add another page"
        );

        alloc.full_count = 0;
        alloc.full_start = 0;
        let mut retry_batch = [0usize; 2];
        alloc
            .allocate_batch_v2(ptr_map, &mut retry_batch, 2, 1)
            .expect("retry after restoring metadata should reuse the original holes");
        assert_eq!(
            alloc.pages.len(),
            before_page_count,
            "retry should not grow another page if the failed attempt rolled back both holes"
        );
        assert!(retry_batch.contains(&freed[0]));
        assert!(retry_batch.contains(&freed[1]));

        alloc
            .deallocate_batch(&mut retry_batch, 2, ptr_map)
            .expect("cleanup should return the retried batch");
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_batch_v2_rolls_back_reserved_head_when_zero_progress_retire_fails() {
        let mut alloc = SCAllocator::new(PAGE_SIZE / 2, 1);
        let ptr_map = empty_radix_tree();
        assert_eq!(
            alloc.pg_count, 2,
            "test geometry needs one retained free slot to expose page growth"
        );

        // Build a real retained empty page with a radix mapping.  The batch
        // allocator will reserve its head from this page before it encounters
        // the corrupt zero-progress partial descriptor below.
        let warm = alloc
            .allocate(1, ptr_map)
            .expect("warm page allocation should succeed");
        let warm_base = align_12k(warm.as_ptr() as usize);
        alloc
            .deallocate(warm, ptr_map)
            .expect("warm page deallocation should retain an empty slab");
        assert_eq!(alloc.empty_count, 1);
        let before_pages = alloc.pages.len();

        // Add a real descriptor-shaped partial page that cannot make progress:
        // all bitmap bits are already set and the page has no backing object
        // memory.  Then poison only the uninit ring so retiring that descriptor
        // fails after allocate_batch_v2 has already reserved its batch head.
        let words_per_page = alloc
            .bitfield_words_for_current_page()
            .expect("valid bitmap geometry");
        alloc.pages.push(ObjectPage::new());
        for _ in 0..words_per_page {
            alloc.bitfields.push(u32::MAX);
        }
        let corrupt_partial = alloc.pages.len() - 1;
        alloc.partial_start = corrupt_partial;
        alloc.partial_count = 1;
        alloc.uninit_start = usize::MAX;
        alloc.uninit_count = 1;

        let mut failed_batch = [0usize; 2];
        let err = alloc
            .allocate_batch_v2(ptr_map, &mut failed_batch, 2, 1)
            .expect_err("corrupt uninit ring should fail zero-progress retirement");
        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());

        // Restore the test-only corruption.  A correct failed batch has returned
        // the reserved head to the warm page, so retrying the same two-object
        // batch reuses that page instead of allocating a third descriptor.
        alloc.uninit_start = 0;
        alloc.uninit_count = 0;
        alloc.partial_start = 0;
        alloc.partial_count = 0;

        let mut retry_batch = [0usize; 2];
        alloc
            .allocate_batch_v2(ptr_map, &mut retry_batch, 2, 1)
            .expect("retry should reuse the rolled-back warm page");
        assert_eq!(
            alloc.pages.len(),
            before_pages + 1,
            "failed batch must not leave the reserved head allocated and force retry page growth"
        );
        assert!(
            retry_batch.iter().all(|addr| align_12k(*addr) == warm_base),
            "both retry objects should come from the original warm page"
        );

        alloc
            .deallocate_batch(&mut retry_batch, 2, ptr_map)
            .expect("cleanup should return the retried batch");
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_batch_v2_rolls_back_reserved_head_when_get_empty_fails() {
        let mut alloc = SCAllocator::new(PAGE_SIZE / 2, 1);
        let ptr_map = empty_radix_tree();
        assert_eq!(
            alloc.pg_count, 2,
            "test geometry needs a two-slot page to fill after reserving the head"
        );

        let warm = alloc
            .allocate(1, ptr_map)
            .expect("warm page allocation should succeed");
        let warm_base = align_12k(warm.as_ptr() as usize);
        alloc
            .deallocate(warm, ptr_map)
            .expect("warm page deallocation should retain an empty slab");
        assert_eq!(alloc.empty_count, 1);
        let before_pages = alloc.pages.len();

        // Keep the warm page available for the up-front reserved head, but
        // poison the uninit ring that the allocator must consult after that
        // warm page is filled and the requested batch still needs another
        // object.  The failure therefore happens after both the reserved head
        // and one prefix object have been allocated.
        alloc.uninit_start = usize::MAX;
        alloc.uninit_count = 1;

        let mut failed_batch = [0usize; 3];
        let err = alloc
            .allocate_batch_v2(ptr_map, &mut failed_batch, 3, 1)
            .expect_err("corrupt uninit ring should fail the later get_empty");
        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());

        alloc.uninit_start = 0;
        alloc.uninit_count = 0;

        let mut retry_batch = [0usize; 2];
        alloc
            .allocate_batch_v2(ptr_map, &mut retry_batch, 2, 1)
            .expect("retry should reuse the rolled-back warm page");
        assert_eq!(
            alloc.pages.len(),
            before_pages,
            "failed get_empty must not leave the warm page full and force retry page growth"
        );
        assert!(
            retry_batch.iter().all(|addr| align_12k(*addr) == warm_base),
            "retry objects should come from the original warm page"
        );

        alloc
            .deallocate_batch(&mut retry_batch, 2, ptr_map)
            .expect("cleanup should return the retried batch");
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_batch_v2_rolls_back_reserved_head_when_empty_candidate_metadata_is_missing() {
        let mut alloc = SCAllocator::new(PAGE_SIZE / 2, 1);
        let ptr_map = empty_radix_tree();
        assert_eq!(alloc.pg_count, 2);
        let words_per_page = alloc
            .bitfield_words_for_current_page()
            .expect("valid bitmap geometry");

        let warm = alloc
            .allocate(1, ptr_map)
            .expect("warm page allocation should succeed");
        let warm_base = align_12k(warm.as_ptr() as usize);
        alloc
            .deallocate(warm, ptr_map)
            .expect("warm page deallocation should retain an empty slab");
        assert_eq!(alloc.empty_count, 1);

        let mut corrupt_page = ObjectPage::new();
        let corrupt_addr = corrupt_page
            .allocate_page(alloc.pg_num)
            .expect("corrupt test candidate should have real backing")
            as usize;
        alloc.pages.push(corrupt_page);
        let corrupt_idx = alloc.pages.len() - 1;
        alloc
            .handle_rd_tree_insert(ptr_map, corrupt_idx, corrupt_addr)
            .expect("test candidate should be mapped before entering the empty list");
        alloc
            .back_ety(corrupt_idx)
            .expect("test candidate should enter the empty list");
        assert_eq!(alloc.empty_start, 0);
        assert_eq!(alloc.empty_count, 2);

        // Do not append bitfield words for `corrupt_idx`.  The candidate is a
        // real backed empty page with a radix mapping, but its metadata slice is
        // missing.  The failure happens after the warm page has supplied the
        // reserved head and one prefix object.
        let mut failed_batch = [0usize; 3];
        let err = alloc
            .allocate_batch_v2(ptr_map, &mut failed_batch, 3, 1)
            .expect_err("missing candidate bitfield should fail the later empty-page path");
        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());

        if alloc.empty_count > 0 && alloc.empty_start == corrupt_idx {
            let removed = alloc
                .remove_empty_index()
                .expect("restored corrupt candidate should be removable for test cleanup");
            assert_eq!(removed, corrupt_idx);
        }
        alloc
            .handle_rd_tree_remove(ptr_map, corrupt_addr)
            .expect("test cleanup should remove the corrupt candidate mapping");
        while alloc.bitfields.len() < (corrupt_idx + 1) * words_per_page {
            alloc.bitfields.push(0);
        }
        alloc
            .back_uninit(corrupt_idx)
            .expect("test cleanup should recycle the corrupt candidate descriptor");

        let mut retry_batch = [0usize; 2];
        alloc
            .allocate_batch_v2(ptr_map, &mut retry_batch, 2, 1)
            .expect("retry should reuse the rolled-back warm page");
        assert!(
            retry_batch.iter().all(|addr| align_12k(*addr) == warm_base),
            "failed candidate metadata lookup must not leave the original warm page full"
        );

        alloc
            .deallocate_batch(&mut retry_batch, 2, ptr_map)
            .expect("cleanup should return the retried batch");
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_batch_v2_rotates_partial_start_to_non_full_strict_alignment_match() {
        let mut alloc = SCAllocator::new(8, 1);
        let ptr_map = empty_radix_tree();
        let expected_count = PAGE_SIZE / 8;
        let strict_cap = crate::page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT;
        assert!(
            expected_count / 2 > strict_cap,
            "test needs more 16-byte-aligned slots than the strict-alignment cap"
        );

        let mut page_a_batch = [0usize; PAGE_SIZE / 8];
        let page_a = alloc
            .allocate_batch_v2(ptr_map, &mut page_a_batch, expected_count, 1)
            .expect("first real page-backed allocation should succeed")
            .as_ptr();
        let mut page_b_batch = [0usize; PAGE_SIZE / 8];
        let page_b = alloc
            .allocate_batch_v2(ptr_map, &mut page_b_batch, expected_count, 1)
            .expect("second real page-backed allocation should succeed")
            .as_ptr();

        assert_eq!((page_a as usize) & 15, 0);
        assert_eq!((page_b as usize) & 15, 0);
        assert_ne!(align_12k(page_a as usize), align_12k(page_b as usize));

        let page_a_idx = alloc
            .lookup_page_index_for_page(ptr_map, align_12k(page_a as usize))
            .expect("page A should remain indexed");
        let page_b_idx = alloc
            .lookup_page_index_for_page(ptr_map, align_12k(page_b as usize))
            .expect("page B should remain indexed");

        let unaligned_a_free = unsafe { page_a.add(8) };
        assert_ne!((unaligned_a_free as usize) & 15, 0);
        alloc
            .deallocate(
                NonNull::new(unaligned_a_free).expect("non-null allocated slot"),
                ptr_map,
            )
            .expect("freeing one unaligned object from A should make A partial");
        assert_eq!(alloc.partial_start, page_a_idx);

        let b_aligned_free_count = strict_cap + 1;
        let mut b_aligned_frees = [0usize; crate::page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT + 1];
        for (idx, slot) in b_aligned_frees.iter_mut().enumerate() {
            *slot = unsafe { page_b.add(idx * 16) as usize };
            assert_eq!(*slot & 15, 0);
        }
        alloc
            .deallocate_batch(&mut b_aligned_frees, b_aligned_free_count, ptr_map)
            .expect("freeing many aligned objects from B should make B partial");
        assert_eq!(
            alloc.partial_start, page_a_idx,
            "A should remain the partial-ring head before the strict-alignment scan"
        );
        assert!(
            !alloc
                .pages
                .get(page_b_idx)
                .expect("page B metadata")
                .is_full(expected_count),
            "B should be partial before strict allocation"
        );

        let mut first_strict_batch = [0usize; crate::page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT];
        let first_strict = alloc
            .allocate_batch_v2(ptr_map, &mut first_strict_batch, strict_cap, 16)
            .expect("strict-alignment scan should skip A and allocate capped batch from B");
        assert_eq!(first_strict.as_ptr(), page_b);
        for (idx, ptr) in first_strict_batch
            .iter()
            .take(strict_cap - 1)
            .copied()
            .enumerate()
        {
            assert_eq!(ptr, unsafe { page_b.add((idx + 1) * 16) as usize });
            assert_eq!(ptr & 15, 0);
        }
        assert_eq!(first_strict_batch[strict_cap - 1], page_b as usize);
        assert_eq!(
            alloc.partial_start, page_b_idx,
            "non-full strict-alignment match should become the next partial scan head"
        );
        assert!(
            !alloc
                .pages
                .get(page_b_idx)
                .expect("page B metadata")
                .is_full(expected_count),
            "B should remain partial because one aligned slot is still free"
        );

        let mut second_strict_batch = [0usize; 1];
        let second_strict = alloc
            .allocate_batch_v2(ptr_map, &mut second_strict_batch, 1, 16)
            .expect("rotated head should let the next strict request consume B's remaining match");
        assert_eq!(second_strict.as_ptr(), unsafe {
            page_b.add(strict_cap * 16)
        });
        assert_eq!(second_strict_batch[0], second_strict.as_ptr() as usize);
        assert!(alloc
            .pages
            .get(page_b_idx)
            .expect("page B metadata")
            .is_full(expected_count));
        assert_eq!(
            alloc.partial_start, page_a_idx,
            "after B moves full, A should be the remaining partial page"
        );

        let mut reused_a_batch = [0usize; 1];
        let reused_a = alloc
            .allocate_batch_v2(ptr_map, &mut reused_a_batch, 1, 1)
            .expect("A's unaligned free slot should remain reusable after B rotation");
        assert_eq!(reused_a.as_ptr(), unaligned_a_free);
        assert_eq!(reused_a_batch[0], reused_a.as_ptr() as usize);
        assert!(alloc
            .pages
            .get(page_a_idx)
            .expect("page A metadata")
            .is_full(expected_count));
        assert_eq!(alloc.partial_count, 0);

        fill_page_batch(
            &mut page_a_batch,
            page_a as usize,
            alloc.pg_align,
            alloc.pg_count,
        );
        alloc
            .deallocate_batch(&mut page_a_batch, expected_count, ptr_map)
            .expect("cleanup of full page A should succeed");
        fill_page_batch(
            &mut page_b_batch,
            page_b as usize,
            alloc.pg_align,
            alloc.pg_count,
        );
        alloc
            .deallocate_batch(&mut page_b_batch, expected_count, ptr_map)
            .expect("cleanup of full page B should succeed");
        assert_eq!(alloc.empty_count, 2);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_rotates_partial_start_to_non_full_strict_alignment_match() {
        let mut alloc = SCAllocator::new(8, 1);
        let ptr_map = empty_radix_tree();
        let expected_count = PAGE_SIZE / 8;

        let mut page_a_batch = [0usize; PAGE_SIZE / 8];
        let page_a = alloc
            .allocate_batch_v2(ptr_map, &mut page_a_batch, expected_count, 1)
            .expect("first real page-backed allocation should succeed")
            .as_ptr();
        let mut page_b_batch = [0usize; PAGE_SIZE / 8];
        let page_b = alloc
            .allocate_batch_v2(ptr_map, &mut page_b_batch, expected_count, 1)
            .expect("second real page-backed allocation should succeed")
            .as_ptr();

        assert_eq!((page_a as usize) & 15, 0);
        assert_eq!((page_b as usize) & 15, 0);
        assert_ne!(align_12k(page_a as usize), align_12k(page_b as usize));

        let page_a_idx = alloc
            .lookup_page_index_for_page(ptr_map, align_12k(page_a as usize))
            .expect("page A should remain indexed");
        let page_b_idx = alloc
            .lookup_page_index_for_page(ptr_map, align_12k(page_b as usize))
            .expect("page B should remain indexed");

        let unaligned_a_free = unsafe { page_a.add(8) };
        assert_ne!((unaligned_a_free as usize) & 15, 0);
        alloc
            .deallocate(
                NonNull::new(unaligned_a_free).expect("non-null allocated slot"),
                ptr_map,
            )
            .expect("freeing one unaligned object from A should make A partial");
        assert_eq!(alloc.partial_start, page_a_idx);

        let mut b_aligned_frees = [page_b as usize, unsafe { page_b.add(16) as usize }];
        assert!(b_aligned_frees.iter().all(|ptr| *ptr & 15 == 0));
        alloc
            .deallocate_batch(&mut b_aligned_frees, 2, ptr_map)
            .expect("freeing two aligned objects from B should make B partial");
        assert_eq!(
            alloc.partial_start, page_a_idx,
            "A should remain the partial-ring head before the strict-alignment scan"
        );
        assert!(
            !alloc
                .pages
                .get(page_b_idx)
                .expect("page B metadata")
                .is_full(expected_count),
            "B should be partial before strict allocation"
        );

        let first_strict = alloc
            .allocate(16, ptr_map)
            .expect("strict-alignment scan should skip A and allocate from B");
        assert_eq!(first_strict.as_ptr(), page_b);
        assert_eq!(
            alloc.partial_start, page_b_idx,
            "non-full strict-alignment single-object match should become the next partial scan head"
        );
        assert!(
            !alloc
                .pages
                .get(page_b_idx)
                .expect("page B metadata")
                .is_full(expected_count),
            "B should remain partial because one aligned slot is still free"
        );

        let second_strict = alloc
            .allocate(16, ptr_map)
            .expect("rotated head should let the next strict request consume B's remaining match");
        assert_eq!(second_strict.as_ptr(), unsafe { page_b.add(16) });
        assert!(alloc
            .pages
            .get(page_b_idx)
            .expect("page B metadata")
            .is_full(expected_count));
        assert_eq!(
            alloc.partial_start, page_a_idx,
            "after B moves full, A should be the remaining partial page"
        );

        let reused_a = alloc
            .allocate(1, ptr_map)
            .expect("A's unaligned free slot should remain reusable after B rotation");
        assert_eq!(reused_a.as_ptr(), unaligned_a_free);
        assert!(alloc
            .pages
            .get(page_a_idx)
            .expect("page A metadata")
            .is_full(expected_count));
        assert_eq!(alloc.partial_count, 0);

        fill_page_batch(
            &mut page_a_batch,
            page_a as usize,
            alloc.pg_align,
            alloc.pg_count,
        );
        alloc
            .deallocate_batch(&mut page_a_batch, expected_count, ptr_map)
            .expect("cleanup of full page A should succeed");
        fill_page_batch(
            &mut page_b_batch,
            page_b as usize,
            alloc.pg_align,
            alloc.pg_count,
        );
        alloc
            .deallocate_batch(&mut page_b_batch, expected_count, ptr_map)
            .expect("cleanup of full page B should succeed");
        assert_eq!(alloc.empty_count, 2);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_batch_v2_strict_alignment_caps_request_above_limit() {
        let mut alloc = SCAllocator::new(8, 1);
        let ptr_map = empty_radix_tree();
        let strict_cap = strict_alignment_batch_request_limit(16, alloc.pg_align);
        assert_eq!(strict_cap, crate::page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT);
        assert!(
            alloc.pg_count / 2 > strict_cap,
            "test needs remaining aligned slots after one capped strict request"
        );

        let mut batch = [0usize; crate::page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT + 8];
        let first = alloc
            .allocate_batch_v2(ptr_map, &mut batch, strict_cap + 8, 16)
            .expect("strict batch above cap should succeed with a capped effective count")
            .as_ptr() as usize;

        let filled = batch.iter().filter(|ptr| **ptr != 0).count();
        assert_eq!(filled, strict_cap);
        assert_eq!(batch[strict_cap - 1], first);
        assert!(batch.iter().take(strict_cap).all(|ptr| *ptr & 15 == 0));
        assert!(batch.iter().skip(strict_cap).all(|ptr| *ptr == 0));

        let page_idx = alloc
            .lookup_page_index_for_page(ptr_map, align_12k(first))
            .expect("strict allocation page should be indexed");
        assert!(
            !alloc
                .pages
                .get(page_idx)
                .expect("strict allocation page metadata")
                .is_full(alloc.pg_count),
            "capped strict batch should leave the slab partial"
        );

        let mut next = [0usize; 1];
        let next_ptr = alloc
            .allocate_batch_v2(ptr_map, &mut next, 1, 16)
            .expect("remaining aligned slot should still be available after capped request")
            .as_ptr() as usize;
        assert_eq!(next[0], next_ptr);
        assert_eq!(next_ptr & 15, 0);
        assert!(!batch.iter().take(strict_cap).any(|ptr| *ptr == next_ptr));

        let mut cleanup = [0usize; crate::page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT + 9];
        cleanup[..strict_cap].copy_from_slice(&batch[..strict_cap]);
        cleanup[strict_cap] = next_ptr;
        alloc
            .deallocate_batch(&mut cleanup, strict_cap + 1, ptr_map)
            .expect("cleanup of strict capped allocations should succeed");
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn handle_rd_tree_insert_rejects_radix_key_overflow_without_panic() {
        let alloc = SCAllocator::new(64, 1);

        let err = alloc
            .handle_rd_tree_insert(empty_radix_tree(), 0, usize::MAX)
            .expect_err("overflowing radix key must be an allocator error, not a panic");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn handle_rd_tree_insert_rejects_index_value_overflow_without_panic() {
        let alloc = SCAllocator::new(64, 1);

        let err = alloc
            .handle_rd_tree_insert(empty_radix_tree(), usize::MAX, 0x1000)
            .expect_err("radix value overflow must be rejected before insertion");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn handle_rd_tree_remove_rejects_radix_key_overflow_without_panic() {
        let alloc = SCAllocator::new(64, 1);

        let err = alloc
            .handle_rd_tree_remove(empty_radix_tree(), usize::MAX)
            .expect_err("overflowing radix remove key must be rejected");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn handle_rd_tree_insert_does_not_leave_first_split_on_second_key_overflow() {
        let alloc = SCAllocator::new(64, 2);
        let ptr_map = empty_radix_tree();
        let addr = split_radix_second_key_overflow_addr();
        let first_key = SCAllocator::checked_radix_key(align_12k(addr))
            .expect("first split radix key should be representable");

        let err = alloc
            .handle_rd_tree_insert(ptr_map, 0, addr)
            .expect_err("second split radix key should fail before leaving a stale first split");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(
            ptr_map.get_mut(first_key),
            0,
            "failed split insertion must not leave the first chunk mapped"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn handle_rd_tree_remove_does_not_drop_first_split_on_second_key_overflow() {
        let alloc = SCAllocator::new(64, 2);
        let ptr_map = empty_radix_tree();
        let addr = split_radix_second_key_overflow_addr();
        let first_key = SCAllocator::checked_radix_key(align_12k(addr))
            .expect("first split radix key should be representable");
        ptr_map
            .insert(first_key, 7, 1)
            .expect("test precondition should install first split mapping");

        let err = alloc
            .handle_rd_tree_remove(ptr_map, addr)
            .expect_err("second split radix key should fail without deleting first split");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(
            ptr_map.get_mut(first_key),
            7,
            "failed split removal must preserve the first chunk mapping"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn deallocate_restores_full_page_when_partial_insert_fails() {
        let mut alloc = SCAllocator::new(1024, 1);
        let ptr_map = empty_radix_tree();
        let (ptr_res, stride, count) = alloc.allocate_batch(1, ptr_map);
        let ptr = ptr_res.expect("real full-page allocation should succeed");
        let base = ptr.as_ptr() as usize;
        let idx = alloc
            .lookup_page_index(ptr_map, base)
            .expect("allocated page must be indexed");
        let mut batch = [0usize; 128];
        assert!(count > 1);
        assert!(count <= batch.len());
        assert_eq!(alloc.full_count, 1);

        alloc.partial_count = 1;
        alloc.partial_start = usize::MAX;
        let err = alloc
            .deallocate(ptr, ptr_map)
            .expect_err("corrupt partial list must fail after full-page deallocation");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(alloc.full_count, 1);
        assert_eq!(alloc.full_start, idx);
        assert_eq!(
            alloc
                .lookup_page_index(ptr_map, base)
                .expect("mapping must survive"),
            idx
        );
        assert!(
            alloc.pages.get_mut(idx).expect("page").is_full(count),
            "failed list transition must restore the cleared allocation bit and counter"
        );

        alloc.partial_count = 0;
        alloc.partial_start = 0;
        fill_page_batch(&mut batch, base, stride, count);
        alloc
            .deallocate_batch(&mut batch[..count], count, ptr_map)
            .expect("cleanup after rollback should free the real page");
        assert_eq!(alloc.empty_count, 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn deallocate_batch_restores_full_page_when_partial_insert_fails() {
        let mut alloc = SCAllocator::new(1024, 1);
        let ptr_map = empty_radix_tree();
        let (ptr_res, stride, count) = alloc.allocate_batch(1, ptr_map);
        let ptr = ptr_res.expect("real full-page allocation should succeed");
        let base = ptr.as_ptr() as usize;
        let idx = alloc
            .lookup_page_index(ptr_map, base)
            .expect("allocated page must be indexed");
        let mut batch = [0usize; 128];
        assert!(count > 1);
        assert!(count <= batch.len());
        fill_page_batch(&mut batch, base, stride, count);

        alloc.partial_count = 1;
        alloc.partial_start = usize::MAX;
        let err = alloc
            .deallocate_batch(&mut batch[..1], 1, ptr_map)
            .expect_err("corrupt partial list must fail after batch full-to-partial move");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(alloc.full_count, 1);
        assert_eq!(alloc.full_start, idx);
        assert_eq!(
            alloc
                .lookup_page_index(ptr_map, base)
                .expect("mapping must survive"),
            idx
        );
        assert!(
            alloc.pages.get_mut(idx).expect("page").is_full(count),
            "failed batch transition must restore page allocation state"
        );

        alloc.partial_count = 0;
        alloc.partial_start = 0;
        fill_page_batch(&mut batch, base, stride, count);
        alloc
            .deallocate_batch(&mut batch[..count], count, ptr_map)
            .expect("cleanup after rollback should free the real page");
        assert_eq!(alloc.empty_count, 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn deallocate_batch_restores_radix_mapping_when_uninit_recycle_fails() {
        let mut alloc = SCAllocator::new(1024, 1);
        let ptr_map = empty_radix_tree();
        let (ptr_res, stride, count) = alloc.allocate_batch(1, ptr_map);
        let ptr = ptr_res.expect("real full-page allocation should succeed");
        let base = ptr.as_ptr() as usize;
        let idx = alloc
            .lookup_page_index(ptr_map, base)
            .expect("allocated page must be indexed");
        let mut batch = [0usize; 128];
        assert!(count > 1);
        assert!(count <= batch.len());
        fill_page_batch(&mut batch, base, stride, count);

        alloc.empty_count = EMPTY_SLAB_RETAIN_LIMIT;
        alloc.uninit_count = 1;
        alloc.uninit_start = usize::MAX;
        let err = alloc
            .deallocate_batch(&mut batch[..count], count, ptr_map)
            .expect_err("corrupt uninit list must fail after radix removal is attempted");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(alloc.full_count, 1);
        assert_eq!(alloc.full_start, idx);
        assert_eq!(
            alloc
                .lookup_page_index(ptr_map, base)
                .expect("failed recycle must restore page-to-object radix mapping"),
            idx
        );
        assert!(
            alloc.pages.get_mut(idx).expect("page").is_full(count),
            "failed recycle must restore page allocation state"
        );

        alloc.empty_count = 0;
        alloc.empty_start = 0;
        alloc.uninit_count = 0;
        alloc.uninit_start = 0;
        fill_page_batch(&mut batch, base, stride, count);
        alloc
            .deallocate_batch(&mut batch[..count], count, ptr_map)
            .expect("cleanup after restoring radix mapping and lists should succeed");
        assert_eq!(alloc.empty_count, 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_restores_partial_page_when_full_insert_fails() {
        let mut alloc = SCAllocator::new(1024, 1);
        let ptr_map = empty_radix_tree();
        let count = alloc.pg_count;
        let mut batch = [0usize; 128];
        assert!(count > 1);
        assert!(count <= batch.len());
        let existing = count - 1;

        alloc
            .allocate_batch_v2(ptr_map, &mut batch[..existing], existing, 1)
            .expect("real partial-page allocation should succeed");
        let idx = alloc
            .lookup_page_index(ptr_map, batch[0])
            .expect("partial page must be indexed");
        assert_eq!(alloc.partial_count, 1);
        assert_eq!(alloc.partial_start, idx);
        assert!(!alloc.pages.get_mut(idx).expect("page").is_full(count));

        alloc.full_count = 1;
        alloc.full_start = usize::MAX;
        let err = alloc
            .allocate(1, ptr_map)
            .expect_err("corrupt full list must fail after page-local allocation");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(alloc.partial_count, 1);
        assert_eq!(alloc.partial_start, idx);
        assert!(
            !alloc.pages.get_mut(idx).expect("page").is_full(count),
            "failed partial-to-full move must roll back the just-allocated object"
        );

        alloc.full_count = 0;
        alloc.full_start = 0;
        let last = alloc
            .allocate(1, ptr_map)
            .expect("rolled-back slot should be reusable");
        batch[existing] = last.as_ptr() as usize;
        alloc
            .deallocate_batch(&mut batch[..count], count, ptr_map)
            .expect("cleanup after allocation rollback should free the real page");
        assert_eq!(alloc.empty_count, 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_rolls_back_new_full_page_when_full_insert_fails() {
        let mut alloc = SCAllocator::new(PAGE_SIZE, 1);
        let ptr_map = empty_radix_tree();
        assert_eq!(alloc.pg_count, 1);

        alloc.full_count = 1;
        alloc.full_start = usize::MAX;
        let err = alloc
            .allocate(1, ptr_map)
            .expect_err("corrupt full list must fail after new-page allocation");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(alloc.pages.len(), 1);
        assert_eq!(alloc.uninit_count, 1);
        assert!(
            !alloc.pages.get_mut(0).expect("page").is_inited(),
            "failed new-page insertion must destroy the page and return it to uninit"
        );

        alloc.full_count = 0;
        alloc.full_start = 0;
        let ptr = alloc
            .allocate(1, ptr_map)
            .expect("rolled-back uninit page should be reusable");
        let mut batch = [ptr.as_ptr() as usize];
        alloc
            .deallocate_batch(&mut batch, 1, ptr_map)
            .expect("cleanup after new-page rollback should succeed");
        assert_eq!(alloc.empty_count, 0);
        assert_eq!(alloc.uninit_count, 1);
    }

    #[test]
    fn new_rejects_oversized_size_class_without_dividing_by_zero() {
        let alloc = SCAllocator::new(PAGE_SIZE + 1, 1);
        assert_eq!(alloc.pg_count, 0);
        assert_eq!(alloc.pg_num, 0);
        assert_eq!(alloc.pg_align, 0);
    }

    #[test]
    fn new_preserves_normal_size_class_geometry() {
        let alloc = SCAllocator::new(64, 1);
        assert_eq!(alloc.pg_count, PAGE_SIZE / 64);
        assert_eq!(alloc.pg_num, 1);
        assert_eq!(alloc.pg_align, 64);
    }
}
