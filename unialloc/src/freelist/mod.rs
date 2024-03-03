#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sys_alloc as system_alloc;
mod bump;
use crate::collections::radix_tree::{get_rd_tree, RadixTree, TreeNode};
use crate::error::AllocError;
use crate::sc::{align_12k, META_BUMP};
use crate::size_class::BACKEND_MAX_PAGE;
use bump::BumpAlloc;
use core::alloc::{Allocator, GlobalAlloc, Layout};
use core::borrow::BorrowMut;
use core::ptr::{null, null_mut, NonNull};
use core::sync::atomic::{AtomicPtr, Ordering};
use spin::Mutex;

const PG_SIZE: usize = 4096;

pub static mut BUMP: Mutex<BumpAlloc> = Mutex::new(BumpAlloc::new());

struct DoubleLinkedList {
    prev: Option<*mut DoubleLinkedList>,
    next: Option<*mut DoubleLinkedList>,
}

impl DoubleLinkedList {
    const fn new() -> Self {
        Self {
            prev: None,
            next: None,
        }
    }

    fn push_before_head(&'static mut self, new_node: *mut DoubleLinkedList) {
        self.prev = Some(new_node);
        let self_ptr = self as *const _ as *mut DoubleLinkedList;
        Self::get_ref(new_node).next = Some(self_ptr);
        // unsafe {core::ptr::write(&mut new_node.next as * const _ as * mut Option<&'static DoubleLinkedList>, Some(self))}
        Self::get_ref(new_node).prev = None;
    }

    fn get_ref(ptr: *mut DoubleLinkedList) -> &'static mut DoubleLinkedList {
        unsafe { ptr.as_mut().expect("err") }
    }

    fn remove_current(&'static mut self) -> Option<&'static mut DoubleLinkedList> {
        return if let Some(prev) = self.prev {
            if let Some(next) = self.next {
                Self::get_ref(prev).next = Some(next);
                Self::get_ref(next).prev = Some(prev);
            } else {
                Self::get_ref(prev).next = None;
            }
            None
        } else if let Some(next) = self.next {
            Self::get_ref(next).prev = None;
            Some(Self::get_ref(next))
        } else {
            //nothing to do
            None
        };
    }
}

pub struct FreeList {
    lists: AtomicPtr<Mutex<Option<&'static mut DoubleLinkedList>>>,
}

impl FreeList {
    const fn new() -> Self {
        Self {
            lists: AtomicPtr::new(null_mut()),
        }
    }

    fn remove_one(&mut self, idx: usize) -> Option<*mut u8> {
        let cur: &mut Mutex<Option<&'static mut DoubleLinkedList>> = &mut self.get_slice()[idx];
        // if cur.get_mut().is_some() {
        let mut locked = cur.lock();
        if let Some(to_remove) = locked.take() {
            let ans = to_remove as *const _ as *mut u8;
            let rd_tree = get_rd_tree();
            rd_tree.remove((ans as usize) << 16, 1).expect("err");
            rd_tree
                .remove((ans as usize + idx * PG_SIZE) << 16, 1)
                .expect("err");
            if let Some(next) = to_remove.remove_current() {
                *locked = Some(next);
            } else {
                *locked = None;
            }
            return Some(ans);
        }
        // }
        None
    }

    fn remove_spec_one(
        &mut self,
        idx: usize,
        ptr: *mut DoubleLinkedList,
        rd_tree: &mut RadixTree,
    ) -> Option<*mut u8> {
        let cur: &mut Mutex<Option<&'static mut DoubleLinkedList>> = &mut self.get_slice()[idx];
        // if cur.get_mut().is_some() {
        let mut locked = cur.lock();
        if -rd_tree.get_mut((ptr as usize) << 16) > 0 {
            let target = DoubleLinkedList::get_ref(ptr);
            if let Some(prev_next) = target.next {
                if prev_next as usize == ptr as usize {
                    rd_tree.remove((ptr as usize) << 16, 1).expect("err");
                    rd_tree
                        .remove((ptr as usize + idx * PG_SIZE) << 16, 1)
                        .expect("err");
                    if let Some(new_head) = target.remove_current() {
                        *locked = Some(new_head);
                    }
                    return Some(ptr as *mut u8);
                }
            }
        }
        None
    }

