//! Feature-selected slab-class backend adapter.
//!
//! UniAlloc's default slab path remains `efficient_sc`; enabling the
//! `separate_sc_backend` Cargo feature switches `crate::sc::SCAllocator` to the
//! bitmap-backed `separate_sc` implementation through this adapter.  The
//! adapter exists because the two backends intentionally expose different
//! low-level APIs: `efficient_sc` stores page metadata in the process-global
//! radix tree and returns either contiguous bump batches or linked lists, while
//! `separate_sc` owns per-size-class page descriptors and needs an explicit
//! radix map plus array-based batch handoff.
//!
//! The separate backend keeps the same empty-slab retention policy as the
//! default backend, so enabling it is a real allocator backend choice rather
//! than a test-only symbol export.  It can improve internal-fragmentation
//! experiments by using bitmap object pages and separated size-class metadata,
//! while the retained-empty OS-page caps still bound external fragmentation and
//! RSS after bursty workloads.  The feature is opt-in so existing builds keep
//! the default efficient backend and compatibility behavior.

#[cfg(feature = "fixed_heap")]
use crate::collections::radix_tree::allocate_fixed_heap_radix_tree_from_global_shape;
#[cfg(not(feature = "fixed_heap"))]
use crate::collections::radix_tree::allocate_node;
use crate::collections::radix_tree::RadixTree;
use crate::error::{AllocError, Result};
use crate::mm::BackendAllocator as GlobalBackend;
use crate::prelude::*;
use crate::sc::separate_sc;
use crate::*;
use alloc::vec::Vec;
use core::mem::align_of;
use core::ptr::NonNull;

/// Maximum linked objects accepted in one separate-backend deallocation handoff.
///
/// Thread-cache flushes are intentionally bounded before they reach the slab:
/// ordinary soft flushes keep a hot prefix and hard flushes happen around twice
/// that object budget.  This larger adapter-side cap is a defensive boundary
/// for corrupted linked lists (especially cycles) before we stage pointers in a
/// `Vec`; it is not a new steady-state cache size or allocator metadata field.
const SEPARATE_BACKEND_DEALLOCATE_BATCH_OBJECT_LIMIT: usize = 16 * 1024;

/// Largest staging array retained after a separate-backend batch-free handoff.
///
/// The adapter needs a temporary pointer array because `separate_sc` consumes
/// array batches while the zone/thread-cache contract passes linked lists.
/// Keeping enough space for the common thread-cache soft flush avoids churn, but
/// a rare hard flush should not permanently pin a 128KiB `Vec` in every hot size
/// class.  Oversized buffers are released after the slab handoff returns.  The
/// staged array is only pointer metadata: once the backend has either accepted
/// the batch or reported an error, keeping a rare oversized array would only
/// inflate the per-size-class footprint.
const SEPARATE_BACKEND_STAGING_RETAIN_OBJECT_LIMIT: usize = 4 * 1024;
const SEPARATE_BACKEND_BUMP_BITMAP_BITS: usize = core::mem::size_of::<usize>() * 8;
/// Smallest slot stride the adapter can safely represent in a bump batch.
///
/// The deallocation contract writes a `usize` next pointer into cached objects,
/// so a valid linked/bump object slot cannot be smaller than one machine word.
/// `batch_request_count` also caps natural batches to one page payload, meaning
/// the contiguous-bump detector only needs one bit per possible object slot,
/// not one bit per byte of `PAGE_SIZE`.
const SEPARATE_BACKEND_BUMP_BITMAP_MIN_STRIDE: usize = core::mem::size_of::<usize>();
const SEPARATE_BACKEND_BUMP_BITMAP_MAX_SLOTS: usize =
    PAGE_SIZE / SEPARATE_BACKEND_BUMP_BITMAP_MIN_STRIDE;
const SEPARATE_BACKEND_BUMP_BITMAP_WORDS: usize =
    (SEPARATE_BACKEND_BUMP_BITMAP_MAX_SLOTS + SEPARATE_BACKEND_BUMP_BITMAP_BITS - 1)
        / SEPARATE_BACKEND_BUMP_BITMAP_BITS;

