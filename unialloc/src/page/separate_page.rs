use crate::error::{AllocError, Result};
use crate::prelude::*;
use crate::*;
use alloc::boxed::Box;
use core::alloc::{GlobalAlloc, Layout};
use core::ptr::{self, NonNull};

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

impl ObjectPage {
    pub const fn new() -> Self {
        Self {
            counter: 0,
            data: ptr::null_mut(),
        }
    }

    pub fn allocate_page(&mut self, pg_num: usize) -> *mut u8 {
        let ans = unsafe {
            let layout = Layout::from_size_align_unchecked(PAGE_SIZE * pg_num, 8);
            GlobalBackend.alloc(layout)
        };
        assert_ne!(ans as usize, 0);
        self.data = ans;
        ans
    }

    //This function is used only in sc.rs, for more details, please refer to sc:deallocate
    pub fn get_data_ptr(&self) -> *mut u8 {
        if !self.data.is_null() {
            self.data
        } else {
            panic!("error double free")
        }
    }

    pub fn destroy_page(&mut self, pg_num: usize) -> *mut u8 {
        if !self.data.is_null() {
            let p = self.data;
            unsafe {
                let layout = Layout::from_size_align_unchecked(PAGE_SIZE * pg_num, 8);
                GlobalBackend.dealloc(p as *mut u8, layout);
            }
            self.data = ptr::null_mut();
            p
        } else {
            panic!("errror double free")
        }
    }

    #[inline]
    pub fn is_inited(&self) -> bool {
        !self.data.is_null()
    }
}

impl ObjectPage {
    fn first_fit(
        &mut self,
        base_addr: usize,
        align: usize,
        bitfield: &mut [u32],
        pg_count: usize,
        pg_align: usize,
        pg_num: usize,
    ) -> Option<(usize, usize)> {
        for (base_idx, bitval) in bitfield.iter().enumerate() {
            if *bitval == u32::MAX {
                continue;
            }
            let mut first_free = (*bitval).trailing_ones() as usize;
            while first_free < 32 {
                let idx: usize = base_idx * 32 + first_free;
                if idx > pg_count {
                    return None;
                }
                let offset = idx * pg_align;

                if offset + pg_align > PAGE_SIZE * pg_num {
                    return None;
                }

                let addr: usize = base_addr + offset;
                let alignment_ok = addr & (align - 1) == 0;
                let block_is_free = (*bitval) & (1 << first_free) == 0;
                if alignment_ok && block_is_free {
                    bitfield[base_idx] = (*bitval) | (1 << first_free);
                    return Some((idx, addr));
                }

                first_free += 1;
            }
        }
        None
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
        if self.counter >= pg_count {
            ptr::null_mut()
        } else {
            let base_addr = (self.data as *const u8) as usize;
            match self.first_fit(base_addr, align, bitfield, pg_count, pg_align, pg_num) {
                Some((_, addr)) => {
                    self.counter += 1;
                    addr as *mut u8
                }
                None => ptr::null_mut(),
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

    pub(crate) fn allocate_all(&mut self, bitfield: &mut [u32], pg_count: usize) -> *mut u8 {
        for (_base_idx, bitval) in bitfield.iter_mut().enumerate() {
            *bitval = u32::MAX;
        }
        self.counter = pg_count;
        self.data
    }

    /// Deallocates a memory object within this page.
    pub(crate) fn deallocate(
        &mut self,
        ptr: NonNull<u8>,
        bitfield: &mut [u32],
        pg_align: usize,
    ) -> Result<()> {
        let base_addr = (self.data as *const u8) as usize;
        let page_offset = (ptr.as_ptr() as usize) - base_addr;
        let num = page_offset / pg_align;
        let idx = num / 32;
        let rem = num % 32;
        let bitval = bitfield[idx];
        if bitval & (1 << rem) == 0 {
            return Err(AllocError::EDBFRE);
        }
        let newval = bitval & (!(1 << rem));
        bitfield[idx] = newval;
        self.counter -= 1;
        Ok(())
    }

    /// Deallocates a memory object within this page.
    pub(crate) fn deallocate_batch(
        &mut self,
        res_array: &mut [usize],
        bitfield: &mut [u32],
        pg_align: usize,
        pg_num: usize,
    ) -> usize {
        let base_addr = (self.data as *const u8) as usize;
        let mut ans = 0usize;
        for it in res_array.iter() {
            let ptr = *it;
            if ptr < base_addr || ptr - base_addr >= pg_num * PAGE_SIZE {
                break;
            }
            let page_offset = ptr - base_addr;
            let num = page_offset / pg_align;
            let idx = num / 32;
            let rem = num & 31;
            assert_ne!(bitfield[idx] & (1 << rem), 0);
            bitfield[idx] &= !(1 << rem);
            ans += 1;
        }
        self.counter -= ans;
        ans
    }

    pub(crate) fn allocate_batch(
        &mut self,
        res_array: &mut [usize],
        start: usize,
        count: usize,
        bitfield: &mut [u32],
        pg_count: usize,
        pg_align: usize,
    ) -> usize {
        if self.counter >= pg_count {
            panic!("error tracing obj status, (counter: {:x})", self.counter)
        } else {
            let mut allocated = 0_usize;
            for (base_idx, bitval) in bitfield.iter_mut().enumerate() {
                if *bitval == u32::MAX {
                    continue;
                }
                let mut first_free = (*bitval).trailing_ones() as usize;
                while first_free < 32 {
                    let n: u32 = 1_u32 << first_free;
                    if n & (*bitval) == 0 {
                        let idx: usize = base_idx * 32 + first_free;
                        if idx >= pg_count {
                            return allocated;
                        }
                        let offset = idx * pg_align;
                        let addr: usize = self.data as usize + offset;
                        res_array[start + allocated] = addr;
                        (*bitval) |= 1 << first_free;
                        allocated += 1;
                        self.counter += 1;
                        if allocated >= count {
                            return allocated;
                        }
                    }
                    first_free += 1;
                }
            }
            allocated
        }
    }
}
