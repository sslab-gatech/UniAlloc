mod efficient_sc;
mod separate_sc;
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sys_alloc as system_alloc;
#[cfg(not(feature = "fixed_heap"))]
use crate::sync::PthreadMutex as Mutex;
use core::alloc::{AllocError, Allocator, GlobalAlloc, Layout};
use core::ptr::{null_mut, NonNull};
use core::sync::atomic::{AtomicPtr, AtomicUsize, Ordering};
pub use efficient_sc::*;
#[cfg(feature = "fixed_heap")]
use spin::Mutex;

pub struct BumpAlloc {
    start: usize,
    current: usize,
}

impl Default for BumpAlloc {
    fn default() -> Self {
        Self::new()
    }
}

impl BumpAlloc {
    const DEFAULT_SIZE: usize = 4 * (1 << 24);
    pub const fn new() -> Self {
        Self {
            start: 0,
            current: 0,
        }
    }

    #[allow(unused_variables)]
    pub unsafe fn extend(&mut self, size: usize, page_size: usize) {
        #[cfg(feature = "fixed_heap")]
        {
            let new_end = BUMP.lock().extend_with_range(size);
            let (prev_rd_tree, prev_count) = RDTREE.extend_with_range(size, new_end, page_size);
            if PG_BUMP
                .lock()
                .try_extend(
                    prev_rd_tree,
                    prev_rd_tree + prev_count * core::mem::size_of::<i64>(),
                    page_size,
                )
                .is_err()
            {
                //we return this new page to backend
                assert_eq!(prev_rd_tree & (page_size - 1), 0);
                let prev_size =
                    (prev_count * core::mem::size_of::<i64>() + page_size - 1) & !(page_size - 1);
                GlobalBackend.dealloc(
                    prev_rd_tree as *mut u8,
                    Layout::from_size_align_unchecked(prev_size, 1),
                );
            }
        }
        #[cfg(not(feature = "fixed_heap"))]
        panic!()
    }

    #[allow(unused_variables)]
    pub unsafe fn init_with_range(&mut self, start: usize, end: usize, page_size: usize) {
        #[cfg(feature = "fixed_heap")]
        {
            let page_count = (end - start) / page_size;
            let total_metadata = page_count
                * (core::mem::size_of::<i64>() + core::mem::size_of::<EfObjectPage>())
                + core::mem::size_of::<ThreadCache>()
                + core::mem::size_of::<ZoneAllocator>();
            let meta_pages = (total_metadata + page_size - 1) / page_size;
            self.start = start;
            let meta_end = start + page_size * meta_pages;
            self.current = meta_end;
            //init other components
            //backend bump
            BUMP.lock().init_with_range(meta_end, end);
            //init tcache
            let tcache = self
                .alloc(core::mem::size_of::<ThreadCache>())
                .expect("err");
            GlobalTcache_ptr = tcache as *mut ThreadCache;
            core::ptr::write(GlobalTcache_ptr, ThreadCache::new());
            //init zone
            let zone = self
                .alloc(core::mem::size_of::<ZoneAllocator>())
                .expect("err");
            GLOBAL_ZONE_ptr = zone as *mut ZoneAllocator;
            core::ptr::write(GLOBAL_ZONE_ptr, ZoneAllocator::new());
            //then page_bump
            let page_bump_end = self.current;
            let page_bump_start = self
                .alloc(page_count * core::mem::size_of::<EfObjectPage>())
                .expect("err") as usize;
            PG_BUMP
                .lock()
                .init_with_range(page_bump_start, page_bump_end);
            //we init rd tree, then page_bump, then backend bump and finally tcache and zone
            RDTREE.init_with_range(
                start,
                self.alloc(page_count * core::mem::size_of::<i64>())
                    .expect("err") as *mut i64,
            );
        }
        #[cfg(not(feature = "fixed_heap"))]
        panic!()
    }

