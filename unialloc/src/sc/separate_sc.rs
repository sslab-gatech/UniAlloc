use crate::collections::linklist::*;
use crate::collections::radix_tree::RadixTree;
use crate::collections::radix_tree::{allocate_node, RadixBottomNode, TreeNode};
use crate::error::{AllocError, Result};
use crate::page::ObjectPage;
use crate::prelude::*;
use crate::*;
use alloc::vec::Vec;
use core::alloc::Layout;
use core::borrow::BorrowMut;
use core::cmp::Ordering;
use core::ptr::{self, NonNull};

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
    full_start: usize,
    /// Tracks the start of partial slab. It is meaningful only if `partial_count >= 1`
    partial_start: usize,
    /// Tracks the start of empty slab. It is meaningful only if `empty_count >= 1`
    empty_start: usize,
    /// Tracks the start of uninitialized slab. It is meaningful only if `uninit_count >= 1`
    uninit_start: usize,
    full_count: usize,
    empty_count: usize,
    partial_count: usize,
    uninit_count: usize,

    // start, count
    // full: (usize, usize),
    // partial: (usize, usize),
    // empty: (usize, usize),
    // uninit: (usize, usize),

    // page_num: how many OS pages in our page
    pg_num: usize,
    // pg_count: how many chunks of current size class in one "page"
    pg_count: usize,
    // when allocating, what align between chunks shall we take
    pg_align: usize,
    bitfields: Vec<u32, GlobalBackend>,
    pages: ArrayLinkedList<ObjectPage>,
}

