use crate::error::{AllocError, Result};
use crate::prelude::*;
use crate::*;
use alloc::boxed::Box;
use core::alloc::{GlobalAlloc, Layout};
use core::ptr::{self, null_mut, NonNull};
include!(concat!(env!("OUT_DIR"), "/consts.rs"));

const OBJECT_BITMAP_BITS: usize = core::mem::size_of::<usize>() * 8;
const MIN_OBJECT_SLOT_BYTES: usize = core::mem::size_of::<usize>();
const MAX_PAGE_OBJECT_BITMAP_WORDS: usize =
    (PAGE_SIZE / MIN_OBJECT_SLOT_BYTES + OBJECT_BITMAP_BITS - 1) / OBJECT_BITMAP_BITS;
/// Maximum objects returned from a single strict-alignment slab-page split.
///
/// Natural-alignment batches still return the whole page and use the bump fast
/// path.  This cap applies only when the requested alignment is stricter than
/// the page's object stride, so only a subset of slots can satisfy the request.
/// Returning every matching slot to one thread cache can pin many aligned
/// objects privately and leave the rest of the page fragmented.  Keep a small
/// batch for amortization, and leave the remaining aligned/skipped slots on the
/// slab freelist so other threads can reuse them without forcing new pages.
pub(crate) const STRICT_ALIGNMENT_BATCH_RETURN_LIMIT: usize = 32;

/// Holds allocated data within pages.
///
/// Has a data-section where objects are allocated from
/// and a small amount of meta-data in the form of a bitmap
/// to track allocations at the end of the page.
///
/// # Notes
/// An object of this type will be exactly 8 KiB.
/// It is marked `repr(C)` because we rely on a well defined order of struct
/// members.
///
/// # Generics
///
/// * `N` - used to calculate the `bitfield` array
/// * `TAR` - the size class (e.g., 8, 16, 24, ...)
/// * `NP` - number of pages pointed by the `data` pointer
#[repr(C)]
pub struct ObjectPage {
    /// number of chunks that have been allocated
    counter: usize,
    data: *mut u8,
    ptr: *mut u8,
    prev: usize,
    next: usize,
}

#[derive(Clone, Copy)]
pub(crate) struct ObjectPageAllocationState {
    counter: usize,
    free_head: *mut u8,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct ObjectPageDeallocation {
    next_batch_head: *mut usize,
    caller_tail: usize,
    caller_tail_next: usize,
}

impl ObjectPageDeallocation {
    #[inline]
    pub(crate) fn next_batch_head(&self) -> *mut usize {
        self.next_batch_head
    }

    #[inline]
    pub(crate) fn restore_caller_tail_link(self) {
        unsafe {
            *(self.caller_tail as *mut usize) = self.caller_tail_next;
        }
    }
}

// impl Default for ObjectPage {
//     fn default() -> Self {
//         Self {
//             counter: 0,
//             prev: 0i32,
//             next: 0i32,
//             data: ptr::null_mut(),
//         }
//     }
// }
impl Default for ObjectPage {
    fn default() -> Self {
        Self {
            counter: 0,
            data: ptr::null_mut(),
            ptr: ptr::null_mut(),
            prev: 0,
            next: 0,
        }
    }
}

impl ObjectPage {
    pub const fn new() -> Self {
        Self {
            counter: 0,
            data: ptr::null_mut(),
            ptr: ptr::null_mut(),
            prev: 0,
            next: 0,
        }
    }

    pub fn has_next(&self) -> bool {
        self.next != 0
    }

    pub fn has_prev(&self) -> bool {
        self.prev != 0
    }

    #[allow(clippy::mut_from_ref)]
    pub fn get_prev(&self) -> Result<&mut ObjectPage> {
        unsafe {
            (self.prev as *const ObjectPage as *mut ObjectPage)
                .as_mut()
                .ok_or(AllocError::EFATAL)
        }
    }

    #[allow(clippy::mut_from_ref)]
    pub fn try_get_prev(&self) -> Result<&mut ObjectPage> {
        self.get_prev()
    }

    #[allow(clippy::mut_from_ref)]
    pub fn get_next(&self) -> Result<&mut ObjectPage> {
        unsafe {
            (self.next as *const ObjectPage as *mut ObjectPage)
                .as_mut()
                .ok_or(AllocError::EFATAL)
        }
    }

    #[allow(clippy::mut_from_ref)]
    pub fn try_get_next(&self) -> Result<&mut ObjectPage> {
        self.get_next()
    }

    pub fn set_prev(&mut self, nprev: usize) {
        self.prev = nprev;
    }

    pub fn set_next(&mut self, nnext: usize) {
        self.next = nnext;
    }

    pub fn allocate_page(&mut self, pg_num: usize) -> Result<*mut u8> {
        let layout = super::try_object_page_layout(pg_num)?;
        let ans = unsafe { GlobalBackend.alloc(layout) };
        if ans.is_null() {
            return Err(AllocError::ENOMEM);
        }
        self.data = ans;
        self.ptr = ptr::null_mut();
        self.counter = 0;
        Ok(ans)
    }

    //This function is used only in sc.rs, for more details, please refer to sc:deallocate
    pub fn get_data_ptr(&self) -> Option<*mut u8> {
        (!self.data.is_null()).then_some(self.data)
    }

    pub fn destroy_page(&mut self, pg_num: usize) -> Option<*mut u8> {
        if self.data.is_null() {
            return None;
        }
        let layout = match super::try_object_page_layout(pg_num) {
            Ok(layout) => layout,
            Err(_) => return None,
        };
        let p = self.data;
        unsafe {
            GlobalBackend.dealloc(p as *mut u8, layout);
        }
        self.data = ptr::null_mut();
        self.ptr = ptr::null_mut();
        self.counter = 0;
        Some(p)
    }

    #[inline]
    pub(crate) fn allocation_state_snapshot(&self) -> ObjectPageAllocationState {
        ObjectPageAllocationState {
            counter: self.counter,
            free_head: self.ptr,
        }
    }

    #[inline]
    pub(crate) fn restore_allocation_state(&mut self, state: ObjectPageAllocationState) {
        self.counter = state.counter;
        self.ptr = state.free_head;
    }

    #[inline]
    pub fn is_inited(&self) -> bool {
        !self.data.is_null()
    }
}

impl ObjectPage {
    #[inline]
    fn object_addr(base: usize, object_idx: usize, pg_align: usize) -> Result<usize> {
        object_idx
            .checked_mul(pg_align)
            .and_then(|offset| base.checked_add(offset))
            .ok_or(AllocError::ESIZE)
    }

    #[inline]
    fn link_tail(head: &mut usize, tail: &mut usize, addr: usize) {
        if *head == 0 {
            *head = addr;
        } else {
            unsafe {
                *(*tail as *mut usize) = addr;
            }
        }
        *tail = addr;
    }

    #[inline]
    fn terminate_link(tail: usize) {
        if tail != 0 {
            unsafe {
                *(tail as *mut usize) = 0;
            }
        }
    }

    #[inline]
    fn object_ptr_is_aligned(addr: usize, align: usize) -> bool {
        addr & (align - 1) == 0
    }

    #[inline]
    fn strict_alignment_batch_return_limit(pg_align: usize) -> usize {
        let object_limit = STRICT_ALIGNMENT_BATCH_RETURN_LIMIT.max(1);
        if pg_align == 0 {
            return 1;
        }
        // Do not let a strict-alignment split donate more than about one page of
        // payload to one thread cache.  Small objects still use the old object
        // cap for amortization; large/multi-page classes keep the remaining
        // aligned slots on the slab freelist for other threads.
        let page_payload_limit = (PAGE_SIZE / pg_align).max(1);
        core::cmp::min(object_limit, page_payload_limit)
    }

