use crate::error::{AllocError, Result};
use crate::prelude::*;
use crate::*;
use alloc::boxed::Box;
use core::alloc::{GlobalAlloc, Layout};
use core::ptr::{self, NonNull};

const BITFIELD_BITS: usize = u32::BITS as usize;
const MIN_SEPARATE_PAGE_OBJECT_BYTES: usize = core::mem::size_of::<usize>();
const MAX_DEALLOCATE_BATCH_SEEN_WORDS: usize =
    (PAGE_SIZE / MIN_SEPARATE_PAGE_OBJECT_BYTES + BITFIELD_BITS - 1) / BITFIELD_BITS;

/// Holds allocated data within pages.
///
/// Has a data-section where objects are allocated from
/// and a small amount of meta-data in the form of a bitmap
/// to track allocations at the end of the page.
#[repr(C)]
pub struct ObjectPage {
    /// number of chunks that have been allocated
    counter: usize,
    data: *mut u8,
}

#[derive(Clone, Copy)]
pub(crate) struct ObjectPageAllocationState {
    counter: usize,
}

impl ObjectPage {
    pub const fn new() -> Self {
        Self {
            counter: 0,
            data: ptr::null_mut(),
        }
    }

    pub fn allocate_page(&mut self, pg_num: usize) -> Result<*mut u8> {
        let layout = super::try_object_page_layout(pg_num)?;
        let ans = unsafe { GlobalBackend.alloc(layout) };
        if ans.is_null() {
            return Err(AllocError::ENOMEM);
        }
        self.data = ans;
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
        Some(p)
    }

    #[inline]
    pub fn is_inited(&self) -> bool {
        !self.data.is_null()
    }

    pub(crate) fn repair_counter_from_bitfield(
        &mut self,
        bitfield: &[u32],
        pg_count: usize,
    ) -> Result<usize> {
        let mut used = 0usize;
        for (base_idx, bitval) in bitfield.iter().enumerate() {
            let valid_mask = match Self::valid_object_mask_for_word(base_idx, pg_count)? {
                Some(mask) => mask,
                None => break,
            };
            used = used
                .checked_add((*bitval & valid_mask).count_ones() as usize)
                .ok_or(AllocError::EFATAL)?;
        }
        self.counter = used;
        Ok(used)
    }

    pub(crate) fn clear_allocations(&mut self, bitfield: &mut [u32]) {
        for bitval in bitfield.iter_mut() {
            *bitval = 0;
        }
        self.counter = 0;
    }

    #[inline]
    pub(crate) fn allocation_state_snapshot(&self) -> ObjectPageAllocationState {
        ObjectPageAllocationState {
            counter: self.counter,
        }
    }

    pub(crate) fn restore_deallocated_prefix(
        &mut self,
        state: ObjectPageAllocationState,
        freed_ptrs: &[usize],
        bitfield: &mut [u32],
        pg_count: usize,
        pg_align: usize,
        pg_num: usize,
    ) -> Result<()> {
        let page_span = super::checked_object_page_span_bytes(pg_num)?;
        let base_addr = self.data as usize;
        for ptr in freed_ptrs.iter().copied() {
            let object_idx =
                Self::object_index_in_span(base_addr, ptr, pg_count, pg_align, page_span)?
                    .ok_or(AllocError::EOOB)?;
            let (idx, mask) = Self::bit_slot(bitfield.len(), object_idx)?;
            bitfield[idx] |= mask;
        }
        self.counter = state.counter;
        Ok(())
    }

