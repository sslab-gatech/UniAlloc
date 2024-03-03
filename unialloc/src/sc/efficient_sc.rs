use crate::collections::linklist::*;
use crate::collections::radix_tree::RadixTree;
use crate::collections::radix_tree::{allocate_node, RadixBottomNode, TreeNode};
use crate::error::{AllocError, Result};
use crate::page::{EfObjectPage, PG_BUMP};
use crate::prelude::*;
use crate::*;
use alloc::vec::Vec;
use core::alloc::Layout;
use core::borrow::BorrowMut;
use core::cmp::Ordering;
use core::ptr::{self, null_mut, NonNull};

pub fn align_12k(ptr: usize) -> usize {
    let mut page_vaddr = ptr & !((PAGE_SIZE - 1) as usize);
    match 12_u32.cmp(&PAGE_SIZE.trailing_zeros()) {
        Ordering::Greater => page_vaddr <<= 12 - PAGE_SIZE.trailing_zeros(),
        Ordering::Less => page_vaddr >>= PAGE_SIZE.trailing_zeros() - 12,
        Ordering::Equal => {}
    };
    page_vaddr
}

/// A slab allocator allocates elements of a fixed size.
///
/// It maintains three internal lists of `ObjectPage8k`
/// from which it can allocate memory.
///
///  * `empty_slabs`: Is a list of pages that the SCAllocator maintains, but
///    has 0 allocations in them.
///  * `slabs`: A list of pages partially allocated and still have room for more.
///  * `full_slabs`: A list of pages that are completely allocated.
///
/// On allocation we allocate memory from `slabs`, however if the list is empty
/// we try to reclaim a page from `empty_slabs` before we return with an out-of-memory
/// error. If a page becomes full after the allocation we move it from `slabs` to
/// `full_slabs`.
///
/// Similarly, on dealloaction we might move a page from `full_slabs` to `slabs`
/// or from `slabs` to `empty_slabs` after we deallocated an object.
pub struct SCAllocator {
    /// Tracks the start of full slab. It is meaningful only if `full_count >= 1`
    full_start: *mut EfObjectPage,
    /// Tracks the start of partial slab. It is meaningful only if `partial_count >= 1`
    partial_start: *mut EfObjectPage,
    /// Tracks the start of empty slab. It is meaningful only if `empty_count >= 1`
    empty_start: *mut EfObjectPage,
    /// Tracks the start of uninitialized slab. It is meaningful only if `uninit_count >= 1`
    uninit_start: *mut EfObjectPage,
    empty_count: usize,

    // start, count
    // full: (usize, usize),
    // partial: (usize, usize),
    // empty: (usize, usize),
    // uninit: (usize, usize),

    // page_num: how many OS pages in our page
    pg_num: usize,
    // pg_count: how many chunks of current size class in one "page"
    pg_count: i32,
    // when allocating, what align between chunks shall we take
    pg_align: i32,
}

impl SCAllocator {
    // The new "new" function takes three parameters:
    // current size class, current size class's idx, how many OS pages are combined into one page.rs
    pub fn new(size_class: usize, num_os_pages: usize) -> Self {
        if size_class == 0 {
            return Self {
                full_start: null_mut(),
                partial_start: null_mut(),
                empty_start: null_mut(),
                uninit_start: null_mut(),
                empty_count: 0,
                pg_count: 0,
                pg_num: 0,
                pg_align: 0,
            };
        }
        let pg_count = (PAGE_SIZE * num_os_pages) / size_class;
        let perfect_align: usize = (PAGE_SIZE * num_os_pages) / pg_count;
        let diff_aligh: usize = perfect_align - size_class;

        let min_align: usize = if diff_aligh.next_power_of_two() / 2 == 0 {
            1
        } else {
            diff_aligh.next_power_of_two() / 2
        };
        let rem: usize = if size_class % min_align == 0 {
            size_class
        } else {
            (size_class / min_align + 1) * min_align
        };
        let align: usize = if perfect_align.is_power_of_two() {
            perfect_align
        } else {
            rem
        };

        Self {
            full_start: null_mut(),
            partial_start: null_mut(),
            empty_start: null_mut(),
            uninit_start: null_mut(),
            empty_count: 0,
            pg_count: pg_count as i32,
            pg_num: num_os_pages,
            pg_align: align as i32,
        }
    }

