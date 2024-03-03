use crate::mm::BackendAllocator as GlobalBackend;
use crate::size_class::{get_index_and_size, get_size_from_idx, SizeClass, NUM_SIZE_CLASSES};
use crate::zone::get_zone_mut;
use crate::PAGE_SIZE;
use alloc::vec::Vec;
use core::alloc::{AllocError, Layout};
use core::ptr::NonNull;
include!(concat!(env!("OUT_DIR"), "/sizeclass_consts.rs"));

#[repr(align(16))]
pub struct ThreadCache {
    list: [Vec<usize, GlobalBackend>; NUM_SIZE_CLASSES],
}

impl ThreadCache {
    // pub fn new() -> Self {
    //     Self {
    //         list: [Vec::<usize, GlobalBackend>::with_capacity_in(0, GlobalBackend); NUM_SIZE_CLASSES],
    //     }
    // }

    //todo dealloc batch size array might be too large
    pub fn cleanup_cache(&mut self) {
        for idx in 0..self.list.len() {
            let list: &mut Vec<usize, GlobalBackend> = &mut self.list[idx];
            let count = list.len();
            let zone = get_zone_mut();
            zone.deallocate_batch(
                &mut list[0..],
                count,
                Layout::from_size_align(get_size_from_idx(idx), 1).expect("err"),
                Some((SizeClass::Base(idx), get_size_from_idx(idx))),
            )
            .expect("cannot deallocate");
            unsafe {
                list.set_len(0);
            }
            list.shrink_to_fit();
        }
    }

    /// Push a pointer into corresponding freelist
    ///
    /// # Return
    ///
    /// - true: successful push
    /// - false: the list is too long
    fn push(&mut self, ptr: *mut u8, cl: usize) -> bool {
        let freelist: &mut Vec<usize, GlobalBackend> = &mut self.list[cl];
        if freelist.len() >= (IDX_NP[cl] as usize) * 2 {
            return false;
        }
        if freelist.capacity() == freelist.len() {
            freelist.reserve(PAGE_SIZE / 8);
        }
        unsafe {
            let len = freelist.len();
            let end = freelist.as_mut_ptr().add(len);
            freelist.set_len(len + 1);
            *end = ptr as usize;
            // core::ptr::write(end, ptr as usize);
        }
        assert!(freelist.len() < PAGE_SIZE / 4 + 1);
        true
    }

    fn pop_aligned(&mut self, cl: usize, _align: usize) -> *mut u8 {
        let freelist: &mut Vec<usize, GlobalBackend> = &mut self.list[cl];
        for (i, item) in freelist.iter_mut().enumerate() {
            if *item & (_align - 1) == 0 {
                return freelist.swap_remove(i) as *mut u8;
            }
        }
        core::ptr::null_mut()
    }

    pub fn allocate(&mut self, layout: Layout) -> Result<NonNull<u8>, AllocError> {
        // 1. round the size up to next size class
        let (cls, size) = get_index_and_size(layout.size());
        let new_layout =
            Layout::from_size_align(size, layout.align()).expect("cannot create layout");

        // 2. try to pop one from freelist
        if let SizeClass::Base(idx) = cls {
            // (1) pop one from thread local freelist
            let result = self.pop_aligned(idx, layout.align());

            // (2) There is no chunk inside the freelist
            if result.is_null() {
                // allocate a batch of objects from zone
                let zone = get_zone_mut();
                let freelist: &mut Vec<usize, GlobalBackend> = &mut self.list[idx];
                let original_len = freelist.len();
                if freelist.capacity() - freelist.len() < IDX_NP[idx] as usize {
                    freelist.reserve(PAGE_SIZE / 8);
                }
                freelist.resize(original_len + IDX_NP[idx] as usize, 0);

                let ans = zone.allocate_batch_v2(
                    layout,
                    Some((cls, size)),
                    &mut freelist[original_len..],
                    IDX_NP[idx] as usize,
                );
                if ans.is_ok() {
                    // unsafe {freelist.set_len(original_len + IDX_NP[idx])};
                    freelist.swap_remove(original_len);
                }
                return ans.or(Err(AllocError));
            } else {
                return Ok(NonNull::new(result).expect("err"));
            }
        }

        // 3. Large size class goes to zone directly
        if let SizeClass::Large(_) = cls {
            return Self::alloc_from_zone(new_layout, Some((cls, size))).or(Err(AllocError));
        }
        Err(AllocError)
    }

