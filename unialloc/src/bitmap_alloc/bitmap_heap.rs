use super::simd_bitmap::SIMDBitmap512;
use crate::collections::linklist::ArrayLinkedList;
use crate::error::{self, Result};
use crate::prelude::*;
use alloc::boxed::Box;
use alloc::vec::Vec;
use core::borrow::BorrowMut;
use core::cell::{Cell, RefCell};
use core::ptr::NonNull;
use libc::ENOMEM;

// 1 ~ 2000K

// 4K, 8K, 8K×2, 8K×3, 8K×4, 8K×5, 8K×6, 8K×7, 8K×8, 8K×9,
// 8K×10, 8K×11, 8K×12, 8K×13, 8K×16, 8K×32, 8K×64。

// 1-9: <=128  -> single bit
// 10-17 > 128 -> multiple bits

// 3409 ~ 10K
// 10K ~ 2M

// base const numbers
const FOUR_K: usize = 1 << 12;
const EIGHT_K: usize = 2 * FOUR_K;

// 8, 16, 32, 48, 64, 80, 96, 112, 128

trait AllocableChunk {
    fn new() -> Self;
    fn reset(&mut self);
    fn get_first_free_bit(&self) -> u64;
    fn claim_first_free_bit(&self) -> u64;
    fn unset_bit(&mut self, index: usize);
    fn size() -> usize;
    fn is_full(&self) -> bool;
}

#[repr(C)]
struct AllocablePage4K {
    bitmap: SIMDBitmap512,
    data: [u8; FOUR_K - 128],
}

impl AllocablePage4K {
    pub fn new() -> Self {
        Self {
            bitmap: SIMDBitmap512::new(),
            data: [0; FOUR_K - 128],
        }
    }

    /// A common reset interface
    pub fn reset(&mut self) {
        self.fill_zero();
    }

    /// Resets memory to zero
    pub fn fill_zero(&mut self) {
        self.data.fill(0);
        self.bitmap.fill_zero();
    }

    /// Gets the index of first available bit (zero-indexed)
    pub fn get_first_free_bit(&self) -> u64 {
        let unaviable_bits = 128 / 8;
        // the first set of bits are occupied by the bitmap metadata
        self.bitmap.select(unaviable_bits)
    }

    /// Gets the index of first free bit and set it
    pub fn claim_first_free_bit(&mut self) -> u64 {
        let bitidx = self.get_first_free_bit();
        self.bitmap.flip(bitidx as usize);
        bitidx
    }

    /// Flips the bit at given index
    pub fn flip_bit(&mut self, idx: usize) {
        self.bitmap.flip(idx);
    }

    /// Gets the bit at the given idx
    pub fn get_bit(&self, idx: usize) -> bool {
        self.bitmap.get(idx)
    }

    /// Releases the bit at given `index`
    ///
    /// # Safety:
    /// index-th bit must be `1` before releasing
    pub fn unset_bit(&mut self, index: usize) {
        debug_assert_eq!(self.get_bit(index), true);

        self.flip_bit(index);
    }

    /// Returns size of current type
    pub fn size() -> usize {
        FOUR_K
    }

    pub fn is_full(&self) -> bool {
        self.bitmap.real_size() == (512 - 16)
    }
}

struct SLABAllocator<const LEN: usize, T: AllocableChunk> {
    slabs: RefCell<Vec<*mut T, GlobalBackend>>,
    last_used: Cell<*mut T>,
}

impl<const LEN: usize, T: AllocableChunk> SLABAllocator<LEN, T> {
    // pub fn new() -> Self {
    //     Self {
    //         slabs: Default::default(),
    //         last_used: Default::default(),
    //     }
    // }

    // pub fn allocate(&mut self) -> Result<NonNull<u8>> {
    //     let slabs = self.slabs.borrow_mut();
    //     if slabs.len() == 0 {
    //         self.add_new_allocable_page();
    //     }

    //     let selected_page: &mut T;

    //     for i in self.slabs.borrow().into_iter() {
    //         let page = unsafe { &mut *i };
    //         if !page.is_full() {
    //             selected_page = page;
    //             break;
    //         }
    //     }

    //     let index = selected_page.claim_first_free_bit();
    //     let addr = selected_page as *mut T as usize + index as usize * 8;

    //     Ok(NonNull::new(addr as *mut u8).unwrap())
    // }

    /// Releases the given pointer and unset the corresponding bit
    ///
    /// # Safety
    ///
    /// Caller needs to make sure `ptr` is valid
    pub fn deallocate(&mut self, ptr: NonNull<u8>) {
        // find metadata
        let ptr_addr = ptr.as_ptr() as usize;
        let base = core::ptr::addr_of!(self) as usize;

        let delta = ptr_addr - base;
        let index = delta / LEN;

        self.last_used.set(base as *mut T);
        let allocable_page = unsafe { &mut **self.last_used.get_mut() };
        allocable_page.unset_bit(index)
    }

    fn add_new_allocable_page(&mut self) {
        let new_page = Box::new_in(T::new(), GlobalBackend);
        let new_page_ptr = Box::into_raw(new_page);

        self.slabs.borrow_mut().push(new_page_ptr);
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn sanity() {}
}
