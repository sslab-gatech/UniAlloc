use crate::mm::BackendAllocator as GlobalBackend;
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sys_alloc as system_alloc;
#[cfg(target_os = "linux")]
use crate::sync::PthreadMutex as Mutex;
use alloc::boxed::Box;
use core::alloc::{AllocError, Allocator, GlobalAlloc, Layout};
use core::ffi::c_void;
use core::option::Option::Some;
use core::ptr::NonNull;
use core::slice;
use core::sync::atomic::{AtomicPtr, Ordering};
#[cfg(not(target_os = "linux"))]
use spin::Mutex;

include!(concat!(env!("OUT_DIR"), "/consts.rs"));
pub trait Tree128Node {
    fn insert(&mut self, k: u128, v: i32, n: i32) -> Result<(), &'static str>;

    fn remove(&mut self, k: u128, n: i32) -> Result<(), &'static str>;

    fn get_mut(&mut self, k: u128) -> i32;
}

pub struct RadixNode128Head<V>
where
    V: Tree128Node,
{
    nodes: [AtomicPtr<V>; 1 << 21],
}

pub fn allocate_node<V: Tree128Node>() -> *mut V {
    let prot = system_alloc::prots::get_prot(true, true, false);
    unsafe { system_alloc::mmap(core::mem::size_of::<V>(), prot) as *mut V }
}
pub fn deallocate_node<V: Tree128Node>(ptr: usize) {
    unsafe { system_alloc::munmap(ptr as *mut u8, core::mem::size_of::<V>()) }
}

#[derive(Clone, Copy)]
pub struct RdTree128Alloc;

unsafe impl Allocator for RdTree128Alloc {
    fn allocate(&self, layout: Layout) -> Result<NonNull<[u8]>, AllocError> {
        let prot = system_alloc::prots::get_prot(true, true, false);
        unsafe {
            let mut ptr = system_alloc::mmap(layout.size(), prot) as *mut u8;
            // When fail, mmap return -1, which is 0xffffffffffff
            // So need to use i64 to identify if it fails and return null
            if ptr as i64 == -1 {
                ptr = core::ptr::null_mut();
            }
            Ok(NonNull::new(slice::from_raw_parts_mut(ptr, layout.size()))
                .expect("MMAP_ALLOC cannot allocate"))
        }
    }

    unsafe fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
        system_alloc::munmap(ptr.as_ptr(), layout.size());
    }
}

impl<V> Tree128Node for RadixNode128Head<V>
where
    V: Tree128Node,
{
    fn insert(&mut self, k: u128, v: i32, n: i32) -> Result<(), &'static str> {
        let mask = !((1_u128 << 107) - 1);
        let idx = ((mask & k) >> 107) as usize;
        let node_ptr_ref: &mut AtomicPtr<V> = &mut self.nodes[idx];
        let mut node_ptr = node_ptr_ref.load(Ordering::Relaxed);

        if node_ptr.is_null() {
            node_ptr = allocate_node::<V>();
            loop {
                match {
                    node_ptr_ref.compare_exchange_weak(
                        core::ptr::null_mut(),
                        node_ptr,
                        Ordering::AcqRel,
                        Ordering::Relaxed,
                    )
                } {
                    Ok(_p) => {
                        break;
                    }
                    Err(p) => {
                        if !p.is_null() {
                            deallocate_node::<V>(node_ptr as usize);
                            node_ptr = p;
                            break;
                        }
                    }
                }
            }
        }
        assert_ne!(node_ptr as usize, 0);
        let node: &mut V = unsafe { node_ptr.as_mut().expect("err") };
        node.insert(k << 21, v, n)
    }

    fn remove(&mut self, k: u128, n: i32) -> Result<(), &'static str> {
        let mask = !((1_u128 << 107) - 1);
        let idx = ((mask & k) >> 107) as usize;
        let node_ptr_ref: &mut AtomicPtr<V> = &mut self.nodes[idx];
        let node_ptr = node_ptr_ref.load(Ordering::Relaxed);
        if node_ptr.is_null() {
            Ok(())
        } else {
            let node: &mut V = unsafe { node_ptr.as_mut().expect("err") };
            node.remove(k << 21, n)
        }
    }

    fn get_mut(&mut self, k: u128) -> i32 {
        let mask = !((1_u128 << 107) - 1);
        let idx = ((mask & k) >> 107) as usize;
        let node_ptr_ref: &mut AtomicPtr<V> = &mut self.nodes[idx];
        let node_ptr = node_ptr_ref.load(Ordering::Relaxed);
        if node_ptr.is_null() {
            -1
        } else {
            let node: &mut V = unsafe { node_ptr.as_mut().expect("err") };
            node.get_mut(k << 21)
        }
    }
}

pub struct RadixBottom128Node {
    nodes: [i32; 1 << 23],
}

impl Tree128Node for RadixBottom128Node {
    fn insert(&mut self, k: u128, v: i32, n: i32) -> Result<(), &'static str> {
        let mask = !((1_u128 << 105) - 1);
        let idx = ((mask & k) >> 105) as usize;
        for i in 0..n as usize {
            self.nodes[idx + i] = v;
        }
        Ok(())
    }

    fn remove(&mut self, k: u128, n: i32) -> Result<(), &'static str> {
        let mask = !((1_u128 << 105) - 1);
        let idx = ((mask & k) >> 105) as usize;
        for i in 0..n as usize {
            self.nodes[idx + i] = 0;
        }
        Ok(())
    }

    fn get_mut(&mut self, k: u128) -> i32 {
        let mask = !((1_u128 << 105) - 1);
        let idx = ((mask & k) >> 105) as usize;
        self.nodes[idx]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::cmp::Ordering;

    pub type RadixTree = RadixNode128Head<
        RadixNode128Head<RadixNode128Head<RadixNode128Head<RadixNode128Head<RadixBottom128Node>>>>,
    >;

    #[cfg(unix)]
    #[test]
    fn rd_simple_test() {
        let mut rd = unsafe { allocate_node::<RadixTree>().as_mut().expect("err") };
        let mut last = (PAGE_SIZE * 4094) as u128;
        match 12_u32.cmp(&PAGE_SIZE.trailing_zeros()) {
            Ordering::Greater => last <<= 12 - PAGE_SIZE.trailing_zeros(),
            Ordering::Less => last >>= PAGE_SIZE.trailing_zeros() - 12,
            Ordering::Equal => {}
        };
        rd.insert((last << 16) as u128, 4, 2);
        rd.insert(((last + 2 * 4096) << 16) as u128, 3, 2);
        assert_eq!(rd.get_mut(0), -1);
        assert_eq!(rd.get_mut((last << 16) as u128), 4);
        assert_eq!(rd.get_mut((last << 16) + 1 as u128), 4);
        assert_eq!(rd.get_mut((last << 16) + 2 as u128), 0);
        assert_eq!(rd.get_mut((last + 4096) << 16), -1);
        assert_eq!(rd.get_mut((last + 2 * 4096) << 16), 3);
        assert_eq!(rd.get_mut(((last + 2 * 4096) << 16) + 1), 3);
        assert_eq!(rd.get_mut(((last + 2 * 4096) << 16) + 2), 0);
        assert_eq!(rd.get_mut((last + 3 * 4096) << 16), -1);
        assert_eq!(rd.get_mut((last + 4 * 4096) << 16), -1);
        assert_eq!(rd.get_mut((last + 5 * 4096) << 16), -1);
        assert_eq!(rd.get_mut((last + 6 * 4096) << 16), -1);
        assert_eq!(rd.get_mut((last + 4096_u128 * (1 << 16 + 1)) << 16), -1);
    }
}