/// Zone-facing slab allocator backed by `separate_sc`.
///
/// `zone` and thread-cache code use the historical efficient-backend contract:
/// `allocate_batch_v2(align) -> (head, count, linked-or-bump)` and
/// `deallocate_batch(linked_head)`.  This wrapper translates that contract to
/// the separate backend's explicit `(ptr_map, array, count, align)` calls.
pub struct SCAllocator {
    inner: separate_sc::SCAllocator,
    ptr_map: *mut RadixTree,
    batch: Vec<usize, GlobalBackend>,
    batch_capacity: usize,
    batch_stride: usize,
}

impl SCAllocator {
    pub fn new(size_class: usize, num_os_pages: usize) -> Self {
        let (batch_capacity, batch_stride) =
            match super::checked_size_class_geometry(size_class, num_os_pages) {
                Some(geometry) => geometry,
                // Keep the adapter in the same inert state as
                // `separate_sc::SCAllocator::empty`: future allocation attempts
                // fail with `ESIZE` instead of constructing partially valid
                // metadata for an invalid size-class geometry.
                None => (0, 0),
            };
        Self {
            inner: separate_sc::SCAllocator::new(size_class, num_os_pages),
            ptr_map: core::ptr::null_mut(),
            batch: Vec::with_capacity_in(0, GlobalBackend),
            batch_capacity,
            batch_stride,
        }
    }

    fn ensure_ptr_map(&mut self) -> Result<*mut RadixTree> {
        if self.ptr_map.is_null() {
            #[cfg(feature = "fixed_heap")]
            {
                self.ptr_map = allocate_fixed_heap_radix_tree_from_global_shape();
            }
            #[cfg(not(feature = "fixed_heap"))]
            {
                self.ptr_map = allocate_node::<RadixTree>();
            }
            if self.ptr_map.is_null() {
                return Err(AllocError::ENOMEM);
            }
        }
        Ok(self.ptr_map)
    }

    fn ensure_batch_len(&mut self, len: usize) -> Result<()> {
        if len == 0 {
            return Err(AllocError::ESIZE);
        }
        if self.batch.len() < len {
            self.batch
                .try_reserve_exact(len - self.batch.len())
                .map_err(|_| AllocError::ENOMEM)?;
            self.batch.resize(len, 0);
        }
        Ok(())
    }

    fn release_oversized_batch_staging_after_handoff(&mut self) {
        if self.batch.capacity() > SEPARATE_BACKEND_STAGING_RETAIN_OBJECT_LIMIT {
            self.batch = Vec::with_capacity_in(0, GlobalBackend);
        }
    }

    #[inline]
    fn batch_request_count(&self, align: usize) -> Result<usize> {
        if align == 0 || !align.is_power_of_two() || align > PAGE_SIZE || self.batch_capacity == 0 {
            return Err(AllocError::ESIZE);
        }
        let page_payload_limit = if self.batch_stride == 0 {
            1
        } else {
            (PAGE_SIZE / self.batch_stride).max(1)
        };

        if align <= self.batch_stride {
            // `separate_sc` may back tiny classes with multiple independent
            // object pages.  Returning all of them as one thread-cache batch
            // forces a linked-list representation, so cold tail objects count
            // as local free-list nodes and can trigger retention flushes too
            // early.  Cap natural-alignment batches to one page payload: the
            // adapter can then expose the result as a compact bump tail, which
            // preserves the historical thread-cache accounting while reducing
            // cross-page private retention.
            return Ok(core::cmp::min(self.batch_capacity, page_payload_limit));
        }

        if align == PAGE_SIZE {
            // A page-aligned tiny-object result consumes at most one useful slot
            // per object page.  Caching a strict batch here would let one thread
            // retain dozens of mostly-empty pages, so return exactly the object
            // requested and leave the remaining aligned slots on the slab.
            return Ok(1);
        }

        let object_limit = crate::page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT.max(1);
        Ok(core::cmp::min(
            self.batch_capacity,
            core::cmp::min(object_limit, page_payload_limit),
        ))
    }