    /// Return the first object slot satisfying a stricter power-of-two alignment.
    ///
    /// Empty-page strict-alignment allocation must prove that at least one slot
    /// can be returned before it writes any free-list links into the page.  A
    /// naive proof scans every object and the split pass scans the page again.
    /// For a power-of-two alignment the slot addresses repeat modulo `align`
    /// after `align / gcd(align, pg_align)` objects, so this proof only needs
    /// to inspect one alignment period (or the whole page, whichever is
    /// smaller).  The later split pass still walks all slots once to build the
    /// selected/skipped lists in deterministic address order.
    fn first_aligned_object_index(
        base: usize,
        pg_count: usize,
        pg_align: usize,
        align: usize,
    ) -> Result<Option<usize>> {
        if pg_count == 0 || pg_align == 0 || align == 0 || !align.is_power_of_two() {
            return Err(AllocError::EFATAL);
        }

        let align_mask = align - 1;
        let base_mod = base & align_mask;
        if base_mod == 0 {
            return Ok(Some(0));
        }

        // `align` is a power of two, so gcd(align, pg_align) is the largest
        // power-of-two divisor of `pg_align`, capped at `align`.
        let stride_power_of_two = 1usize << pg_align.trailing_zeros();
        let stride_gcd = core::cmp::min(stride_power_of_two, align);
        if (base_mod & (stride_gcd - 1)) != 0 {
            return Ok(None);
        }

        let period = align / stride_gcd;
        for object_idx in 0..core::cmp::min(pg_count, period) {
            let addr = Self::object_addr(base, object_idx, pg_align)?;
            if Self::object_ptr_is_aligned(addr, align) {
                return Ok(Some(object_idx));
            }
        }
        Ok(None)
    }

    fn checked_object_span_end(
        &self,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
    ) -> Result<usize> {
        let page_span = super::checked_object_page_span_bytes(pg_num)?;
        let object_span = pg_count.checked_mul(pg_align).ok_or(AllocError::ESIZE)?;
        if object_span > page_span {
            return Err(AllocError::ELAYOUT);
        }
        (self.data as usize)
            .checked_add(object_span)
            .ok_or(AllocError::ESIZE)
    }

    fn validate_partial_freelist_exact(
        &self,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
    ) -> Result<usize> {
        let free_count = pg_count
            .checked_sub(self.counter)
            .ok_or(AllocError::EFATAL)?;
        if free_count == 0 || self.ptr.is_null() {
            return Err(AllocError::EFATAL);
        }
        let mut seen = 0usize;
        let mut cur = self.ptr as usize;
        while cur != 0 {
            if seen >= free_count {
                return Err(AllocError::EDBFRE);
            }
            self.validate_deallocate_object_ptr(cur, pg_num, pg_count, pg_align)?;
            seen = seen.checked_add(1).ok_or(AllocError::ESIZE)?;
            cur = unsafe { *(cur as *mut usize) };
        }
        if seen != free_count {
            return Err(AllocError::EFATAL);
        }
        Ok(free_count)
    }

    fn allocate_aligned_slots_from_empty_page(
        &mut self,
        align: usize,
        pg_count: usize,
        pg_align: usize,
    ) -> Result<(*mut u8, usize, Option<usize>)> {
        let base = self.data as usize;
        let return_limit = Self::strict_alignment_batch_return_limit(pg_align);
        // Prove success before writing object links.  Finding the first match
        // is enough; the split loop below still applies the full return limit.
        if Self::first_aligned_object_index(base, pg_count, pg_align, align)?.is_none() {
            return Err(AllocError::ENOMEM);
        }

        let mut selected_head = 0usize;
        let mut selected_tail = 0usize;
        let mut skipped_head = 0usize;
        let mut skipped_tail = 0usize;
        let mut selected = 0usize;

        for object_idx in 0..pg_count {
            let addr = Self::object_addr(base, object_idx, pg_align)?;
            if Self::object_ptr_is_aligned(addr, align) && selected < return_limit {
                Self::link_tail(&mut selected_head, &mut selected_tail, addr);
                selected = selected.checked_add(1).ok_or(AllocError::ESIZE)?;
            } else {
                Self::link_tail(&mut skipped_head, &mut skipped_tail, addr);
            }
        }
        Self::terminate_link(selected_tail);
        Self::terminate_link(skipped_tail);
        self.ptr = skipped_head as *mut u8;
        self.counter = selected;
        Ok((selected_head as *mut u8, selected, None))
    }

    fn allocate_aligned_slots_from_freelist(
        &mut self,
        align: usize,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
    ) -> Result<(*mut u8, usize, Option<usize>)> {
        let return_limit = Self::strict_alignment_batch_return_limit(pg_align);
        let mut selected_head = 0usize;
        let mut selected_tail = 0usize;
        let mut skipped_head = 0usize;
        let mut skipped_tail = 0usize;
        let mut selected = 0usize;
        let free_count = pg_count
            .checked_sub(self.counter)
            .ok_or(AllocError::EFATAL)?;
        let mut seen = 0usize;
        let mut cur = self.ptr as usize;
        while cur != 0 {
            if seen >= free_count {
                return Err(AllocError::EDBFRE);
            }
            self.validate_deallocate_object_ptr(cur, pg_num, pg_count, pg_align)?;
            let next = unsafe { *(cur as *mut usize) };
            if Self::object_ptr_is_aligned(cur, align) && selected < return_limit {
                Self::link_tail(&mut selected_head, &mut selected_tail, cur);
                selected = selected.checked_add(1).ok_or(AllocError::ESIZE)?;
            } else {
                Self::link_tail(&mut skipped_head, &mut skipped_tail, cur);
            }
            seen = seen.checked_add(1).ok_or(AllocError::ESIZE)?;
            cur = next;
        }
        if seen != free_count {
            return Err(AllocError::EFATAL);
        }
        if selected == 0 {
            return Err(AllocError::ENOMEM);
        }

        Self::terminate_link(selected_tail);
        Self::terminate_link(skipped_tail);
        self.ptr = skipped_head as *mut u8;
        self.counter = self
            .counter
            .checked_add(selected)
            .ok_or(AllocError::ESIZE)?;
        Ok((selected_head as *mut u8, selected, None))
    }

    fn classify_freelist_for_alignment_bitmap(
        &self,
        align: usize,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
        selected_bitmap: &mut [usize],
        skipped_bitmap: &mut [usize],
    ) -> Result<usize> {
        let return_limit = Self::strict_alignment_batch_return_limit(pg_align);
        for word in selected_bitmap.iter_mut().chain(skipped_bitmap.iter_mut()) {
            *word = 0;
        }

        let free_count = pg_count
            .checked_sub(self.counter)
            .ok_or(AllocError::EFATAL)?;
        let mut selected = 0usize;
        let mut seen = 0usize;
        let mut cur = self.ptr as usize;
        while cur != 0 {
            if seen >= free_count {
                return Err(AllocError::EDBFRE);
            }

            let object_idx = self.validated_object_index(cur, pg_num, pg_count, pg_align)?;
            let next = unsafe { *(cur as *mut usize) };
            if Self::object_ptr_is_aligned(cur, align) && selected < return_limit {
                Self::set_object_bitmap_bit(selected_bitmap, object_idx)?;
                selected = selected.checked_add(1).ok_or(AllocError::ESIZE)?;
            } else {
                Self::set_object_bitmap_bit(skipped_bitmap, object_idx)?;
            }

            seen = seen.checked_add(1).ok_or(AllocError::ESIZE)?;
            cur = next;
        }
        if seen != free_count {
            return Err(AllocError::EFATAL);
        }

        Ok(selected)
    }