    pub fn alloc(&mut self, size: usize) -> Result<*mut u8, AllocError> {
        let cur_start = self.start;
        if !(self.current > self.start && (self.current - cur_start) <= Self::DEFAULT_SIZE) {
            #[cfg(not(feature = "fixed_heap"))]
            {
                let prot = system_alloc::prots::get_prot(true, true, false);
                let start = unsafe {
                    #[cfg(feature = "hugepage")]
                    let mut ptr = system_alloc::mmap_huge(Self::DEFAULT_SIZE, prot) as *mut u8;
                    #[cfg(not(feature = "hugepage"))]
                    let mut ptr = system_alloc::mmap(Self::DEFAULT_SIZE, prot) as *mut u8;
                    // When fail, mmap return -1, which is 0xffffffffffff
                    // So need to use i64 to identify if it fails and return null
                    if ptr as usize == usize::MAX {
                        ptr = core::ptr::null_mut();
                    }
                    ptr
                };
                self.start = start as usize;
                self.current = self.start + Self::DEFAULT_SIZE;
            }
            #[cfg(feature = "fixed_heap")]
            return Err(AllocError);
        }
        let new_cur = self.current - size;
        self.current = new_cur;
        Ok(new_cur as *mut u8)
    }
}

pub struct MetaBumpAlloc {
    bumper: BumpAlloc,
    backup: *mut usize,
}

impl Default for MetaBumpAlloc {
    fn default() -> Self {
        Self::new()
    }
}

impl MetaBumpAlloc {
    const DEFAULT_SIZE: usize = 1 << 21;
    const ALLOC_UNIT: usize = 1 << 11;
    pub const fn new() -> Self {
        Self {
            bumper: BumpAlloc::new(),
            backup: null_mut(),
        }
    }

    pub unsafe fn extend(&mut self, size: usize, page_size: usize) {
        self.bumper.extend(size, page_size);
    }

    pub unsafe fn init_with_range(&mut self, start: usize, end: usize, page_size: usize) {
        self.bumper.init_with_range(start, end, page_size);
    }

    pub fn alloc(&mut self, size: usize) -> Result<*mut u8, AllocError> {
        let alloc_unit = core::mem::size_of::<ThreadCache>();
        let size = (size + alloc_unit - 1) / alloc_unit * alloc_unit;
        if size == alloc_unit {
            let head = self.backup;
            if !head.is_null() {
                let next;
                unsafe { next = *head as *mut usize };
                self.backup = next;
                return Ok(head as *mut u8);
            }
        }
        self.bumper.alloc(size)
    }

    pub fn dealloc(&mut self, ptr: *mut usize) {
        //assert this size is half page (size of ThreadCache) tls
        let head = self.backup;
        unsafe { *ptr = head as usize };
        self.backup = ptr
    }
}

pub static mut META_BUMP: Mutex<MetaBumpAlloc> = Mutex::new(MetaBumpAlloc::new());

#[derive(Copy, Clone, Default)]
pub struct MetaAllocator {}

unsafe impl Allocator for MetaAllocator {
    fn allocate(&self, layout: Layout) -> Result<NonNull<[u8]>, AllocError> {
        let raw_ptr = unsafe { META_BUMP.lock().alloc(layout.size()) }.expect("err") as *mut u8;
        let ptr = NonNull::new(raw_ptr).ok_or(core::alloc::AllocError)?;
        Ok(NonNull::slice_from_raw_parts(ptr, layout.size()))
    }

    unsafe fn deallocate(&self, ptr: NonNull<u8>, _layout: Layout) {
        META_BUMP.lock().dealloc(ptr.as_ptr() as *mut usize)
    }
}
#[cfg(feature = "fixed_heap")]
use crate::cache::GlobalTcache_ptr;
use crate::cache::ThreadCache;
#[cfg(feature = "fixed_heap")]
use crate::collections::radix_tree::RDTREE;
use crate::freelist::BUMP;
use crate::page::{EfObjectPage, PG_BUMP};
use crate::prelude::GlobalBackend;
#[cfg(feature = "fixed_heap")]
use crate::zone::{GLOBAL_ZONE_ptr, ZoneAllocator};
pub use MetaAllocator as MetadataAllocator;
