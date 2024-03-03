use crate::collections::radix_tree::{get_rd_tree, RadixTree};
use crate::error::{AllocError, Result};
use crate::prelude::*;
use crate::sc::SCAllocator;
use crate::sc::META_BUMP;
#[cfg(not(feature = "fixed_heap"))]
use crate::sync::PthreadMutex as Mutex;
use alloc::{boxed::Box, slice};
use core::alloc::{Allocator, GlobalAlloc, Layout};
use core::borrow::BorrowMut;
use core::cmp;
use core::mem::MaybeUninit;
use core::ops::Deref;
use core::ptr::{null_mut, NonNull};
use core::sync::atomic::{AtomicPtr, Ordering};
#[cfg(feature = "fixed_heap")]
use spin::Mutex;

/// An allocator holding a bunch of slabs
///
/// It dispatches the allocation request to different slab
/// according to the index of size class
pub struct ZoneAllocator {
    slabs: [Mutex<SCAllocator>; TOTAL_SIZE_CLASS],
}

impl ZoneAllocator {
    pub fn new() -> Self {
        let mut ans = Self {
            slabs: unsafe { MaybeUninit::uninit().assume_init() },
        };
        for (idx, item) in ans.slabs.iter_mut().enumerate() {
            unsafe {
                core::ptr::write(
                    item,
                    Mutex::new(SCAllocator::new(
                        get_rounded_size_by_idx(idx),
                        get_num_pages_by_idx(idx),
                    )),
                );
            }
        }
        ans
    }

    // /// Allocates a chunk from a specific slab described by `idx`
    // pub fn allocate_from_slab(&mut self, idx: usize, align: usize) -> Result<NonNull<u8>> {
    //     debug_assert!(idx < self.slabs.len());
    //     let sc: &mut Mutex<SCAllocator> = &mut self.slabs[idx];
    //     sc.lock().allocate(align, get_rd_tree())
    // }

    /// Allocates a batch of chunks from a specific slab described by `idx`
    pub fn allocate_batch_from_slab(
        &mut self,
        idx: usize,
        align: usize,
    ) -> Result<(*mut u8, usize, Option<usize>)> {
        debug_assert!(idx < self.slabs.len(), "idx: {}", idx);
        let sc: &mut Mutex<SCAllocator> = &mut self.slabs[idx];
        sc.lock().allocate_batch_v2(align, get_rd_tree())
    }

    // /// Deallocates a chunk to the slab desceibed by `idx`
    // pub fn deallocate_to_slab(&mut self, idx: usize, ptr: NonNull<u8>) -> Result<()> {
    //     assert!(idx < self.slabs.len());
    //     let sc: &mut Mutex<SCAllocator> = &mut self.slabs[idx];
    //     sc.lock().deallocate(ptr, get_rd_tree());
    //     // TODO: remove this result
    //     Ok(())
    // }

    pub fn deallocate_batch_to_slab(&mut self, idx: usize, ptr: *mut u8) -> Result<()> {
        assert!(idx < self.slabs.len());
        let sc: &mut Mutex<SCAllocator> = &mut self.slabs[idx];
        sc.lock().deallocate_batch(ptr as *mut usize, get_rd_tree())
    }
}

impl ZoneAllocator {
    pub fn allocate_large(&mut self, layout: Layout) -> Result<NonNull<u8>> {
        unsafe {
            let ptr = GlobalBackend.alloc(layout);
            let res = NonNull::new_unchecked(ptr);
            Ok(res)
        }
    }

    pub fn deallocate_large(&mut self, page_ptr: NonNull<u8>, layout: Layout) {
        unsafe {
            GlobalBackend.dealloc(page_ptr.as_ptr(), layout);
        }
    }
}
#[cfg(not(feature = "fixed_heap"))]
atomic_static! {
    pub static ref GLOBAL_ZONE: ZoneAllocator = {ZoneAllocator::new()};
}
#[cfg(feature = "fixed_heap")]
pub mod Fixed_Zone {
    use crate::zone::ZoneAllocator;
    use core::ptr::null_mut;

    pub struct GLOBAL_ZONE;

    pub static mut GLOBAL_ZONE_ptr: *mut ZoneAllocator = null_mut();

    impl core::ops::Deref for GLOBAL_ZONE {
        type Target = ZoneAllocator;

        fn deref(&self) -> &Self::Target {
            unsafe {
                assert!(!GLOBAL_ZONE_ptr.is_null());
                GLOBAL_ZONE_ptr.as_ref().expect("err")
            }
        }
    }