impl SCAllocator {
    // The new "new" function takes three parameters:
    // current size class, current size class's idx, how many OS pages are combined into one page.rs
    pub fn new(size_class: usize, num_os_pages: usize) -> Self {
        if size_class == 0 {
            return Self {
                full_start: 0,
                partial_start: 0,
                empty_start: 0,
                uninit_start: 0,
                full_count: 0,
                empty_count: 0,
                partial_count: 0,
                uninit_count: 0,
                pg_count: 0,
                pg_num: 0,
                pg_align: 0,
                bitfields: Vec::with_capacity_in(0, GlobalBackend),
                pages: ArrayLinkedList::new(),
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
            full_start: 0,
            partial_start: 0,
            empty_start: 0,
            uninit_start: 0,
            full_count: 0,
            empty_count: 0,
            partial_count: 0,
            uninit_count: 0,
            pg_count,
            pg_num: num_os_pages,
            pg_align: align,
            bitfields: Vec::with_capacity_in(0, GlobalBackend),
            pages: ArrayLinkedList::new(),
        }
    }

    pub fn remove_full(&mut self, idx: usize) {
        if idx == self.full_start {
            self.full_start = self.pages.get_next(idx).expect("index error");
        }

        self.pages.remove_node(idx);
        self.full_count -= 1;
    }

    pub fn insert_full(&mut self, idx: usize) {
        if self.full_count == 0 {
            self.full_start = idx;
            self.pages.reset_links(idx);
        } else {
            self.pages.insert_to_prev(self.full_start, idx);
        }
        self.full_count += 1;
    }

    pub fn insert_partial(&mut self, idx: usize) {
        if self.partial_count == 0 {
            self.partial_start = idx;
            self.pages.reset_links(idx);
        } else {
            self.pages.insert_to_prev(self.partial_start, idx);
        }
        self.partial_count += 1;
    }

    pub fn remove_partial(&mut self, idx: usize) {
        if idx == self.partial_start {
            self.partial_start = self.pages.get_next(idx).expect("index error");
        }

        self.pages.remove_node(idx);
        self.partial_count -= 1;
    }

    pub fn remove_uninit(&mut self, idx: usize) {
        if idx == self.uninit_start {
            self.uninit_start = self.pages.get_next(idx).expect("index error");
        }

        self.pages.remove_node(idx);
        self.uninit_count -= 1;
    }

    pub fn get_uninit(&mut self) -> (usize, usize) {
        if self.uninit_count == 0 {
            if self.pages.capacity() == 0 {
                self.pages.reserve(512);
            }

            // the linklist's algorithm sets its prev and next to itself
            self.pages.push(ObjectPage::new());
            let n = (self.pg_count + 32 - 1) / 32;
            if self.bitfields.capacity() - self.bitfields.len() < n {
                self.bitfields
                    .reserve(PAGE_SIZE / core::mem::size_of::<u32>());
            }
            for _ in 0..n {
                self.bitfields.push(0);
            }
            let index = self.pages.len() - 1;

            self.uninit_start = index;
            self.uninit_count += 1;
        }

        let res = self.uninit_start;
        self.remove_uninit(self.uninit_start);

        let obj = self.pages.get_mut(res as usize).expect("Index error");
        let ptr = obj.allocate_page(self.pg_num) as usize;

        (res, ptr)
    }

    pub fn get_empty(&mut self) -> (usize, Option<usize>) {
        if self.empty_count > 0 {
            let res = self.empty_start;
            self.empty_start = self.pages.get_next(self.empty_start).expect("index error");
            self.pages.remove_node(res);

            self.empty_count -= 1;
            (res, None)
        } else {
            let ans = self.get_uninit();
            (ans.0, Some(ans.1))
        }
    }

    pub fn back_uninit(&mut self, idx: usize) -> *mut u8 {
        if self.uninit_count == 0 {
            self.uninit_start = idx;
            self.pages.reset_links(idx);
        } else {
            self.pages.insert_to_prev(self.uninit_start, idx);
        }
        let obj = self.pages.get_mut(idx as usize).expect("cannot getmut");
        self.uninit_count += 1;
        obj.destroy_page(self.pg_num)
    }

    // This function is only used in deallocate, for more info, refer to that function
    pub fn try_back_ety(&mut self, idx: usize) -> Option<usize> {
        if self.empty_count > 2048 {
            let ptr = self
                .pages
                .get_mut(idx as usize)
                .expect("cannot getmut")
                .get_data_ptr();
            Some(ptr as usize)
        } else {
            None
        }
    }

    pub fn back_ety(&mut self, idx: usize) -> Option<usize> {
        if self.empty_count > 2048 {
            let ptr = self.back_uninit(idx);
            Some(ptr as usize)
        } else {
            if self.empty_count == 0 {
                self.empty_start = idx;
                self.pages.reset_links(idx);
            } else {
                self.pages.insert_to_prev(self.empty_start, idx);
            }
            self.empty_count += 1;
            None
        }
    }

    /// Allocates a block of memory described by `layout`.
    ///
    /// Returns a pointer to a valid region of memory or an
    /// Error.
    ///
    /// The function may also move around pages between lists
    /// (empty -> partial or partial -> full).
    pub fn allocate(&mut self, align: usize, ptr_map: &mut RadixTree) -> Result<NonNull<u8>> {
        let mut ptr: *mut u8;
        let n = (self.pg_count + 32 - 1) / 32;
        if self.partial_count > 0 {
            let mut head = self.partial_start;
            loop {
                let obj = self.pages.get_mut(head as usize).expect("cannot getmut");
                ptr = obj.allocate(
                    align,
                    &mut self.bitfields[head * n..(head + 1) * n],
                    self.pg_count,
                    self.pg_align,
                    self.pg_num,
                );
                if ptr.is_null() {
                    head = self.pages.get_next(head).expect("index error");
                } else {
                    if obj.is_full(self.pg_count) {
                        self.remove_partial(head);
                        self.insert_full(head);
                    }
                    break;
                }
                if head == self.partial_start {
                    break;
                }
            }
            if !ptr.is_null() {
                return NonNull::new(ptr).ok_or(AllocError::ENOMEM);
            }
        } //final case, try to get a new one
        let idx = self.get_empty();
        let obj = self.pages.get_mut(idx.0 as usize).expect("cannot getmut");
        ptr = obj.allocate(
            align,
            &mut self.bitfields[idx.0 * n..(idx.0 + 1) * n],
            self.pg_count,
            self.pg_align,
            self.pg_num,
        );
        if obj.is_full(self.pg_count) {
            self.insert_full(idx.0);
        } else {
            self.insert_partial(idx.0);
        }
        if let Some(addr) = idx.1 {
            self.handle_rd_tree_insert(ptr_map, idx.0, addr);
        }

        NonNull::new(ptr).ok_or(AllocError::ENOMEM)
    }

    fn handle_rd_tree_insert(&self, ptr_map: &mut RadixTree, idx: usize, addr: usize) {
        let num = 1_usize << 16;
        let rem = num - ((addr >> PAGE_SIZE.trailing_zeros()) & (num - 1));
        let ptr = align_12k(addr);
        if rem >= self.pg_num {
            ptr_map
                .insert(ptr << 16, (idx + 1) as i64, self.pg_num)
                .expect("err");
        } else {
            let temp = ptr;
            ptr_map
                .insert(temp << 16, (idx + 1) as i64, rem)
                .expect("err");
            ptr_map
                .insert(
                    (ptr + 4096_usize * rem) << 16,
                    (idx + 1) as i64,
                    self.pg_num - rem,
                )
                .expect("err");
        }
    }

    // We use the same strategy from tcmalloc:
    // We first try to alloc from partial, then create empty page
    pub fn allocate_batch_v2(
        &mut self,
        ptr_map: &mut RadixTree,
        res_array: &mut [usize],
        count_p: usize,
    ) -> Result<NonNull<u8>> {
        let count = count_p.min(res_array.len());
        let n = (self.pg_count + 32 - 1) / 32;
        if count == 0 {
            return Err(AllocError::ESIZE);
        }
        // TODO: discuss whther this align is required
        // TODO: use the alignment
        let res: usize = (self.allocate(1, ptr_map).expect("alloc failed").as_ptr()) as usize;
        res_array[count - 1] = res;
        let mut allocated = 0_usize;
        let count = count - 1;
        while self.partial_count > 0 && allocated < count {
            let cur_idx = self.partial_start;
            let obj = self.pages.get_mut(cur_idx as usize).expect("cannot getmut");
            allocated += obj.allocate_batch(
                res_array,
                allocated,
                count - allocated,
                &mut self.bitfields[cur_idx * n..(cur_idx + 1) * n],
                self.pg_count,
                self.pg_align,
            );
            if obj.is_full(self.pg_count) {
                self.remove_partial(cur_idx);
                self.insert_full(cur_idx);
            }
            if allocated == count {
                return Ok(NonNull::new(res as *mut u8).expect("err"));
            }
        }
        while allocated < count {
            let idx = self.get_empty();
            let obj = self.pages.get_mut(idx.0 as usize).expect("cannot getmut");

            if count - allocated >= self.pg_count {
                let ptr = obj.allocate_all(
                    &mut self.bitfields[idx.0 * n..(idx.0 + 1) * n],
                    self.pg_count,
                );
                for i in 0..self.pg_count {
                    res_array[allocated + i] = (ptr as usize) + i * self.pg_align;
                }
                allocated += self.pg_count;
                self.insert_full(idx.0);
            } else {
                allocated += obj.allocate_batch(
                    res_array,
                    allocated,
                    count - allocated,
                    &mut self.bitfields[idx.0 * n..(idx.0 + 1) * n],
                    self.pg_count,
                    self.pg_align,
                );
                self.insert_partial(idx.0);
            }
            if let Some(addr) = idx.1 {
                self.handle_rd_tree_insert(ptr_map, idx.0, addr);
            }
        }
        assert!(allocated == count);
        Ok(NonNull::new(res as *mut u8).expect("err"))
    }

    pub fn allocate_batch(
        &mut self,
        align: usize,
        ptr_map: &mut RadixTree,
    ) -> (Result<NonNull<u8>>, usize, usize) {
        let mut ptr: *mut u8;
        let n = (self.pg_count + 32 - 1) / 32;
        if self.partial_count > 0 {
            let mut head = self.partial_start;
            loop {
                let obj = self.pages.get_mut(head as usize).expect("cannot getmut");

                ptr = obj.allocate(
                    align,
                    &mut self.bitfields[head * n..(head + 1) * n],
                    self.pg_count,
                    self.pg_align,
                    self.pg_num,
                );
                if ptr.is_null() {
                    // head = obj.get_next() as i32;
                    head = self.pages.get_next(head).expect("index error");
                } else {
                    if obj.is_full(self.pg_count) {
                        self.remove_partial(head);
                        self.insert_full(head);
                    } else {
                    }
                    break;
                }
                if head == self.partial_start {
                    break;
                }
            }
            if !ptr.is_null() {
                return (Ok(NonNull::new(ptr).unwrap()), self.pg_align, 0);
            }
        } //final case, try to get a new one
        let idx = self.get_empty();
        let obj = self.pages.get_mut(idx.0 as usize).expect("cannot getmut");
        ptr = obj.allocate_all(
            &mut self.bitfields[idx.0 * n..(idx.0 + 1) * n],
            self.pg_count,
        );
        if obj.is_full(self.pg_count) {
            self.insert_full(idx.0);
        } else {
            self.insert_partial(idx.0);
        }
        if let Some(addr) = idx.1 {
            self.handle_rd_tree_insert(ptr_map, idx.0, addr);
        }

        (
            NonNull::new(ptr).ok_or(AllocError::ENOMEM),
            self.pg_align,
            self.pg_count,
        )
    }

    fn handle_rd_tree_remove(&self, ptr_map: &mut RadixTree, addr: usize) {
        let rem = 4096 - ((addr >> PAGE_SIZE.trailing_zeros()) % 4096);
        let ptr = align_12k(addr);
        if rem >= self.pg_num {
            ptr_map.remove(ptr << 16, self.pg_num).expect("err");
        } else {
            ptr_map.remove(ptr << 16, rem).expect("err");
            ptr_map
                .remove((ptr + 4096_usize * rem) << 16, self.pg_num - rem)
                .expect("err");
        }
    }

    /// Deallocating a previously allocated `ptr` described by `Layout`.
    ///
    /// May return an error in case an invalid `layout` is provided.
    /// The function may also move internal slab pages between lists partial -> empty
    /// or full -> partial lists.
    pub fn deallocate(&mut self, ptr: NonNull<u8>, ptr_map: &mut RadixTree) {
        let page_vaddr = align_12k(ptr.as_ptr() as usize);
        //Rd tree can return 0 as default even when key not exists, so we store everything plus 1
        //To ensure we can detect this
        let idx = ptr_map.get_mut(page_vaddr << 16) - 1;
        assert!(idx >= 0);
        let n = (self.pg_count + 32 - 1) / 32;
        #[cfg(feature = "allow_mem_leak")]
        if idx < 0 {
            return;
        }
        // assert!(idx >= 0);
        let obj_pge = self.pages.get_mut(idx as usize).expect("cannot getmut");
        let mut back_partial = false;
        if obj_pge.is_full(self.pg_count) {
            back_partial = true;
        }
        let idxu: usize = idx as usize;
        obj_pge
            .deallocate(
                ptr,
                &mut self.bitfields[idxu * n..(idxu + 1) * n],
                self.pg_align,
            )
            .expect("The deallocation failed in ObjectPage");

        if back_partial {
            self.remove_full(idx as usize);
            let obj_pge = self.pages.get_mut(idx as usize).expect("cannot getmut");
            if obj_pge.is_empty() {
                // In the allocate function, we
                //      1. alloc page from buddy
                //      2. add it to rd tree
                // So here in deallocate, we need to do this in reversed order:
                // We use try_back to get the page to be returned, then 1. remove from rd 2. ret to buddy
                // This operation is the key for lock-free rd tree, we use buddy as our 'lock'
                if let Some(p) = self.try_back_ety(idx as usize) {
                    self.handle_rd_tree_remove(ptr_map, p);
                }
                self.back_ety(idx as usize);
            } else {
                self.insert_partial(idx as usize);
            }
        } else if obj_pge.is_empty() {
            self.remove_partial(idx as usize);
            if let Some(p) = self.try_back_ety(idx as usize) {
                self.handle_rd_tree_remove(ptr_map, p);
            }
            self.back_ety(idx as usize);
        }
    }

    //Here is a decision whether to use a stupid cache for previous result
    //This can benefit if the returned value are in the same object page
    //todo bench to see if this decision is good
    pub fn deallocate_batch(
        &mut self,
        res_array: &mut [usize],
        count_p: usize,
        ptr_map: &mut RadixTree,
    ) -> Result<()> {
        let count = count_p.min(res_array.len());
        let n = (self.pg_count + 32 - 1) / 32;
        if count == 0 {
            return Ok(());
        }
        let mut deallocated = 0;
        while deallocated < count {
            let ptr = res_array[deallocated];
            let page_vaddr = align_12k(ptr);
            let idx = ptr_map.get_mut(page_vaddr << 16) - 1;
            #[cfg(feature = "allow_mem_leak")]
            if idx < 0 {
                return Ok(());
            }
            // assert!(idx >= 0);
            let obj_pge = self.pages.get_mut(idx as usize).expect("cannot getmut");
            let mut back_partial = false;
            if obj_pge.is_full(self.pg_count) {
                back_partial = true;
            }
            let idxu: usize = idx as usize;
            let t = obj_pge.deallocate_batch(
                &mut res_array[deallocated..],
                &mut self.bitfields[idxu * n..(idxu + 1) * n],
                self.pg_align,
                self.pg_num,
            );
            assert_ne!(t, 0);
            deallocated += t;
            if back_partial {
                self.remove_full(idx as usize);
                let obj_pge = self.pages.get_mut(idx as usize).expect("cannot getmut");
                if obj_pge.is_empty() {
                    //
                    //In the allocate function, we 1. alloc page from buddy 2. add it to rd tree
                    //So here in deallocate, we need to do this in reversed order:
                    //We use try_back to get the page to be returned, then 1. remove from rd 2. ret to buddy
                    //This operation is the key for lock-free rd tree, we use buddy as our 'lock'
                    if let Some(p) = self.try_back_ety(idx as usize) {
                        self.handle_rd_tree_remove(ptr_map, p);
                    }
                    self.back_ety(idx as usize);
                } else {
                    self.insert_partial(idx as usize);
                }
            } else if obj_pge.is_empty() {
                self.remove_partial(idx as usize);
                if let Some(p) = self.try_back_ety(idx as usize) {
                    self.handle_rd_tree_remove(ptr_map, p);
                }
                self.back_ety(idx as usize);
            }
        }
        Ok(())
    }
}