    pub(crate) fn restore_allocated_prefix(
        &mut self,
        state: ObjectPageAllocationState,
        allocated_ptrs: &[usize],
        bitfield: &mut [u32],
        pg_count: usize,
        pg_align: usize,
        pg_num: usize,
    ) -> Result<()> {
        let page_span = super::checked_object_page_span_bytes(pg_num)?;
        let base_addr = self.data as usize;
        for ptr in allocated_ptrs.iter().copied() {
            let object_idx =
                Self::object_index_in_span(base_addr, ptr, pg_count, pg_align, page_span)?
                    .ok_or(AllocError::EOOB)?;
            let (idx, mask) = Self::bit_slot(bitfield.len(), object_idx)?;
            bitfield[idx] &= !mask;
        }
        self.counter = state.counter;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deallocate_rejects_pointer_below_page_without_underflow() {
        let mut page = ObjectPage {
            counter: 1,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [1_u32];
        let ptr = NonNull::new(0x800 as *mut u8).expect("non-null test pointer");

        let err = page
            .deallocate(ptr, &mut bitfield, 1, 8)
            .expect_err("pointer before base must be rejected");

        assert_eq!(err.to_raw_errno(), AllocError::EOOB.to_raw_errno());
        assert_eq!(page.counter, 1);
        assert_eq!(bitfield[0], 1);
    }

    #[test]
    fn deallocate_rejects_zero_alignment_without_dividing() {
        let mut page = ObjectPage {
            counter: 1,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [1_u32];
        let ptr = NonNull::new(0x1000 as *mut u8).expect("non-null test pointer");

        let err = page
            .deallocate(ptr, &mut bitfield, 1, 0)
            .expect_err("zero alignment must be rejected");

        assert_eq!(err.to_raw_errno(), AllocError::ELAYOUT.to_raw_errno());
        assert_eq!(page.counter, 1);
        assert_eq!(bitfield[0], 1);
    }

    #[test]
    fn deallocate_rejects_misaligned_pointer_without_freeing_neighbor() {
        let mut page = ObjectPage {
            counter: 1,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [1_u32];
        let ptr = NonNull::new(0x1001 as *mut u8).expect("non-null test pointer");

        let err = page
            .deallocate(ptr, &mut bitfield, 1, 8)
            .expect_err("misaligned object pointer must not free its aligned neighbor");

        assert_eq!(err.to_raw_errno(), AllocError::EOOB.to_raw_errno());
        assert_eq!(page.counter, 1);
        assert_eq!(bitfield[0], 1);
    }

    #[test]
    fn deallocate_rejects_spare_slot_beyond_pg_count_even_if_bitfield_is_set() {
        let mut page = ObjectPage {
            counter: 1,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0b11_u32];
        let ptr = NonNull::new(0x1008 as *mut u8).expect("non-null test pointer");

        let err = page
            .deallocate(ptr, &mut bitfield, 1, 8)
            .expect_err("bitfield capacity bits past pg_count are not real objects");

        assert_eq!(err.to_raw_errno(), AllocError::EOOB.to_raw_errno());
        assert_eq!(page.counter, 1);
        assert_eq!(bitfield[0], 0b11);
    }

    #[test]
    fn deallocate_rejects_empty_counter_without_underflow() {
        let mut page = ObjectPage {
            counter: 0,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [1_u32];
        let ptr = NonNull::new(0x1000 as *mut u8).expect("non-null test pointer");

        let err = page
            .deallocate(ptr, &mut bitfield, 1, 8)
            .expect_err("empty page counter must not underflow");

        assert_eq!(err.to_raw_errno(), AllocError::EUAF.to_raw_errno());
        assert_eq!(page.counter, 0);
        assert_eq!(bitfield[0], 1);
    }

    #[test]
    fn allocate_page_rejects_zero_pages_without_panic() {
        let mut page = ObjectPage::new();
        let err = page
            .allocate_page(0)
            .expect_err("zero-page separate page allocation must fail closed");
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert!(page.get_data_ptr().is_none());
    }

    #[test]
    fn destroy_uninitialized_page_is_noop() {
        let mut page = ObjectPage::new();
        assert!(page.destroy_page(1).is_none());
    }

    #[test]
    fn allocate_rejects_uninitialized_page_without_bitmap_mutation() {
        let mut page = ObjectPage::new();
        let mut bitfield = [0_u32];

        let ptr = page.allocate(1, &mut bitfield, 1, 8, 1);

        assert!(ptr.is_null());
        assert_eq!(page.counter, 0);
        assert_eq!(bitfield[0], 0);
    }

    #[test]
    fn allocate_rejects_zero_alignment_without_bitmap_mutation() {
        let mut page = ObjectPage {
            counter: 0,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0_u32];

        let ptr = page.allocate(0, &mut bitfield, 1, 8, 1);

        assert!(ptr.is_null());
        assert_eq!(page.counter, 0);
        assert_eq!(bitfield[0], 0);
    }

    #[test]
    fn allocate_rejects_page_span_overflow_without_bitmap_mutation() {
        let mut page = ObjectPage {
            counter: 0,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0_u32];
        let overflowing_pages = usize::MAX / PAGE_SIZE + 1;

        let ptr = page.allocate(1, &mut bitfield, 1, 8, overflowing_pages);

        assert!(ptr.is_null());
        assert_eq!(page.counter, 0);
        assert_eq!(bitfield[0], 0);
    }

    #[test]
    fn allocate_all_rejects_uninitialized_page_without_bitmap_mutation() {
        let mut page = ObjectPage::new();
        let mut bitfield = [0_u32];

        let ptr = page.allocate_all(&mut bitfield, 1, 8, 1);

        assert!(ptr.is_null());
        assert_eq!(page.counter, 0);
        assert_eq!(bitfield[0], 0);
    }

    #[test]
    fn allocate_all_marks_only_real_slots() {
        let mut page = ObjectPage {
            counter: 0,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0_u32; 3];

        let ptr = page.allocate_all(&mut bitfield, 33, 8, 1);

        assert_eq!(ptr as usize, 0x1000);
        assert_eq!(page.counter, 33);
        assert_eq!(bitfield, [u32::MAX, 1, 0]);
    }

    #[test]
    fn allocate_all_rejects_slot_that_would_cross_page_span() {
        let mut page = ObjectPage {
            counter: 0,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0_u32];
        let oversized_stride = PAGE_SIZE - 1;

        let ptr = page.allocate_all(&mut bitfield, 2, oversized_stride, 1);

        assert!(
            ptr.is_null(),
            "whole-page fast path must not publish a logical slot whose object crosses the mapped page span"
        );
        assert_eq!(page.counter, 0);
        assert_eq!(bitfield[0], 0);
        assert_ne!(oversized_stride, 0);
    }

    #[test]
    fn allocate_all_rejects_short_bitfield_without_counter_drift() {
        let mut page = ObjectPage {
            counter: 0,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0_u32; 1];

        let ptr = page.allocate_all(&mut bitfield, BITFIELD_BITS + 1, 8, 1);

        assert!(
            ptr.is_null(),
            "allocate_all must not publish objects that the bitmap cannot represent"
        );
        assert_eq!(page.counter, 0);
        assert_eq!(bitfield, [0]);
    }

    #[test]
    fn clear_allocations_resets_counter_and_bitmap() {
        let mut page = ObjectPage {
            counter: 3,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [u32::MAX, 0b1010];

        page.clear_allocations(&mut bitfield);

        assert_eq!(page.counter, 0);
        assert_eq!(bitfield, [0, 0]);
    }

    #[test]
    fn deallocate_batch_rejects_double_free_without_panic() {
        let mut page = ObjectPage {
            counter: 1,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0_u32];
        let mut batch = [0x1000usize];

        let err = page
            .deallocate_batch(&mut batch, &mut bitfield, 1, 8, 1)
            .expect_err("batch double-free must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EDBFRE.to_raw_errno());
        assert_eq!(page.counter, 1);
        assert_eq!(bitfield[0], 0);
        assert_eq!(batch[0], 0x1000);
    }

    #[test]
    fn deallocate_batch_rejects_empty_counter_without_underflow() {
        let mut page = ObjectPage {
            counter: 0,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [1_u32];
        let mut batch = [0x1000usize];

        let err = page
            .deallocate_batch(&mut batch, &mut bitfield, 1, 8, 1)
            .expect_err("batch free must not underflow empty counter");

        assert_eq!(err.to_raw_errno(), AllocError::EUAF.to_raw_errno());
        assert_eq!(page.counter, 0);
        assert_eq!(bitfield[0], 1);
        assert_eq!(batch[0], 0x1000);
    }

    #[test]
    fn deallocate_batch_rejects_misaligned_pointer_without_partial_mutation() {
        let mut page = ObjectPage {
            counter: 2,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0b11_u32];
        let mut batch = [0x1000usize, 0x1001usize];

        let err = page
            .deallocate_batch(&mut batch, &mut bitfield, 2, 8, 1)
            .expect_err("batch validation must reject misaligned pointers before clearing bits");

        assert_eq!(err.to_raw_errno(), AllocError::EOOB.to_raw_errno());
        assert_eq!(page.counter, 2);
        assert_eq!(bitfield[0], 0b11);
        assert_eq!(batch, [0x1000, 0x1001]);
    }

    #[test]
    fn deallocate_batch_rejects_duplicate_pointer_without_partial_mutation() {
        let mut page = ObjectPage {
            counter: 1,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [1_u32];
        let mut batch = [0x1000usize, 0x1000usize];

        let err = page
            .deallocate_batch(&mut batch, &mut bitfield, 1, 8, 1)
            .expect_err("duplicate entries in one batch are double-free attempts");

        assert_eq!(err.to_raw_errno(), AllocError::EDBFRE.to_raw_errno());
        assert_eq!(page.counter, 1);
        assert_eq!(bitfield[0], 1);
        assert_eq!(batch, [0x1000, 0x1000]);
    }

    #[test]
    fn deallocate_batch_seen_bitmap_clears_cross_word_prefix() {
        let mut page = ObjectPage {
            counter: 40,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [u32::MAX, 0xff_u32];
        let mut batch = [0usize; 40];
        for (idx, slot) in batch.iter_mut().enumerate() {
            *slot = 0x1000 + idx * 8;
        }

        let deallocated = page
            .deallocate_batch(&mut batch, &mut bitfield, 40, 8, 1)
            .expect("cross-word batch should deallocate through the stack seen bitmap");

        assert_eq!(deallocated, 40);
        assert_eq!(page.counter, 0);
        assert_eq!(bitfield, [0, 0]);
    }

    #[test]
    fn deallocate_batch_seen_bitmap_rejects_cross_word_duplicate_without_mutation() {
        let mut page = ObjectPage {
            counter: 34,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [u32::MAX, 0b11_u32];
        let mut batch = [0usize; 34];
        for (idx, slot) in batch.iter_mut().enumerate() {
            *slot = 0x1000 + idx * 8;
        }
        batch[33] = batch[32];

        let err = page
            .deallocate_batch(&mut batch, &mut bitfield, 34, 8, 1)
            .expect_err("duplicate in the second bitmap word must fail before clearing bits");

        assert_eq!(err.to_raw_errno(), AllocError::EDBFRE.to_raw_errno());
        assert_eq!(page.counter, 34);
        assert_eq!(bitfield, [u32::MAX, 0b11]);
    }

    #[test]
    fn deallocate_batch_rejects_counter_bitfield_mismatch_without_partial_mutation() {
        let mut page = ObjectPage {
            counter: 1,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0b11_u32];
        let mut batch = [0x1000usize, 0x1008usize];

        let err = page
            .deallocate_batch(&mut batch, &mut bitfield, 2, 8, 1)
            .expect_err("batch cannot free more live objects than the page counter records");

        assert_eq!(err.to_raw_errno(), AllocError::EUAF.to_raw_errno());
        assert_eq!(page.counter, 1);
        assert_eq!(bitfield[0], 0b11);
        assert_eq!(batch, [0x1000, 0x1008]);
    }

    #[test]
    fn deallocate_batch_rejects_spare_slot_beyond_pg_count_without_partial_mutation() {
        let mut page = ObjectPage {
            counter: 1,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0b11_u32];
        let mut batch = [0x1000usize, 0x1008usize];

        let err = page
            .deallocate_batch(&mut batch, &mut bitfield, 1, 8, 1)
            .expect_err("batch preflight must reject bitfield capacity bits beyond pg_count");

        assert_eq!(err.to_raw_errno(), AllocError::EOOB.to_raw_errno());
        assert_eq!(page.counter, 1);
        assert_eq!(bitfield[0], 0b11);
        assert_eq!(batch, [0x1000, 0x1008]);
    }

    #[test]
    fn allocate_batch_rejects_full_page_without_panic() {
        let mut page = ObjectPage {
            counter: 1,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [1_u32];
        let mut out = [0usize; 1];

        let allocated = page.allocate_batch(1, &mut out, 0, 1, &mut bitfield, 1, 8, 1);

        assert_eq!(allocated, 0);
        assert_eq!(page.counter, 1);
        assert_eq!(bitfield[0], 1);
        assert_eq!(out[0], 0);
    }

    #[test]
    fn allocate_batch_rejects_output_oob_without_bitmap_mutation() {
        let mut page = ObjectPage {
            counter: 0,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0_u32];
        let mut out = [0usize; 1];

        let allocated = page.allocate_batch(1, &mut out, 1, 1, &mut bitfield, 1, 8, 1);

        assert_eq!(allocated, 0);
        assert_eq!(page.counter, 0);
        assert_eq!(bitfield[0], 0);
        assert_eq!(out[0], 0);
    }

    #[test]
    fn allocate_batch_rejects_zero_alignment_without_mutation() {
        let mut page = ObjectPage {
            counter: 0,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0_u32];
        let mut out = [0usize; 1];

        let allocated = page.allocate_batch(1, &mut out, 0, 1, &mut bitfield, 1, 0, 1);

        assert_eq!(allocated, 0);
        assert_eq!(page.counter, 0);
        assert_eq!(bitfield[0], 0);
        assert_eq!(out[0], 0);
    }

    #[test]
    fn allocate_batch_skips_slots_that_do_not_satisfy_requested_alignment() {
        let mut page = ObjectPage {
            counter: 0,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0_u32];
        let mut out = [0usize; 2];

        let allocated = page.allocate_batch(16, &mut out, 0, 2, &mut bitfield, 4, 8, 1);

        assert_eq!(allocated, 2);
        assert_eq!(page.counter, 2);
        assert_eq!(bitfield[0], 0b0101);
        assert_eq!(out, [0x1000, 0x1010]);
    }

    #[test]
    fn allocate_batch_strict_alignment_returns_only_capped_matches() {
        let cap = ObjectPage::strict_alignment_batch_return_limit(16, 8);
        assert_eq!(cap, crate::page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT);
        let pg_count = cap * 2 + 8;
        let mut page = ObjectPage {
            counter: 0,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield =
            [0_u32; (crate::page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT * 2 + 8 + 31) / 32];
        let mut out = [0usize; crate::page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT + 8];

        let allocated =
            page.allocate_batch(16, &mut out, 0, cap + 8, &mut bitfield, pg_count, 8, 1);

        assert_eq!(allocated, cap);
        assert_eq!(page.counter, cap);
        assert!(out.iter().take(cap).all(|ptr| *ptr != 0 && *ptr & 15 == 0));
        assert!(out.iter().skip(cap).all(|ptr| *ptr == 0));

        let remaining_aligned_slots = (0..pg_count)
            .filter(|idx| {
                let addr = 0x1000usize + idx * 8;
                let is_allocated = (bitfield[idx / 32] & (1_u32 << (idx % 32))) != 0;
                addr & 15 == 0 && !is_allocated
            })
            .count();
        assert!(
            remaining_aligned_slots > 0,
            "strict cap should leave later aligned slots reusable on the page"
        );

        let mut second = [0usize; 1];
        let second_allocated =
            page.allocate_batch(16, &mut second, 0, 1, &mut bitfield, pg_count, 8, 1);
        assert_eq!(second_allocated, 1);
        assert_eq!(second[0] & 15, 0);
        assert!(!out.iter().take(cap).any(|ptr| *ptr == second[0]));
        assert_eq!(page.counter, cap + 1);
    }

    #[test]
    fn allocate_batch_rejects_uninitialized_page_without_mutation() {
        let mut page = ObjectPage::new();
        let mut bitfield = [0_u32];
        let mut out = [0usize; 1];

        let allocated = page.allocate_batch(1, &mut out, 0, 1, &mut bitfield, 1, 8, 1);

        assert_eq!(allocated, 0);
        assert_eq!(page.counter, 0);
        assert_eq!(bitfield[0], 0);
        assert_eq!(out[0], 0);
    }

    #[test]
    fn allocate_batch_preserves_normal_allocation() {
        let mut page = ObjectPage {
            counter: 0,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0_u32];
        let mut out = [0usize; 2];

        let allocated = page.allocate_batch(1, &mut out, 0, 2, &mut bitfield, 2, 8, 1);

        assert_eq!(allocated, 2);
        assert_eq!(page.counter, 2);
        assert_eq!(bitfield[0], 0b11);
        assert_eq!(out, [0x1000, 0x1008]);
    }

    #[test]
    fn allocate_scans_dense_bitmap_by_free_bits_across_words() {
        let mut page = ObjectPage {
            counter: 31,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [u32::MAX & !(1_u32 << 31)];

        let ptr = page.allocate(1, &mut bitfield, 32, 8, 1);

        assert_eq!(ptr as usize, 0x1000 + 31 * 8);
        assert_eq!(page.counter, 32);
        assert_eq!(bitfield[0], u32::MAX);
    }

    #[test]
    fn allocate_batch_scans_dense_bitmap_by_free_bits_across_words() {
        let mut page = ObjectPage {
            counter: 62,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [u32::MAX & !(1_u32 << 31), u32::MAX & !(1_u32 << 3)];
        let mut out = [0usize; 2];

        let allocated = page.allocate_batch(1, &mut out, 0, 2, &mut bitfield, 64, 8, 1);

        assert_eq!(allocated, 2);
        assert_eq!(page.counter, 64);
        assert_eq!(bitfield, [u32::MAX, u32::MAX]);
        assert_eq!(out, [0x1000 + 31 * 8, 0x1000 + 35 * 8]);
    }

    #[test]
    fn allocate_ignores_spare_bits_beyond_pg_count() {
        let mut page = ObjectPage {
            counter: BITFIELD_BITS,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [u32::MAX, 0_u32];

        let ptr = page.allocate(1, &mut bitfield, BITFIELD_BITS, 8, 1);

        assert!(ptr.is_null());
        assert_eq!(page.counter, BITFIELD_BITS);
        assert_eq!(
            bitfield,
            [u32::MAX, 0],
            "spare final-word bits must not be marked as allocated slots"
        );
    }

    #[test]
    fn allocate_batch_ignores_spare_bits_beyond_pg_count_without_counter_drift() {
        let pg_count = BITFIELD_BITS + 1;
        let mut page = ObjectPage {
            counter: BITFIELD_BITS,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [u32::MAX, 0_u32];
        let mut out = [0usize; 2];

        let allocated = page.allocate_batch(1, &mut out, 0, 2, &mut bitfield, pg_count, 8, 1);

        assert_eq!(allocated, 1);
        assert_eq!(page.counter, pg_count);
        assert_eq!(bitfield, [u32::MAX, 1]);
        assert_eq!(out[0], 0x1000 + BITFIELD_BITS * 8);
        assert_eq!(
            out[1], 0,
            "spare final-word bits must not produce synthetic output pointers"
        );
    }

    #[test]
    fn allocate_batch_rejects_slot_that_would_cross_page_span() {
        let mut page = ObjectPage {
            counter: 1,
            data: 0x1000 as *mut u8,
        };
        let mut bitfield = [0b01_u32];
        let mut out = [0usize; 1];
        let oversized_stride = PAGE_SIZE - 1;

        let allocated =
            page.allocate_batch(1, &mut out, 0, 1, &mut bitfield, 2, oversized_stride, 1);

        assert_eq!(
            allocated, 0,
            "the second logical object starts inside the page but extends beyond it"
        );
        assert_eq!(page.counter, 1);
        assert_eq!(bitfield[0], 0b01);
        assert_eq!(out[0], 0);
    }
}

impl ObjectPage {
    #[inline]
    fn strict_alignment_batch_return_limit(align: usize, pg_align: usize) -> usize {
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

    fn bitfield_capacity_span_bytes(bitfield_len: usize, pg_align: usize) -> Result<usize> {
        bitfield_len
            .checked_mul(BITFIELD_BITS)
            .and_then(|slots| slots.checked_mul(pg_align))
            .ok_or(AllocError::ESIZE)
    }

    fn object_index_in_span(
        base_addr: usize,
        ptr_addr: usize,
        pg_count: usize,
        pg_align: usize,
        page_span: usize,
    ) -> Result<Option<usize>> {
        if pg_count == 0 {
            return Err(AllocError::ESIZE);
        }
        if pg_align == 0 {
            return Err(AllocError::ELAYOUT);
        }
        if base_addr == 0 {
            return Err(AllocError::EUAF);
        }
        let page_offset = match ptr_addr.checked_sub(base_addr) {
            Some(offset) if offset < page_span => offset,
            _ => return Ok(None),
        };
        let object_span = pg_count.checked_mul(pg_align).ok_or(AllocError::ESIZE)?;
        if page_offset >= object_span || page_offset % pg_align != 0 {
            return Err(AllocError::EOOB);
        }
        let object_idx = page_offset / pg_align;
        if object_idx >= pg_count {
            return Err(AllocError::EOOB);
        }
        Ok(Some(object_idx))
    }

    fn bit_slot(bitfield_len: usize, object_idx: usize) -> Result<(usize, u32)> {
        let idx = object_idx / BITFIELD_BITS;
        if idx >= bitfield_len {
            return Err(AllocError::EOOB);
        }
        Ok((idx, 1_u32 << (object_idx % BITFIELD_BITS)))
    }

    #[inline]
    fn bitfield_word_count(pg_count: usize) -> Result<usize> {
        pg_count
            .checked_add(BITFIELD_BITS - 1)
            .map(|rounded| rounded / BITFIELD_BITS)
            .ok_or(AllocError::ESIZE)
    }

    #[inline]
    fn valid_object_mask_for_word(base_idx: usize, pg_count: usize) -> Result<Option<u32>> {
        let first_idx = base_idx
            .checked_mul(BITFIELD_BITS)
            .ok_or(AllocError::EFATAL)?;
        if first_idx >= pg_count {
            return Ok(None);
        }
        let remaining = pg_count - first_idx;
        Ok(Some(if remaining >= BITFIELD_BITS {
            u32::MAX
        } else {
            (1_u32 << remaining) - 1
        }))
    }

    #[inline]
    fn set_seen_batch_object(seen_bitmap: &mut [u32], object_idx: usize) -> Result<()> {
        let (idx, mask) = Self::bit_slot(seen_bitmap.len(), object_idx)?;
        if seen_bitmap[idx] & mask != 0 {
            return Err(AllocError::EDBFRE);
        }
        seen_bitmap[idx] |= mask;
        Ok(())
    }

    #[inline]
    fn take_next_free_bit(free_mask: &mut u32) -> Option<usize> {
        if *free_mask == 0 {
            return None;
        }
        let bit = free_mask.trailing_zeros() as usize;
        *free_mask &= !(1_u32 << bit);
        Some(bit)
    }

    /// Clear a prevalidated deallocation prefix from the page bitfield.
    ///
    /// `validated_deallocate_batch_prefix` fills `seen_bitmap` only after it
    /// proves that every pointer is in-range, allocated, and unique.  Scanning
    /// the bitmap here avoids decoding the same batch pointers a second time on
    /// the common one-page geometry, and it keeps the fail-before-mutate rule:
    /// the real allocation bitmap is touched only after validation succeeds.
    fn clear_deallocated_batch_bitmap(
        bitfield: &mut [u32],
        seen_bitmap: &[u32],
        pg_count: usize,
    ) -> Result<usize> {
        let mut cleared = 0usize;
        for (word_idx, seen_word) in seen_bitmap.iter().copied().enumerate() {
            let first_idx = word_idx
                .checked_mul(BITFIELD_BITS)
                .ok_or(AllocError::ESIZE)?;
            if first_idx >= pg_count {
                break;
            }

            let remaining = pg_count - first_idx;
            let valid_mask = if remaining >= BITFIELD_BITS {
                u32::MAX
            } else {
                (1_u32 << remaining) - 1
            };
            let seen_word = seen_word & valid_mask;
            if seen_word == 0 {
                continue;
            }

            let bitfield_word = bitfield.get_mut(word_idx).ok_or(AllocError::EOOB)?;
            debug_assert_eq!(
                *bitfield_word & seen_word,
                seen_word,
                "batch validation should only mark allocated objects"
            );
            *bitfield_word &= !seen_word;
            cleared = cleared
                .checked_add(seen_word.count_ones() as usize)
                .ok_or(AllocError::ESIZE)?;
        }
        Ok(cleared)
    }

    fn validated_deallocate_batch_prefix(
        &self,
        res_array: &[usize],
        bitfield: &[u32],
        pg_count: usize,
        pg_align: usize,
        page_span: usize,
        mut seen_bitmap: Option<&mut [u32]>,
    ) -> Result<usize> {
        if let Some(bitmap) = seen_bitmap.as_deref_mut() {
            for word in bitmap.iter_mut() {
                *word = 0;
            }
        }

        let base_addr = self.data as usize;
        let mut validated = 0usize;
        for ptr in res_array.iter().copied() {
            let object_idx =
                match Self::object_index_in_span(base_addr, ptr, pg_count, pg_align, page_span)? {
                    Some(object_idx) => object_idx,
                    None => break,
                };
            let (idx, mask) = Self::bit_slot(bitfield.len(), object_idx)?;
            if bitfield[idx] & mask == 0 {
                return Err(AllocError::EDBFRE);
            }

            if let Some(bitmap) = seen_bitmap.as_deref_mut() {
                Self::set_seen_batch_object(bitmap, object_idx)?;
            } else {
                for prior in res_array[..validated].iter().copied() {
                    if Self::object_index_in_span(base_addr, prior, pg_count, pg_align, page_span)?
                        == Some(object_idx)
                    {
                        return Err(AllocError::EDBFRE);
                    }
                }
            }
            if validated >= self.counter {
                return Err(AllocError::EUAF);
            }
            validated += 1;
        }
        Ok(validated)
    }

    fn first_fit(
        &mut self,
        base_addr: usize,
        align: usize,
        bitfield: &mut [u32],
        pg_count: usize,
        pg_align: usize,
        pg_num: usize,
    ) -> Result<Option<(usize, usize)>> {
        let page_span = super::checked_object_page_span_bytes(pg_num)?;
        for (base_idx, bitval) in bitfield.iter_mut().enumerate() {
            let valid_mask = match Self::valid_object_mask_for_word(base_idx, pg_count) {
                Ok(Some(mask)) => mask,
                Ok(None) | Err(_) => return Ok(None),
            };
            let mut free_mask = !*bitval & valid_mask;
            while let Some(first_free) = Self::take_next_free_bit(&mut free_mask) {
                let idx: usize = base_idx * BITFIELD_BITS + first_free;
                let offset = match idx.checked_mul(pg_align) {
                    Some(offset) => offset,
                    None => return Ok(None),
                };

                if offset
                    .checked_add(pg_align)
                    .filter(|end| *end <= page_span)
                    .is_none()
                {
                    return Ok(None);
                }

                let addr: usize = match base_addr.checked_add(offset) {
                    Some(addr) => addr,
                    None => return Ok(None),
                };
                let alignment_ok = addr & (align - 1) == 0;
                if alignment_ok {
                    *bitval |= 1_u32 << first_free;
                    return Ok(Some((idx, addr)));
                }
            }
        }
        Ok(None)
    }

    /// Tries to allocate an object within this page.
    ///
    /// In case the slab is full, returns a null ptr.
    pub(crate) fn allocate(
        &mut self,
        align: usize,
        bitfield: &mut [u32],
        pg_count: usize,
        pg_align: usize,
        pg_num: usize,
    ) -> *mut u8 {
        if self.counter >= pg_count
            || self.data.is_null()
            || align == 0
            || !align.is_power_of_two()
            || pg_align == 0
        {
            ptr::null_mut()
        } else {
            let base_addr = (self.data as *const u8) as usize;
            match self.first_fit(base_addr, align, bitfield, pg_count, pg_align, pg_num) {
                Ok(Some((_, addr))) => {
                    self.counter += 1;
                    addr as *mut u8
                }
                Ok(None) | Err(_) => ptr::null_mut(),
            }
        }
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

    pub(crate) fn allocate_all(
        &mut self,
        bitfield: &mut [u32],
        pg_count: usize,
        pg_align: usize,
        pg_num: usize,
    ) -> *mut u8 {
        if self.data.is_null() || pg_count == 0 || pg_align == 0 {
            return ptr::null_mut();
        }
        let required_words = match Self::bitfield_word_count(pg_count) {
            Ok(words) => words,
            Err(_) => return ptr::null_mut(),
        };
        if required_words > bitfield.len() {
            return ptr::null_mut();
        }
        let page_span = match super::checked_object_page_span_bytes(pg_num) {
            Ok(span) => span,
            Err(_) => return ptr::null_mut(),
        };
        if pg_count
            .checked_mul(pg_align)
            .filter(|span| *span <= page_span)
            .is_none()
        {
            return ptr::null_mut();
        }
        for (base_idx, bitval) in bitfield.iter_mut().enumerate() {
            let first_idx = match base_idx.checked_mul(BITFIELD_BITS) {
                Some(first_idx) => first_idx,
                None => return ptr::null_mut(),
            };
            if first_idx >= pg_count {
                *bitval = 0;
                continue;
            }
            let remaining = pg_count - first_idx;
            *bitval = if remaining >= BITFIELD_BITS {
                u32::MAX
            } else {
                (1_u32 << remaining) - 1
            };
        }
        self.counter = pg_count;
        self.data
    }

    /// Deallocates a memory object within this page.
    pub(crate) fn deallocate(
        &mut self,
        ptr: NonNull<u8>,
        bitfield: &mut [u32],
        pg_count: usize,
        pg_align: usize,
    ) -> Result<()> {
        let page_span = pg_count.checked_mul(pg_align).ok_or(AllocError::ESIZE)?;
        let base_addr = self.data as usize;
        let num = match Self::object_index_in_span(
            base_addr,
            ptr.as_ptr() as usize,
            pg_count,
            pg_align,
            page_span,
        )? {
            Some(num) => num,
            None => return Err(AllocError::EOOB),
        };
        let (idx, mask) = Self::bit_slot(bitfield.len(), num)?;
        let bitval = bitfield[idx];
        if self.counter == 0 {
            return Err(AllocError::EUAF);
        }
        if bitval & mask == 0 {
            return Err(AllocError::EDBFRE);
        }
        let newval = bitval & !mask;
        bitfield[idx] = newval;
        self.counter -= 1;
        Ok(())
    }

    /// Deallocates a memory object within this page.
    pub(crate) fn deallocate_batch(
        &mut self,
        res_array: &mut [usize],
        bitfield: &mut [u32],
        pg_count: usize,
        pg_align: usize,
        pg_num: usize,
    ) -> Result<usize> {
        if pg_align == 0 {
            return Err(AllocError::ELAYOUT);
        }
        let page_span = super::checked_object_page_span_bytes(pg_num)?;
        let seen_words = Self::bitfield_word_count(pg_count)?;
        let mut seen_bitmap = [0_u32; MAX_DEALLOCATE_BATCH_SEEN_WORDS];
        let use_seen_bitmap = seen_words <= MAX_DEALLOCATE_BATCH_SEEN_WORDS;
        let ans = if use_seen_bitmap {
            self.validated_deallocate_batch_prefix(
                res_array,
                bitfield,
                pg_count,
                pg_align,
                page_span,
                Some(&mut seen_bitmap[..seen_words]),
            )?
        } else {
            self.validated_deallocate_batch_prefix(
                res_array, bitfield, pg_count, pg_align, page_span, None,
            )?
        };

        if use_seen_bitmap {
            let cleared = Self::clear_deallocated_batch_bitmap(
                bitfield,
                &seen_bitmap[..seen_words],
                pg_count,
            )?;
            debug_assert_eq!(cleared, ans);
        } else {
            let base_addr = self.data as usize;
            for ptr in res_array.iter().take(ans).copied() {
                let object_idx =
                    Self::object_index_in_span(base_addr, ptr, pg_count, pg_align, page_span)?
                        .ok_or(AllocError::EOOB)?;
                let (idx, mask) = Self::bit_slot(bitfield.len(), object_idx)?;
                bitfield[idx] &= !mask;
            }
        }
        self.counter = self.counter.checked_sub(ans).ok_or(AllocError::EFATAL)?;
        Ok(ans)
    }

    pub(crate) fn allocate_batch(
        &mut self,
        align: usize,
        res_array: &mut [usize],
        start: usize,
        count: usize,
        bitfield: &mut [u32],
        pg_count: usize,
        pg_align: usize,
        pg_num: usize,
    ) -> usize {
        if self.counter >= pg_count
            || self.data.is_null()
            || align == 0
            || !align.is_power_of_two()
            || pg_align == 0
            || pg_num == 0
            || count == 0
        {
            return 0;
        }

        let base_addr = self.data as usize;
        let page_span = match super::checked_object_page_span_bytes(pg_num) {
            Ok(span) => span,
            Err(_) => return 0,
        };
        // Strict-alignment splits can find many matching slots in one slab page.
        // Cap the returned batch so one thread cache does not privatize too many
        // aligned objects at once; leftover aligned/skipped slots stay on the
        // slab/page for reuse by other caches. This is a fragmentation policy,
        // not just a correctness guard.
        let requested = count.min(Self::strict_alignment_batch_return_limit(align, pg_align));
        let mut allocated = 0_usize;
        for (base_idx, bitval) in bitfield.iter_mut().enumerate() {
            let valid_mask = match Self::valid_object_mask_for_word(base_idx, pg_count) {
                Ok(Some(mask)) => mask,
                Ok(None) | Err(_) => return allocated,
            };
            let mut free_mask = !*bitval & valid_mask;
            while let Some(first_free) = Self::take_next_free_bit(&mut free_mask) {
                let idx = match base_idx
                    .checked_mul(BITFIELD_BITS)
                    .and_then(|base| base.checked_add(first_free))
                {
                    Some(idx) => idx,
                    None => return allocated,
                };
                let offset = match idx.checked_mul(pg_align) {
                    Some(offset)
                        if offset
                            .checked_add(pg_align)
                            .filter(|end| *end <= page_span)
                            .is_some() =>
                    {
                        offset
                    }
                    _ => return allocated,
                };
                let addr = match base_addr.checked_add(offset) {
                    Some(addr) => addr,
                    None => return allocated,
                };
                if addr & (align - 1) != 0 {
                    continue;
                }
                let out_idx = match start.checked_add(allocated) {
                    Some(idx) => idx,
                    None => return allocated,
                };
                let slot = match res_array.get_mut(out_idx) {
                    Some(slot) => slot,
                    None => return allocated,
                };
                *slot = addr;
                *bitval |= 1_u32 << first_free;
                allocated += 1;
                self.counter += 1;
                if allocated >= requested {
                    return allocated;
                }
            }
        }
        allocated
    }
}