    fn insert_one(&mut self, idx: usize, node: &'static mut DoubleLinkedList) {
        let cur: &mut Mutex<Option<&'static mut DoubleLinkedList>> = &mut self.get_slice()[idx];
        let mut locked = cur.lock();
        if let Some(to_remove) = locked.take() {
            to_remove.push_before_head(node);
        }
        let rd_tree = get_rd_tree();
        rd_tree
            .insert(
                (node as *const _ as usize) << 16,
                (-(idx as i64 + 1)) << 48,
                1,
            )
            .expect("err");
        rd_tree
            .insert(
                (node as *const _ as usize + idx * PG_SIZE) << 16,
                -(idx as i64 + 1) << 48,
                1,
            )
            .expect("err");
        *locked = Some(node);
    }

    pub fn alloc(&mut self, size: usize) -> Result<*mut u8, AllocError> {
        let origin_size = (size + PG_SIZE - 1) / PG_SIZE - 1;
        if origin_size < self.get_slice().len() {
            if let Some(ans) = self.remove_one(origin_size) {
                return Ok(ans);
            }
            //another complex case, we first iterate all its parents to find if we can get one, then
            //fall into bump alloc
            let mut parent_idx = origin_size + 1;
            while parent_idx < self.get_slice().len() {
                if let Some(parent) = self.remove_one(parent_idx) {
                    let remain = (parent as usize) + PG_SIZE * (origin_size + 1);
                    let node: &'static mut DoubleLinkedList = unsafe {
                        core::ptr::write(
                            remain as *const DoubleLinkedList as *mut DoubleLinkedList,
                            DoubleLinkedList::new(),
                        );
                        (remain as *const DoubleLinkedList as *mut DoubleLinkedList)
                            .as_mut()
                            .expect("err")
                    };
                    self.insert_one(parent_idx - origin_size - 1, node);
                    return Ok(parent);
                }
                parent_idx += 1;
            }
            //fall into bump alloc
        }
        return unsafe { BUMP.lock().alloc(size) };
    }

    pub fn free(&mut self, ptr: *mut u8, size: usize) {
        let origin_size = (size + PG_SIZE - 1) / PG_SIZE - 1;

        let mut final_ptr = ptr;
        let mut final_idx = origin_size;
        let rd_tree = get_rd_tree();
        //check prev
        let prev = ptr as usize - PG_SIZE;
        let pflag = -(rd_tree.get_mut(prev << 16) >> 48);
        if pflag > 0 {
            let start = ptr as usize - (pflag as usize) * PG_SIZE;
            if let Some(prev_ptr) = self.remove_spec_one(
                (pflag - 1) as usize,
                start as *mut DoubleLinkedList,
                rd_tree,
            ) {
                //successfully combine with previous
                final_ptr = prev_ptr;
                final_idx += pflag as usize;
            }
        }
        //check next
        let next = ptr as usize + (origin_size + 1) * PG_SIZE;
        let nflag = -(rd_tree.get_mut(next << 16) >> 48);
        if nflag > 0 {
            let start = next;
            if self
                .remove_spec_one(
                    (nflag as usize - 1) as usize,
                    start as *mut DoubleLinkedList,
                    rd_tree,
                )
                .is_some()
            {
                //successfully combine with next
                final_idx += nflag as usize;
            }
        }
        if final_idx < self.get_slice().len() {
            let node: &'static mut DoubleLinkedList = unsafe {
                core::ptr::write(
                    final_ptr as *const _ as *mut DoubleLinkedList,
                    DoubleLinkedList::new(),
                );
                (final_ptr as *const _ as *mut DoubleLinkedList)
                    .as_mut()
                    .expect("err")
            };
            self.insert_one(final_idx, node);
        } else {
            #[cfg(not(feature = "fixed_heap"))]
            unsafe {
                system_alloc::munmap(final_ptr, (final_idx + 1) * PG_SIZE)
            };
        }
    }

    fn get_slice(&mut self) -> &mut [Mutex<Option<&'static mut DoubleLinkedList>>] {
        let mut ptr_val = self.lists.load(Ordering::Relaxed);
        if ptr_val.is_null() {
            unsafe {
                let new_ptr = META_BUMP
                    .lock()
                    .alloc(core::mem::size_of::<
                        [Mutex<Option<&'static mut DoubleLinkedList>>; BACKEND_MAX_PAGE],
                    >())
                    .expect("err");
                let slice = core::slice::from_raw_parts_mut(
                    new_ptr as *mut Mutex<Option<&'static mut DoubleLinkedList>>,
                    BACKEND_MAX_PAGE,
                );
                #[allow(clippy::declare_interior_mutable_const)]
                const VAL: Mutex<Option<&'static mut DoubleLinkedList>> = Mutex::new(None);
                for s in slice {
                    *s = VAL;
                }
                if let Err(real_ptr) = self.lists.compare_exchange(
                    null_mut(),
                    new_ptr as *mut Mutex<Option<&'static mut DoubleLinkedList>>,
                    Ordering::AcqRel,
                    Ordering::Relaxed,
                ) {
                    META_BUMP.lock().dealloc(new_ptr as *mut usize);
                    ptr_val = real_ptr;
                } else {
                    ptr_val = new_ptr as *mut Mutex<Option<&'static mut DoubleLinkedList>>;
                }
            }
        }
        unsafe { core::slice::from_raw_parts_mut(ptr_val, BACKEND_MAX_PAGE) }
    }
}