    impl core::ops::DerefMut for GLOBAL_ZONE {
        fn deref_mut(&mut self) -> &mut Self::Target {
            unsafe {
                assert!(!GLOBAL_ZONE_ptr.is_null());
                GLOBAL_ZONE_ptr.as_mut().expect("err")
            }
        }
    }
}
#[cfg(feature = "fixed_heap")]
pub use Fixed_Zone::*;

// ZoneAllocator
// pub struct CentralFreelist;
//
// unsafe impl GlobalAlloc for CentralFreelist {
//     unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
//         match (*GLOBAL_ZONE).allocate(layout.size(), layout.align()) {
//             Ok(r) => r.as_ptr(),
//             Err(_) => core::ptr::null_mut(),
//         }
//     }
//
//     unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
//         (*GLOBAL_ZONE).deallocate(NonNull::new_unchecked(ptr), layout.size())
//     }
//
//     unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
//         let old_idx = get_size_class(layout.size()).index();
//         let new_idx = get_size_class(new_size).index();
//         if old_idx == new_idx {
//             ptr
//         } else {
//             let new_ptr_res = (*GLOBAL_ZONE).allocate_from_slab(new_idx, layout.align());
//             if new_ptr_res.is_err() {
//                 return core::ptr::null_mut();
//             }
//             let new_ptr = new_ptr_res.expect("impossible").as_ptr();
//             if !new_ptr.is_null() {
//                 // SAFETY: the previously allocated block cannot overlap the newly allocated block.
//                 // The safety contract for `dealloc` must be upheld by the caller.
//                 core::ptr::copy_nonoverlapping(ptr, new_ptr, cmp::min(layout.size(), new_size));
//
//                 // free the old memory
//                 (*GLOBAL_ZONE)
//                     .deallocate_to_slab(old_idx, NonNull::new_unchecked(ptr))
//                     .expect("dealloc error");
//             }
//             new_ptr
//         }
//     }
// }
//
// unsafe impl Allocator for CentralFreelist
// where
//     CentralFreelist: GlobalAlloc,
// {
//     fn allocate(
//         &self,
//         layout: Layout,
//     ) -> core::result::Result<NonNull<[u8]>, core::alloc::AllocError> {
//         unsafe {
//             let p = self.alloc(layout);
//             // The actual size may be larger than the request
//             Ok(NonNull::new_unchecked(slice::from_raw_parts_mut(
//                 p,
//                 layout.size(),
//             )))
//         }
//     }
//
//     unsafe fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
//         self.dealloc(ptr.as_ptr() as *mut u8, layout);
//     }
// }

#[cfg(test)]
mod test {
    use super::*;

    // #[test]
    // fn allocate_batch_sanity_check() {
    //     let mut batch = [1usize, 512];
    //     let layout = Layout::from_size_align(33, 8).expect("cannot create");
    //     let res = (*GLOBAL_ZONE).allocate_batch(layout, None, &mut batch, 1);
    // }

    // #[test]
    // fn allocate_batch_off_by_one() {
    //     let mut batch = [1usize, 512];
    //     let layout = Layout::from_size_align(33, 8).expect("cannot create");
    //     let res = (*GLOBAL_ZONE).allocate_batch(layout, None, &mut batch, 513);
    //     let res = (*GLOBAL_ZONE).allocate_batch(layout, None, &mut batch, 514);
    // }

    // #[test]
    // fn allocate_batch_oob() {
    //     let mut batch = [1usize, 512];
    //     let layout = Layout::from_size_align(33, 8).expect("cannot create");
    //     let res = (*GLOBAL_ZONE).allocate_batch(layout, None, &mut batch, 1025);

    //     let mut batch = [1usize, 0];
    //     let layout = Layout::from_size_align(33, 8).expect("cannot create");
    //     let res = (*GLOBAL_ZONE).allocate_batch(layout, None, &mut batch, 2);
    // }

    // #[test]
    // fn allocate_batch_redundant() {
    //     let mut batch = [1usize; 512];
    //     let layout = Layout::from_size_align(33, 8).expect("cannot create");
    //     let res = (*GLOBAL_ZONE).allocate_batch(layout, None, &mut batch, 101);

    //     for i in 1..100 {
    //         assert_ne!(batch[i], 1);
    //     }
    //     for i in 101..512 {
    //         assert_eq!(batch[i], 1);
    //     }
    // }

    // #[test]
    // fn dellocate_batch_sanity_check() {
    //     let mut batch = [0usize, 512];
    //     let layout = Layout::from_size_align(32, 8).expect("cannot create");
    //     let res = (*GLOBAL_ZONE).allocate_batch(layout, None, &mut batch, 64);
    //     let mut batch2 = &mut batch[63..];
    //     zone.deallocate_batch(&mut batch2, 1, layout, None)
    //         .expect("cannot dealloc batch");
    // }
}
