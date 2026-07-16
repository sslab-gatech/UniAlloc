use crate::collections::linklist::*;
use crate::collections::radix_tree::{allocate_node, TreeNode};
use crate::collections::radix_tree::{try_with_rd_tree, RadixTree};
use crate::error::{AllocError, Result};
use crate::page::{EfObjectPage, EfObjectPageAllocationState, PG_BUMP};
use crate::prelude::*;
use crate::*;
use core::cmp::Ordering;
use core::ptr::{self, null_mut};

/// Maximum number of completely empty one-OS-page object pages retained per size class.
///
/// The previous hard-coded threshold kept more than 2048 empty pages in every
/// size-class slab before returning page backing to the uninitialized pool. A
/// later 32-page warm cache fixed the worst case, but a mostly idle process can
/// still touch many size classes and retain one warm cache per class.  Keep a
/// small restart-friendly cache for cold bursts, and recycle beyond that so
/// fully empty slabs stop contributing to RSS/external fragmentation.
///
/// The actual limit is class/span aware (see
/// `empty_slab_retain_limit_for_geometry`).  This constant is the cap for tiny
/// multi-slot classes whose object page consumes one OS page; larger span
/// classes retain proportionally fewer empty pages so the allocator bounds
/// external fragmentation in OS pages rather than in object-page descriptors.
/// Single-slot classes retain no empty slabs because an empty one-object span
/// has almost no batching value but can pin one or more OS pages after bursts.
const EMPTY_SLAB_RETAIN_LIMIT: usize = 8;
const EMPTY_SLAB_RETAIN_OS_PAGE_BUDGET: usize = 8;
const DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_SETS: usize = 4;
const DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_WAYS: usize = 2;
const DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES: usize =
    DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_SETS * DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_WAYS;
const DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_EMPTY: i64 = 0;

/// Return the pg_num-scaled warm empty-page cache size for a multi-slot class.
///
/// `pg_num` is the number of OS pages backing one object page for the class.
/// Tiny multi-slot classes (`pg_num == 1`) keep an 8-page warm set.  Classes
/// backed by multi-page spans keep fewer empty object pages so post-burst RSS
/// is bounded by roughly `EMPTY_SLAB_RETAIN_OS_PAGE_BUDGET` OS pages per class.
/// Very large spans (`pg_num > budget`) are recycled immediately: retaining even
/// one fully empty huge span would dominate the allocator's footprint and worsen
/// external fragmentation.
#[inline]
fn empty_slab_retain_limit_for_pg_num(pg_num: usize) -> usize {
    if pg_num == 0 || pg_num > EMPTY_SLAB_RETAIN_OS_PAGE_BUDGET {
        return 0;
    }
    let span_scaled_limit = EMPTY_SLAB_RETAIN_OS_PAGE_BUDGET / pg_num;
    core::cmp::min(EMPTY_SLAB_RETAIN_LIMIT, span_scaled_limit)
}

/// Return the warm empty-page cache size for a size-class geometry.
///
/// Single-slot pages/spans are recycled immediately: once the sole object is
/// freed, retaining the empty span gives no useful batching while still pinning
/// its backing OS pages.  Multi-slot classes keep the existing pg_num-scaled
/// warm-cache policy.
#[inline]
fn empty_slab_retain_limit_for_geometry(pg_num: usize, pg_count: usize) -> usize {
    if pg_count <= 1 {
        0
    } else {
        empty_slab_retain_limit_for_pg_num(pg_num)
    }
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
    full_start: *mut EfObjectPage,
    /// Tracks the start of partial slab. It is meaningful only if `partial_count >= 1`
    partial_start: *mut EfObjectPage,
    /// Tracks the start of empty slab. It is meaningful only if `empty_count >= 1`
    empty_start: *mut EfObjectPage,
    /// Tracks the start of uninitialized slab. It is meaningful only if `uninit_count >= 1`
    uninit_start: *mut EfObjectPage,
    empty_count: usize,

    // start, count
    // full: (usize, usize),
    // partial: (usize, usize),
    // empty: (usize, usize),
    // uninit: (usize, usize),

    // page_num: how many OS pages in our page
    pg_num: usize,
    // pg_count: how many chunks of current size class in one "page"
    pg_count: i32,
    // when allocating, what align between chunks shall we take
    pg_align: i32,
}

impl SCAllocator {
    fn empty() -> Self {
        Self {
            full_start: null_mut(),
            partial_start: null_mut(),
            empty_start: null_mut(),
            uninit_start: null_mut(),
            empty_count: 0,
            pg_count: 0,
            pg_num: 0,
            pg_align: 0,
        }
    }

    // The new "new" function takes three parameters:
    // current size class, current size class's idx, how many OS pages are combined into one page.rs
    pub fn new(size_class: usize, num_os_pages: usize) -> Self {
        let (pg_count, align) = match super::checked_size_class_geometry(size_class, num_os_pages) {
            Some(geometry) => geometry,
            None => return Self::empty(),
        };

        if pg_count > i32::MAX as usize || align > i32::MAX as usize {
            return Self::empty();
        }

        Self {
            full_start: null_mut(),
            partial_start: null_mut(),
            empty_start: null_mut(),
            uninit_start: null_mut(),
            empty_count: 0,
            pg_count: pg_count as i32,
            pg_num: num_os_pages,
            pg_align: align as i32,
        }
    }