    pub fn get_ref(ptr: *mut EfObjectPage) -> &'static mut EfObjectPage {
        unsafe { &mut *ptr }
    }

    pub fn remove_full(&mut self, idx: &mut EfObjectPage) {
        assert!(!self.full_start.is_null());
        let full_head = self.full_start;
        if core::ptr::eq(Self::get_ref(full_head).get_next(), full_head) {
            assert_eq!(idx as *const _ as usize, full_head as *const _ as usize);
            self.full_start = null_mut();
        } else {
            let prev = idx.get_prev();
            idx.get_next().set_prev(prev as *const _ as usize);
            let next = idx.get_next();
            idx.get_prev().set_next(next as *const _ as usize);
            // idx.set_next(idx as *const _ as usize);
            // idx.set_prev(idx as *const _ as usize);
            if core::ptr::eq(full_head, idx) {
                self.full_start = next;
            }
        }
    }

    pub fn insert_full(&mut self, idx: &mut EfObjectPage) {
        if !self.full_start.is_null() {
            let full_head = self.full_start;
            let prev = Self::get_ref(full_head).get_prev();
            idx.set_prev(prev as *const _ as usize);
            idx.set_next(full_head as *const _ as usize);
            prev.set_next(idx as *const _ as usize);
            Self::get_ref(full_head).set_prev(idx as *const _ as usize);
        } else {
            idx.set_prev(idx as *const _ as usize);
            idx.set_next(idx as *const _ as usize);
            self.full_start = idx;
        }
    }

    pub fn insert_partial(&mut self, idx: &mut EfObjectPage) {
        if !self.partial_start.is_null() {
            let partial_head = self.partial_start;
            let prev = Self::get_ref(partial_head).get_prev();
            idx.set_prev(prev as *const _ as usize);
            idx.set_next(partial_head as *const _ as usize);
            prev.set_next(idx as *const _ as usize);
            Self::get_ref(partial_head).set_prev(idx as *const _ as usize);
        } else {
            idx.set_prev(idx as *const _ as usize);
            idx.set_next(idx as *const _ as usize);
            self.partial_start = idx;
        }
    }

    pub fn remove_partial(&mut self, idx: &mut EfObjectPage) {
        assert!(!self.partial_start.is_null());
        let partial_head = self.partial_start;
        if core::ptr::eq(Self::get_ref(partial_head).get_next(), partial_head) {
            assert_eq!(idx as *const _ as usize, partial_head as *const _ as usize);
            self.partial_start = null_mut();
        } else {
            let prev = idx.get_prev();
            idx.get_next().set_prev(prev as *const _ as usize);
            let next = idx.get_next();
            idx.get_prev().set_next(next as *const _ as usize);
            // idx.set_next(idx as *const _ as usize);
            // idx.set_prev(idx as *const _ as usize);
            if core::ptr::eq(partial_head, idx) {
                self.partial_start = next;
            }
        }
    }

    pub fn remove_uninit(&mut self) -> &'static mut EfObjectPage {
        assert!(!self.uninit_start.is_null());
        let uninit_head = self.uninit_start;
        if core::ptr::eq(Self::get_ref(uninit_head).get_next(), uninit_head) {
            self.uninit_start = null_mut();
            Self::get_ref(uninit_head)
        } else {
            let idx = Self::get_ref(uninit_head);
            let prev = idx.get_prev();
            idx.get_next().set_prev(prev as *const _ as usize);
            let next = idx.get_next();
            idx.get_prev().set_next(next as *const _ as usize);
            // idx.set_next(idx as *const _ as usize);
            // idx.set_prev(idx as *const _ as usize);
            self.uninit_start = next;
            idx
        }
    }

    pub fn remove_empty(&mut self) -> &'static mut EfObjectPage {
        assert!(!self.empty_start.is_null());
        assert!(self.empty_count > 0);

        let ety_head = Self::get_ref(self.empty_start);
        let prev = ety_head.get_prev();
        ety_head.get_next().set_prev(prev as *const _ as usize);
        let next = ety_head.get_next();
        ety_head.get_prev().set_next(next as *const _ as usize);
        // idx.set_next(idx as *const _ as usize);
        // idx.set_prev(idx as *const _ as usize);
        if self.empty_count > 1 {
            self.empty_start = next;
        } else {
            self.empty_start = null_mut();
        }

        self.empty_count -= 1;
        ety_head
    }

    pub fn get_uninit(&mut self) -> (&mut EfObjectPage, usize) {
        if !self.uninit_start.is_null() {
            let res = self.remove_uninit();
            let ptr = res.allocate_page(self.pg_num as usize) as usize;
            (res, ptr)
        } else {
            if let Ok(memory) =
                unsafe { PG_BUMP.lock().alloc(core::mem::size_of::<EfObjectPage>()) }
            {
                let res = unsafe {
                    core::ptr::write(memory as *mut EfObjectPage, EfObjectPage::new());
                    (memory as *mut EfObjectPage).as_mut().expect("err")
                };
                let ptr = res.allocate_page(self.pg_num as usize) as usize;
                return (res, ptr);
            }
            panic!();
        }
    }

    pub fn get_empty(&mut self) -> (*mut EfObjectPage, Option<usize>) {
        if !self.empty_start.is_null() {
            let res = self.remove_empty();
            (res, None)
        } else {
            let ans = self.get_uninit();
            (ans.0, Some(ans.1))
        }
    }

    pub fn insert_uninit(&mut self, idx: &mut EfObjectPage) -> *mut u8 {
        if !self.uninit_start.is_null() {
            let uninit_head = self.uninit_start;
            let prev = Self::get_ref(uninit_head).get_prev();
            idx.set_prev(prev as *const _ as usize);
            idx.set_next(uninit_head as *const _ as usize);
            prev.set_next(idx as *const _ as usize);
            Self::get_ref(uninit_head).set_prev(idx as *const _ as usize);
        } else {
            idx.set_prev(idx as *const _ as usize);
            idx.set_next(idx as *const _ as usize);
            self.uninit_start = idx;
        }
        idx.destroy_page(self.pg_num as usize)
    }

    // This function is only used in deallocate, for more info, refer to that function
    pub fn try_insert_ety(&mut self, idx: &mut EfObjectPage) -> Option<usize> {
        if self.empty_count > 2048 {
            let ptr = idx.get_data_ptr();
            Some(ptr as usize)
        } else {
            None
        }
    }

    pub fn insert_ety(&mut self, idx: &mut EfObjectPage) -> Option<usize> {
        if self.empty_count > 2048 {
            let ptr = self.insert_uninit(idx);
            Some(ptr as usize)
        } else {
            if !self.empty_start.is_null() {
                let ety_head = self.empty_start;
                let prev = Self::get_ref(ety_head).get_prev();
                idx.set_prev(prev as *const _ as usize);
                idx.set_next(ety_head as *const _ as usize);
                prev.set_next(idx as *const _ as usize);
                Self::get_ref(ety_head).set_prev(idx as *const _ as usize);
            } else {
                idx.set_prev(idx as *const _ as usize);
                idx.set_next(idx as *const _ as usize);
                self.empty_start = idx;
            }
            self.empty_count += 1;
            None
        }
    }

    fn handle_rd_tree_insert(pg_num: usize, ptr_map: &mut RadixTree, idx: usize, addr: usize) {
        let num = 1_usize << 16;
        let rem = num - ((addr >> PAGE_SIZE.trailing_zeros()) & (num - 1));
        let ptr = align_12k(addr);
        if rem >= pg_num {
            ptr_map
                .insert(ptr << 16, (idx) as i64, pg_num)
                .expect("err");
        } else {
            let temp = ptr;
            ptr_map.insert(temp << 16, (idx) as i64, rem).expect("err");
            ptr_map
                .insert((ptr + 4096_usize * rem) << 16, (idx) as i64, pg_num - rem)
                .expect("err");
        }
    }

    // We use the same strategy from tcmalloc:
    // We first try to alloc from partial, then create empty page
    pub fn allocate_batch_v2(
        &mut self,
        align: usize,
        ptr_map: &mut RadixTree,
    ) -> Result<(*mut u8, usize, Option<usize>)> {
        let pg_count = self.pg_count;
        let pg_align = self.pg_align;
        if !self.partial_start.is_null() && align == 1 {
            let idx = Self::get_ref(self.partial_start);
            let ans = idx.allocate_all(pg_count as usize, pg_align as usize);
            self.remove_partial(idx);
            self.insert_full(idx);
            Ok(ans)
        } else {
            let idx = self.get_empty();
            let obj = Self::get_ref(idx.0);
            let new_page = idx.1;
            let ans = obj.allocate_all(pg_count as usize, pg_align as usize);

            if let Some(addr) = new_page {
                Self::handle_rd_tree_insert(
                    self.pg_num as usize,
                    ptr_map,
                    obj as *const _ as usize,
                    addr,
                );
            }
            self.insert_full(obj);
            Ok(ans)
        }
    }

    fn handle_rd_tree_remove(&self, ptr_map: &mut RadixTree, addr: usize) {
        let rem = 4096 - ((addr >> PAGE_SIZE.trailing_zeros()) % 4096);
        let ptr = align_12k(addr);
        if rem >= self.pg_num as usize {
            ptr_map
                .remove(ptr << 16, self.pg_num as usize)
                .expect("err");
        } else {
            ptr_map.remove(ptr << 16, rem).expect("err");
            ptr_map
                .remove((ptr + 4096_usize * rem) << 16, self.pg_num as usize - rem)
                .expect("err");
        }
    }

    //Here is a decision whether to use a stupid cache for previous result
    //This can benefit if the returned value are in the same object page
    //todo bench to see if this decision is good
    pub fn deallocate_batch(&mut self, ptr: *mut usize, ptr_map: &mut RadixTree) -> Result<()> {
        let mut head = ptr;
        while !head.is_null() {
            let page_vaddr = align_12k(head as usize);
            let idx = ptr_map.get_mut(page_vaddr << 16) & ((1i64 << 48) - 1);
            assert_ne!(idx, 0);
            let obj_pge = unsafe {
                (idx as *const EfObjectPage as *mut EfObjectPage)
                    .as_mut()
                    .expect("err")
            };
            // assert_ne!(obj_pge.is_empty());
            let mut back_partial = false;
            if obj_pge.is_full(self.pg_count as usize) {
                back_partial = true;
            }
            head = obj_pge.deallocate(head as usize, self.pg_num as usize);

            if obj_pge.is_empty() {
                if back_partial {
                    self.remove_full(obj_pge);
                } else {
                    self.remove_partial(obj_pge);
                }
                if let Some(p) = self.try_insert_ety(obj_pge) {
                    self.handle_rd_tree_remove(ptr_map, p);
                }
                self.insert_ety(obj_pge);
            } else if back_partial {
                self.remove_full(obj_pge);
                self.insert_partial(obj_pge);
            }
        }
        Ok(())
    }
}