    fn allocate_aligned_slots_from_freelist_bitmap(
        &mut self,
        align: usize,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
    ) -> Result<(*mut u8, usize, Option<usize>)> {
        let bitmap_words = Self::object_bitmap_word_count(pg_count)?;
        if bitmap_words > MAX_PAGE_OBJECT_BITMAP_WORDS {
            return self.allocate_aligned_slots_from_freelist(align, pg_num, pg_count, pg_align);
        }

        let mut selected_bitmap = [0usize; MAX_PAGE_OBJECT_BITMAP_WORDS];
        let mut skipped_bitmap = [0usize; MAX_PAGE_OBJECT_BITMAP_WORDS];
        let selected = self.classify_freelist_for_alignment_bitmap(
            align,
            pg_num,
            pg_count,
            pg_align,
            &mut selected_bitmap[..bitmap_words],
            &mut skipped_bitmap[..bitmap_words],
        )?;
        if selected == 0 {
            return Err(AllocError::ENOMEM);
        }

        let base = self.data as usize;
        let mut selected_head = 0usize;
        let mut selected_tail = 0usize;
        let mut skipped_head = 0usize;
        let mut skipped_tail = 0usize;

        // The freelist was fully validated above before any link is rewritten.
        // Rebuilding from bitmaps avoids the old "count once, split once" double
        // pointer-chase on the slab lock path while preserving fail-before-mutate
        // behavior for corrupt partial lists.
        for object_idx in 0..pg_count {
            let addr = Self::object_addr(base, object_idx, pg_align)?;
            if Self::object_bitmap_contains(&selected_bitmap[..bitmap_words], object_idx)? {
                Self::link_tail(&mut selected_head, &mut selected_tail, addr);
            } else if Self::object_bitmap_contains(&skipped_bitmap[..bitmap_words], object_idx)? {
                Self::link_tail(&mut skipped_head, &mut skipped_tail, addr);
            }
        }

        Self::terminate_link(selected_tail);
        Self::terminate_link(skipped_tail);
        self.ptr = skipped_head as *mut u8;
        self.counter = self
            .counter
            .checked_add(selected)
            .ok_or(AllocError::ESIZE)?;
        Ok((selected_head as *mut u8, selected, None))
    }

    /// Checks if we can still allocate more objects of a given layout within the page.
    #[inline]
    pub(crate) fn is_full(&self, pg_count: usize) -> bool {
        pg_count <= self.counter as usize
    }

    /// Checks if the page has currently no allocations.
    #[inline]
    pub fn is_empty(&self) -> bool {
        self.counter == 0
    }

    pub(crate) fn allocate_aligned_batch(
        &mut self,
        align: usize,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
    ) -> Result<(*mut u8, usize, Option<usize>)> {
        if pg_count == 0
            || pg_align == 0
            || self.data.is_null()
            || align == 0
            || !align.is_power_of_two()
        {
            return Err(AllocError::EFATAL);
        }
        if self.counter >= pg_count {
            return Err(AllocError::ENOMEM);
        }
        self.checked_object_span_end(pg_num, pg_count, pg_align)?;

        let base = self.data as usize;
        if base & (align - 1) == 0 && pg_align % align == 0 {
            return self.allocate_all(pg_num, pg_count, pg_align);
        }

        if self.counter == 0 && self.ptr.is_null() {
            self.allocate_aligned_slots_from_empty_page(align, pg_count, pg_align)
        } else {
            self.allocate_aligned_slots_from_freelist_bitmap(align, pg_num, pg_count, pg_align)
        }
    }

    pub(crate) fn allocate_all(
        &mut self,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
    ) -> Result<(*mut u8, usize, Option<usize>)> {
        if pg_count == 0 || pg_align == 0 || self.data.is_null() {
            return Err(AllocError::EFATAL);
        }
        self.checked_object_span_end(pg_num, pg_count, pg_align)?;
        if self.counter == 0 {
            self.counter = pg_count;
            if !self.ptr.is_null() {
                self.ptr = null_mut();
            }
            Ok((self.data, pg_count, Some(pg_align)))
        } else {
            if self.counter >= pg_count || self.ptr.is_null() {
                return Err(AllocError::EFATAL);
            }
            let to_allocate = pg_count
                .checked_sub(self.counter)
                .ok_or(AllocError::EFATAL)?;
            self.validate_partial_freelist_exact(pg_num, pg_count, pg_align)?;
            self.counter = pg_count;
            let ans = self.ptr;
            self.ptr = null_mut();
            Ok((ans, to_allocate, None))
        }
    }

    /// Deallocates a memory object within this page.
    pub(crate) fn deallocate(
        &mut self,
        free_ptr: usize,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
    ) -> Result<ObjectPageDeallocation> {
        if self.counter == 0 {
            return Err(AllocError::EUAF);
        }

        let bitmap_words = Self::object_bitmap_word_count(pg_count)?;
        let mut batch_bitmap = [0usize; MAX_PAGE_OBJECT_BITMAP_WORDS];
        let use_stack_bitmap = bitmap_words <= MAX_PAGE_OBJECT_BITMAP_WORDS;
        let (tail, counter, next) = if use_stack_bitmap {
            self.validate_deallocation_batch(
                free_ptr,
                pg_num,
                pg_count,
                pg_align,
                Some(&mut batch_bitmap[..bitmap_words]),
            )?
        } else {
            self.validate_deallocation_batch(free_ptr, pg_num, pg_count, pg_align, None)?
        };

        if self.counter < counter {
            return Err(AllocError::EDBFRE);
        }
        self.reject_prevalidated_batch_overlap_with_existing_freelist(
            free_ptr,
            counter,
            pg_num,
            pg_count,
            pg_align,
            if use_stack_bitmap {
                Some(&batch_bitmap[..bitmap_words])
            } else {
                None
            },
        )?;
        let caller_tail_next = next;
        unsafe { *(tail as *mut usize) = self.ptr as usize };
        self.ptr = free_ptr as *mut u8;
        self.counter -= counter;
        Ok(ObjectPageDeallocation {
            next_batch_head: next as *mut usize,
            caller_tail: tail,
            caller_tail_next,
        })
    }

    /// Validate an incoming same-page deallocation batch before mutating links.
    ///
    /// The caller may pass a stack bitmap for normal object-page geometries.
    /// Filling it during validation avoids a second traversal of the batch when
    /// we later reject overlap with the existing freelist.  Large geometries
    /// still use the old pointer-walk fallback by passing `None`.
    fn validate_deallocation_batch(
        &self,
        batch_head: usize,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
        mut batch_bitmap: Option<&mut [usize]>,
    ) -> Result<(usize, usize, usize)> {
        if let Some(bitmap) = batch_bitmap.as_deref_mut() {
            for word in bitmap.iter_mut() {
                *word = 0;
            }
        }

        let base = self.data as usize;
        let page_span = super::checked_object_page_span_bytes(pg_num)?;
        let page_end = base.checked_add(page_span).ok_or(AllocError::ESIZE)?;

        let object_idx = self.validated_object_index(batch_head, pg_num, pg_count, pg_align)?;
        if let Some(bitmap) = batch_bitmap.as_deref_mut() {
            Self::set_object_bitmap_bit(bitmap, object_idx)?;
        }

        let mut tail = batch_head;
        let mut counter = 1usize;
        let mut next = unsafe { *(tail as *mut usize) };
        while next >= base && next < page_end {
            let object_idx = self.validated_object_index(next, pg_num, pg_count, pg_align)?;
            if counter >= self.counter {
                return Err(AllocError::EDBFRE);
            }
            if let Some(bitmap) = batch_bitmap.as_deref_mut() {
                Self::set_object_bitmap_bit(bitmap, object_idx)?;
            }
            counter = counter.checked_add(1).ok_or(AllocError::ESIZE)?;
            tail = next;
            next = unsafe { *(tail as *mut usize) };
        }

        Ok((tail, counter, next))
    }

