use crate::mm::BackendAllocator as GlobalBackend;
use crate::*;
use alloc::alloc::{AllocError, Allocator, GlobalAlloc, Layout};
use alloc::boxed::Box;
use core::ptr::{self, write_bytes, NonNull};
use core::slice;
use prelude::*;
// #[cfg(target_os = "linux")]
// pub mod cpu_cache;
// #[cfg(target_os = "linux")]
// mod thread_array_cache;
// #[cfg(target_os = "linux")]
// pub mod thread_cache;
// mod thread_mem_cache;
mod thread_cache;

// #[cfg(target_os = "linux")]
// use cpu_cache::*;
use crate::page::{PageBumpAlloc, PG_BUMP};
use crate::sc::META_BUMP;
pub use thread_cache::*;

#[derive(Copy, Clone)]
pub struct RustAllocator;

impl RustAllocator {
    pub const fn new() -> Self {
        Self {}
    }

    ///
    /// # Safety
    /// This will statically init heap
    pub unsafe fn init(&self, heap_start: usize, heap_size: usize, page_size: usize) {
        META_BUMP
            .lock()
            .init_with_range(heap_start, heap_size + heap_start, page_size);
    }
    ///
    /// # Safety
    /// Extend the fixed heap size. Assuming the size is just after the previous size
    /// When use this function, require global lock on allcoator
    pub unsafe fn extend(&self, size: usize, page_size: usize) {
        META_BUMP.lock().extend(size, page_size);
    }
}

unsafe impl GlobalAlloc for RustAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        // if HAS_RSEQ {
        //     if let Ok(result) = (*GLOBAL_CCACHE).allocate(layout) {
        //         result.as_ptr()
        //     } else {
        //         ptr::null_mut()
        //     }
        // } else {
        let alloc = &mut (*GlobalTcache);
        match alloc.allocate(layout) {
            Ok(r) => r.as_ptr(),
            Err(_) => core::ptr::null_mut(),
        }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        // if HAS_RSEQ {
        //     let ptr = NonNull::new(ptr).expect("ptr is null!");
        // //     (*GLOBAL_CCACHE).deallocate(ptr, layout);
        // } else {
        let alloc = &mut (*GlobalTcache);
        alloc.deallocate(NonNull::new_unchecked(ptr), layout)
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        let old_idx = get_size_class(layout.size()).index();
        let new_idx = get_size_class(new_size).index();

        if old_idx == new_idx {
            ptr
        } else {
            let new_layout = Layout::from_size_align_unchecked(new_size, layout.align());
            // SAFETY: the caller must ensure that `new_layout` is greater than zero.
            let new_ptr = self.alloc(new_layout);
            if !new_ptr.is_null() {
                // SAFETY: the previously allocated block cannot overlap the newly allocated block.
                // The safety contract for `dealloc` must be upheld by the caller.
                ptr::copy_nonoverlapping(ptr, new_ptr, core::cmp::min(layout.size(), new_size));
                self.dealloc(ptr, layout);
            }
            new_ptr
        }
    }
}

unsafe impl Allocator for RustAllocator {
    fn allocate(&self, layout: Layout) -> Result<NonNull<[u8]>, AllocError> {
        unsafe {
            let p = self.alloc(layout);
            // TODO: eliminate redundant overhead
            // can we avoid using get_index function?
            // let (_, alloc_size) = get_index_and_size(layout.size());
            Ok(NonNull::new_unchecked(slice::from_raw_parts_mut(
                p,
                layout.size(),
            )))
        }
    }

    unsafe fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
        self.dealloc(ptr.as_ptr() as *mut u8, layout);
    }
}
