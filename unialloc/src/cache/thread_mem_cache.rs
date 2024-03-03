use crate::error::{AllocError, Result};
use crate::mm::BackendAllocator as GlobalBackend;
use crate::size_class::*;
use crate::zone::GLOBAL_ZONE;
use crate::PAGE_SIZE;
use core::alloc::Layout;
use core::ptr::NonNull;
use prelude::*;

#[repr(align(8))]
pub struct ThreadCache {
    head: [u16; TOTAL_SIZE_CLASS],
    list: [u16; TOTAL_SIZE_CLASS],
}

impl ThreadCache {
    pub fn init(&mut self) {
        let mut base: usize = 2 * (TOTAL_SIZE_CLASS / 4 + 1);
        for i in 1..TOTAL_SIZE_CLASS {
            let cursize = get_rounded_size_by_idx(i);
            let page = get_num_pages_by_idx(i);
            let num = page * 4096 / cursize * 2;
            self.head[i - 1] = base as u16;
            self.list[i] = base as u16;
            base += num;
        }
        self.head[TOTAL_SIZE_CLASS - 1] = base as u16;
        assert!(base <= 14 * 512);
    }

    fn get_self(&self, idx: u16) -> *mut usize {
        let s = self as *const _ as usize;
        (s + (idx as usize) * 8) as *mut usize
    }

    fn get_slice(&mut self, start: u16, end: u16) -> &mut [usize] {
        unsafe { core::slice::from_raw_parts_mut(self.get_self(start), (end - start) as usize) }
    }

    // //todo dealloc batch size array might be too large
    pub fn cleanup_cache(&mut self) {
        for idx in 1..TOTAL_SIZE_CLASS {
            let start = self.head[idx - 1];
            let end = self.list[idx];

            if end > start {
                let arr = self.get_slice(start, end);
                (*GLOBAL_ZONE)
                    .deallocate_batch_to_slab(idx, arr, (end - start) as usize)
                    .expect("cannot deallocate");
            }
        }
    }

    /// Push a pointer into corresponding freelist
    ///
    /// # Return
    ///
    /// - true: successful push
    /// - false: the list is too long
    fn push(&mut self, ptr: *mut u8, cl: usize) -> bool {
        let count = self.list[cl];
        if count >= self.head[cl] {
            return false;
        }
        unsafe {
            let loc = self.get_self(count);
            *loc = ptr as usize;
        }
        self.list[cl] = count + 1;
        assert!(self.list[cl] <= self.head[cl]);
        true
    }

    fn pop_fast(&mut self, cl: usize, _align: usize) -> *mut u8 {
        let count = self.list[cl];
        let i = count - 1;
        if count >= self.head[cl - 1] && _align == 1 {
            let ans = unsafe { *self.get_self(i) };
            self.list[cl] -= 1;
            return ans as *mut u8;
        }
        core::ptr::null_mut()
    }

    fn pop_aligned(&mut self, cl: usize, _align: usize) -> *mut u8 {
        let count = self.list[cl];
        let mut i = count - 1;
        while i >= self.head[cl - 1] {
            let ptr = self.get_self(i);
            let ans = unsafe { *ptr };
            if ans & (_align - 1) == 0 {
                unsafe {
                    *ptr = *(self.get_self(count - 1));
                    self.list[cl] -= 1;
                }
                return ans as *mut u8;
            }
            i -= 1;
        }
        core::ptr::null_mut()
    }
    pub fn allocate(&mut self, layout: Layout) -> Result<NonNull<u8>> {
        // 1. round the size up to next size class
        // let (cls, size) = get_index_and_size(layout.size());
        // let align = layout.align();

        // // 2. try to pop one from freelist
        // if let SizeClass::Base(idx) = cls {
        //     // (1) pop one from thread local freelist
        //     let count = self.list[idx];
        //     if likely(count > OFFSET_ARRAY[idx] && align == 1) {
        //         let ans = unsafe { *self.get_self(count - 1) };
        //         self.list[idx] -= 1;
        //         return Ok(NonNull::new(ans as *mut u8).expect("err"));
        //     } else {
        //         return self.alloc_slowpath(layout, cls, size, align, idx);
        //     }
        // } else if let SizeClass::Large(_) = cls {
        //     // 3. Large size class goes to zone directly
        //     return Self::alloc_from_zone(size, layout.align());
        // }
        // Err(AllocError::ESIZE)

        // get the index in the corresponding size class
        let cls = get_size_class(layout.size());
        let align = layout.align();

        // allocate
        if let SizeClass::Base(idx) = cls {
            // (1) pop one from thread local freelist
            let count = self.list[idx];
            if likely(count > self.head[idx - 1] && align == 1) {
                let ans = unsafe { *self.get_self(count - 1) };
                self.list[idx] -= 1;
                return Ok(NonNull::new(ans as *mut u8).expect("err"));
            } else {
                return self.alloc_slowpath(idx, align);
            }
        } else if let SizeClass::Large(_) = cls {
            // 3. Large size class goes to zone directly
            return Self::alloc_from_zone(layout.size(), align);
        }
        Err(AllocError::ESIZE)
    }