    fn batch_has_contiguous_bump(
        batch: &[usize],
        head: NonNull<u8>,
        count: usize,
        stride: usize,
    ) -> bool {
        if count == 0 || count > batch.len() || stride == 0 {
            return false;
        }
        let bitmap_words =
            (count + SEPARATE_BACKEND_BUMP_BITMAP_BITS - 1) / SEPARATE_BACKEND_BUMP_BITMAP_BITS;
        if bitmap_words > SEPARATE_BACKEND_BUMP_BITMAP_WORDS {
            return false;
        }

        let head_addr = head.as_ptr() as usize;
        let mut seen = [0usize; SEPARATE_BACKEND_BUMP_BITMAP_WORDS];
        for &ptr in &batch[..count] {
            let delta = match ptr.checked_sub(head_addr) {
                Some(delta) => delta,
                None => return false,
            };
            if delta % stride != 0 {
                return false;
            }
            let slot = delta / stride;
            if slot >= count {
                return false;
            }
            let word_idx = slot / SEPARATE_BACKEND_BUMP_BITMAP_BITS;
            let bit = 1usize << (slot % SEPARATE_BACKEND_BUMP_BITMAP_BITS);
            let word = &mut seen[word_idx];
            if *word & bit != 0 {
                return false;
            }
            *word |= bit;
        }
        true
    }

    fn link_backend_batch(batch: &mut [usize], head: NonNull<u8>, count: usize) -> Result<()> {
        if count == 0 || count > batch.len() {
            return Err(AllocError::ESIZE);
        }
        let head_addr = head.as_ptr() as usize;
        let head_pos = batch[..count]
            .iter()
            .position(|&ptr| ptr == head_addr)
            .ok_or(AllocError::EFATAL)?;
        batch.swap(0, head_pos);
        for idx in 0..count - 1 {
            let next = batch[idx + 1];
            unsafe { *(batch[idx] as *mut usize) = next };
        }
        unsafe { *(batch[count - 1] as *mut usize) = 0 };
        Ok(())
    }

    /// Allocate a thread-cache batch using the separate bitmap backend.
    ///
    /// The adapter returns a normal contiguous bump batch when the separate
    /// backend produced a full contiguous span.  For sparse strict-alignment
    /// results it returns a linked-list batch (`None` stride), preserving the
    /// exact pointer set instead of pretending skipped bitmap slots are part of
    /// the allocation.  That avoids internal-fragmentation regressions from
    /// hidden skipped slots.
    pub fn allocate_batch_v2(&mut self, align: usize) -> Result<(*mut u8, usize, Option<usize>)> {
        let count = self.batch_request_count(align)?;
        self.ensure_batch_len(count)?;
        self.batch[..count].fill(0);

        let ptr_map = self.ensure_ptr_map()?;
        let head = self.inner.allocate_batch_v2(
            unsafe { ptr_map.as_mut().ok_or(AllocError::ENOMEM)? },
            &mut self.batch[..count],
            count,
            align,
        )?;
        if Self::batch_has_contiguous_bump(&self.batch[..count], head, count, self.batch_stride) {
            return Ok((head.as_ptr(), count, Some(self.batch_stride)));
        }

        if align <= self.batch_stride {
            // Natural-alignment callers can consume any object.  If dirty
            // partial pages make the result non-contiguous, keep ownership
            // transactional by returning the whole bounded one-page payload as
            // a linked batch.  The older tail-shrink path allocated the whole
            // batch and then tried to deallocate the surplus tail from inside
            // the adapter; a later slab transition failure could leave the
            // caller with an error after some objects had already moved back to
            // the slab and others remained allocated.  Returning the exact
            // pointer set avoids that split ownership state.  Clean pages still
            // use the compact bump representation above, so the common hot path
            // preserves low per-thread metadata and fast refill accounting.
            Self::link_backend_batch(&mut self.batch[..count], head, count)?;
            return Ok((head.as_ptr(), count, None));
        }

        Self::link_backend_batch(&mut self.batch[..count], head, count)?;
        Ok((head.as_ptr(), count, None))
    }

    #[inline]
    fn read_link_checked(ptr: usize) -> Result<usize> {
        if ptr == 0 || ptr % align_of::<usize>() != 0 {
            return Err(AllocError::ESIZE);
        }
        let next = unsafe { *(ptr as *const usize) };
        if next != 0 && next % align_of::<usize>() != 0 {
            return Err(AllocError::ESIZE);
        }
        Ok(next)
    }