    pub fn deallocate(&mut self, ptr: NonNull<u8>, layout: Layout) {
        // 1. round the size up to next size class
        let (cls, size) = get_index_and_size(layout.size());
        let new_layout =
            Layout::from_size_align(size, layout.align()).expect("cannot create layout");

        // 2. try to push the ptr to freelist
        if let SizeClass::Base(idx) = cls {
            // The thread local free list is full
            if !self.push(ptr.as_ptr(), idx) {
                let freelist: &mut Vec<usize, GlobalBackend> = &mut self.list[idx];
                let original_len = freelist.len();
                let zone = get_zone_mut();
                let ans = zone.deallocate_batch(
                    &mut freelist[(original_len - IDX_NP[idx] as usize)..],
                    IDX_NP[idx] as usize,
                    layout,
                    Some((cls, size)),
                );
                if ans.is_ok() {
                    unsafe { freelist.set_len(original_len - IDX_NP[idx] as usize) };
                    self.push(ptr.as_ptr(), idx);
                }
            }
        }

        // 3. for large chunks, the deallocation directly goes to zone
        if let SizeClass::Large(_) = cls {
            Self::dealloc_to_zone(ptr, new_layout, Some((cls, size)));
        }
    }

    fn alloc_from_zone(
        layout: Layout,
        cl: Option<(SizeClass, usize)>,
    ) -> Result<NonNull<u8>, &'static str> {
        let zone = get_zone_mut();
        zone.allocate(layout, cl)
    }

    fn alloc_batch_from_zone(
        layout: Layout,
        cl: Option<(SizeClass, usize)>,
    ) -> (Result<NonNull<u8>, &'static str>, usize, usize) {
        let zone = get_zone_mut();
        zone.allocate_batch(layout, cl)
    }

    fn dealloc_to_zone(ptr: NonNull<u8>, layout: Layout, cl: Option<(SizeClass, usize)>) {
        let zone = get_zone_mut();
        zone.deallocate(ptr, layout, cl).expect("cannot deallocate");
    }
}

#[cfg(feature = "pthread_dtor")]
mod pthread_tcache {
    use super::*;
    use alloc::boxed::Box;
    use core::alloc::{Allocator, GlobalAlloc};
    use core::ffi::c_void;

    #[thread_local]
    pub static mut TCACHE: *mut ThreadCache = core::ptr::null_mut::<ThreadCache>();
    #[cfg(target_os = "linux")]
    static mut PKEY: libc::pthread_key_t = 0u32;
    #[cfg(target_os = "macos")]
    static mut PKEY: libc::pthread_key_t = 0u64;
    static mut TSD_INITIALIZED: bool = false;

    // An experiemental implementation of dtor of thread_local cache
    // To use this feature, we need to guarantee that the undelying thread
    // is pthread.
    #[cfg(unix)]
    unsafe extern "C" fn free_thread_cache(ptr: *mut libc::c_void) {
        // println!("dtor triggered! {:x}", ptr as usize);
        let ptr = ptr as *mut ThreadCache;
        let mut tcache = Box::from_raw_in(ptr as *mut ThreadCache, GlobalBackend);
        // debug_assert_ne!(ptr as usize, 0);
        // let tcache = ptr.as_mut().expect("wtf");
        tcache.cleanup_cache();
        // #[cfg(test)]
        // {
        //     let u8ptr = ptr as *const u8;
        //     for i in 0..core::mem::size_of::<ThreadCache>() {
        //         debug_assert_eq!(*u8ptr, 0);
        //     }
        // }
    }

    pub fn get_thread_cache() -> &'static mut ThreadCache {
        unsafe {
            if TCACHE.is_null() {
                TCACHE = GlobalBackend.alloc_zeroed(Layout::from_size_align_unchecked(
                    core::mem::size_of::<ThreadCache>(),
                    1,
                )) as *mut ThreadCache;

                if !TSD_INITIALIZED {
                    libc::pthread_key_create(
                        &PKEY as *const _ as *mut libc::pthread_key_t,
                        Some(free_thread_cache),
                    );
                    TSD_INITIALIZED = true;
                }
                libc::pthread_setspecific(PKEY, TCACHE as *mut c_void);
            }
            TCACHE.as_mut().expect("wtf")
        }
    }
}

#[cfg(feature = "pthread_dtor")]
pub use pthread_tcache::*;
