//! Linklist based thread local cache
use crate::error::{AllocError, Result};
use crate::mm::linklist::Linklist;
use crate::sc::MetadataAllocator;
use crate::sc::META_BUMP;
use crate::size_class::*;
use crate::zone::GLOBAL_ZONE;
use crate::*;
use alloc::boxed::Box;
use core::cell::RefCell;
use core::ptr::null_mut;
use core::{
    alloc::{Allocator, GlobalAlloc, Layout},
    ptr::NonNull,
};

include!(concat!(env!("OUT_DIR"), "/sizeclass_consts.rs"));

#[derive(Clone, Copy)]
struct ThreadCacheUnit {
    list: Linklist,
    bump_ptr: usize,
    bump_count: i32,
    bump_unit: i32,
}

impl ThreadCacheUnit {
    pub const fn new() -> Self {
        Self {
            list: Linklist::new(),
            bump_ptr: 0,
            bump_count: 0,
            bump_unit: 0,
        }
    }

    pub fn clean_up(&mut self, idx: usize) {
        while self.bump_count > 0 {
            self.free(self.bump_ptr as *mut u8);
            self.bump_ptr += self.bump_unit as usize;
            self.bump_count -= 1;
        }
        if self.list.length() > 0 {
            (*GLOBAL_ZONE)
                .deallocate_batch_to_slab(idx, self.list.link as *mut u8)
                .expect("dealloc err");
        }
        *self = Self::new();
    }

    // fn validate(&self){
    //     let mut head = self.list.link;
    //     let mut counter = 0;
    //     while head !=0 {
    //         counter += 1;
    //         head = unsafe{*(head as * mut usize)};
    //     }
    //     assert_eq!(counter, self.list.length);
    // }

    fn free(&mut self, ptr: *mut u8) {
        self.list.push_unchecked(ptr);
    }

    pub fn deallocate(&mut self, idx: usize, ptr: NonNull<u8>, size: usize) {
        self.list.push_unchecked(ptr.as_ptr());

        // a test
        return;

        if self.list.length * size > (1usize << 28) {
            //return half to back
            let half = self.list.length / 2;
            let mut counter = 1usize;
            let mut cur = self.list.link;
            while counter < half {
                cur = unsafe { *(cur as *mut usize) };
                counter += 1;
            }
            let to_free = unsafe { *(cur as *mut usize) };
            unsafe { *(cur as *mut usize) = 0 };
            self.list.length = counter;
            //self.validate();
            (*GLOBAL_ZONE)
                .deallocate_batch_to_slab(idx, to_free as *mut u8)
                .expect("dealloc err");
        }
    }

    pub fn allocate(&mut self, idx: usize, align: usize) -> NonNull<u8> {
        //case 1: we can reuse previous
        if self.list.length > 0 {
            let ans = self.list.pop_unchecked_aligned(align);
            if !ans.is_null() {
                return NonNull::new(ans).expect("err");
            }
        }
        //case 2: if we have bump
        let mut ans = 0usize;
        while self.bump_count > 0 && ans == 0 {
            if self.bump_ptr & (align - 1) == 0 {
                ans = self.bump_ptr;
            } else {
                //else we will consume all the bump allocation and then fall into backend if needed
                self.free(self.bump_ptr as *mut u8);
            }
            self.bump_ptr += self.bump_unit as usize;
            self.bump_count -= 1;
        }
        if ans != 0 {
            NonNull::new(ans as *mut u8).expect("err")
        } else {
            //allocate from back
            let alloc_res = (*GLOBAL_ZONE).allocate_batch_from_slab(idx, align);

            if let Ok(back_alloc) = alloc_res {
                if let Some(bump) = back_alloc.2 {
                    self.bump_count = (back_alloc.1 - 1) as i32;
                    self.bump_unit = bump as i32;
                    self.bump_ptr = back_alloc.0 as usize + bump;
                    ans = back_alloc.0 as usize;
                } else {
                    assert_eq!(self.list.length, 0);
                    ans = back_alloc.0 as usize;
                    let head = unsafe { *(ans as *mut usize) };
                    self.list.link = head;
                    self.list.length = back_alloc.1 - 1;
                    //self.validate();
                }
                NonNull::new(ans as *mut u8).expect("err")
            } else {
                panic!();
            }
        }
    }
}

fn get_upper_bits(ptr: usize) -> usize {
    ptr >> 48
}

fn set_upper_bits(ptr: usize, mask: usize) {
    mask << 48 | ptr;
}

fn has_upper_bits(ptr: usize) -> bool {
    ptr >> 48 > 0
}

#[repr(align(8))]
pub struct ThreadCache {
    list: [ThreadCacheUnit; TOTAL_SIZE_CLASS],
    // queue: usize,
}