    fn alloc_slowpath(&mut self, idx: usize, align: usize) -> Result<NonNull<u8>> {
        let result = self.pop_aligned(idx, align);
        if result.is_null() {
            // allocate a batch of objects from zone
            let end = self.list[idx];
            assert!(self.head[idx] - end > 0);
            let st = self.head[idx - 1];
            let ed = self.head[idx];
            // let ans = (*GLOBAL_ZONE).allocate_batch(
            //     layout,
            //     Some((cls, size)),
            //     self.get_slice(end, OFFSET_LIMIT[idx]),
            //     (IDX_NP[idx].min(OFFSET_LIMIT[idx] - end)) as usize,
            // );

            let ans = (*GLOBAL_ZONE).allocate_batch_from_slab(
                idx,
                self.get_slice(end, self.head[idx]),
                (((ed - st) / 2) as usize).min((ed - end) as usize),
            );

            if ans.is_ok() {
                // unsafe {freelist.set_len(original_len + IDX_NP[idx])};
                // freelist.swap_remove(original_len);
                self.list[idx] += ((((self.head[idx] - self.head[idx - 1]) / 2) as usize)
                    .min((self.head[idx] - end) as usize)
                    - 1) as u16;
                let ans = unsafe { *self.get_self(self.list[idx]) };
                return Ok(NonNull::new(ans as *mut u8).expect("err"));
            } else {
                return Err(AllocError::ENOMEM);
            }
        }
        Ok(NonNull::new(result).expect("err"))
    }

    pub fn deallocate(&mut self, ptr: NonNull<u8>, layout: Layout) {
        let cls = get_size_class(layout.size());

        if let SizeClass::Base(idx) = cls {
            // The thread local free list is full
            if unlikely(!self.push(ptr.as_ptr(), idx)) {
                let end = self.list[idx];
                let st = self.head[idx - 1];
                let ed = self.head[idx];
                let start = end - ((ed - st) / 2);
                assert!(start >= self.head[idx - 1]);
                let ans = (*GLOBAL_ZONE).deallocate_batch_to_slab(
                    idx,
                    self.get_slice(start, end),
                    ((ed - st) / 2) as usize,
                );
                if ans.is_ok() {
                    self.list[idx] -= ((self.head[idx] - self.head[idx - 1]) / 2) - 1;
                    unsafe {
                        let loc = self.get_self(self.list[idx] - 1);
                        *loc = ptr.as_ptr() as usize;
                    }
                }
            }
        } else if let SizeClass::Large(_) = cls {
            // 3. for large chunks, the deallocation directly goes to zone
            Self::dealloc_to_zone(ptr, layout.size());
        }
    }

    /// A simple trampoline to zone
    #[inline]
    fn alloc_from_zone(sz: usize, align: usize) -> Result<NonNull<u8>> {
        (*GLOBAL_ZONE).allocate_large(sz, align)
    }

    /// A simple trampoline to zone
    #[inline]
    fn dealloc_to_zone(ptr: NonNull<u8>, sz: usize) {
        (*GLOBAL_ZONE).deallocate_large(ptr, sz, 1);
    }
}

use super::*;
use crate::pal::sync::general_thread_local::{register_tls_key, save_tls};
use alloc_macros::tls_static;
use core::alloc::{Allocator, GlobalAlloc};

// An experiemental implementation of dtor of thread_local cache
// To use this feature, we need to guarantee that the undelying thread
// is pthread.
unsafe extern "C" fn free_thread_cache(ptr: *mut libc::c_void) {
    let ptr = ptr as *mut ThreadCache;
    let tcache = ptr.as_mut().expect("err");
    tcache.cleanup_cache();
    GlobalBackend.dealloc(
        ptr as *mut u8,
        Layout::from_size_align_unchecked(14 * PAGE_SIZE, 1),
    );
}

tls_static! {
    ThreadCache GlobalTcache, free_thread_cache
}

use core::intrinsics::{likely, unlikely};
use core::mem::MaybeUninit;
