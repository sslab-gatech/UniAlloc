use crate::mm::BackendAllocator as GlobalBackend;
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sys_alloc as system_alloc;
use crate::sc::META_BUMP;
use alloc::boxed::Box;
use core::alloc::{AllocError, Allocator, GlobalAlloc, Layout};
use core::ffi::c_void;
use core::option::Option::Some;
use core::ptr::{null_mut, NonNull};
use core::slice;
use core::sync::atomic::{AtomicPtr, Ordering};

include!(concat!(env!("OUT_DIR"), "/consts.rs"));
#[cfg(not(feature = "fixed_heap"))]
pub type RadixTree = RadixNodeHead<RadixBottomNode>;
#[cfg(feature = "fixed_heap")]
pub type RadixTree = ArrayNode;

pub trait TreeNode {
    fn insert(&mut self, k: usize, v: i64, n: usize) -> Result<(), &'static str>;

    fn remove(&mut self, k: usize, n: usize) -> Result<(), &'static str>;

    fn get_mut(&mut self, k: usize) -> i64;
}

pub struct RadixNodeHead<V>
where
    V: TreeNode,
{
    nodes: [AtomicPtr<V>; 1 << 18],
}

pub fn allocate_node<V: TreeNode>() -> *mut V {
    #[cfg(not(feature = "fixed_heap"))]
    {
        let prot = system_alloc::prots::get_prot(true, true, false);
        #[cfg(feature = "hugepage")]
        unsafe {
            system_alloc::mmap_huge(core::mem::size_of::<V>(), prot) as *mut V
        }
        #[cfg(not(feature = "hugepage"))]
        unsafe {
            system_alloc::mmap(core::mem::size_of::<V>(), prot) as *mut V
        }
    }
    #[cfg(feature = "fixed_heap")]
    unsafe {
        META_BUMP
            .lock()
            .alloc(core::mem::size_of::<V>())
            .expect("err") as *mut V
    }
}
pub fn deallocate_node<V: TreeNode>(ptr: usize) {
    #[cfg(not(feature = "fixed_heap"))]
    unsafe {
        system_alloc::munmap(ptr as *mut u8, core::mem::size_of::<V>())
    }
}

impl<V> TreeNode for RadixNodeHead<V>
where
    V: TreeNode,
{
    fn insert(&mut self, k: usize, v: i64, n: usize) -> Result<(), &'static str> {
        let mask = !((1_usize << 46) - 1);
        let idx = (mask & k) >> 46;
        let node_ptr_ref: &mut AtomicPtr<V> = &mut self.nodes[idx];
        let mut node_ptr = node_ptr_ref.load(Ordering::Relaxed);

        if node_ptr.is_null() {
            node_ptr = allocate_node::<V>();
            let res = node_ptr_ref.compare_exchange(
                core::ptr::null_mut(),
                node_ptr,
                Ordering::AcqRel,
                Ordering::Relaxed,
            );
            if let Err(cur_ptr) = res {
                assert!(!cur_ptr.is_null());
                deallocate_node::<V>(node_ptr as usize);
                node_ptr = cur_ptr;
            }
        }
        assert_ne!(node_ptr as usize, 0);
        let node: &mut V = unsafe { node_ptr.as_mut().expect("err") };
        node.insert(k << 18, v, n)
    }

    fn remove(&mut self, k: usize, n: usize) -> Result<(), &'static str> {
        let mask = !((1_usize << 46) - 1);
        let idx = (mask & k) >> 46;
        let node_ptr_ref: &mut AtomicPtr<V> = &mut self.nodes[idx];
        let node_ptr = node_ptr_ref.load(Ordering::Relaxed);
        if node_ptr.is_null() {
            Ok(())
        } else {
            let node: &mut V = unsafe { node_ptr.as_mut().expect("err") };
            node.remove(k << 18, n)
        }
    }

    fn get_mut(&mut self, k: usize) -> i64 {
        let mask = !((1_usize << 46) - 1);
        let idx = (mask & k) >> 46;
        let node_ptr_ref: &mut AtomicPtr<V> = &mut self.nodes[idx];
        let node_ptr = node_ptr_ref.load(Ordering::Relaxed);
        if node_ptr.is_null() {
            0
        } else {
            let node: &mut V = unsafe { node_ptr.as_mut().expect("err") };
            node.get_mut(k << 18)
        }
    }
}

pub struct RadixBottomNode {
    nodes: [i64; 1 << 18],
}

