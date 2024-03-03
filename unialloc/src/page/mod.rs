mod efficient_page;
mod separate_page;

use crate::error::AllocError;
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sys_alloc as system_alloc;
use crate::prelude::GlobalBackend;
use crate::PAGE_SIZE;
use core::alloc::{GlobalAlloc, Layout};
use core::ptr::null_mut;
use core::result::Result::Err;
use core::sync::atomic::{AtomicPtr, AtomicUsize, Ordering};
pub use efficient_page::ObjectPage as EfObjectPage;
pub use separate_page::ObjectPage;
use spin::Mutex;

pub struct PageBumpAlloc {
    start: usize,
    current: usize,
}

impl Default for PageBumpAlloc {
    fn default() -> Self {
        Self::new()
    }
}

impl PageBumpAlloc {
    #[cfg(feature = "hugepage")]
    const DEFAULT_SIZE: usize = core::mem::size_of::<EfObjectPage>() * (1 << 21);
    #[cfg(not(feature = "hugepage"))]
    const DEFAULT_SIZE: usize = core::mem::size_of::<EfObjectPage>() * (PAGE_SIZE);
    pub const fn new() -> Self {
        Self {
            start: 0,
            current: 0,
        }
    }

    pub fn init_with_range(&mut self, start: usize, end: usize) {
        self.current = end;
        self.start = start;
    }

    pub fn try_extend(&mut self, start: usize, end: usize, page_size: usize) -> Result<(), ()> {
        if end == self.start {
            self.start = start & !(page_size - 1);
            return Ok(());
        }
        Err(())
    }

    pub fn alloc(&mut self, size: usize) -> Result<*mut u8, AllocError> {
        assert_eq!(size, core::mem::size_of::<EfObjectPage>());
        let cur = self.current;

        let cur_start = self.start;
        if cur <= cur_start || (cur - cur_start) > Self::DEFAULT_SIZE {
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
                let end = (start as usize + Self::DEFAULT_SIZE) as *mut u8;
                self.current = end as usize;
                self.start = start as usize;
            }
            #[cfg(feature = "fixed_heap")]
            {
                let start =
                    unsafe { GlobalBackend.alloc(Layout::from_size_align_unchecked(4096, 1)) }
                        as usize;
                let end = (start + 4096) as *mut u8;
                self.current = end as usize;
                self.start = start as usize;
            }
        }
        let new_cur = self.current - size;
        self.current = new_cur;
        Ok(new_cur as *mut u8)
    }
}

pub static mut PG_BUMP: Mutex<PageBumpAlloc> = Mutex::new(PageBumpAlloc::new());