impl ThreadCache {
    pub const fn new() -> Self {
        Self {
            list: [ThreadCacheUnit::new(); TOTAL_SIZE_CLASS],
        }
    }
    pub fn init(&mut self) {}
    //todo dealloc batch size array might be too large
    pub fn cleanup_cache_unchecked(&mut self) {
        for idx in 1..self.list.len() {
            let list: &mut ThreadCacheUnit = &mut self.list[idx];
            list.clean_up(idx);
        }
    }

    pub fn allocate(&mut self, layout: Layout) -> Result<NonNull<u8>> {
        // 1. round the size up to next size class
        let cls = get_size_class(layout.size());

        // 2. try to pop one from freelist
        if let SizeClass::Base(idx) = cls {
            if unlikely(idx == 0) {
                return Ok(NonNull::new(0x100000000000usize as *mut u8).expect("err"));
            }
            let size_cache: &mut ThreadCacheUnit = &mut self.list[idx];
            let ans = size_cache.allocate(idx, layout.align());
            Ok(ans)
        } else {
            // 3. Large size class goes to zone directly
            //todo return Self::alloc_from_zone(new_layout, Some((cls, size))).or(Err(AllocError));
            if let Ok(ans) = (*GLOBAL_ZONE).allocate_large(layout) {
                Ok(ans)
            } else {
                Err(AllocError::ENOMEM)
            }
        }
    }

    pub fn deallocate(&mut self, ptr: NonNull<u8>, layout: Layout) {
        // self.handle_delay_case(ptr.as_ptr() as usize, layout.size());
        // return;

        // 1. round the size up to next size class
        let cls = get_size_class(layout.size());

        // 2. try to push the ptr to freelist
        if let SizeClass::Base(idx) = cls {
            if unlikely(idx == 0) {
                return;
            }
            // The thread local free list is full
            let size_cache: &mut ThreadCacheUnit = &mut self.list[idx];
            size_cache.deallocate(idx, ptr, get_rounded_size_by_idx(idx));
        } else {
            // 3. for large chunks, the deallocation directly goes to zone
            //todo
            (*GLOBAL_ZONE).deallocate_large(ptr, layout);
        }
    }

    pub fn handle_delay_case(&mut self, ptr: usize, size: usize) {}
}

use super::*;
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sync::general_thread_local::{register_tls_key, save_tls};
use alloc_macros::tls_static;
#[cfg(not(feature = "fixed_heap"))]
unsafe extern "C" fn free_thread_cache(ptr: *mut libc::c_void) {
    // println!("dtor triggered! {:x}", ptr as usize);
    let ptr = ptr as *mut ThreadCache;
    let tcache = ptr.as_mut().expect("err");
    tcache.cleanup_cache_unchecked();
    META_BUMP.lock().dealloc(ptr as *mut usize);
}
#[cfg(not(feature = "fixed_heap"))]
tls_static! {
    ThreadCache GlobalTcache, free_thread_cache
}

#[cfg(feature = "fixed_heap")]
pub mod Fixed_TCache {
    use crate::cache::ThreadCache;
    use core::ptr::null_mut;

    pub struct GlobalTcache;

    pub static mut GlobalTcache_ptr: *mut ThreadCache = null_mut();

    impl core::ops::Deref for GlobalTcache {
        type Target = ThreadCache;

        fn deref(&self) -> &Self::Target {
            unsafe {
                assert!(!GlobalTcache_ptr.is_null());
                GlobalTcache_ptr.as_ref().expect("err")
            }
        }
    }

    impl core::ops::DerefMut for GlobalTcache {
        fn deref_mut(&mut self) -> &mut Self::Target {
            unsafe {
                assert!(!GlobalTcache_ptr.is_null());
                GlobalTcache_ptr.as_mut().expect("err")
            }
        }
    }
}
#[cfg(feature = "fixed_heap")]
pub use Fixed_TCache::*;

// #[cfg(test)]
// mod tests {
//     use super::*;
//
//     struct B;
//     impl Drop for B {
//         fn drop(&mut self) {
//             println!("dropin");
//         }
//     }
//
//     #[cfg(target_os = "linux")]
//     #[test]
//     fn tls_drop_test() {
//         use core::sync::atomic::{AtomicI32, Ordering};
//         extern crate std;
//         use static_init::dynamic;
//         use std::thread::spawn;
//
//         #[dynamic(drop)]
//         #[thread_local]
//         static B1: B = B;
//
//         std::thread::spawn(|| {
//             let _ = &*B1;
//         })
//         .join()
//         .unwrap();
//     }
//
//     #[cfg(target_os = "linux")]
//     #[test]
//     fn static_init_tcache_drop_test() {
//         use core::sync::atomic::{AtomicI32, Ordering};
//         extern crate std;
//         use std::thread::spawn;
//
//         spawn(move || {
//             let _x = get_thread_cache();
//         })
//         .join()
//         .unwrap();
//     }
// }
