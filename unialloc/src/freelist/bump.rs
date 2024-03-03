use crate::error::AllocError;
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sync::general_lock::{
    dynamic_initialize, lock, unlock, OsLock, STATIC_INITIALIZER,
};
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sys_alloc as system_alloc;
use core::alloc::{GlobalAlloc, Layout};
use core::ptr::{null, null_mut};
use core::result::Result::{Err, Ok};
use core::sync::atomic::{AtomicPtr, Ordering};
use spin::Mutex;

const PG_SIZE: usize = 4096;

pub struct BumpAlloc {
    check_point: usize,
    current: usize,
}

impl Default for BumpAlloc {
    fn default() -> Self {
        Self::new()
    }
}

impl BumpAlloc {
    const DEFAULT_SIZE: usize = 0x100000000;
    pub const fn new() -> Self {
        Self {
            check_point: 0,
            current: 0,
        }
    }

    fn init(&mut self) {
        #[cfg(not(feature = "fixed_heap"))]
        {
            //clean up previous
            if self.check_point != self.current {
                let size = self.current - self.check_point;
                unsafe { system_alloc::munmap(self.check_point as *mut u8, size) };
            }
            //init now
            let prot = system_alloc::prots::get_prot(true, true, false);
            let start = unsafe {
                let mut ptr = system_alloc::mmap(Self::DEFAULT_SIZE, prot) as *mut u8;
                // When fail, mmap return -1, which is 0xffffffffffff
                // So need to use i64 to identify if it fails and return null
                if ptr as usize == usize::MAX {
                    ptr = core::ptr::null_mut();
                }
                ptr
            };
            let end = (start as usize + Self::DEFAULT_SIZE) as *mut u8;
            self.check_point = start as usize;
            self.current = end as usize;
        }
        #[cfg(feature = "fixed_heap")]
        unimplemented!()
    }

    pub fn init_with_range(&mut self, start: usize, end: usize) {
        self.check_point = end as usize;
        self.current = start as usize;
    }

    pub fn extend_with_range(&mut self, size: usize) -> usize {
        self.check_point += size;
        self.check_point
    }

    pub fn alloc(&mut self, size: usize) -> Result<*mut u8, AllocError> {
        let alloc_size = (size + PG_SIZE - 1) / PG_SIZE * PG_SIZE;
        let current_ptr = self.current as usize;
        #[cfg(not(feature = "fixed_heap"))]
        if self.check_point + alloc_size > current_ptr {
            self.init();
        }
        #[cfg(feature = "fixed_heap")]
        if self.check_point - alloc_size < current_ptr {
            return Err(AllocError::ENOMEM);
        }
        let ptr = self.current as usize;
        #[cfg(not(feature = "fixed_heap"))]
        if let Some(new_ptr) = ptr.checked_sub(alloc_size) {
            // Round down to the requested alignment.
            // let new_ptr = new_ptr & !(align - 1);
            self.current = new_ptr;
            Ok(new_ptr as *mut u8)
        } else {
            Err(AllocError::EFATAL)
        }
        #[cfg(feature = "fixed_heap")]
        if let Some(new_ptr) = ptr.checked_add(alloc_size) {
            // Round down to the requested alignment.
            let ans = self.current;
            self.current = new_ptr;
            Ok(ans as *mut u8)
        } else {
            Err(AllocError::EFATAL)
        }
    }
}