    /// Count a linked deallocation batch before staging it in `self.batch`.
    ///
    /// The efficient backend consumes a linked list directly, but the separate
    /// bitmap backend needs an array.  Measuring first lets us fail closed on a
    /// cycle or implausibly long chain without growing the staging `Vec` one
    /// element at a time on corrupted input.
    fn measure_deallocation_batch(head: usize) -> Result<usize> {
        let mut count = 0usize;
        let mut cur = head;
        let mut slow = head;
        let mut fast = head;

        while cur != 0 {
            if count >= SEPARATE_BACKEND_DEALLOCATE_BATCH_OBJECT_LIMIT {
                return Err(AllocError::ESIZE);
            }
            count += 1;
            cur = Self::read_link_checked(cur)?;

            if fast != 0 {
                fast = Self::read_link_checked(fast)?;
                if fast != 0 {
                    fast = Self::read_link_checked(fast)?;
                    slow = Self::read_link_checked(slow)?;
                    if fast != 0 && fast == slow {
                        return Err(AllocError::ESIZE);
                    }
                }
            }
        }

        Ok(count)
    }

    pub fn deallocate_batch(&mut self, ptr: *mut usize) -> Result<()> {
        if ptr.is_null() {
            return Ok(());
        }

        let count = Self::measure_deallocation_batch(ptr as usize)?;
        self.ensure_batch_len(count)?;

        let result = {
            let mut cur = ptr as usize;
            for slot in &mut self.batch[..count] {
                *slot = cur;
                cur = Self::read_link_checked(cur)?;
            }

            let ptr_map = self.ensure_ptr_map()?;
            self.inner.deallocate_batch_promoting_batch_head(
                &mut self.batch[..count],
                count,
                unsafe { ptr_map.as_mut().ok_or(AllocError::ENOMEM)? },
            )
        };
        self.release_oversized_batch_staging_after_handoff();
        result
    }

    pub(crate) fn retained_empty_slab_os_pages(&self) -> usize {
        self.inner.retained_empty_slab_os_pages()
    }

    #[cfg(test)]
    pub(crate) fn retained_empty_slab_count_for_tests(&self) -> usize {
        self.inner.retained_empty_slab_count_for_tests()
    }