pub static mut FREELIST: FreeList = FreeList::new();

/// # Experimental
///
/// It can be very hard to use if we do not have `Copy` trait.
/// Thus, we sadly add another global variable :(
#[derive(Copy, Clone)]
pub struct BuddySystemAllocator;

impl Default for BuddySystemAllocator {
    fn default() -> Self {
        BuddySystemAllocator
    }
}

unsafe impl GlobalAlloc for BuddySystemAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if layout.size() > isize::MAX as usize {
            return core::ptr::null_mut::<u8>();
        }
        FREELIST.alloc(layout.size()).unwrap_or(null_mut())
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        FREELIST.free(ptr, layout.size())
    }
}

unsafe impl Allocator for BuddySystemAllocator {
    /// Follow the implementation in
    /// https://github.com/rust-lang/rust/blob/master/library/alloc/src/alloc.rs#L161
    fn allocate(&self, layout: Layout) -> Result<NonNull<[u8]>, core::alloc::AllocError> {
        match layout.size() {
            0 => Ok(NonNull::slice_from_raw_parts(layout.dangling(), 0)),
            // SAFETY: `layout` is non-zero in size,
            size => unsafe {
                let raw_ptr = self.alloc(layout);
                let ptr = NonNull::new(raw_ptr).ok_or(core::alloc::AllocError)?;
                Ok(NonNull::slice_from_raw_parts(ptr, size))
            },
        }
    }

    unsafe fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
        if layout.size() != 0 {
            self.dealloc(ptr.as_ptr(), layout);
        }
    }
}

#[cfg(test)]
mod test {
    use super::*;

    #[cfg(all(target_arch = "x86_64", target_os = "linux"))]
    #[test]
    fn it_works() {
        let lay = Layout::from_size_align(4095, 1).expect("err");
        unsafe {
            let ptr = BuddySystemAllocator.alloc(lay);
            let ptr2 = BuddySystemAllocator.alloc(lay);
            BuddySystemAllocator.dealloc(ptr, lay);
            BuddySystemAllocator.dealloc(ptr2, lay);
            let ptr3 = BuddySystemAllocator.alloc(lay);
            let ptr4 = BuddySystemAllocator.alloc(lay);
            assert_ne!(ptr3 as usize, ptr4 as usize)
        }
    }
}
