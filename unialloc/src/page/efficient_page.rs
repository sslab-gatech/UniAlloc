use crate::error::{AllocError, Result};
use crate::prelude::*;
use crate::*;
use alloc::boxed::Box;
use core::alloc::{GlobalAlloc, Layout};
use core::ptr::{self, null_mut, NonNull};
include!(concat!(env!("OUT_DIR"), "/consts.rs"));
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
    pub fn get_prev(&self) -> &mut ObjectPage {
        unsafe {
            (self.prev as *const ObjectPage as *mut ObjectPage)
                .as_mut()
                .expect("err")
        }
    }

    #[allow(clippy::mut_from_ref)]
    pub fn get_next(&self) -> &mut ObjectPage {
        unsafe {
            (self.next as *const ObjectPage as *mut ObjectPage)
                .as_mut()
                .expect("err")
        }
    }

    pub fn set_prev(&mut self, nprev: usize) {
        self.prev = nprev;
    }

    pub fn set_next(&mut self, nnext: usize) {
        self.next = nnext;
    }

    pub fn allocate_page(&mut self, pg_num: usize) -> *mut u8 {
        let ans = unsafe {
            let layout = Layout::from_size_align_unchecked(PAGE_SIZE * pg_num, 8);
            GlobalBackend.alloc(layout)
        };
        assert_ne!(ans as usize, 0);
        self.data = ans;
        self.ptr = ptr::null_mut();
        self.counter = 0;
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
            self.ptr = ptr::null_mut();
            self.counter = 0;
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
        pg_count: usize,
        pg_align: usize,
    ) -> (*mut u8, usize, Option<usize>) {
        if self.counter == 0 {
            self.counter = pg_count;
            if !self.ptr.is_null() {
                self.ptr = null_mut();
            }
            (self.data, pg_count, Some(pg_align))
        } else {
            let to_allocate = pg_count - self.counter;
            self.counter = pg_count;
            let ans = self.ptr;
            self.ptr = null_mut();
            (ans, to_allocate, None)
        }
    }

    /// Deallocates a memory object within this page.
    pub(crate) fn deallocate(&mut self, free_ptr: usize, pg_num: usize) -> *mut usize {
        //check if the rd tree's record is correct
        let base = self.data as usize;
        assert!(free_ptr >= base);
        assert!(free_ptr < base + pg_num * PAGE_SIZE);
        assert!(self.counter > 0);
        let head = free_ptr as *mut usize;
        let mut cur = head;
        let mut counter = 1;
        let mut next = unsafe { *cur };
        while next >= base && next < base + pg_num * PAGE_SIZE {
            counter += 1;
            cur = next as *mut usize;
            next = unsafe { *cur };
        }

        unsafe { *cur = self.ptr as usize };
        self.ptr = free_ptr as *mut u8;
        assert!(self.counter >= counter);
        self.counter -= counter;
        next as *mut usize
    }
}