impl TreeNode for RadixBottomNode {
    fn insert(&mut self, k: usize, v: i64, n: usize) -> Result<(), &'static str> {
        let mask = !((1_usize << 46) - 1);
        let idx = (mask & k) >> 46;
        for i in 0..n {
            self.nodes[idx + i] = v;
        }
        Ok(())
    }

    fn remove(&mut self, k: usize, n: usize) -> Result<(), &'static str> {
        let mask = !((1_usize << 46) - 1);
        let idx = (mask & k) >> 46;
        for i in 0..n {
            self.nodes[idx + i] = 0;
        }
        Ok(())
    }

    fn get_mut(&mut self, k: usize) -> i64 {
        let mask = !((1_usize << 46) - 1);
        let idx = (mask & k) >> 46;
        self.nodes[idx]
    }
}
#[cfg(not(feature = "fixed_heap"))]
static RDPTR: AtomicPtr<RadixTree> = AtomicPtr::new(null_mut());
#[cfg(not(feature = "fixed_heap"))]
pub fn get_rd_tree() -> &'static mut RadixTree {
    let mut ptr = RDPTR.load(Ordering::Relaxed);
    if ptr.is_null() {
        ptr = allocate_node::<RadixTree>();
        let res = RDPTR.compare_exchange(null_mut(), ptr, Ordering::Acquire, Ordering::Relaxed);
        if let Err(eptr) = res {
            deallocate_node::<RadixTree>(ptr as usize);
            ptr = eptr;
        }
    }
    return unsafe { ptr.as_mut().expect("err") };
}
#[cfg(feature = "fixed_heap")]
pub static mut RDTREE: RadixTree = RadixTree::new();
#[cfg(feature = "fixed_heap")]
pub fn get_rd_tree() -> &'static mut RadixTree {
    unsafe { &mut RDTREE }
}

pub struct ArrayNode {
    base: usize,
    nodes: *mut i64,
}

impl ArrayNode {
    pub const fn new() -> Self {
        Self {
            base: 0,
            nodes: null_mut(),
        }
    }

    pub fn init_with_range(&mut self, start: usize, array: *mut i64) {
        self.base = start;
        self.nodes = array;
    }

    pub unsafe fn extend_with_range(
        &mut self,
        size: usize,
        new_end: usize,
        page_size: usize,
    ) -> (usize, usize) {
        let prev = self.nodes;
        let count = (new_end - size - self.base) / page_size;
        let array = GlobalBackend.alloc(Layout::from_size_align_unchecked(
            ((new_end - self.base) / page_size) * core::mem::size_of::<i64>(),
            1,
        )) as *mut i64;
        core::ptr::copy(prev, array, count);
        self.nodes = array;
        (prev as usize, count)
    }

    fn get_slice(&mut self, k: usize, n: usize) -> &mut [i64] {
        let idx = (k - self.base) / PAGE_SIZE + n - 1;
        unsafe { core::slice::from_raw_parts_mut(self.nodes, idx + 1) }
    }
}

impl TreeNode for ArrayNode {
    fn insert(&mut self, k: usize, v: i64, n: usize) -> Result<(), &'static str> {
        assert!(!self.nodes.is_null());
        let k = k >> 16;
        let start: usize = (k - self.base) / PAGE_SIZE;
        let nodes = self.get_slice(k, n);
        for i in 0..n {
            nodes[start + i] = v;
        }
        Ok(())
    }

    fn remove(&mut self, k: usize, n: usize) -> Result<(), &'static str> {
        assert!(!self.nodes.is_null());
        let k = k >> 16;
        let start: usize = (k - self.base) / PAGE_SIZE;
        let nodes = self.get_slice(k, n);
        for i in 0..n {
            nodes[start + i] = 0;
        }
        Ok(())
    }

    fn get_mut(&mut self, k: usize) -> i64 {
        let k = k >> 16;
        let start: usize = (k - self.base) / PAGE_SIZE;
        let nodes = self.get_slice(k, 1);
        nodes[start]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::cmp::Ordering;

    pub type RadixTree = RadixNodeHead<RadixBottomNode>;

    #[cfg(unix)]
    #[test]
    fn rd_simple_test() {
        let mut rd = unsafe { allocate_node::<RadixTree>().as_mut().expect("err") };
        let mut last = (PAGE_SIZE * 4094);
        match 12_u32.cmp(&PAGE_SIZE.trailing_zeros()) {
            Ordering::Greater => last <<= 12 - PAGE_SIZE.trailing_zeros(),
            Ordering::Less => last >>= PAGE_SIZE.trailing_zeros() - 12,
            Ordering::Equal => {}
        };
        rd.insert(last << 16, 4, 2);
        rd.insert((last + 2 * 4096) << 16, 3, 2);
        assert_eq!(rd.get_mut(0), 0);
        assert_eq!(rd.get_mut(last << 16), 4);
        assert_eq!(rd.get_mut((last + 4096) << 16), 4);
        assert_eq!(rd.get_mut((last + 2 * 4096) << 16), 3);
        assert_eq!(rd.get_mut((last + 3 * 4096) << 16), 3);
        assert_eq!(rd.get_mut((last + 4 * 4096) << 16), 0);
        assert_eq!(rd.get_mut((last + 5 * 4096) << 16), 0);
        assert_eq!(rd.get_mut((last + 6 * 4096) << 16), 0);
        assert_eq!(rd.get_mut((last + 4096_usize * (1 << 16 + 1)) << 16), 0);
    }
}
