//! We support different allocation APIS for various usage
// use crate::prelude::*;
// use alloc::alloc::{Allocator, GlobalAlloc, Layout};
// use core::ptr::NonNull;
//
// mod cpu_cache;
//
// use cpu_cache::GLOBAL_CPU_CACHE as GlobalFrontend;
//
// #[derive(Copy, Clone)]
// pub struct RustAllocator;
//
// unsafe impl GlobalAlloc for RustAllocator {
//     unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
//         if let Ok(ptr) = (&mut GlobalFrontend).allocate(layout) {
//             ptr.as_ptr()
//         } else {
//             core::ptr::null_mut()
//         }
//     }
//
//     unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
//         (&mut GlobalFrontend).deallocate(core::ptr::NonNull::new_unchecked(ptr), layout);
//     }
//
//     unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
//         let old_slab_idx = get_size_class(layout.size()).index();
//         let new_slab_idx = get_size_class(new_size).index();
//
//         if old_slab_idx == new_slab_idx {
//             ptr
//         } else {
//             let new_layout = Layout::from_size_align_unchecked(new_size, layout.align());
//             // SAFETY: the caller must ensure that `new_layout` is greater than zero.
//             let new_ptr = self.alloc(new_layout);
//             if !new_ptr.is_null() {
//                 // SAFETY: the previously allocated block cannot overlap the newly allocated block.
//                 // The safety contract for `dealloc` must be upheld by the caller.
//                 core::ptr::copy_nonoverlapping(
//                     ptr,
//                     new_ptr,
//                     core::cmp::min(layout.size(), new_size),
//                 );
//                 self.dealloc(ptr, layout);
//             }
//             new_ptr
//         }
//     }
// }
//
// unsafe impl Allocator for RustAllocator {
//     fn allocate(&self, layout: Layout) -> Result<NonNull<[u8]>, core::alloc::AllocError> {
//         unsafe {
//             let p = self.alloc(layout);
//             // TODO: eliminate redundant overhead
//             // can we avoid using get_index function?
//             // let (_, alloc_size) = get_index_and_size(layout.size());
//             Ok(core::ptr::NonNull::new_unchecked(
//                 core::slice::from_raw_parts_mut(p, layout.size()),
//             ))
//         }
//     }
//
//     unsafe fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
//         self.dealloc(ptr.as_ptr() as *mut u8, layout);
//     }
// }

pub mod type_aware;
pub mod type_isolation;