    pub(crate) fn recycle_one_retained_empty_slab(&mut self) -> Result<bool> {
        let ptr_map = self.ensure_ptr_map()?;
        self.inner
            .recycle_one_retained_empty_slab(unsafe { ptr_map.as_mut().ok_or(AllocError::ENOMEM)? })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn separate_backend_adapter_allocates_and_deallocates_real_batch() {
        let mut alloc = SCAllocator::new(64, 1);
        let (head, count, stride) = alloc
            .allocate_batch_v2(8)
            .expect("separate backend batch allocation should succeed");
        assert!(!head.is_null());
        assert!(count > 1);
        let linked_head = head as *mut usize;
        if let Some(stride) = stride {
            assert_eq!(stride, 64);
            for idx in 0..count - 1 {
                unsafe {
                    *((head as usize + idx * stride) as *mut usize) =
                        head as usize + (idx + 1) * stride;
                }
            }
            unsafe { *((head as usize + (count - 1) * stride) as *mut usize) = 0 };
        } else {
            let mut seen = 0usize;
            let mut cur = linked_head;
            while !cur.is_null() {
                seen += 1;
                cur = unsafe { *cur as *mut usize };
            }
            assert_eq!(seen, count);
        }

        alloc
            .deallocate_batch(linked_head)
            .expect("separate backend batch deallocation should succeed");
        assert!(alloc.retained_empty_slab_os_pages() <= 1);
    }

    #[cfg(feature = "metadata_segregation")]
    #[test]
    fn metadata_segregation_feature_selects_bitmap_backend_adapter() {
        let mut alloc = SCAllocator::new(64, 1);
        let (head, count, stride) = alloc
            .allocate_batch_v2(core::mem::align_of::<usize>())
            .expect("metadata_segregation should use the bitmap-backed backend");

        assert!(!head.is_null());
        assert!(count > 1);
        assert_eq!(
            stride,
            Some(64),
            "natural metadata-segregation batches should use a compact bump tail"
        );

        let stride = stride.expect("validated bump tail");
        for idx in 0..count - 1 {
            unsafe {
                *((head as usize + idx * stride) as *mut usize) =
                    head as usize + (idx + 1) * stride;
            }
        }
        unsafe { *((head as usize + (count - 1) * stride) as *mut usize) = 0 };

        alloc
            .deallocate_batch(head as *mut usize)
            .expect("metadata_segregation backend batch should deallocate");
        assert!(alloc.retained_empty_slab_os_pages() <= 1);
    }

    #[test]
    fn separate_backend_adapter_preserves_strict_alignment() {
        let mut alloc = SCAllocator::new(64, 1);
        let (head, count, stride) = alloc
            .allocate_batch_v2(256)
            .expect("strictly aligned allocation should use bitmap backend path");
        assert_eq!((head as usize) & 255, 0);
        assert_eq!(stride, None);
        assert!(count > 0);
        alloc
            .deallocate_batch(head as *mut usize)
            .expect("strict batch cleanup should succeed");
    }

    #[test]
    fn separate_backend_adapter_returns_single_page_aligned_tiny_object() {
        let mut alloc = SCAllocator::new(8, 2);
        let (head, count, stride) = alloc
            .allocate_batch_v2(PAGE_SIZE)
            .expect("page-aligned tiny allocation should succeed");
        assert_eq!(head as usize & (PAGE_SIZE - 1), 0);
        assert_eq!(count, 1);
        assert_eq!(stride, Some(8));
        unsafe { *(head as *mut usize) = 0 };
        alloc
            .deallocate_batch(head as *mut usize)
            .expect("single page-aligned tiny object should deallocate");
    }

    #[test]
    fn separate_backend_adapter_caps_tiny_natural_batch_to_one_page_bump() {
        let mut alloc = SCAllocator::new(8, 2);
        let (head, count, stride) = alloc
            .allocate_batch_v2(core::mem::align_of::<usize>())
            .expect("tiny natural-alignment batch should allocate");
        assert_eq!(count, PAGE_SIZE / 8);
        assert_eq!(stride, Some(8));

        for idx in 0..count - 1 {
            unsafe {
                *((head as usize + idx * 8) as *mut usize) = head as usize + (idx + 1) * 8;
            }
        }
        unsafe { *((head as usize + (count - 1) * 8) as *mut usize) = 0 };
        alloc
            .deallocate_batch(head as *mut usize)
            .expect("capped tiny bump batch should deallocate");
    }

    #[test]
    fn separate_backend_bump_bitmap_words_track_min_object_slots() {
        let old_byte_sized_words =
            (PAGE_SIZE + SEPARATE_BACKEND_BUMP_BITMAP_BITS - 1) / SEPARATE_BACKEND_BUMP_BITMAP_BITS;

        assert_eq!(
            SEPARATE_BACKEND_BUMP_BITMAP_MIN_STRIDE,
            core::mem::size_of::<usize>()
        );
        assert_eq!(
            SEPARATE_BACKEND_BUMP_BITMAP_MAX_SLOTS,
            PAGE_SIZE / core::mem::size_of::<usize>()
        );
        assert!(
            SEPARATE_BACKEND_BUMP_BITMAP_WORDS * SEPARATE_BACKEND_BUMP_BITMAP_BITS
                >= SEPARATE_BACKEND_BUMP_BITMAP_MAX_SLOTS
        );
        if SEPARATE_BACKEND_BUMP_BITMAP_WORDS > 0 {
            assert!(
                (SEPARATE_BACKEND_BUMP_BITMAP_WORDS - 1) * SEPARATE_BACKEND_BUMP_BITMAP_BITS
                    < SEPARATE_BACKEND_BUMP_BITMAP_MAX_SLOTS
            );
        }
        assert!(
            SEPARATE_BACKEND_BUMP_BITMAP_WORDS < old_byte_sized_words,
            "scratch bitmap should be sized by object slots, not by every byte in PAGE_SIZE"
        );
    }

    #[test]
    fn separate_backend_contiguous_bump_detection_is_bitmap_exact() {
        let head = NonNull::new(PAGE_SIZE as *mut u8).expect("non-null synthetic head");
        let base = head.as_ptr() as usize;
        let stride = 64;
        let rotated = [base + stride, base + stride * 2, base];
        assert!(
            SCAllocator::batch_has_contiguous_bump(&rotated, head, rotated.len(), stride),
            "a rotated exact contiguous set should still be a bump batch"
        );

        let duplicate = [base, base + stride, base + stride];
        assert!(
            !SCAllocator::batch_has_contiguous_bump(&duplicate, head, duplicate.len(), stride),
            "duplicate entries must not hide a missing bump slot"
        );

        let gap = [base, base + stride * 2, base + stride * 3];
        assert!(
            !SCAllocator::batch_has_contiguous_bump(&gap, head, gap.len(), stride),
            "a gap cannot be represented by the thread-cache bump cursor"
        );

        let misaligned = [base, base + 96, base + stride * 2];
        assert!(
            !SCAllocator::batch_has_contiguous_bump(&misaligned, head, misaligned.len(), stride),
            "non-stride-aligned pointers must stay in linked-list form"
        );

        let min_stride = SEPARATE_BACKEND_BUMP_BITMAP_MIN_STRIDE;
        let max_slots = SEPARATE_BACKEND_BUMP_BITMAP_MAX_SLOTS;
        let mut max_dense = alloc::vec::Vec::with_capacity(max_slots);
        for idx in (0..max_slots).rev() {
            max_dense.push(base + idx * min_stride);
        }
        assert!(
            SCAllocator::batch_has_contiguous_bump(&max_dense, head, max_dense.len(), min_stride),
            "bitmap must cover the largest one-page batch at minimum object stride"
        );

        max_dense.push(base + max_slots * min_stride);
        assert!(
            !SCAllocator::batch_has_contiguous_bump(&max_dense, head, max_dense.len(), min_stride),
            "larger-than-one-page synthetic batches exceed the scratch bitmap by design"
        );
    }

    #[test]
    fn separate_backend_adapter_returns_linked_dirty_natural_batch() {
        let mut alloc = SCAllocator::new(1024, 2);
        let (first, first_count, first_stride) = alloc
            .allocate_batch_v2(core::mem::align_of::<usize>())
            .expect("first page batch should allocate");
        let (second, second_count, second_stride) = alloc
            .allocate_batch_v2(core::mem::align_of::<usize>())
            .expect("second page batch should allocate");
        let first_stride = first_stride.expect("first clean page should be a bump batch");
        let second_stride = second_stride.expect("second clean page should be a bump batch");
        assert_eq!(first_count, second_count);
        assert!(first_count >= 4);

        let partial_per_page = first_count / 2;
        let mut partial = alloc::vec::Vec::with_capacity(partial_per_page * 2);
        for idx in 0..partial_per_page {
            partial.push(first as usize + idx * first_stride);
            partial.push(second as usize + idx * second_stride);
        }
        for idx in 0..partial.len() - 1 {
            unsafe {
                *(partial[idx] as *mut usize) = partial[idx + 1];
            }
        }
        unsafe { *(partial[partial.len() - 1] as *mut usize) = 0 };
        alloc
            .deallocate_batch(partial[0] as *mut usize)
            .expect("seed partial pages");

        let (head, count, stride) = alloc
            .allocate_batch_v2(core::mem::align_of::<usize>())
            .expect("dirty natural batch should still allocate");
        assert!(!head.is_null());
        assert!(
            count > 1,
            "dirty natural batch should preserve the whole bounded pointer set"
        );
        assert_eq!(
            stride, None,
            "dirty natural batch must be linked instead of tail-returned inside the adapter"
        );
        alloc
            .deallocate_batch(head as *mut usize)
            .expect("linked dirty natural batch should deallocate");
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn separate_backend_fixed_heap_bootstrap_allocates_every_adapter_stage() {
        let _fixed_heap_guard = crate::sc::fixed_heap_test_guard();
        assert!(crate::sc::fixed_heap_roots_initialized());

        let mut alloc = SCAllocator::new(64, 1);
        let count = alloc
            .batch_request_count(8)
            .expect("fixed-heap geometry should be valid");
        alloc
            .ensure_batch_len(count)
            .expect("adapter staging Vec should allocate from fixed heap");
        let ptr_map = alloc.ensure_ptr_map().expect("ptr map should allocate");
        assert!(!ptr_map.is_null());
        let ptr_map = unsafe { ptr_map.as_mut().expect("non-null ptr map") };
        alloc
            .inner
            .allocate(8, ptr_map)
            .expect("separate bitmap backend should allocate its first object");
    }

    #[test]
    fn separate_backend_adapter_rejects_cyclic_deallocation_batch_without_vec_growth() {
        let mut alloc = SCAllocator::new(64, 1);
        let (head, count, stride) = alloc
            .allocate_batch_v2(8)
            .expect("real batch allocation should succeed");
        let stride = stride.expect("natural-alignment batch should be contiguous");
        let original_batch_len = alloc.batch.len();

        unsafe { *(head as *mut usize) = head as usize };
        let err = alloc
            .deallocate_batch(head as *mut usize)
            .expect_err("cyclic deallocation chains must fail before staging");
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(
            alloc.batch.len(),
            original_batch_len,
            "cycle rejection should not grow the adapter staging Vec"
        );

        for idx in 0..count - 1 {
            unsafe {
                *((head as usize + idx * stride) as *mut usize) =
                    head as usize + (idx + 1) * stride;
            }
        }
        unsafe { *((head as usize + (count - 1) * stride) as *mut usize) = 0 };
        alloc
            .deallocate_batch(head as *mut usize)
            .expect("valid repaired batch should still deallocate cleanly");
    }

    #[test]
    fn separate_backend_adapter_rejects_misaligned_deallocation_link_without_vec_growth() {
        let mut alloc = SCAllocator::new(64, 1);
        let (head, count, stride) = alloc
            .allocate_batch_v2(8)
            .expect("real batch allocation should succeed");
        let stride = stride.expect("natural-alignment batch should be contiguous");
        assert!(count > 1, "test needs a linked tail to corrupt");
        let original_batch_len = alloc.batch.len();

        unsafe { *(head as *mut usize) = head as usize + 1 };
        let err = alloc
            .deallocate_batch(head as *mut usize)
            .expect_err("misaligned deallocation links must fail before staging");
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(
            alloc.batch.len(),
            original_batch_len,
            "misaligned-link rejection should not grow the adapter staging Vec"
        );

        for idx in 0..count - 1 {
            unsafe {
                *((head as usize + idx * stride) as *mut usize) =
                    head as usize + (idx + 1) * stride;
            }
        }
        unsafe { *((head as usize + (count - 1) * stride) as *mut usize) = 0 };
        alloc
            .deallocate_batch(head as *mut usize)
            .expect("valid repaired batch should still deallocate cleanly");
    }

    #[test]
    fn separate_backend_adapter_releases_oversized_staging_after_large_free() {
        let mut alloc = SCAllocator::new(8, 2);
        let batches = (SEPARATE_BACKEND_STAGING_RETAIN_OBJECT_LIMIT / (PAGE_SIZE / 8)) + 2;
        let mut objects = alloc::vec::Vec::new();

        for _ in 0..batches {
            let (head, count, stride) = alloc
                .allocate_batch_v2(core::mem::align_of::<usize>())
                .expect("tiny natural batch should allocate");
            let stride = stride.expect("tiny natural batch should be a bump batch");
            for idx in 0..count {
                objects.push(head as usize + idx * stride);
            }
        }

        assert!(
            objects.len() > SEPARATE_BACKEND_STAGING_RETAIN_OBJECT_LIMIT,
            "test must build a deallocation batch larger than the retained staging cap"
        );
        assert!(objects.len() < SEPARATE_BACKEND_DEALLOCATE_BATCH_OBJECT_LIMIT);

        for idx in 0..objects.len() - 1 {
            unsafe { *(objects[idx] as *mut usize) = objects[idx + 1] };
        }
        unsafe { *(objects[objects.len() - 1] as *mut usize) = 0 };

        alloc
            .deallocate_batch(objects[0] as *mut usize)
            .expect("large real deallocation batch should succeed");
        assert_eq!(
            alloc.batch.capacity(),
            0,
            "rare large flushes must not permanently pin oversized staging metadata"
        );
    }

    #[test]
    fn separate_backend_adapter_releases_oversized_staging_after_rejected_large_free() {
        let mut alloc = SCAllocator::new(8, 2);
        let count = SEPARATE_BACKEND_STAGING_RETAIN_OBJECT_LIMIT + 1;
        let mut words = alloc::vec::Vec::with_capacity(count);
        words.resize(count, 0usize);

        for idx in 0..count - 1 {
            words[idx] = (&mut words[idx + 1] as *mut usize) as usize;
        }
        words[count - 1] = 0;

        let err = alloc
            .deallocate_batch((&mut words[0]) as *mut usize)
            .expect_err("unowned linked words must be rejected by the slab backend");
        assert_eq!(err.to_raw_errno(), AllocError::EUAF.to_raw_errno());
        assert!(
            alloc.batch.capacity() <= SEPARATE_BACKEND_STAGING_RETAIN_OBJECT_LIMIT,
            "failed large invalid frees must not retain oversized staging metadata: cap={} limit={}",
            alloc.batch.capacity(),
            SEPARATE_BACKEND_STAGING_RETAIN_OBJECT_LIMIT
        );
    }
}