    fn reject_prevalidated_batch_overlap_with_existing_freelist(
        &self,
        batch_head: usize,
        batch_count: usize,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
        batch_bitmap: Option<&[usize]>,
    ) -> Result<()> {
        let free_count = pg_count
            .checked_sub(self.counter)
            .ok_or(AllocError::EFATAL)?;
        if free_count == 0 {
            if self.ptr.is_null() {
                return Ok(());
            }
            return Err(AllocError::EDBFRE);
        }

        if let Some(batch_bitmap) = batch_bitmap {
            return self.reject_freelist_overlap_with_batch_bitmap(
                free_count,
                pg_num,
                pg_count,
                pg_align,
                batch_bitmap,
            );
        }

        self.reject_batch_overlap_with_existing_freelist_slow(
            batch_head,
            batch_count,
            free_count,
            pg_num,
            pg_count,
            pg_align,
        )
    }

    fn reject_batch_overlap_with_existing_freelist_slow(
        &self,
        batch_head: usize,
        batch_count: usize,
        free_count: usize,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
    ) -> Result<()> {
        let mut free_seen = 0usize;
        let mut free_cur = self.ptr as usize;
        while free_cur != 0 {
            if free_seen >= free_count {
                return Err(AllocError::EDBFRE);
            }
            self.validate_deallocate_object_ptr(free_cur, pg_num, pg_count, pg_align)?;
            if self.validated_batch_contains_object(
                batch_head,
                batch_count,
                free_cur,
                pg_num,
                pg_count,
                pg_align,
            )? {
                return Err(AllocError::EDBFRE);
            }
            free_seen = free_seen.checked_add(1).ok_or(AllocError::ESIZE)?;
            free_cur = unsafe { *(free_cur as *mut usize) };
        }
        if free_seen != free_count {
            return Err(AllocError::EFATAL);
        }
        Ok(())
    }

    #[inline]
    fn object_bitmap_word_count(pg_count: usize) -> Result<usize> {
        pg_count
            .checked_add(OBJECT_BITMAP_BITS - 1)
            .map(|rounded| rounded / OBJECT_BITMAP_BITS)
            .ok_or(AllocError::ESIZE)
    }

    #[inline]
    fn set_object_bitmap_bit(bitmap: &mut [usize], object_idx: usize) -> Result<()> {
        let word_idx = object_idx / OBJECT_BITMAP_BITS;
        let bit_idx = object_idx % OBJECT_BITMAP_BITS;
        let word = bitmap.get_mut(word_idx).ok_or(AllocError::ESIZE)?;
        let mask = 1usize << bit_idx;
        if *word & mask != 0 {
            return Err(AllocError::EDBFRE);
        }
        *word |= mask;
        Ok(())
    }

    #[inline]
    fn object_bitmap_contains(bitmap: &[usize], object_idx: usize) -> Result<bool> {
        let word_idx = object_idx / OBJECT_BITMAP_BITS;
        let bit_idx = object_idx % OBJECT_BITMAP_BITS;
        let word = bitmap.get(word_idx).ok_or(AllocError::ESIZE)?;
        Ok(*word & (1usize << bit_idx) != 0)
    }

    fn reject_freelist_overlap_with_batch_bitmap(
        &self,
        free_count: usize,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
        batch_bitmap: &[usize],
    ) -> Result<()> {
        let mut free_seen = 0usize;
        let mut free_cur = self.ptr as usize;
        while free_cur != 0 {
            if free_seen >= free_count {
                return Err(AllocError::EDBFRE);
            }
            let object_idx = self.validated_object_index(free_cur, pg_num, pg_count, pg_align)?;
            if Self::object_bitmap_contains(batch_bitmap, object_idx)? {
                return Err(AllocError::EDBFRE);
            }
            free_seen = free_seen.checked_add(1).ok_or(AllocError::ESIZE)?;
            free_cur = unsafe { *(free_cur as *mut usize) };
        }
        if free_seen != free_count {
            return Err(AllocError::EFATAL);
        }
        Ok(())
    }

    fn validated_batch_contains_object(
        &self,
        batch_head: usize,
        batch_count: usize,
        needle: usize,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
    ) -> Result<bool> {
        let base = self.data as usize;
        let page_span = super::checked_object_page_span_bytes(pg_num)?;
        let page_end = base.checked_add(page_span).ok_or(AllocError::ESIZE)?;
        let mut seen = 0usize;
        let mut cur = batch_head;
        while cur != 0 && seen < batch_count {
            if cur == needle {
                return Ok(true);
            }
            self.validate_deallocate_object_ptr(cur, pg_num, pg_count, pg_align)?;
            let next = unsafe { *(cur as *mut usize) };
            seen = seen.checked_add(1).ok_or(AllocError::ESIZE)?;
            if next < base || next >= page_end {
                break;
            }
            cur = next;
        }
        if seen != batch_count {
            return Err(AllocError::EFATAL);
        }
        Ok(false)
    }

    fn validate_deallocate_object_ptr(
        &self,
        object_ptr: usize,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
    ) -> Result<()> {
        self.validated_object_index(object_ptr, pg_num, pg_count, pg_align)
            .map(|_| ())
    }