    fn try_get_ref(ptr: *mut EfObjectPage) -> Result<&'static mut EfObjectPage> {
        unsafe { ptr.as_mut().ok_or(AllocError::EFATAL) }
    }

    fn page_ptr(idx: &mut EfObjectPage) -> *mut EfObjectPage {
        idx as *mut _
    }

    fn insert_ring(head: &mut *mut EfObjectPage, idx: &mut EfObjectPage) -> Result<()> {
        let idx_ptr = Self::page_ptr(idx);
        if !(*head).is_null() {
            let head_ptr = *head;
            let head_ref = Self::try_get_ref(head_ptr)?;
            let prev = head_ref.try_get_prev()?;
            idx.set_prev(Self::page_ptr(prev) as usize);
            idx.set_next(head_ptr as usize);
            prev.set_next(idx_ptr as usize);
            head_ref.set_prev(idx_ptr as usize);
        } else {
            idx.set_prev(idx_ptr as usize);
            idx.set_next(idx_ptr as usize);
            *head = idx_ptr;
        }
        Ok(())
    }

    fn remove_ring(head: &mut *mut EfObjectPage, idx: &mut EfObjectPage) -> Result<()> {
        if (*head).is_null() {
            return Err(AllocError::EFATAL);
        }
        let head_ptr = *head;
        let idx_ptr = Self::page_ptr(idx);
        let next = idx.try_get_next()?;
        let next_ptr = Self::page_ptr(next);
        let prev = idx.try_get_prev()?;
        let prev_ptr = Self::page_ptr(prev);

        if ptr::eq(next_ptr, idx_ptr) {
            if !ptr::eq(prev_ptr, idx_ptr) || !ptr::eq(idx_ptr, head_ptr) {
                return Err(AllocError::EFATAL);
            }
            *head = null_mut();
        } else {
            next.set_prev(prev_ptr as usize);
            prev.set_next(next_ptr as usize);
            if ptr::eq(head_ptr, idx_ptr) {
                *head = next_ptr;
            }
        }
        idx.set_next(0);
        idx.set_prev(0);
        Ok(())
    }

    fn pop_ring_head(head: &mut *mut EfObjectPage) -> Result<&'static mut EfObjectPage> {
        let idx = Self::try_get_ref(*head)?;
        Self::remove_ring(head, idx)?;
        Ok(idx)
    }

    pub fn remove_full(&mut self, idx: &mut EfObjectPage) -> Result<()> {
        Self::remove_ring(&mut self.full_start, idx)
    }

    pub fn insert_full(&mut self, idx: &mut EfObjectPage) -> Result<()> {
        Self::insert_ring(&mut self.full_start, idx)
    }

    pub fn insert_partial(&mut self, idx: &mut EfObjectPage) -> Result<()> {
        Self::insert_ring(&mut self.partial_start, idx)
    }

    pub fn remove_partial(&mut self, idx: &mut EfObjectPage) -> Result<()> {
        Self::remove_ring(&mut self.partial_start, idx)
    }

    pub fn remove_uninit(&mut self) -> Result<&'static mut EfObjectPage> {
        Self::pop_ring_head(&mut self.uninit_start)
    }

    pub fn remove_empty(&mut self) -> Result<&'static mut EfObjectPage> {
        if self.empty_start.is_null() || self.empty_count == 0 {
            return Err(AllocError::EFATAL);
        }

        let ety_head = Self::pop_ring_head(&mut self.empty_start)?;
        self.empty_count = self.empty_count.checked_sub(1).ok_or(AllocError::EFATAL)?;
        Ok(ety_head)
    }

    fn restore_page_to_allocation_list(
        &mut self,
        idx: &mut EfObjectPage,
        was_full: bool,
    ) -> Result<()> {
        if was_full {
            self.insert_full(idx)
        } else {
            self.insert_partial(idx)
        }
    }

    fn remove_from_allocation_list(
        &mut self,
        idx: &mut EfObjectPage,
        was_full: bool,
    ) -> Result<()> {
        if was_full {
            self.remove_full(idx)
        } else {
            self.remove_partial(idx)
        }
    }

    fn move_full_page_to_partial_list(&mut self, idx: &mut EfObjectPage) -> Result<()> {
        self.remove_full(idx)?;
        if let Err(err) = self.insert_partial(idx) {
            let _ = self.insert_full(idx);
            return Err(err);
        }
        Ok(())
    }

    fn move_partial_page_to_full_list(&mut self, idx: &mut EfObjectPage) -> Result<()> {
        self.remove_partial(idx)?;
        if let Err(err) = self.insert_full(idx) {
            let _ = self.insert_partial(idx);
            return Err(err);
        }
        Ok(())
    }

    fn rollback_new_page_allocation(
        &mut self,
        idx: &mut EfObjectPage,
        allocation_state: EfObjectPageAllocationState,
        new_page_addr: Option<usize>,
    ) -> Result<()> {
        let mut radix_remove_err = None;
        let radix_removed = if let Some(p) = new_page_addr {
            match try_with_rd_tree(|ptr_map| self.handle_rd_tree_remove(ptr_map, p))
                .map_err(|_| AllocError::ENOMEM)
                .and_then(|res| res)
            {
                Ok(()) => true,
                Err(err) => {
                    radix_remove_err = Some(err);
                    false
                }
            }
        } else {
            false
        };

        let recycle_res = if new_page_addr.is_some() && radix_removed {
            self.insert_uninit(idx).map(|_| ())
        } else {
            idx.restore_allocation_state(allocation_state);
            self.insert_ety(idx).map(|_| ())
        };
        recycle_res.err().or(radix_remove_err).map_or(Ok(()), Err)
    }

    fn rollback_new_page_radix_insert_failure(
        &mut self,
        idx: &mut EfObjectPage,
        new_page_addr: usize,
    ) -> Result<()> {
        let mut radix_remove_err = None;
        if let Err(err) =
            try_with_rd_tree(|ptr_map| self.handle_rd_tree_remove(ptr_map, new_page_addr))
                .map_err(|_| AllocError::ENOMEM)
                .and_then(|res| res)
        {
            radix_remove_err = Some(err);
        }

        let recycle_res = self.insert_uninit(idx).map(|_| ());
        recycle_res.err().or(radix_remove_err).map_or(Ok(()), Err)
    }

    #[inline]
    fn should_evict_retained_empty_slab_for_new_empty(&self) -> bool {
        let limit = self.empty_slab_retain_limit();
        limit > 0 && self.empty_count >= limit && !self.empty_start.is_null()
    }

    fn recycle_current_empty_page(&mut self, idx: &mut EfObjectPage, was_full: bool) -> Result<()> {
        let mut removed_radix_addr = None;
        if let Some(p) = self.try_insert_ety(idx)? {
            if let Err(err) = try_with_rd_tree(|ptr_map| self.handle_rd_tree_remove(ptr_map, p))
                .map_err(|_| AllocError::ENOMEM)
                .and_then(|res| res)
            {
                let _ = self.restore_page_to_allocation_list(idx, was_full);
                return Err(err);
            }
            removed_radix_addr = Some(p);
        }

        if let Err(err) = self.insert_ety(idx) {
            if let Some(p) = removed_radix_addr {
                // `insert_ety` can still fail after the page-to-object radix
                // mapping has been removed (for example a corrupt uninit
                // ring).  Recreate the mapping before restoring the page to
                // the full/partial list so callers can still resolve and free
                // the still-live objects on a retry.
                let _ = try_with_rd_tree(|ptr_map| {
                    Self::handle_rd_tree_insert(
                        self.pg_num as usize,
                        ptr_map,
                        idx as *const _ as usize,
                        p,
                    )
                });
            }
            let _ = self.restore_page_to_allocation_list(idx, was_full);
            return Err(err);
        }
        Ok(())
    }

    fn evict_retained_empty_slab_for_new_empty(
        &mut self,
        idx: &mut EfObjectPage,
        was_full: bool,
    ) -> Result<()> {
        let evicted = match self.remove_empty() {
            Ok(evicted) => evicted,
            Err(_) => return self.recycle_current_empty_page(idx, was_full),
        };
        let evicted_addr = match evicted.get_data_ptr().map(|ptr| ptr as usize) {
            Some(addr) => addr,
            None => {
                let _ = self.insert_ety(evicted);
                return self.recycle_current_empty_page(idx, was_full);
            }
        };

        if let Err(err) =
            try_with_rd_tree(|ptr_map| self.handle_rd_tree_remove(ptr_map, evicted_addr))
                .map_err(|_| AllocError::ENOMEM)
                .and_then(|res| res)
        {
            let _ = self.insert_ety(evicted);
            let _ = self.restore_page_to_allocation_list(idx, was_full);
            return Err(err);
        }

        if let Err(err) = self.insert_uninit(evicted) {
            let _ = try_with_rd_tree(|ptr_map| {
                Self::handle_rd_tree_insert(
                    self.pg_num as usize,
                    ptr_map,
                    evicted as *const _ as usize,
                    evicted_addr,
                )
            });
            let _ = self.insert_ety(evicted);
            let _ = self.restore_page_to_allocation_list(idx, was_full);
            return Err(err);
        }

        if let Err(err) = self.insert_ety(idx) {
            let _ = self.restore_page_to_allocation_list(idx, was_full);
            return Err(err);
        }
        self.empty_start = Self::page_ptr(idx);
        Ok(())
    }

    fn move_empty_page_to_recycle_list(
        &mut self,
        idx: &mut EfObjectPage,
        was_full: bool,
    ) -> Result<()> {
        self.remove_from_allocation_list(idx, was_full)?;
        if self.should_evict_retained_empty_slab_for_new_empty() {
            self.evict_retained_empty_slab_for_new_empty(idx, was_full)
        } else {
            self.recycle_current_empty_page(idx, was_full)
        }
    }

    pub fn get_uninit(&mut self) -> Result<(&mut EfObjectPage, usize)> {
        if self.pg_num == 0 || self.pg_count <= 0 {
            return Err(AllocError::ESIZE);
        }
        if !self.uninit_start.is_null() {
            let res = self.remove_uninit()?;
            return self.back_descriptor_on_page_alloc_failure(res);
        }

        let memory = PG_BUMP
            .lock()
            .alloc(core::mem::size_of::<EfObjectPage>())
            .map_err(|_| AllocError::ENOMEM)?;
        let res = unsafe {
            core::ptr::write(memory as *mut EfObjectPage, EfObjectPage::new());
            (memory as *mut EfObjectPage)
                .as_mut()
                .ok_or(AllocError::ENOMEM)?
        };
        self.back_descriptor_on_page_alloc_failure(res)
    }

    fn back_descriptor_on_page_alloc_failure<'page>(
        &mut self,
        res: &'page mut EfObjectPage,
    ) -> Result<(&'page mut EfObjectPage, usize)> {
        match res.allocate_page(self.pg_num as usize) {
            Ok(ptr) => Ok((res, ptr as usize)),
            Err(err) => {
                // `res` is allocator metadata, not user memory.  It may have
                // come from the uninitialized ring or from the metadata bump
                // allocator above.  If backing page allocation/layout
                // validation fails, return the descriptor to the uninitialized
                // ring so retries do not keep allocating unreachable
                // EfObjectPage metadata and worsening allocator footprint.
                Err(self.insert_uninit(res).err().unwrap_or(err))
            }
        }
    }

    pub fn get_empty(&mut self) -> Result<(*mut EfObjectPage, Option<usize>)> {
        if !self.empty_start.is_null() {
            let res = self.remove_empty()?;
            Ok((res, None))
        } else {
            let ans = self.get_uninit()?;
            Ok((ans.0, Some(ans.1)))
        }
    }

    pub fn insert_uninit(&mut self, idx: &mut EfObjectPage) -> Result<Option<*mut u8>> {
        Self::insert_ring(&mut self.uninit_start, idx)?;
        Ok(idx.destroy_page(self.pg_num as usize))
    }

    // This function is only used in deallocate, for more info, refer to that function
    pub fn try_insert_ety(&mut self, idx: &mut EfObjectPage) -> Result<Option<usize>> {
        if self.empty_count >= self.empty_slab_retain_limit() {
            Ok(idx.get_data_ptr().map(|ptr| ptr as usize))
        } else {
            Ok(None)
        }
    }

    pub fn insert_ety(&mut self, idx: &mut EfObjectPage) -> Result<Option<usize>> {
        if self.empty_count >= self.empty_slab_retain_limit() {
            Ok(self.insert_uninit(idx)?.map(|ptr| ptr as usize))
        } else {
            Self::insert_ring(&mut self.empty_start, idx)?;
            self.empty_count = self.empty_count.checked_add(1).ok_or(AllocError::EFATAL)?;
            Ok(None)
        }
    }

    fn restore_retained_empty_slab(&mut self, idx: &mut EfObjectPage) -> Result<()> {
        Self::insert_ring(&mut self.empty_start, idx)?;
        self.empty_count = self.empty_count.checked_add(1).ok_or(AllocError::EFATAL)?;
        Ok(())
    }

    pub(crate) fn retained_empty_slab_os_pages(&self) -> usize {
        self.empty_count.saturating_mul(self.pg_num)
    }

    #[cfg(test)]
    pub(crate) fn retained_empty_slab_count_for_tests(&self) -> usize {
        self.empty_count
    }

    pub(crate) fn recycle_one_retained_empty_slab(&mut self) -> Result<bool> {
        if self.empty_start.is_null() && self.empty_count == 0 {
            return Ok(false);
        }
        if self.empty_start.is_null() || self.empty_count == 0 {
            return Err(AllocError::EFATAL);
        }

        let evicted = self.remove_empty()?;
        let evicted_addr = match evicted.get_data_ptr().map(|ptr| ptr as usize) {
            Some(addr) => addr,
            None => {
                self.restore_retained_empty_slab(evicted)?;
                return Ok(false);
            }
        };

        if let Err(err) =
            try_with_rd_tree(|ptr_map| self.handle_rd_tree_remove(ptr_map, evicted_addr))
                .map_err(|_| AllocError::ENOMEM)
                .and_then(|res| res)
        {
            let _ = self.restore_retained_empty_slab(evicted);
            return Err(err);
        }

        if let Err(err) = self.insert_uninit(evicted) {
            let _ = try_with_rd_tree(|ptr_map| {
                Self::handle_rd_tree_insert(
                    self.pg_num as usize,
                    ptr_map,
                    evicted as *const _ as usize,
                    evicted_addr,
                )
            });
            let _ = self.restore_retained_empty_slab(evicted);
            return Err(err);
        }

        Ok(true)
    }

    #[inline]
    fn empty_slab_retain_limit(&self) -> usize {
        empty_slab_retain_limit_for_geometry(self.pg_num, self.pg_count.max(0) as usize)
    }

    fn handle_rd_tree_insert(
        pg_num: usize,
        ptr_map: &mut RadixTree,
        idx: usize,
        addr: usize,
    ) -> Result<()> {
        let ptr = align_12k(addr);
        let value = Self::checked_radix_value(idx)?;
        ptr_map
            .insert(Self::checked_radix_key(ptr)?, value, pg_num)
            .map_err(|_| AllocError::EFATAL)
    }

    fn checked_radix_key(page_addr: usize) -> Result<usize> {
        page_addr
            .checked_mul(1_usize << 16)
            .ok_or(AllocError::EFATAL)
    }

    fn checked_radix_value(idx: usize) -> Result<i64> {
        if idx > i64::MAX as usize {
            return Err(AllocError::EFATAL);
        }
        Ok(idx as i64)
    }

    // We use the same strategy from tcmalloc:
    // We first try to alloc from partial, then create empty page
    pub fn allocate_batch_v2(&mut self, align: usize) -> Result<(*mut u8, usize, Option<usize>)> {
        if align == 0 || !align.is_power_of_two() || align > PAGE_SIZE {
            return Err(AllocError::ESIZE);
        }
        let pg_count = self.pg_count;
        let pg_align = self.pg_align;
        if pg_count <= 0 || pg_align <= 0 || self.pg_num == 0 {
            return Err(AllocError::ESIZE);
        }
        let pg_count = pg_count as usize;
        let pg_align = pg_align as usize;

        if !self.partial_start.is_null() {
            let start = self.partial_start;
            let mut cur = start;
            loop {
                let idx = Self::try_get_ref(cur)?;
                let next = idx.try_get_next().map(Self::page_ptr)?;
                let allocation_state = idx.allocation_state_snapshot();
                match idx.allocate_aligned_batch(align, self.pg_num as usize, pg_count, pg_align) {
                    Ok(ans) => {
                        if idx.is_full(pg_count) {
                            if let Err(err) = self.move_partial_page_to_full_list(idx) {
                                idx.restore_allocation_state(allocation_state);
                                return Err(err);
                            }
                        } else {
                            self.partial_start = Self::page_ptr(idx);
                        }
                        return Ok(ans);
                    }
                    Err(err) if err.to_raw_errno() == AllocError::ENOMEM.to_raw_errno() => {
                        cur = next;
                        if cur == start {
                            break;
                        }
                    }
                    Err(err) => return Err(err),
                }
            }
        }

        let idx = self.get_empty()?;
        let obj = Self::try_get_ref(idx.0)?;
        let new_page = idx.1;
        let allocation_state = obj.allocation_state_snapshot();
        let ans = match obj.allocate_aligned_batch(align, self.pg_num as usize, pg_count, pg_align)
        {
            Ok(ans) => ans,
            Err(err) => {
                let restore_err = if new_page.is_some() {
                    self.insert_uninit(obj).err()
                } else {
                    self.insert_ety(obj).err()
                };
                return Err(restore_err.unwrap_or(err));
            }
        };

        if let Some(addr) = new_page {
            if let Err(err) = try_with_rd_tree(|ptr_map| {
                Self::handle_rd_tree_insert(
                    self.pg_num as usize,
                    ptr_map,
                    obj as *const _ as usize,
                    addr,
                )
            })
            .map_err(|_| AllocError::ENOMEM)
            .and_then(|res| res)
            {
                let rollback_result = self.rollback_new_page_radix_insert_failure(obj, addr);
                return Err(rollback_result.err().unwrap_or(err));
            }
        }

        let insert_result = if obj.is_full(pg_count) {
            self.insert_full(obj)
        } else {
            self.insert_partial(obj)
        };
        if let Err(err) = insert_result {
            let rollback_result =
                self.rollback_new_page_allocation(obj, allocation_state, new_page);
            return Err(rollback_result.err().unwrap_or(err));
        }
        Ok(ans)
    }

    fn handle_rd_tree_remove(&self, ptr_map: &mut RadixTree, addr: usize) -> Result<()> {
        let ptr = align_12k(addr);
        ptr_map
            .remove(Self::checked_radix_key(ptr)?, self.pg_num as usize)
            .map_err(|_| AllocError::EFATAL)
    }

    fn lookup_object_page(
        page_vaddr: usize,
        cached_page_vaddr: &mut usize,
        cached_idx: &mut i64,
    ) -> Result<&'static mut EfObjectPage> {
        let idx = if *cached_idx != 0 && *cached_page_vaddr == page_vaddr {
            *cached_idx
        } else {
            let key = Self::checked_radix_key(page_vaddr)?;
            let idx = try_with_rd_tree(|ptr_map| ptr_map.get_mut(key))
                .map_err(|_| AllocError::ENOMEM)?
                & ((1i64 << 48) - 1);
            if idx == 0 {
                *cached_page_vaddr = 0;
                *cached_idx = 0;
                return Err(AllocError::EUAF);
            }
            *cached_page_vaddr = page_vaddr;
            *cached_idx = idx;
            idx
        };

        Self::object_page_from_cached_idx(idx)
    }

    #[inline]
    fn object_page_from_cached_idx(idx: i64) -> Result<&'static mut EfObjectPage> {
        unsafe {
            (idx as *const EfObjectPage as *mut EfObjectPage)
                .as_mut()
                .ok_or(AllocError::EFATAL)
        }
    }

    #[inline]
    fn deallocate_batch_object_page_cache_set(page_vaddr: usize) -> usize {
        debug_assert!(DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_SETS.is_power_of_two());
        (page_vaddr >> PAGE_SIZE.trailing_zeros()) & (DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_SETS - 1)
    }

    #[inline]
    fn deallocate_batch_object_page_cache_set_range(page_vaddr: usize) -> core::ops::Range<usize> {
        let set = Self::deallocate_batch_object_page_cache_set(page_vaddr);
        let start = set * DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_WAYS;
        start..start + DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_WAYS
    }

    #[inline]
    fn deallocate_batch_object_page_cache_replacement_way(page_vaddr: usize) -> usize {
        debug_assert!(DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_WAYS.is_power_of_two());
        (page_vaddr
            >> (PAGE_SIZE.trailing_zeros()
                + DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_SETS.trailing_zeros()))
            & (DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_WAYS - 1)
    }

    #[inline]
    fn fill_deallocate_batch_object_page_cache(
        page_vaddr: usize,
        idx: i64,
        cached_pages: &mut [usize; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES],
        cached_indices: &mut [i64; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES],
    ) {
        let set_range = Self::deallocate_batch_object_page_cache_set_range(page_vaddr);
        let slot = set_range
            .clone()
            .find(|&slot| cached_indices[slot] == DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_EMPTY)
            .unwrap_or_else(|| {
                set_range.start
                    + Self::deallocate_batch_object_page_cache_replacement_way(page_vaddr)
            });
        cached_pages[slot] = page_vaddr;
        cached_indices[slot] = idx;
    }

    fn lookup_object_page_batch_cached_idx(
        page_vaddr: usize,
        cached_pages: &mut [usize; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES],
        cached_indices: &mut [i64; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES],
    ) -> Result<i64> {
        for slot in Self::deallocate_batch_object_page_cache_set_range(page_vaddr) {
            if cached_indices[slot] != DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_EMPTY
                && cached_pages[slot] == page_vaddr
            {
                return Ok(cached_indices[slot]);
            }
        }

        let key = Self::checked_radix_key(page_vaddr)?;
        let idx = try_with_rd_tree(|ptr_map| ptr_map.get_mut(key))
            .map_err(|_| AllocError::ENOMEM)?
            & ((1i64 << 48) - 1);
        if idx == 0 {
            Self::invalidate_deallocate_batch_object_page_cache(
                page_vaddr,
                cached_pages,
                cached_indices,
            );
            return Err(AllocError::EUAF);
        }
        Self::fill_deallocate_batch_object_page_cache(
            page_vaddr,
            idx,
            cached_pages,
            cached_indices,
        );
        Ok(idx)
    }

    fn lookup_object_page_batch_cached(
        page_vaddr: usize,
        cached_pages: &mut [usize; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES],
        cached_indices: &mut [i64; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES],
    ) -> Result<&'static mut EfObjectPage> {
        let idx =
            Self::lookup_object_page_batch_cached_idx(page_vaddr, cached_pages, cached_indices)?;
        Self::object_page_from_cached_idx(idx)
    }

    #[inline]
    fn invalidate_deallocate_batch_object_page_cache(
        page_vaddr: usize,
        cached_pages: &mut [usize; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES],
        cached_indices: &mut [i64; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES],
    ) {
        for slot in Self::deallocate_batch_object_page_cache_set_range(page_vaddr) {
            if cached_pages[slot] == page_vaddr {
                cached_indices[slot] = DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_EMPTY;
            }
        }
    }

    /// Deallocate a thread-cache batch while preserving pointer order.
    ///
    /// `EfObjectPage::deallocate` consumes each contiguous same-page prefix.
    /// Thread-cache lists can still interleave pages (`A, B, A, ...`), so this
    /// cold handoff path keeps a small stack-local page cache to avoid repeated
    /// radix lookups when a page reappears later in the same batch.  The 4-set
    /// by 2-way shape is intentionally stack-local: it improves locality for
    /// common interleaved frees where two active pages map to the same set,
    /// without adding persistent allocator metadata or retained memory.  The cache
    /// is invalidated as soon as a page becomes empty and its radix mapping is
    /// removed/recycled, so later stale pointers cannot deallocate through a
    /// cached descriptor.
    pub fn deallocate_batch(&mut self, ptr: *mut usize) -> Result<()> {
        if self.pg_num == 0 || self.pg_count <= 0 || self.pg_align <= 0 {
            return Err(AllocError::ESIZE);
        }

        let mut head = ptr;
        let mut cached_pages = [0usize; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES];
        let mut cached_indices =
            [DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_EMPTY; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES];
        while !head.is_null() {
            let page_vaddr = align_12k(head as usize);
            let obj_pge = Self::lookup_object_page_batch_cached(
                page_vaddr,
                &mut cached_pages,
                &mut cached_indices,
            )?;
            // assert_ne!(obj_pge.is_empty());
            let mut back_partial = false;
            if obj_pge.is_full(self.pg_count as usize) {
                back_partial = true;
            }
            let allocation_state = obj_pge.allocation_state_snapshot();
            let deallocation = obj_pge.deallocate(
                head as usize,
                self.pg_num as usize,
                self.pg_count as usize,
                self.pg_align as usize,
            )?;
            head = deallocation.next_batch_head();
            let page_is_empty = obj_pge.is_empty();

            let transition_result = if page_is_empty {
                self.move_empty_page_to_recycle_list(obj_pge, back_partial)
            } else if back_partial {
                self.move_full_page_to_partial_list(obj_pge)
            } else {
                Ok(())
            };
            if let Err(err) = transition_result {
                obj_pge.restore_allocation_state(allocation_state);
                deallocation.restore_caller_tail_link();
                return Err(err);
            }
            if page_is_empty {
                Self::invalidate_deallocate_batch_object_page_cache(
                    page_vaddr,
                    &mut cached_pages,
                    &mut cached_indices,
                );
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    extern crate std;

    use super::*;

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
        let err = match alloc.get_empty() {
            Ok(_) => panic!("zero-page size class must fail closed"),
            Err(err) => err,
        };
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
    }

    #[test]
    fn deallocate_batch_rejects_unmapped_pointer_without_panic() {
        let mut alloc = SCAllocator::new(64, 1);
        let err = alloc
            .deallocate_batch(0x1000usize as *mut usize)
            .expect_err("unmapped pointer must fail closed");
        assert_eq!(err.to_raw_errno(), AllocError::EUAF.to_raw_errno());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_deallocate_roundtrip_reuses_real_page_without_mocking() {
        let mut alloc = SCAllocator::new(64, 1);
        let expected_count = PAGE_SIZE / 64;

        let (ptr, count, stride) = alloc
            .allocate_batch_v2(1)
            .expect("real page-backed allocation should succeed");
        let stride = stride.expect("new page allocation should report object stride");

        assert!(!ptr.is_null());
        assert_eq!(count, expected_count);
        assert_eq!(stride, 64);
        assert!(!alloc.full_start.is_null());

        unsafe { link_allocation_batch(ptr, count, stride) };
        alloc
            .deallocate_batch(ptr as *mut usize)
            .expect("real batch deallocation should succeed");

        let first_empty_page = alloc.empty_start;
        assert!(alloc.full_start.is_null());
        assert!(alloc.partial_start.is_null());
        assert!(!first_empty_page.is_null());
        assert_eq!(alloc.empty_count, 1);

        let (reused_ptr, reused_count, reused_stride) = alloc
            .allocate_batch_v2(1)
            .expect("empty page should be reusable");

        assert_eq!(reused_ptr, ptr);
        assert_eq!(reused_count, expected_count);
        assert_eq!(reused_stride, Some(64));
        assert!(alloc.empty_start.is_null());
        assert_eq!(alloc.empty_count, 0);
        assert_eq!(alloc.full_start, first_empty_page);

        unsafe { link_allocation_batch(reused_ptr, reused_count, 64) };
        alloc
            .deallocate_batch(reused_ptr as *mut usize)
            .expect("reused page should deallocate cleanly");
        assert_eq!(alloc.empty_count, 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn over_8k_size_class_allocates_real_single_slot_minimum_span() {
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
        assert_eq!(rounded, 9344);

        let pages = crate::size_class::get_num_pages_by_idx(idx);
        let expected_pages = ((rounded - 1) / PAGE_SIZE) + 1;
        assert_eq!(
            pages, expected_pages,
            "active size-class policy should reserve only the minimum whole-page span"
        );

        let mut alloc = SCAllocator::new(rounded, pages);
        assert_eq!(alloc.pg_num, pages);
        assert_eq!(
            alloc.pg_count, 1,
            ">8KiB classes should expose one real object slot per span"
        );

        let (ptr, count, stride) = alloc
            .allocate_batch_v2(1)
            .expect("real page-backed >8KiB allocation should succeed");
        let stride = stride.expect("fresh one-slot page allocation should report object stride");

        assert!(!ptr.is_null());
        assert_eq!(count, 1);
        assert!(
            stride >= rounded,
            "object stride {} must fit rounded class {}",
            stride,
            rounded
        );
        assert!(!alloc.full_start.is_null());

        unsafe { link_allocation_batch(ptr, count, stride) };
        alloc
            .deallocate_batch(ptr as *mut usize)
            .expect("real one-slot page should deallocate cleanly");

        assert!(alloc.full_start.is_null());
        assert!(alloc.partial_start.is_null());
        assert_eq!(alloc.empty_count, 0);
        assert!(alloc.empty_start.is_null());
        assert!(!alloc.uninit_start.is_null());
        assert!(
            unsafe { alloc.uninit_start.as_ref().expect("recycled one-slot page") }
                .get_data_ptr()
                .is_none(),
            "single-slot empty span should release backing immediately"
        );

        let (reallocated, reallocated_count, reallocated_stride) = alloc
            .allocate_batch_v2(1)
            .expect("recycled one-slot descriptor should remain reusable");
        assert!(!reallocated.is_null());
        assert_eq!(reallocated_count, 1);
        assert!(reallocated_stride.expect("fresh one-slot page should report stride") >= rounded);
        assert!(alloc.empty_start.is_null());
        assert_eq!(alloc.empty_count, 0);

        unsafe { link_allocation_batch(reallocated, reallocated_count, stride) };
        alloc
            .deallocate_batch(reallocated as *mut usize)
            .expect("cleanup of reallocated one-slot page should succeed");
        assert_eq!(alloc.empty_count, 0);
        assert!(alloc.empty_start.is_null());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_batch_v2_reuses_aligned_partial_page_before_allocating_new_page() {
        let mut alloc = SCAllocator::new(8, 1);
        let expected_count = PAGE_SIZE / 8;

        let (ptr, count, stride) = alloc
            .allocate_batch_v2(1)
            .expect("real page-backed allocation should succeed");
        let stride = stride.expect("new page allocation should report object stride");
        assert_eq!(count, expected_count);
        assert_eq!(stride, 8);
        assert_eq!((ptr as usize) & 15, 0);

        let aligned_free = unsafe { ptr.add(16) };
        unsafe {
            *(aligned_free as *mut usize) = 0;
        }
        alloc
            .deallocate_batch(aligned_free as *mut usize)
            .expect("single-object deallocation should make the page partial");
        assert!(alloc.full_start.is_null());
        assert!(!alloc.partial_start.is_null());

        let (reused, reused_count, reused_stride) = alloc
            .allocate_batch_v2(16)
            .expect("aligned allocation should reuse the matching partial free slot");

        assert_eq!(reused, aligned_free);
        assert_eq!(reused_count, 1);
        assert_eq!(reused_stride, None);
        assert!(
            alloc.partial_start.is_null(),
            "the only free slot was consumed, so the page should move back to full"
        );

        unsafe { link_allocation_batch(ptr, count, stride) };
        alloc
            .deallocate_batch(ptr as *mut usize)
            .expect("cleanup of the full real page should succeed");
        assert_eq!(alloc.empty_count, 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_batch_v2_rotates_partial_start_to_non_full_strict_alignment_match() {
        let mut alloc = SCAllocator::new(8, 1);
        let expected_count = PAGE_SIZE / 8;
        let strict_cap = crate::page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT;
        assert!(
            expected_count / 2 > strict_cap,
            "test needs more 16-byte-aligned slots than the strict-alignment cap"
        );

        let (page_a, count_a, stride_a) = alloc
            .allocate_batch_v2(1)
            .expect("first real page-backed allocation should succeed");
        let stride_a = stride_a.expect("new page A should report object stride");
        let (page_b, count_b, stride_b) = alloc
            .allocate_batch_v2(1)
            .expect("second real page-backed allocation should succeed");
        let stride_b = stride_b.expect("new page B should report object stride");
        assert_eq!(count_a, expected_count);
        assert_eq!(count_b, expected_count);
        assert_eq!(stride_a, 8);
        assert_eq!(stride_b, 8);
        assert_eq!((page_a as usize) & 15, 0);
        assert_eq!((page_b as usize) & 15, 0);

        let page_a_vaddr = align_12k(page_a as usize);
        let page_b_vaddr = align_12k(page_b as usize);
        let mut cached_page_vaddr = 0usize;
        let mut cached_idx = 0i64;
        let page_a_idx =
            SCAllocator::lookup_object_page(page_a_vaddr, &mut cached_page_vaddr, &mut cached_idx)
                .expect("page A should remain indexed");
        let page_b_idx =
            SCAllocator::lookup_object_page(page_b_vaddr, &mut cached_page_vaddr, &mut cached_idx)
                .expect("page B should remain indexed");

        let unaligned_a_free = unsafe { page_a.add(8) };
        assert_ne!((unaligned_a_free as usize) & 15, 0);
        unsafe {
            *(unaligned_a_free as *mut usize) = 0;
        }
        alloc
            .deallocate_batch(unaligned_a_free as *mut usize)
            .expect("freeing one unaligned object from A should make A partial");
        assert_eq!(alloc.partial_start, SCAllocator::page_ptr(page_a_idx));

        let b_aligned_free_count = strict_cap + 1;
        for idx in 0..b_aligned_free_count {
            let cur = unsafe { page_b.add(idx * 16) };
            let next = if idx + 1 == b_aligned_free_count {
                0
            } else {
                unsafe { page_b.add((idx + 1) * 16) as usize }
            };
            unsafe {
                *(cur as *mut usize) = next;
            }
        }
        alloc
            .deallocate_batch(page_b as *mut usize)
            .expect("freeing many aligned objects from B should make B partial");
        assert_eq!(
            alloc.partial_start,
            SCAllocator::page_ptr(page_a_idx),
            "A should remain the partial-ring head before the strict-alignment scan"
        );
        assert!(!page_b_idx.is_full(expected_count));

        let (first_strict, first_strict_count, first_strict_stride) = alloc
            .allocate_batch_v2(16)
            .expect("strict-alignment scan should skip A and allocate capped batch from B");
        assert_eq!(first_strict, page_b);
        assert_eq!(first_strict_count, strict_cap);
        assert_eq!(first_strict_stride, None);
        assert_eq!(
            alloc.partial_start,
            SCAllocator::page_ptr(page_b_idx),
            "non-full strict-alignment match should become the next partial scan head"
        );
        assert!(
            !page_b_idx.is_full(expected_count),
            "B should remain partial because the strict cap left one aligned slot free"
        );

        let (second_strict, second_strict_count, second_strict_stride) = alloc
            .allocate_batch_v2(16)
            .expect("rotated head should let the next strict request consume B's remaining match");
        assert_eq!(second_strict, unsafe { page_b.add(strict_cap * 16) });
        assert_eq!(second_strict_count, 1);
        assert_eq!(second_strict_stride, None);
        assert!(page_b_idx.is_full(expected_count));
        assert_eq!(
            alloc.partial_start,
            SCAllocator::page_ptr(page_a_idx),
            "after B moves full, A should be the remaining partial page"
        );

        let (reused_a, reused_a_count, reused_a_stride) = alloc
            .allocate_batch_v2(1)
            .expect("A's unaligned free slot should remain reusable after B rotation");
        assert_eq!(reused_a, unaligned_a_free);
        assert_eq!(reused_a_count, 1);
        assert_eq!(reused_a_stride, None);
        assert!(page_a_idx.is_full(expected_count));
        assert!(alloc.partial_start.is_null());

        unsafe { link_allocation_batch(page_a, count_a, stride_a) };
        alloc
            .deallocate_batch(page_a as *mut usize)
            .expect("cleanup of full page A should succeed");
        unsafe { link_allocation_batch(page_b, count_b, stride_b) };
        alloc
            .deallocate_batch(page_b as *mut usize)
            .expect("cleanup of full page B should succeed");
        assert_eq!(alloc.empty_count, 2);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_batch_v2_restores_partial_page_when_full_insert_fails() {
        let mut alloc = SCAllocator::new(8, 1);
        let expected_count = PAGE_SIZE / 8;

        let (ptr, count, stride) = alloc
            .allocate_batch_v2(1)
            .expect("real page-backed allocation should succeed");
        let stride = stride.expect("new page allocation should report object stride");
        assert_eq!(count, expected_count);

        let aligned_free = unsafe { ptr.add(16) };
        unsafe {
            *(aligned_free as *mut usize) = 0;
        }
        alloc
            .deallocate_batch(aligned_free as *mut usize)
            .expect("single-object deallocation should make the page partial");
        assert!(alloc.full_start.is_null());
        assert!(!alloc.partial_start.is_null());

        let page_vaddr = align_12k(ptr as usize);
        let mut cached_page_vaddr = 0usize;
        let mut cached_idx = 0i64;
        let obj_pge =
            SCAllocator::lookup_object_page(page_vaddr, &mut cached_page_vaddr, &mut cached_idx)
                .expect("allocated page should remain indexed");
        assert!(!obj_pge.is_full(count));

        let mut corrupt_full_head = EfObjectPage::new();
        alloc.full_start = &mut corrupt_full_head;
        let err = alloc
            .allocate_batch_v2(16)
            .expect_err("corrupt full list must fail after consuming the partial free slot");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(alloc.partial_start, SCAllocator::page_ptr(obj_pge));
        assert!(
            !obj_pge.is_full(count),
            "failed partial-to-full move must restore the consumed free slot"
        );

        alloc.full_start = null_mut();
        let (reused, reused_count, reused_stride) = alloc
            .allocate_batch_v2(16)
            .expect("rolled-back aligned free slot should be reusable");
        assert_eq!(reused, aligned_free);
        assert_eq!(reused_count, 1);
        assert_eq!(reused_stride, None);
        assert_eq!(alloc.full_start, SCAllocator::page_ptr(obj_pge));

        unsafe { link_allocation_batch(ptr, count, stride) };
        alloc
            .deallocate_batch(ptr as *mut usize)
            .expect("cleanup of the full real page should succeed");
        assert_eq!(alloc.empty_count, 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocate_batch_v2_rolls_back_new_page_when_full_insert_fails() {
        let mut alloc = SCAllocator::new(64, 1);

        let mut corrupt_full_head = EfObjectPage::new();
        alloc.full_start = &mut corrupt_full_head;
        let err = alloc
            .allocate_batch_v2(1)
            .expect_err("corrupt full list must fail after new-page allocation");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert!(!alloc.uninit_start.is_null());
        let recycled = SCAllocator::try_get_ref(alloc.uninit_start)
            .expect("failed insertion should recycle the page as uninit");
        assert!(
            !recycled.is_inited(),
            "failed new-page insertion must destroy the page before uninit recycling"
        );

        alloc.full_start = null_mut();
        let (ptr, count, stride) = alloc
            .allocate_batch_v2(1)
            .expect("rolled-back uninit page should be reusable");
        let stride = stride.expect("new page allocation should report object stride");
        assert_eq!(count, PAGE_SIZE / 64);
        unsafe { link_allocation_batch(ptr, count, stride) };
        alloc
            .deallocate_batch(ptr as *mut usize)
            .expect("cleanup after new-page rollback should succeed");
        assert_eq!(alloc.empty_count, 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn get_uninit_rolls_back_descriptor_when_page_backing_fails() {
        let mut alloc = SCAllocator::new(64, 1);
        alloc.pg_num = usize::MAX;

        let first_err = match alloc.get_uninit() {
            Ok(_) => panic!("overflowing page span must fail before backing allocation"),
            Err(err) => err,
        };
        assert_eq!(first_err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert!(
            !alloc.uninit_start.is_null(),
            "failed backing-page setup must keep the EfObjectPage descriptor reachable"
        );
        let first_descriptor = alloc.uninit_start;
        let recycled =
            SCAllocator::try_get_ref(first_descriptor).expect("rolled-back descriptor is valid");
        assert!(
            !recycled.is_inited(),
            "failed page allocation must not leave backing memory attached"
        );

        let second_err = match alloc.get_uninit() {
            Ok(_) => panic!("retry should reuse the rolled-back descriptor and fail the same way"),
            Err(err) => err,
        };
        assert_eq!(second_err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(
            alloc.uninit_start, first_descriptor,
            "retry failure must not allocate another unreachable EfObjectPage descriptor"
        );
        let recycled =
            SCAllocator::try_get_ref(first_descriptor).expect("descriptor remains reusable");
        assert!(
            !recycled.is_inited(),
            "retry failure must still leave the descriptor unbacked"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn radix_insert_failure_rollback_removes_mapping_and_destroys_new_page() {
        const CHILD_ENV: &str = "UNIALLOC_RADIX_INSERT_ROLLBACK_CHILD";
        const TEST_NAME: &str =
            "sc::efficient_sc::tests::radix_insert_failure_rollback_removes_mapping_and_destroys_new_page";

        // Rollback unmaps the page before the final global-radix assertion.
        // A parallel slab test can legally reuse that virtual address and
        // install its own mapping in the gap, so give this address lifecycle
        // exclusive process ownership.
        if crate::test_support::run_test_in_fresh_process(CHILD_ENV, TEST_NAME) {
            return;
        }

        let mut alloc = SCAllocator::new(64, 1);
        let pg_num = alloc.pg_num;
        let pg_count = alloc.pg_count as usize;
        let pg_align = alloc.pg_align as usize;
        let (obj_ptr, addr) = alloc.get_empty().expect("new object page");
        let addr = addr.expect("fresh uninit page allocation must report a page address");
        let key = SCAllocator::checked_radix_key(align_12k(addr)).expect("radix key");

        let obj = SCAllocator::try_get_ref(obj_ptr).expect("object page");
        obj.allocate_aligned_batch(1, pg_num, pg_count, pg_align)
            .expect("page-local allocation should succeed before rollback");
        try_with_rd_tree(|ptr_map| {
            SCAllocator::handle_rd_tree_insert(pg_num, ptr_map, obj_ptr as usize, addr)
        })
        .expect("radix tree access")
        .expect("test mapping install");
        let pointer_mask = (1i64 << 48) - 1;
        assert_ne!(
            try_with_rd_tree(|ptr_map| ptr_map.get_mut(key)).expect("radix tree lookup")
                & pointer_mask,
            0,
            "test must start with an installed page mapping"
        );

        alloc
            .rollback_new_page_radix_insert_failure(obj, addr)
            .expect("rollback should remove mapping and recycle metadata");

        assert_eq!(
            try_with_rd_tree(|ptr_map| ptr_map.get_mut(key)).expect("radix tree lookup")
                & pointer_mask,
            0,
            "radix-insert rollback must not leave a live object-page pointer mapping"
        );
        assert_eq!(alloc.uninit_start, obj_ptr);
        let recycled = SCAllocator::try_get_ref(alloc.uninit_start).expect("recycled page");
        assert!(
            !recycled.is_inited(),
            "radix-insert rollback must destroy the failed new page before uninit recycling"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn deallocate_batch_rejects_real_duplicate_object_without_recycling_live_page() {
        let mut alloc = SCAllocator::new(64, 1);
        let expected_count = PAGE_SIZE / 64;

        let (ptr, count, stride) = alloc
            .allocate_batch_v2(1)
            .expect("real page-backed allocation should succeed");
        let stride = stride.expect("new page allocation should report object stride");
        assert_eq!(count, expected_count);
        assert!(!alloc.full_start.is_null());

        unsafe {
            *(ptr as *mut usize) = 0;
        }
        alloc
            .deallocate_batch(ptr as *mut usize)
            .expect("first single-object deallocation should make the page partial");

        assert!(alloc.full_start.is_null());
        assert!(!alloc.partial_start.is_null());
        assert_eq!(alloc.empty_count, 0);

        let err = alloc
            .deallocate_batch(ptr as *mut usize)
            .expect_err("second free of the same real slab object must fail closed");
        assert_eq!(err.to_raw_errno(), AllocError::EDBFRE.to_raw_errno());
        assert!(
            alloc.empty_start.is_null(),
            "double-free must not make a page with live objects look empty"
        );
        assert!(!alloc.partial_start.is_null());
        assert_eq!(alloc.empty_count, 0);

        let (reused_ptr, reused_count, reused_stride) = alloc
            .allocate_batch_v2(1)
            .expect("the single legitimately free slot should still be reusable once");
        assert_eq!(reused_ptr, ptr);
        assert_eq!(reused_count, 1);
        assert_eq!(reused_stride, None);
        assert!(!alloc.full_start.is_null());

        unsafe { link_allocation_batch(ptr, count, stride) };
        alloc
            .deallocate_batch(ptr as *mut usize)
            .expect("cleanup of the full real page should succeed");
        assert_eq!(alloc.empty_count, 1);
    }

    #[test]
    fn deallocate_batch_object_page_cache_invalidates_all_matching_ways() {
        let page_vaddr = PAGE_SIZE * 17;
        let mut cached_pages = [0usize; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES];
        let mut cached_indices =
            [DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_EMPTY; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES];
        let set_range = SCAllocator::deallocate_batch_object_page_cache_set_range(page_vaddr);
        for slot in set_range.clone() {
            cached_pages[slot] = page_vaddr;
            cached_indices[slot] = 123 + slot as i64;
        }

        SCAllocator::invalidate_deallocate_batch_object_page_cache(
            page_vaddr,
            &mut cached_pages,
            &mut cached_indices,
        );

        for slot in set_range {
            assert_eq!(cached_pages[slot], page_vaddr);
            assert_eq!(
                cached_indices[slot],
                DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_EMPTY
            );
        }
    }

    #[test]
    fn deallocate_batch_object_page_cache_retains_two_colliding_pages() {
        let first_page_vaddr = PAGE_SIZE * 17;
        let second_page_vaddr =
            first_page_vaddr + PAGE_SIZE * DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_SETS;
        assert_eq!(
            SCAllocator::deallocate_batch_object_page_cache_set(first_page_vaddr),
            SCAllocator::deallocate_batch_object_page_cache_set(second_page_vaddr)
        );

        let mut cached_pages = [0usize; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES];
        let mut cached_indices =
            [DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_EMPTY; DEALLOCATE_BATCH_OBJECT_PAGE_CACHE_ENTRIES];
        let first_idx = 0x101i64;
        let second_idx = 0x202i64;

        SCAllocator::fill_deallocate_batch_object_page_cache(
            first_page_vaddr,
            first_idx,
            &mut cached_pages,
            &mut cached_indices,
        );
        SCAllocator::fill_deallocate_batch_object_page_cache(
            second_page_vaddr,
            second_idx,
            &mut cached_pages,
            &mut cached_indices,
        );

        let set_range = SCAllocator::deallocate_batch_object_page_cache_set_range(first_page_vaddr);
        let first_slot = set_range
            .clone()
            .find(|&slot| {
                cached_pages[slot] == first_page_vaddr && cached_indices[slot] == first_idx
            })
            .expect("first colliding page should remain cached");
        let second_slot = set_range
            .clone()
            .find(|&slot| {
                cached_pages[slot] == second_page_vaddr && cached_indices[slot] == second_idx
            })
            .expect("second colliding page should remain cached");
        assert_ne!(
            first_slot, second_slot,
            "two pages in the same set must occupy distinct ways"
        );
        assert_eq!(
            SCAllocator::lookup_object_page_batch_cached_idx(
                first_page_vaddr,
                &mut cached_pages,
                &mut cached_indices,
            )
            .expect("first colliding page should be a cache hit"),
            first_idx
        );
        assert_eq!(
            SCAllocator::lookup_object_page_batch_cached_idx(
                second_page_vaddr,
                &mut cached_pages,
                &mut cached_indices,
            )
            .expect("second colliding page should be a cache hit"),
            second_idx
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn deallocate_batch_page_cache_invalidates_recycled_page_mapping() {
        const CHILD_ENV: &str = "UNIALLOC_RECYCLED_PAGE_MAPPING_CHILD";
        const TEST_NAME: &str =
            "sc::efficient_sc::tests::deallocate_batch_page_cache_invalidates_recycled_page_mapping";

        if std::env::var_os(CHILD_ENV).is_none() {
            let output = std::process::Command::new(
                std::env::current_exe().expect("current allocator test executable"),
            )
            .args(["--exact", TEST_NAME, "--nocapture", "--test-threads=1"])
            .env(CHILD_ENV, "1")
            .env("RUST_BACKTRACE", "0")
            .output()
            .expect("spawn isolated recycled-page mapping test");
            assert!(
                output.status.success(),
                "isolated recycled-page mapping test failed: {}",
                std::string::String::from_utf8_lossy(&output.stderr)
            );
            return;
        }

        let mut alloc = SCAllocator::new(1024, 1);
        let (first_ptr, first_count, first_stride) = alloc
            .allocate_batch_v2(1)
            .expect("first real full-page allocation should succeed");
        let first_stride = first_stride.expect("new page allocation should report object stride");
        let (second_ptr, second_count, second_stride) = alloc
            .allocate_batch_v2(1)
            .expect("second real full-page allocation should succeed");
        let second_stride = second_stride.expect("new page allocation should report object stride");
        assert!(first_count > 1);
        assert!(second_count > 1);
        assert_ne!(
            align_12k(first_ptr as usize),
            align_12k(second_ptr as usize)
        );

        unsafe {
            link_allocation_batch(first_ptr, first_count, first_stride);
            let first_tail = (first_ptr as usize)
                .checked_add((first_count - 1) * first_stride)
                .expect("first page tail");
            *(first_tail as *mut usize) = second_ptr as usize;
            *(second_ptr as *mut usize) = first_ptr as usize;
        }

        alloc.empty_count = EMPTY_SLAB_RETAIN_LIMIT;
        let err = alloc
            .deallocate_batch(first_ptr as *mut usize)
            .expect_err("stale pointer after page recycle must be reported as unmapped");

        assert_eq!(err.to_raw_errno(), AllocError::EUAF.to_raw_errno());
        assert!(!alloc.uninit_start.is_null());
        let mut cached_page_vaddr = 0usize;
        let mut cached_idx = 0i64;
        assert!(
            SCAllocator::lookup_object_page(
                align_12k(first_ptr as usize),
                &mut cached_page_vaddr,
                &mut cached_idx,
            )
            .is_err(),
            "recycled page must not remain reachable through the batch-local cache or radix map"
        );

        let second_remainder = unsafe {
            let ptr = second_ptr.add(second_stride);
            link_allocation_batch(ptr, second_count - 1, second_stride);
            ptr
        };
        alloc.empty_count = 0;
        alloc
            .deallocate_batch(second_remainder as *mut usize)
            .expect("cleanup should free the rest of the second real page");
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn deallocate_batch_rolls_back_page_state_when_full_to_partial_move_fails() {
        let mut alloc = SCAllocator::new(64, 1);

        let (ptr, count, stride) = alloc
            .allocate_batch_v2(8)
            .expect("real page-backed allocation should succeed");
        let stride = stride.expect("new page allocation should report object stride");
        let page_vaddr = align_12k(ptr as usize);
        let mut cached_page_vaddr = 0usize;
        let mut cached_idx = 0i64;
        let obj_pge =
            SCAllocator::lookup_object_page(page_vaddr, &mut cached_page_vaddr, &mut cached_idx)
                .expect("allocated page should be indexed");
        assert!(obj_pge.is_full(count));

        obj_pge.set_next(0);
        let err = alloc
            .deallocate_batch(ptr as *mut usize)
            .expect_err("corrupt full-list link must fail the full-to-partial move");
        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert!(
            obj_pge.is_full(count),
            "failed list transition must not leave the page partially freed"
        );

        let obj_ptr = SCAllocator::page_ptr(obj_pge) as usize;
        obj_pge.set_next(obj_ptr);
        obj_pge.set_prev(obj_ptr);
        unsafe { link_allocation_batch(ptr, count, stride) };
        alloc
            .deallocate_batch(ptr as *mut usize)
            .expect("cleanup after repairing the full-list link should succeed");
        assert_eq!(alloc.empty_count, 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn deallocate_batch_restores_caller_tail_link_when_empty_transition_fails() {
        let mut alloc = SCAllocator::new(PAGE_SIZE / 2, 1);
        assert_eq!(
            alloc.pg_count, 2,
            "test needs exactly two slots so the second free empties the page"
        );

        let (ptr, count, stride) = alloc
            .allocate_batch_v2(1)
            .expect("real two-slot page allocation should succeed");
        let stride = stride.expect("new page allocation should report object stride");
        assert_eq!(count, 2);

        let first = ptr;
        let second = unsafe { ptr.add(stride) };
        let page_vaddr = align_12k(ptr as usize);
        let mut cached_page_vaddr = 0usize;
        let mut cached_idx = 0i64;
        let obj_pge =
            SCAllocator::lookup_object_page(page_vaddr, &mut cached_page_vaddr, &mut cached_idx)
                .expect("allocated page should be indexed");

        unsafe {
            (first as *mut usize).write(0);
            (second as *mut usize).write(0);
        }
        alloc
            .deallocate_batch(first as *mut usize)
            .expect("first slot free should make the page partial");
        assert!(!obj_pge.is_full(count));

        let obj_ptr = SCAllocator::page_ptr(obj_pge) as usize;
        obj_pge.set_next(0);
        unsafe { (second as *mut usize).write(0) };
        let err = alloc
            .deallocate_batch(second as *mut usize)
            .expect_err("corrupt partial ring must fail the empty-page transition");
        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(
            unsafe { *(second as *mut usize) },
            0,
            "failed transition must restore the caller-owned batch tail link"
        );

        obj_pge.set_next(obj_ptr);
        obj_pge.set_prev(obj_ptr);
        alloc.partial_start = SCAllocator::page_ptr(obj_pge);
        alloc
            .deallocate_batch(second as *mut usize)
            .expect("retrying the same caller batch after ring repair should succeed");
        assert_eq!(alloc.empty_count, 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn deallocate_batch_restores_full_list_when_partial_insert_fails() {
        let mut alloc = SCAllocator::new(64, 1);

        let (ptr, count, stride) = alloc
            .allocate_batch_v2(8)
            .expect("real page-backed allocation should succeed");
        let stride = stride.expect("new page allocation should report object stride");
        let page_vaddr = align_12k(ptr as usize);
        let mut cached_page_vaddr = 0usize;
        let mut cached_idx = 0i64;
        let obj_pge =
            SCAllocator::lookup_object_page(page_vaddr, &mut cached_page_vaddr, &mut cached_idx)
                .expect("allocated page should be indexed");
        assert!(obj_pge.is_full(count));

        let mut corrupt_partial_head = EfObjectPage::new();
        alloc.partial_start = &mut corrupt_partial_head;
        let err = alloc
            .deallocate_batch(ptr as *mut usize)
            .expect_err("corrupt partial-list head must fail insertion after full removal");
        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert!(
            obj_pge.is_full(count),
            "failed partial insertion must roll page allocation state back to full"
        );
        assert_eq!(
            alloc.full_start,
            SCAllocator::page_ptr(obj_pge),
            "failed partial insertion must restore the page to the full list"
        );

        alloc.partial_start = null_mut();
        unsafe { link_allocation_batch(ptr, count, stride) };
        alloc
            .deallocate_batch(ptr as *mut usize)
            .expect("cleanup after restoring list heads should succeed");
        assert_eq!(alloc.empty_count, 1);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn deallocate_batch_restores_radix_mapping_when_uninit_recycle_fails() {
        let mut alloc = SCAllocator::new(64, 1);

        let (ptr, count, stride) = alloc
            .allocate_batch_v2(8)
            .expect("real page-backed allocation should succeed");
        let stride = stride.expect("new page allocation should report object stride");
        let page_vaddr = align_12k(ptr as usize);
        let mut cached_page_vaddr = 0usize;
        let mut cached_idx = 0i64;
        let obj_pge =
            SCAllocator::lookup_object_page(page_vaddr, &mut cached_page_vaddr, &mut cached_idx)
                .expect("allocated page should be indexed");
        assert!(obj_pge.is_full(count));

        let mut corrupt_uninit_head = EfObjectPage::new();
        alloc.uninit_start = &mut corrupt_uninit_head;
        alloc.empty_count = EMPTY_SLAB_RETAIN_LIMIT;
        unsafe { link_allocation_batch(ptr, count, stride) };
        let err = alloc
            .deallocate_batch(ptr as *mut usize)
            .expect_err("corrupt uninit ring must fail after radix removal is attempted");
        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert!(obj_pge.is_full(count));
        assert_eq!(alloc.full_start, SCAllocator::page_ptr(obj_pge));

        let mut retry_cached_page_vaddr = 0usize;
        let mut retry_cached_idx = 0i64;
        let looked_up = SCAllocator::lookup_object_page(
            page_vaddr,
            &mut retry_cached_page_vaddr,
            &mut retry_cached_idx,
        )
        .expect("failed recycle must restore the radix mapping for the still-live page");
        assert_eq!(
            SCAllocator::page_ptr(looked_up),
            SCAllocator::page_ptr(obj_pge)
        );

        alloc.uninit_start = null_mut();
        alloc.empty_count = 0;
        unsafe { link_allocation_batch(ptr, count, stride) };
        alloc
            .deallocate_batch(ptr as *mut usize)
            .expect("cleanup after restoring radix mapping and list heads should succeed");
        assert_eq!(alloc.empty_count, 1);
    }

    #[test]
    fn remove_partial_rejects_empty_list_without_panic() {
        let mut alloc = SCAllocator::new(64, 1);
        let mut page = EfObjectPage::new();

        let err = alloc
            .remove_partial(&mut page)
            .expect_err("empty partial list must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert!(alloc.partial_start.is_null());
    }

    #[test]
    fn remove_full_rejects_empty_list_without_panic() {
        let mut alloc = SCAllocator::new(64, 1);
        let mut page = EfObjectPage::new();

        let err = alloc
            .remove_full(&mut page)
            .expect_err("empty full list must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert!(alloc.full_start.is_null());
    }

    #[test]
    fn remove_empty_rejects_count_head_mismatch_without_panic() {
        let mut alloc = SCAllocator::new(64, 1);
        alloc.empty_count = 1;
        alloc.empty_start = null_mut();

        let err = match alloc.remove_empty() {
            Ok(_) => panic!("empty_count without a head must fail closed"),
            Err(err) => err,
        };

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(alloc.empty_count, 1);
    }

    #[test]
    fn insert_ety_retains_empty_page_below_warm_cache_limit() {
        let mut alloc = SCAllocator::new(64, 1);
        let mut page = EfObjectPage::new();
        alloc.empty_count = EMPTY_SLAB_RETAIN_LIMIT - 1;

        assert_eq!(
            alloc
                .insert_ety(&mut page)
                .expect("below-limit empty page should stay cached"),
            None
        );

        assert_eq!(alloc.empty_count, EMPTY_SLAB_RETAIN_LIMIT);
        assert_eq!(alloc.empty_start, SCAllocator::page_ptr(&mut page));
        assert!(alloc.uninit_start.is_null());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn insert_ety_recycles_empty_page_at_warm_cache_limit() {
        let mut alloc = SCAllocator::new(64, 1);
        let mut page = EfObjectPage::new();
        let page_addr = page
            .allocate_page(alloc.pg_num)
            .expect("real object-page backing should allocate");
        alloc.empty_count = EMPTY_SLAB_RETAIN_LIMIT;

        assert_eq!(
            alloc
                .insert_ety(&mut page)
                .expect("at-limit empty page should recycle backing"),
            Some(page_addr as usize)
        );

        assert_eq!(alloc.empty_count, EMPTY_SLAB_RETAIN_LIMIT);
        assert!(alloc.empty_start.is_null());
        assert_eq!(alloc.uninit_start, SCAllocator::page_ptr(&mut page));
        assert!(
            !page.is_inited(),
            "recycled empty slab must release backing memory instead of retaining RSS"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn empty_slab_retention_is_per_class_and_reuses_retained_real_slabs() {
        let mut alloc64 = SCAllocator::new(64, 1);
        let mut alloc128 = SCAllocator::new(128, 1);
        let mut batches =
            [(core::ptr::null_mut::<u8>(), 0usize, 0usize); EMPTY_SLAB_RETAIN_LIMIT + 1];

        for slot in batches.iter_mut() {
            let (ptr, count, stride) = alloc64
                .allocate_batch_v2(8)
                .expect("64-byte class should allocate a real slab batch");
            *slot = (
                ptr,
                count,
                stride.expect("full-page batch should expose object stride"),
            );
        }

        for (ptr, count, stride) in batches {
            unsafe { link_allocation_batch(ptr, count, stride) };
            alloc64
                .deallocate_batch(ptr as *mut usize)
                .expect("real 64-byte batch should deallocate");
        }

        assert_eq!(
            alloc64.empty_count, EMPTY_SLAB_RETAIN_LIMIT,
            "64-byte class should cap retained empty slabs at its own warm-cache limit"
        );
        let recycled = unsafe {
            alloc64
                .uninit_start
                .as_ref()
                .expect("overflow empty slab should move to the uninitialized list")
        };
        assert!(
            !recycled.is_inited(),
            "overflow empty slab must release backing memory"
        );
        assert_eq!(
            alloc128.empty_count, 0,
            "another size class must not share the 64-byte class retention counter"
        );

        let (ptr128, count128, stride128) = alloc128
            .allocate_batch_v2(8)
            .expect("128-byte class should allocate independently");
        unsafe {
            link_allocation_batch(
                ptr128,
                count128,
                stride128.expect("128-byte full-page batch stride"),
            )
        };
        alloc128
            .deallocate_batch(ptr128 as *mut usize)
            .expect("128-byte class should retain its own empty slab");
        assert_eq!(alloc128.empty_count, 1);

        let (reused, reused_count, reused_stride) = alloc64
            .allocate_batch_v2(8)
            .expect("64-byte class should reuse a retained empty slab before uninit");
        assert_eq!(
            alloc64.empty_count,
            EMPTY_SLAB_RETAIN_LIMIT - 1,
            "hot empty-slab reuse should pop from the retained warm cache"
        );
        unsafe {
            link_allocation_batch(
                reused,
                reused_count,
                reused_stride.expect("reused full-page batch stride"),
            )
        };
        alloc64
            .deallocate_batch(reused as *mut usize)
            .expect("cleanup after retained-slab reuse should succeed");
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn empty_slab_rotation_evicts_old_retained_slab_and_keeps_new_hot_slab() {
        const CHILD_ENV: &str = "UNIALLOC_EMPTY_SLAB_ROTATION_CHILD";
        const TEST_NAME: &str =
            "sc::efficient_sc::tests::empty_slab_rotation_evicts_old_retained_slab_and_keeps_new_hot_slab";

        // Eviction unmaps `oldest_ptr` before the stale-pointer assertion. A
        // parallel slab test can reuse that address and publish a new global
        // radix mapping, so keep this address lifecycle in a private process.
        if crate::test_support::run_test_in_fresh_process(CHILD_ENV, TEST_NAME) {
            return;
        }

        let mut alloc = SCAllocator::new(64, 1);
        let mut batches =
            [(core::ptr::null_mut::<u8>(), 0usize, 0usize); EMPTY_SLAB_RETAIN_LIMIT + 1];

        for slot in batches.iter_mut() {
            let (ptr, count, stride) = alloc
                .allocate_batch_v2(8)
                .expect("64-byte class should allocate a real slab batch");
            *slot = (
                ptr,
                count,
                stride.expect("full-page batch should expose object stride"),
            );
        }

        let oldest_ptr = batches[0].0;
        let newest_ptr = batches[EMPTY_SLAB_RETAIN_LIMIT].0;
        for (ptr, count, stride) in batches {
            unsafe { link_allocation_batch(ptr, count, stride) };
            alloc
                .deallocate_batch(ptr as *mut usize)
                .expect("real 64-byte batch should deallocate");
        }

        assert_eq!(alloc.empty_count, EMPTY_SLAB_RETAIN_LIMIT);
        assert!(!alloc.uninit_start.is_null());
        assert!(
            unsafe { alloc.uninit_start.as_ref().expect("evicted descriptor") }
                .get_data_ptr()
                .is_none()
        );
        assert_eq!(
            unsafe { alloc.empty_start.as_ref().expect("hot empty slab") }
                .get_data_ptr()
                .map(|ptr| ptr as usize),
            Some(newest_ptr as usize),
            "the newest freed slab should become the hot retained empty head"
        );

        let err = alloc
            .deallocate_batch(oldest_ptr as *mut usize)
            .expect_err("evicted retained slab mapping should be removed");
        assert_eq!(err.to_raw_errno(), AllocError::EUAF.to_raw_errno());

        let (reused, reused_count, reused_stride) = alloc
            .allocate_batch_v2(8)
            .expect("hot retained empty slab should be reusable");
        assert_eq!(reused, newest_ptr);
        unsafe {
            link_allocation_batch(
                reused,
                reused_count,
                reused_stride.expect("reused full-page batch stride"),
            )
        };
        alloc
            .deallocate_batch(reused as *mut usize)
            .expect("cleanup after hot retained-slab reuse should succeed");
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn insert_ety_recycles_large_span_without_warm_cache() {
        let mut alloc = SCAllocator::new(1024, EMPTY_SLAB_RETAIN_OS_PAGE_BUDGET + 1);
        let mut page = EfObjectPage::new();
        let page_addr = page
            .allocate_page(alloc.pg_num)
            .expect("real large-span object-page backing should allocate");

        assert_eq!(alloc.empty_slab_retain_limit(), 0);
        assert_eq!(
            alloc
                .insert_ety(&mut page)
                .expect("huge empty spans should be recycled immediately"),
            Some(page_addr as usize)
        );

        assert_eq!(alloc.empty_count, 0);
        assert!(alloc.empty_start.is_null());
        assert_eq!(alloc.uninit_start, SCAllocator::page_ptr(&mut page));
        assert!(
            !page.is_inited(),
            "large-span recycling must release backing memory instead of retaining RSS"
        );
    }

    #[test]
    fn remove_uninit_rejects_empty_list_without_panic() {
        let mut alloc = SCAllocator::new(64, 1);

        let err = match alloc.remove_uninit() {
            Ok(_) => panic!("empty uninit list must fail closed"),
            Err(err) => err,
        };

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert!(alloc.uninit_start.is_null());
    }

    #[test]
    fn lookup_object_page_rejects_radix_key_overflow_without_panic() {
        let mut cached_page_vaddr = 0usize;
        let mut cached_idx = 0i64;

        let err = match SCAllocator::lookup_object_page(
            usize::MAX,
            &mut cached_page_vaddr,
            &mut cached_idx,
        ) {
            Ok(_) => panic!("overflowing lookup key must fail closed"),
            Err(err) => err,
        };

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(cached_page_vaddr, 0);
        assert_eq!(cached_idx, 0);
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn empty_radix_tree() -> &'static mut RadixTree {
        unsafe { allocate_node::<RadixTree>().as_mut().expect("radix tree") }
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn handle_rd_tree_insert_rejects_key_overflow_without_panic() {
        let err = SCAllocator::handle_rd_tree_insert(1, empty_radix_tree(), 0x1000, usize::MAX)
            .expect_err("overflowing radix key must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn handle_rd_tree_insert_rejects_value_overflow_without_panic() {
        let err = SCAllocator::handle_rd_tree_insert(1, empty_radix_tree(), usize::MAX, 0x1000)
            .expect_err("overflowing radix value must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn handle_rd_tree_remove_rejects_key_overflow_without_panic() {
        let alloc = SCAllocator::new(64, 1);
        let err = alloc
            .handle_rd_tree_remove(empty_radix_tree(), usize::MAX)
            .expect_err("overflowing remove key must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
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
        assert_eq!(alloc.pg_count, (PAGE_SIZE / 64) as i32);
        assert_eq!(alloc.pg_num, 1);
        assert_eq!(alloc.pg_align, 64);
    }
}