    fn validated_object_index(
        &self,
        object_ptr: usize,
        pg_num: usize,
        pg_count: usize,
        pg_align: usize,
    ) -> Result<usize> {
        let base = self.data as usize;
        if base == 0 {
            return Err(AllocError::EUAF);
        }
        if pg_count == 0 || pg_align == 0 {
            return Err(AllocError::ELAYOUT);
        }

        let page_span = super::checked_object_page_span_bytes(pg_num)?;
        let page_end = base.checked_add(page_span).ok_or(AllocError::ESIZE)?;
        if object_ptr < base || object_ptr >= page_end {
            return Err(AllocError::EUAF);
        }

        let object_span = pg_count.checked_mul(pg_align).ok_or(AllocError::ESIZE)?;
        if object_span > page_span {
            return Err(AllocError::ELAYOUT);
        }

        let offset = object_ptr.checked_sub(base).ok_or(AllocError::EOOB)?;
        if offset >= object_span || offset % pg_align != 0 {
            return Err(AllocError::EOOB);
        }
        let object_idx = offset / pg_align;
        if object_idx >= pg_count {
            return Err(AllocError::EOOB);
        }

        Ok(object_idx)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[repr(align(16))]
    struct Align16Words([usize; 4]);

    #[repr(align(16))]
    struct Align16Many([usize; 128]);

    fn read_link(addr: usize) -> usize {
        unsafe { *(addr as *mut usize) }
    }

    fn write_link(addr: usize, next: usize) {
        unsafe {
            *(addr as *mut usize) = next;
        }
    }

    fn count_link_chain(mut head: usize, max_nodes: usize) -> usize {
        let mut count = 0usize;
        while head != 0 {
            count += 1;
            assert!(
                count <= max_nodes,
                "linked-list traversal exceeded the expected bound; possible cycle"
            );
            head = read_link(head);
        }
        count
    }

    fn link_object_range(base: usize, count: usize, stride: usize) {
        for object_idx in 0..count {
            let next = if object_idx + 1 == count {
                0
            } else {
                base + (object_idx + 1) * stride
            };
            write_link(base + object_idx * stride, next);
        }
    }

    #[test]
    fn efficient_object_page_rejects_zero_page_allocation_without_panic() {
        let mut page = ObjectPage::new();
        let err = page
            .allocate_page(0)
            .expect_err("zero-page allocation should fail closed");
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert!(page.get_data_ptr().is_none());
    }

    #[test]
    fn efficient_object_page_destroy_uninitialized_is_noop() {
        let mut page = ObjectPage::new();
        assert!(page.destroy_page(1).is_none());
    }

    #[test]
    fn efficient_object_page_try_links_reject_null_without_panic() {
        let page = ObjectPage::new();

        let prev = match page.try_get_prev() {
            Ok(_) => panic!("null prev link must be rejected"),
            Err(err) => err,
        };
        let next = match page.try_get_next() {
            Ok(_) => panic!("null next link must be rejected"),
            Err(err) => err,
        };

        assert_eq!(prev.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(next.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
    }

    #[test]
    fn efficient_object_page_allocate_all_rejects_uninitialized_page_without_mutation() {
        let mut page = ObjectPage::new();

        let err = page
            .allocate_all(1, 1, 8)
            .expect_err("allocate_all must fail closed before installing a null data batch");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(page.counter, 0);
        assert!(page.data.is_null());
        assert!(page.ptr.is_null());
    }

    #[test]
    fn efficient_object_page_allocate_all_rejects_overfull_counter_without_underflow() {
        let mut storage = [0usize; 4];
        let free_head = unsafe { storage.as_mut_ptr().add(1).cast::<u8>() };
        let mut page = ObjectPage {
            counter: 2,
            data: storage.as_mut_ptr().cast::<u8>(),
            ptr: free_head,
            prev: 0,
            next: 0,
        };

        let err = page
            .allocate_all(1, 1, 8)
            .expect_err("corrupt counter must not underflow the returned batch length");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(page.counter, 2);
        assert_eq!(page.ptr, free_head);
    }

    #[test]
    fn efficient_object_page_allocate_all_rejects_missing_partial_freelist() {
        let mut storage = [0usize; 4];
        let mut page = ObjectPage {
            counter: 1,
            data: storage.as_mut_ptr().cast::<u8>(),
            ptr: core::ptr::null_mut(),
            prev: 0,
            next: 0,
        };

        let err = page
            .allocate_all(1, 2, 8)
            .expect_err("partially allocated page needs a free-list head");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(page.counter, 1);
        assert!(page.ptr.is_null());
    }

    #[test]
    fn efficient_object_page_allocate_all_rejects_short_partial_freelist_without_mutation() {
        let mut storage = [0usize; 4];
        let base = storage.as_mut_ptr() as usize;
        storage[1] = 0;
        let mut page = ObjectPage {
            counter: 1,
            data: base as *mut u8,
            ptr: unsafe { storage.as_mut_ptr().add(1).cast::<u8>() },
            prev: 0,
            next: 0,
        };

        let err = page
            .allocate_all(1, 4, core::mem::size_of::<usize>())
            .expect_err("partial freelist shorter than pg_count-counter must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(page.counter, 1);
        assert_eq!(page.ptr as usize, base + core::mem::size_of::<usize>());
        assert_eq!(storage[1], 0);
    }

    #[test]
    fn efficient_object_page_allocate_all_rejects_overlong_partial_freelist_without_mutation() {
        let mut storage = [0usize; 4];
        let base = storage.as_mut_ptr() as usize;
        storage[1] = base + 2 * core::mem::size_of::<usize>();
        storage[2] = base + 3 * core::mem::size_of::<usize>();
        storage[3] = 0;
        let mut page = ObjectPage {
            counter: 2,
            data: base as *mut u8,
            ptr: unsafe { storage.as_mut_ptr().add(1).cast::<u8>() },
            prev: 0,
            next: 0,
        };

        let err = page
            .allocate_all(1, 4, core::mem::size_of::<usize>())
            .expect_err("partial freelist longer than pg_count-counter must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EDBFRE.to_raw_errno());
        assert_eq!(page.counter, 2);
        assert_eq!(page.ptr as usize, base + core::mem::size_of::<usize>());
        assert_eq!(storage[1], base + 2 * core::mem::size_of::<usize>());
        assert_eq!(storage[2], base + 3 * core::mem::size_of::<usize>());
        assert_eq!(storage[3], 0);
    }

    #[test]
    fn efficient_object_page_allocate_all_rejects_object_span_past_page_without_mutation() {
        let mut storage = [0xfeedusize; 4];
        let before = storage;
        let old_free = 0x55usize as *mut u8;
        let mut page = ObjectPage {
            counter: 0,
            data: storage.as_mut_ptr().cast::<u8>(),
            ptr: old_free,
            prev: 0,
            next: 0,
        };

        let err = page
            .allocate_all(1, 2, crate::PAGE_SIZE)
            .expect_err("object span larger than backing page span must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::ELAYOUT.to_raw_errno());
        assert_eq!(page.counter, 0);
        assert_eq!(page.ptr, old_free);
        assert_eq!(storage, before);
    }

    #[test]
    fn efficient_object_page_allocate_all_rejects_object_span_overflow_without_mutation() {
        let mut storage = [0xfeedusize; 4];
        let before = storage;
        let old_free = 0x66usize as *mut u8;
        let mut page = ObjectPage {
            counter: 0,
            data: storage.as_mut_ptr().cast::<u8>(),
            ptr: old_free,
            prev: 0,
            next: 0,
        };

        let err = page
            .allocate_all(usize::MAX, usize::MAX, 2)
            .expect_err("object span multiplication overflow must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(page.counter, 0);
        assert_eq!(page.ptr, old_free);
        assert_eq!(storage, before);
    }

    #[test]
    fn efficient_object_page_aligned_batch_rejects_object_span_past_page_without_mutation() {
        let mut storage = [0xfeedusize; 4];
        let before = storage;
        let mut page = ObjectPage {
            counter: 0,
            data: storage.as_mut_ptr().cast::<u8>(),
            ptr: core::ptr::null_mut(),
            prev: 0,
            next: 0,
        };

        let err = page
            .allocate_aligned_batch(1, 1, 2, crate::PAGE_SIZE)
            .expect_err("aligned allocation must validate object span before writing links");

        assert_eq!(err.to_raw_errno(), AllocError::ELAYOUT.to_raw_errno());
        assert_eq!(page.counter, 0);
        assert!(page.ptr.is_null());
        assert_eq!(storage, before);
    }

    #[test]
    fn efficient_object_page_first_aligned_index_scans_one_alignment_period() {
        let base = 0x1008usize;
        assert_eq!(
            ObjectPage::first_aligned_object_index(base, 1024, 8, 64)
                .expect("valid alignment geometry"),
            Some(7),
            "8-byte slots starting at +8 repeat every eight slots for 64-byte alignment"
        );
        assert_eq!(
            ObjectPage::first_aligned_object_index(base, 7, 8, 64)
                .expect("valid alignment geometry"),
            None,
            "the first aligned slot is outside the truncated object page"
        );
    }

    #[test]
    fn efficient_object_page_first_aligned_index_rejects_unsatisfiable_modulus() {
        assert_eq!(
            ObjectPage::first_aligned_object_index(0x1004, 1024, 8, 64)
                .expect("valid but unsatisfiable alignment geometry"),
            None,
            "base and stride gcd do not permit any 64-byte aligned slot"
        );
    }

    #[test]
    fn efficient_object_page_aligned_batch_rejects_no_matching_empty_page_without_mutation() {
        let mut storage = [0xfeedusize; 16];
        let base = storage.as_mut_ptr() as usize;
        let stride = core::mem::size_of::<usize>();
        let align = 64usize;
        let object_count = 4usize;
        let start_idx = (0..=storage.len() - object_count)
            .find(|start| {
                (0..object_count).all(|idx| (base + (start + idx) * stride) & (align - 1) != 0)
            })
            .expect("test storage must contain a run without a 64-byte aligned slot");
        let data = unsafe { storage.as_mut_ptr().add(start_idx).cast::<u8>() };
        let before = storage;
        let mut page = ObjectPage {
            counter: 0,
            data,
            ptr: core::ptr::null_mut(),
            prev: 0,
            next: 0,
        };

        let err = page
            .allocate_aligned_batch(align, 1, object_count, stride)
            .expect_err("empty page with no aligned slot should fail before linking objects");

        assert_eq!(err.to_raw_errno(), AllocError::ENOMEM.to_raw_errno());
        assert_eq!(page.counter, 0);
        assert!(page.ptr.is_null());
        assert_eq!(storage, before);
    }

    #[test]
    fn efficient_object_page_aligned_batch_filters_empty_page_without_wasting_slots() {
        let mut storage = Align16Words([0usize; 4]);
        let base = storage.0.as_mut_ptr() as usize;
        assert_eq!(base & 15, 0);
        let mut page = ObjectPage {
            counter: 0,
            data: base as *mut u8,
            ptr: core::ptr::null_mut(),
            prev: 0,
            next: 0,
        };

        let (head, count, stride) = page
            .allocate_aligned_batch(16, 1, 4, core::mem::size_of::<usize>())
            .expect("aligned filtered allocation should succeed");

        assert_eq!(head as usize, base);
        assert_eq!(count, 2);
        assert_eq!(stride, None);
        assert_eq!(page.counter, 2);
        assert_eq!(read_link(base), base + 16);
        assert_eq!(read_link(base + 16), 0);
        assert_eq!(page.ptr as usize, base + 8);
        assert_eq!(read_link(base + 8), base + 24);
        assert_eq!(read_link(base + 24), 0);
    }

    #[test]
    fn efficient_object_page_strict_alignment_caps_empty_page_batch() {
        let mut storage = Align16Many([0usize; 128]);
        let base = storage.0.as_mut_ptr() as usize;
        assert_eq!(base & 15, 0);
        assert!(
            STRICT_ALIGNMENT_BATCH_RETURN_LIMIT < storage.0.len() / 2,
            "test needs more aligned slots than the strict-alignment batch cap"
        );
        let mut page = ObjectPage {
            counter: 0,
            data: base as *mut u8,
            ptr: core::ptr::null_mut(),
            prev: 0,
            next: 0,
        };

        let (head, count, stride) = page
            .allocate_aligned_batch(16, 1, storage.0.len(), core::mem::size_of::<usize>())
            .expect("strict-alignment allocation from an empty page should succeed");

        assert_eq!(head as usize, base);
        assert_eq!(count, STRICT_ALIGNMENT_BATCH_RETURN_LIMIT);
        assert_eq!(stride, None);
        assert_eq!(page.counter, STRICT_ALIGNMENT_BATCH_RETURN_LIMIT);
        assert_eq!(
            count_link_chain(head as usize, STRICT_ALIGNMENT_BATCH_RETURN_LIMIT),
            STRICT_ALIGNMENT_BATCH_RETURN_LIMIT,
            "caller batch should contain only the capped aligned prefix"
        );
        assert_eq!(
            count_link_chain(page.ptr as usize, storage.0.len()),
            storage.0.len() - STRICT_ALIGNMENT_BATCH_RETURN_LIMIT,
            "remaining aligned slots plus lower-aligned slots must stay on the page freelist"
        );
        assert_eq!(
            page.ptr as usize,
            base + core::mem::size_of::<usize>(),
            "the first skipped lower-aligned slot remains immediately reusable by the slab"
        );
    }

    #[test]
    fn efficient_object_page_strict_alignment_cap_is_page_payload_adaptive() {
        assert_eq!(
            ObjectPage::strict_alignment_batch_return_limit(core::mem::size_of::<usize>()),
            STRICT_ALIGNMENT_BATCH_RETURN_LIMIT,
            "tiny slots should keep the old object-count amortization cap"
        );
        assert_eq!(
            ObjectPage::strict_alignment_batch_return_limit(crate::PAGE_SIZE / 4),
            4,
            "large slots should not donate more than one page of payload to one cache"
        );
        assert_eq!(
            ObjectPage::strict_alignment_batch_return_limit(crate::PAGE_SIZE * 2),
            1,
            "oversized/multi-page objects should keep at least one strict-alignment match"
        );
    }

    #[test]
    fn efficient_object_page_strict_alignment_multi_page_batch_uses_adaptive_cap() {
        #[repr(align(65536))]
        struct PageishWords([usize; 65536]);

        let mut storage = PageishWords([0usize; 65536]);
        let base = storage.0.as_mut_ptr() as usize;
        let stride = crate::PAGE_SIZE / 16;
        let align = stride * 2;
        let storage_bytes = core::mem::size_of_val(&storage.0);
        let object_count = storage_bytes / stride;
        let pg_num = (object_count * stride + crate::PAGE_SIZE - 1) / crate::PAGE_SIZE;
        let adaptive_limit = ObjectPage::strict_alignment_batch_return_limit(stride);
        assert_eq!(adaptive_limit, 16);
        assert!(
            object_count / 2 > STRICT_ALIGNMENT_BATCH_RETURN_LIMIT,
            "test needs enough strictly aligned slots to prove the adaptive cap is tighter"
        );
        assert_eq!(base & (align - 1), 0);

        let mut page = ObjectPage {
            counter: 0,
            data: base as *mut u8,
            ptr: core::ptr::null_mut(),
            prev: 0,
            next: 0,
        };

        let (head, count, returned_stride) = page
            .allocate_aligned_batch(align, pg_num, object_count, stride)
            .expect("multi-page strict-alignment allocation should succeed");

        assert_eq!(head as usize, base);
        assert_eq!(count, adaptive_limit);
        assert_eq!(returned_stride, None);
        assert_eq!(page.counter, count);
        assert_eq!(count_link_chain(head as usize, object_count), count);
        assert_eq!(
            count_link_chain(page.ptr as usize, object_count),
            object_count - count,
            "adaptive cap should leave most matching multi-page slots on the page freelist"
        );
    }

    #[test]
    fn efficient_object_page_strict_alignment_caps_partial_freelist_batch() {
        let mut storage = Align16Many([0usize; 128]);
        let base = storage.0.as_mut_ptr() as usize;
        let stride = core::mem::size_of::<usize>();
        assert_eq!(base & 15, 0);
        link_object_range(base, storage.0.len(), stride);
        let mut page = ObjectPage {
            counter: 0,
            data: base as *mut u8,
            ptr: base as *mut u8,
            prev: 0,
            next: 0,
        };

        let (head, count, returned_stride) = page
            .allocate_aligned_batch(16, 1, storage.0.len(), stride)
            .expect("strict-alignment allocation from a partial freelist should succeed");

        assert_eq!(head as usize, base);
        assert_eq!(count, STRICT_ALIGNMENT_BATCH_RETURN_LIMIT);
        assert_eq!(returned_stride, None);
        assert_eq!(page.counter, STRICT_ALIGNMENT_BATCH_RETURN_LIMIT);
        assert_eq!(
            count_link_chain(head as usize, STRICT_ALIGNMENT_BATCH_RETURN_LIMIT),
            STRICT_ALIGNMENT_BATCH_RETURN_LIMIT
        );
        assert_eq!(
            count_link_chain(page.ptr as usize, storage.0.len()),
            storage.0.len() - STRICT_ALIGNMENT_BATCH_RETURN_LIMIT,
            "aligned slots beyond the cap must be restored to the freelist with skipped slots"
        );
    }

    #[test]
    fn efficient_object_page_aligned_batch_no_match_keeps_empty_page_unmodified() {
        let mut storage = [0xfeed_cafeusize; 64];
        let storage_base = storage.as_mut_ptr() as usize;
        let align = 128usize;
        let object_count = 4usize;
        let pg_align = core::mem::size_of::<usize>();
        let mut start_idx = None;
        for idx in 0..=(storage.len() - object_count) {
            let base = storage_base + idx * pg_align;
            let any_aligned = (0..object_count)
                .any(|object_idx| (base + object_idx * pg_align) & (align - 1) == 0);
            if !any_aligned {
                start_idx = Some(idx);
                break;
            }
        }
        let start_idx = start_idx.expect("test storage must contain a non-128-aligned 4-word run");
        let base = unsafe { storage.as_mut_ptr().add(start_idx) as usize };
        let before = storage;
        let mut page = ObjectPage {
            counter: 0,
            data: base as *mut u8,
            ptr: core::ptr::null_mut(),
            prev: 0,
            next: 0,
        };

        let err = page
            .allocate_aligned_batch(align, 1, object_count, pg_align)
            .expect_err("no object in the chosen empty page window matches this alignment");

        assert_eq!(err.to_raw_errno(), AllocError::ENOMEM.to_raw_errno());
        assert_eq!(page.counter, 0);
        assert!(page.ptr.is_null());
        assert_eq!(storage, before);
    }

    #[test]
    fn efficient_object_page_aligned_batch_reuses_only_matching_partial_freelist_nodes() {
        let mut storage = Align16Words([0usize; 4]);
        let base = storage.0.as_mut_ptr() as usize;
        assert_eq!(base & 15, 0);
        write_link(base + core::mem::size_of::<usize>(), base + 16);
        write_link(base + 2 * core::mem::size_of::<usize>(), base + 24);
        write_link(base + 3 * core::mem::size_of::<usize>(), 0);
        let mut page = ObjectPage {
            counter: 1,
            data: base as *mut u8,
            ptr: (base + 8) as *mut u8,
            prev: 0,
            next: 0,
        };

        let (head, count, stride) = page
            .allocate_aligned_batch(16, 1, 4, core::mem::size_of::<usize>())
            .expect("partial free list should return only aligned nodes");

        assert_eq!(head as usize, base + 16);
        assert_eq!(count, 1);
        assert_eq!(stride, None);
        assert_eq!(read_link(base + 16), 0);
        assert_eq!(page.counter, 2);
        assert_eq!(page.ptr as usize, base + 8);
        assert_eq!(read_link(base + 8), base + 24);
        assert_eq!(read_link(base + 24), 0);
    }

    #[test]
    fn efficient_object_page_aligned_batch_no_partial_match_keeps_freelist_unmodified() {
        let mut storage = Align16Words([0usize; 4]);
        let base = storage.0.as_mut_ptr() as usize;
        assert_eq!(base & 15, 0);
        write_link(base + core::mem::size_of::<usize>(), base + 24);
        write_link(base + 3 * core::mem::size_of::<usize>(), 0);
        let mut page = ObjectPage {
            counter: 2,
            data: base as *mut u8,
            ptr: (base + 8) as *mut u8,
            prev: 0,
            next: 0,
        };

        let err = page
            .allocate_aligned_batch(16, 1, 4, core::mem::size_of::<usize>())
            .expect_err("partial freelist contains no nodes satisfying the requested alignment");

        assert_eq!(err.to_raw_errno(), AllocError::ENOMEM.to_raw_errno());
        assert_eq!(page.counter, 2);
        assert_eq!(page.ptr as usize, base + 8);
        assert_eq!(read_link(base + 8), base + 24);
        assert_eq!(read_link(base + 24), 0);
    }

    #[test]
    fn efficient_object_page_aligned_batch_rejects_duplicate_partial_node_without_mutation() {
        let mut storage = Align16Words([0usize; 4]);
        let base = storage.0.as_mut_ptr() as usize;
        assert_eq!(base & 15, 0);
        write_link(base + core::mem::size_of::<usize>(), base + 8);
        let mut page = ObjectPage {
            counter: 2,
            data: base as *mut u8,
            ptr: (base + 8) as *mut u8,
            prev: 0,
            next: 0,
        };

        let err = page
            .allocate_aligned_batch(1, 1, 4, core::mem::size_of::<usize>())
            .expect_err("duplicate partial freelist node must fail before relinking");

        assert_eq!(err.to_raw_errno(), AllocError::EDBFRE.to_raw_errno());
        assert_eq!(page.counter, 2);
        assert_eq!(page.ptr as usize, base + 8);
        assert_eq!(read_link(base + 8), base + 8);
    }

    #[test]
    fn efficient_object_page_aligned_batch_rejects_short_partial_freelist_without_mutation() {
        let mut storage = Align16Words([0usize; 4]);
        let base = storage.0.as_mut_ptr() as usize;
        storage.0[1] = 0;
        let mut page = ObjectPage {
            counter: 1,
            data: base as *mut u8,
            ptr: (base + 8) as *mut u8,
            prev: 0,
            next: 0,
        };

        let err = page
            .allocate_aligned_batch(1, 1, 4, core::mem::size_of::<usize>())
            .expect_err("partial freelist shorter than counter-derived free count is corrupt");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(page.counter, 1);
        assert_eq!(page.ptr as usize, base + 8);
        assert_eq!(storage.0[1], 0);
    }

    #[test]
    fn efficient_object_page_aligned_batch_rejects_overlong_partial_freelist_without_mutation() {
        let mut storage = Align16Words([0usize; 4]);
        let base = storage.0.as_mut_ptr() as usize;
        storage.0[1] = base + 16;
        storage.0[2] = 0;
        let mut page = ObjectPage {
            counter: 3,
            data: base as *mut u8,
            ptr: (base + 8) as *mut u8,
            prev: 0,
            next: 0,
        };

        let err = page
            .allocate_aligned_batch(1, 1, 4, core::mem::size_of::<usize>())
            .expect_err("partial freelist longer than counter-derived free count is corrupt");

        assert_eq!(err.to_raw_errno(), AllocError::EDBFRE.to_raw_errno());
        assert_eq!(page.counter, 3);
        assert_eq!(page.ptr as usize, base + 8);
        assert_eq!(storage.0[1], base + 16);
        assert_eq!(storage.0[2], 0);
    }

    #[test]
    fn efficient_object_page_deallocate_rejects_uninitialized_page() {
        let mut page = ObjectPage::new();
        let err = page
            .deallocate(0x1000, 1, 1, core::mem::size_of::<usize>())
            .expect_err("uninitialized page should reject dealloc");
        assert_eq!(err.to_raw_errno(), AllocError::EUAF.to_raw_errno());
    }

    #[test]
    fn efficient_object_page_deallocate_rejects_misaligned_head_without_mutation() {
        let mut storage = [0usize; 4];
        let base = storage.as_mut_ptr() as usize;
        let old_free = 0x55usize as *mut u8;
        let mut page = ObjectPage {
            counter: 1,
            data: base as *mut u8,
            ptr: old_free,
            prev: 0,
            next: 0,
        };

        let err = page
            .deallocate(base + 1, 1, 1, core::mem::size_of::<usize>())
            .expect_err("misaligned efficient-page head must not enter free list");

        assert_eq!(err.to_raw_errno(), AllocError::EOOB.to_raw_errno());
        assert_eq!(page.ptr, old_free);
        assert_eq!(page.counter, 1);
    }

    #[test]
    fn efficient_object_page_deallocate_rejects_spare_slot_beyond_pg_count_without_mutation() {
        let mut storage = [0usize; 4];
        let base = storage.as_mut_ptr() as usize;
        let spare = unsafe { storage.as_mut_ptr().add(1) as usize };
        let old_free = 0x66usize as *mut u8;
        let mut page = ObjectPage {
            counter: 1,
            data: base as *mut u8,
            ptr: old_free,
            prev: 0,
            next: 0,
        };

        let err = page
            .deallocate(spare, 1, 1, core::mem::size_of::<usize>())
            .expect_err("spare address inside page span is not an allocated object");

        assert_eq!(err.to_raw_errno(), AllocError::EOOB.to_raw_errno());
        assert_eq!(page.ptr, old_free);
        assert_eq!(page.counter, 1);
    }

    #[test]
    fn efficient_object_page_deallocate_rejects_misaligned_in_page_chain_without_mutation() {
        let mut storage = [0usize; 4];
        let base = storage.as_mut_ptr() as usize;
        storage[0] = base + 1;

        let old_free = 0x77usize as *mut u8;
        let mut page = ObjectPage {
            counter: 2,
            data: base as *mut u8,
            ptr: old_free,
            prev: 0,
            next: 0,
        };

        let err = page
            .deallocate(base, 1, 2, core::mem::size_of::<usize>())
            .expect_err("misaligned next pointer in same page must fail before mutation");
        assert_eq!(err.to_raw_errno(), AllocError::EOOB.to_raw_errno());
        assert_eq!(storage[0], base + 1);
        assert_eq!(page.ptr, old_free);
        assert_eq!(page.counter, 2);
    }

    #[test]
    fn efficient_object_page_deallocate_rejects_in_page_cycle_without_mutation() {
        let mut storage = [0usize; 4];
        let base = storage.as_mut_ptr() as usize;
        storage[0] = base;

        let old_free = 0x55usize as *mut u8;
        let mut page = ObjectPage {
            counter: 1,
            data: base as *mut u8,
            ptr: old_free,
            prev: 0,
            next: 0,
        };

        let err = page
            .deallocate(base, 1, 1, core::mem::size_of::<usize>())
            .expect_err("cycle in same object page must fail closed");
        assert_eq!(err.to_raw_errno(), AllocError::EDBFRE.to_raw_errno());
        assert_eq!(storage[0], base);
        assert_eq!(page.ptr, old_free);
        assert_eq!(page.counter, 1);
    }

    #[test]
    fn efficient_object_page_deallocate_rejects_overlong_chain_without_mutation() {
        let mut storage = [0usize; 4];
        let base = storage.as_mut_ptr() as usize;
        let second = unsafe { storage.as_mut_ptr().add(1) as usize };
        storage[0] = second;
        storage[1] = base + crate::PAGE_SIZE;

        let old_free = 0x77usize as *mut u8;
        let mut page = ObjectPage {
            counter: 1,
            data: base as *mut u8,
            ptr: old_free,
            prev: 0,
            next: 0,
        };

        let err = page
            .deallocate(base, 1, 2, core::mem::size_of::<usize>())
            .expect_err("chain longer than live counter must fail closed");
        assert_eq!(err.to_raw_errno(), AllocError::EDBFRE.to_raw_errno());
        assert_eq!(storage[0], second);
        assert_eq!(storage[1], base + crate::PAGE_SIZE);
        assert_eq!(page.ptr, old_free);
        assert_eq!(page.counter, 1);
    }

    #[test]
    fn efficient_object_page_deallocate_rejects_already_free_object_without_mutation() {
        let mut storage = [0usize; 4];
        let base = storage.as_mut_ptr() as usize;
        storage[0] = 0;
        let mut page = ObjectPage {
            counter: 1,
            data: base as *mut u8,
            ptr: base as *mut u8,
            prev: 0,
            next: 0,
        };

        let err = page
            .deallocate(base, 1, 2, core::mem::size_of::<usize>())
            .expect_err("object already present in freelist is a double-free");

        assert_eq!(err.to_raw_errno(), AllocError::EDBFRE.to_raw_errno());
        assert_eq!(storage[0], 0);
        assert_eq!(page.ptr as usize, base);
        assert_eq!(page.counter, 1);
    }

    #[test]
    fn efficient_object_page_deallocate_validation_fills_overlap_bitmap_once() {
        let mut storage = [0usize; 4];
        let base = storage.as_mut_ptr() as usize;
        write_link(base, base + core::mem::size_of::<usize>());
        write_link(
            base + core::mem::size_of::<usize>(),
            base + crate::PAGE_SIZE,
        );
        write_link(
            base + 2 * core::mem::size_of::<usize>(),
            base + 3 * core::mem::size_of::<usize>(),
        );
        write_link(base + 3 * core::mem::size_of::<usize>(), 0);
        let page = ObjectPage {
            counter: 2,
            data: base as *mut u8,
            ptr: (base + 2 * core::mem::size_of::<usize>()) as *mut u8,
            prev: 0,
            next: 0,
        };
        let mut batch_bitmap = [0usize; MAX_PAGE_OBJECT_BITMAP_WORDS];
        let bitmap_words = ObjectPage::object_bitmap_word_count(4)
            .expect("small object page should have a stack bitmap");

        let (tail, count, next) = page
            .validate_deallocation_batch(
                base,
                1,
                4,
                core::mem::size_of::<usize>(),
                Some(&mut batch_bitmap[..bitmap_words]),
            )
            .expect("valid two-object batch should validate");

        assert_eq!(tail, base + core::mem::size_of::<usize>());
        assert_eq!(count, 2);
        assert_eq!(next, base + crate::PAGE_SIZE);
        assert!(ObjectPage::object_bitmap_contains(&batch_bitmap[..bitmap_words], 0).unwrap());
        assert!(ObjectPage::object_bitmap_contains(&batch_bitmap[..bitmap_words], 1).unwrap());
        assert!(!ObjectPage::object_bitmap_contains(&batch_bitmap[..bitmap_words], 2).unwrap());
        page.reject_prevalidated_batch_overlap_with_existing_freelist(
            base,
            count,
            1,
            4,
            core::mem::size_of::<usize>(),
            Some(&batch_bitmap[..bitmap_words]),
        )
        .expect("prevalidated batch bitmap should reject no-overlap freelist in one pass");

        assert_eq!(page.counter, 2);
        assert_eq!(
            page.ptr as usize,
            base + 2 * core::mem::size_of::<usize>(),
            "validation helpers must not mutate the page before deallocate commits"
        );
    }
}
