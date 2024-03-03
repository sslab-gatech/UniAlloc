use core::alloc::{AllocError, Allocator, GlobalAlloc, Layout};
use core::cell::RefCell;
use core::cmp;
use core::intrinsics;
use core::iter;
use core::mem;
use core::ops::{Deref, DerefMut, Index, IndexMut};
use core::ptr::{self, NonNull, Unique};
use core::slice;

use super::sync::PthreadMutex as Mutex;
use crate::pal::*;
use static_init::dynamic;

use super::buddy::{Block, BuddyTree, BLOCKS_IN_TREE};
use super::consts::*;

/// Wrapper that just impls deref for a Unique.
///
/// # Safety
///
/// Safe if the Unique is valid.
struct DerefPtr<T>(Unique<T>);

impl<T> DerefPtr<T> {
    const fn new(unique: Unique<T>) -> Self {
        DerefPtr(unique)
    }
}

impl<T> Deref for DerefPtr<T> {
    type Target = T;

    fn deref(&self) -> &T {
        unsafe { self.0.as_ref() }
    }
}

impl<T> DerefMut for DerefPtr<T> {
    fn deref_mut(&mut self) -> &mut T {
        unsafe { self.0.as_mut() }
    }
}

impl<T> Index<usize> for DerefPtr<T>
where
    T: Index<usize, Output = Block>,
{
    type Output = Block;
    fn index(&self, index: usize) -> &Block {
        &self.deref()[index]
    }
}

impl<T> IndexMut<usize> for DerefPtr<T>
where
    T: IndexMut<usize, Output = Block>,
{
    fn index_mut(&mut self, index: usize) -> &mut Block {
        &mut self.deref_mut()[index]
    }
}

/// Buddy System Allocator
///
/// # Notes
///
/// The `heap` is a large continuous chunk that will be split to fine-grained
/// chunks. The `tree` is a region to manage the allocation and deallocation.
/// The `heap` and the `tree` can be separated.
///
/// # Safety
///
/// If there are many buddy allocator exist and the underlying page heap
/// is thread-unsafe, when allocating and deallocatig different instance of
/// buddy system can result in race condition or crash.
///
/// # Explanations
///
/// To use the buddy system allocator correctly, the developer need to understand
/// the meaning of
///
/// (1) different orders (e.g., BASE_ORDER)
///
/// (2) the size of tree array, which is used to store metadata
///
/// (3) the size of the corresponding heap that can be managed by the aforementioned
/// tree array.
///
/// (4) The minimum size of chunk that can be allocated: 2.pow(BASE_ORDER)
///
/// (5) The maximum size of chunk that be allocated: 2.pow(BASE_ORDER + LEVEL_COUNT -1)
///
/// (6) To change the heap size, the developer needs to modify the `LEVEL_COUNT`
/// in consts.rs. Note that we assume the page size is 4096 (2**12).
pub struct BuddySystem<const BASE_ORDER: u8, const MAX_ORDER: u8> {
    /// I embedded the allocator for (1) implementing dropping
    /// (2) implementing dynamic reallocation in the future
    allocator: Mutex<SystemAllocator>,
    /// I store it because I need to deallocate the chunk upon dropping
    tree_start: usize,
    /// The heap is a big continuous chunk (a multiple of page size)
    heap_start: usize,
    /// The tree is a meta-data structure used to manage this big chunk
    tree: Mutex<RefCell<BuddyTree<DerefPtr<[Block; BLOCKS_IN_TREE]>, BASE_ORDER, MAX_ORDER>>>,
}

impl<const BASE_ORDER: u8, const MAX_ORDER: u8> BuddySystem<BASE_ORDER, MAX_ORDER> {
    const MAX_ORDER_SIZE: usize = BASE_ORDER as usize + MAX_ORDER as usize;
    const BASE: usize = BASE_ORDER as usize;
    const MAX: usize = MAX_ORDER as usize;

    pub fn alloc_large(size: usize) -> usize {
        let heap: SystemAllocator = SystemAllocator::default();
        let p1 = unsafe { heap.alloc(Layout::from_size_align_unchecked(size / 2, 1)) as usize };
        let p2 = unsafe { heap.alloc(Layout::from_size_align_unchecked(size / 2, 1)) as usize };
        assert_ne!(p1, usize::MAX);
        assert_ne!(p2, usize::MAX);
        let small = p2.min(p1);
        assert_eq!(small + size / 2, p2.max(p1));
        small
    }

    // When initializing the BuddySystem structure,
    // we need to invoke the underlying allocator to allocate the heap.
    pub fn new() -> Self {
        let tree_size = next_page_size(Self::tree_size());
        debug_assert_eq!(tree_size % PAGE_SIZE, 0);
        let tree_layout = Layout::from_size_align(tree_size, 8).expect("cannot create tree layout");
        let heap_layout =
            Layout::from_size_align(Self::heap_size(), 8).expect("cannot create heap layout");

        let heap: SystemAllocator = SystemAllocator::default();
        let tree_start = unsafe { heap.alloc(tree_layout) as usize };
        // madvise_random(tree_start as *mut u8, tree_size);
        //todo remove this stupid way to alloc for 32GB from OS
        // #[cfg(target_os = "linux")]
        // let heap_start = Self::alloc_large(heap_layout.size());
        // #[cfg(target_os = "macos")]
        let heap_start = unsafe { heap.alloc(heap_layout) as usize };

        Self::new_with_start(heap, heap_start, tree_start)
    }

    /// # Safety
    ///
    /// Safe if all of the parameters are valid, specifically, we need to guarantee
    /// that the `tree` and `heap` have enough space.
    pub fn new_with_start(
        allocator: SystemAllocator,
        heap_start: usize,
        tree_start: usize,
    ) -> Self {
        // TODO: validate the heap_start and tree_start
        let t = RefCell::new(BuddyTree::<
            DerefPtr<[Block; BLOCKS_IN_TREE]>,
            BASE_ORDER,
            MAX_ORDER,
        >::new(
            // check core::ops::Range
            // The range is half-open, so we need to plus one for the `end`
            iter::once(0..(Self::heap_size() + 1)),
            DerefPtr::new(unsafe { Unique::new_unchecked(tree_start as *mut _) }),
        ));
        Self {
            allocator: Mutex::new(allocator),
            tree_start,
            heap_start,
            tree: Mutex::new(t),
        }
    }

    pub const fn tree_size() -> usize {
        mem::size_of::<[Block; BLOCKS_IN_TREE]>()
    }

    pub const fn heap_size() -> usize {
        2usize.pow((BASE_ORDER + MAX_ORDER) as u32)
    }

    fn alloc_impl(&self, layout: Layout, zeroed: bool) -> Result<NonNull<[u8]>, AllocError> {
        match layout.size() {
            0 => Ok(NonNull::slice_from_raw_parts(layout.dangling(), 0)),
            // SAFETY: `layout` is non-zero in size,
            size => unsafe {
                let raw_ptr = if zeroed {
                    alloc_zeroed(layout)
                } else {
                    alloc(layout)
                };
                let ptr = NonNull::new(raw_ptr).ok_or(AllocError)?;
                Ok(NonNull::slice_from_raw_parts(ptr, size))
            },
        }
    }

    unsafe fn grow_impl(
        &self,
        ptr: NonNull<u8>,
        old_layout: Layout,
        new_layout: Layout,
        zeroed: bool,
    ) -> Result<NonNull<[u8]>, AllocError> {
        debug_assert!(
            new_layout.size() >= old_layout.size(),
            "`new_layout.size()` must be greater than or equal to `old_layout.size()`"
        );

        match old_layout.size() {
            0 => self.alloc_impl(new_layout, zeroed),

            // SAFETY: `new_size` is non-zero as `old_size` is greater than or equal to `new_size`
            // as required by safety conditions. Other conditions must be upheld by the caller
            old_size if old_layout.align() == new_layout.align() => {
                let new_size = new_layout.size();

                // `realloc` probably checks for `new_size >= old_layout.size()` or something similar.
                intrinsics::assume(new_size >= old_layout.size());

                let raw_ptr = realloc(ptr.as_ptr(), old_layout, new_size);
                let ptr = NonNull::new(raw_ptr).ok_or(AllocError)?;
                if zeroed {
                    raw_ptr.add(old_size).write_bytes(0, new_size - old_size);
                }
                Ok(NonNull::slice_from_raw_parts(ptr, new_size))
            }

            // SAFETY: because `new_layout.size()` must be greater than or equal to `old_size`,
            // both the old and new memory allocation are valid for reads and writes for `old_size`
            // bytes. Also, because the old allocation wasn't yet deallocated, it cannot overlap
            // `new_ptr`. Thus, the call to `copy_nonoverlapping` is safe. The safety contract
            // for `dealloc` must be upheld by the caller.
            old_size => {
                let new_ptr = self.alloc_impl(new_layout, zeroed)?;
                ptr::copy_nonoverlapping(ptr.as_ptr(), new_ptr.as_mut_ptr(), old_size);
                self.deallocate(ptr, old_layout);
                Ok(new_ptr)
            }
        }
    }
    /// Calculates the order of given usize
    ///
    /// # Safety
    ///
    /// The overflow has been checked
    ///
    /// # Panic
    ///
    /// Panic upon overflow
    fn order(val: usize) -> u8 {
        if val == 0 {
            return 0;
        }

        let log2 = log2(val);

        if log2 > u8::MAX as u32 {
            panic!("Overflow while calculating the order");
        }

        let log2 = log2 as u8;

        if log2 >= BASE_ORDER as u8 {
            log2 - BASE_ORDER as u8
        } else {
            0
        }
    }

    fn order_checked(val: usize) -> Option<u8> {
        if val == 0 {
            return Some(0);
        }

        let log2 = log2(val);

        if log2 > u8::MAX as u32 {
            return None;
        }

        let log2 = log2 as u8;

        if log2 >= BASE_ORDER as u8 {
            Some(log2 - BASE_ORDER as u8)
        } else {
            Some(0)
        }
    }

    fn order_u32(val: usize) -> u32 {
        if val == 0 {
            return 0;
        }

        let log2 = log2(val);

        if log2 >= BASE_ORDER as u32 {
            log2 - BASE_ORDER as u32
        } else {
            0
        }
    }
    fn get_heap_size_by_level(level: usize) -> usize {
        if level > u32::MAX as usize {
            panic!("The level cannot greater than u32::MAX");
        }
        2usize.pow(level as u32 + BASE_ORDER as u32 - 1)
    }
}

/// # Unsafety
///
/// If the underlying allocator is not thread-safe, creating or destroying
/// multiple allocators at the same time can result in race condition or crash
impl<const BASE_ORDER: u8, const MAX_ORDER: u8> Drop for BuddySystem<BASE_ORDER, MAX_ORDER> {
    /// invoking underlying allocator to recycle the memory
    fn drop(&mut self) {
        let tree_size = next_page_size(Self::tree_size());
        debug_assert_eq!(tree_size % PAGE_SIZE, 0);
        let tree_layout = Layout::from_size_align(tree_size, 8).expect("cannot create tree layout");
        let heap_layout =
            Layout::from_size_align(Self::heap_size(), 8).expect("cannot create heap layout");

        unsafe {
            self.allocator
                .lock()
                .dealloc(self.tree_start as *mut u8, tree_layout)
        };
        unsafe {
            self.allocator
                .lock()
                .dealloc(self.heap_start as *mut u8, heap_layout)
        };
    }
}

impl<const BASE_ORDER: u8, const MAX_ORDER: u8> Default for BuddySystem<BASE_ORDER, MAX_ORDER> {
    fn default() -> Self {
        BuddySystem::new()
    }
}

unsafe impl<const BASE_ORDER: u8, const MAX_ORDER: u8> GlobalAlloc
    for BuddySystem<BASE_ORDER, MAX_ORDER>
{
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        // TODO: Currently when we want to allocate objects <= one page, we need to
        // align it to one page, which can result in memory waste

        // debug_assert!(layout.size() >= PAGE_SIZE, "layout.size() cannot < PAGE_SIZE");
        // debug_assert_eq!(
        //     layout.size() % PAGE_SIZE,
        //     0,
        //     "We need to guarantee the size is a multiple of page size"
        // );

        // These are the interesting cases:
        // * exactly isize::MAX should never trigger an error
        // * > isize::MAX should always fail
        //    * On 16/32-bit should CapacityOverflow
        //    * On 64-bit should OOM
        if layout.size() > isize::MAX as usize {
            return core::ptr::null_mut::<u8>();
        }

        // round the size up to the next page
        let layout = Layout::from_size_align(next_page_size(layout.size()), layout.align())
            .expect("crate layout error");

        // When some data structures tries to reserve a supper big size,
        // we need to return immediately.
        //
        // Pitfalls:
        //
        // We the order is 51, we should abort (isize::MAX).
        // However, the overflow check cannot detect it.
        // Thus, we need to handle this case independently
        let order = if let Some(o) = Self::order_checked(layout.size()) {
            o
        } else {
            return core::ptr::null_mut::<u8>();
        };

        let ptr = self
            .tree
            .lock()
            .try_borrow_mut()
            .expect("cannot borrow")
            .allocate(order);

        if ptr.is_none() {
            return core::ptr::null_mut::<u8>();
        }

        assert!(
            layout.size() < usize::MAX,
            "[BuddySytem]: usize::MAX (0x{:x}) should trigger OOM, layout.size(): 0x{:x}",
            usize::MAX,
            layout.size()
        );

        (ptr.expect("cannot allocate") as usize + self.heap_start) as *mut u8
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        // debug_assert!(layout.size() >= PAGE_SIZE, "layout.size() cannot < PAGE_SIZE");
        // debug_assert_eq!(
        //     layout.size() % PAGE_SIZE,
        //     0,
        //     "We need to guarantee the size is a multiple of page size"
        // );

        if ptr.is_null() || layout.size() > isize::MAX as usize {
            return;
        }

        // round the size up to the next page
        let layout = Layout::from_size_align(next_page_size(layout.size()), layout.align())
            .expect("crate layout error");

        assert!(
            ptr as usize >= self.heap_start
                && (ptr as usize) < (self.heap_start + Self::heap_size()),
            "[Buddy] Heap object {:?} pointer not in heap [0x{:x}, 0x{:x})!",
            ptr,
            self.heap_start,
            (self.heap_start + Self::heap_size())
        );

        // When some data structures tries to reserve a supper big size,
        // we need to return immediately.
        //
        // Pitfalls: see `alloc` function
        let order = if let Some(o) = Self::order_checked(layout.size()) {
            o
        } else {
            panic!("Unreachable branch");
        };

        let ptr_offset = ptr as usize - self.heap_start;
        self.tree
            .lock()
            .try_borrow_mut()
            .expect("cannot borrow")
            .deallocate(ptr_offset as *mut _, order);
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        // avoid redundant realloction because currently the minimum size is PAGE_SIZE
        if next_page_size(layout.size()) == next_page_size(new_size) {
            return ptr;
        }

        let new_layout = Layout::from_size_align_unchecked(new_size, layout.align());
        // SAFETY: the caller must ensure that `new_layout` is greater than zero.
        let new_ptr = self.alloc(new_layout);
        if !new_ptr.is_null() {
            // SAFETY: the previously allocated block cannot overlap the newly allocated block.
            // The safety contract for `dealloc` must be upheld by the caller.
            ptr::copy_nonoverlapping(ptr, new_ptr, cmp::min(layout.size(), new_size));
            self.dealloc(ptr, layout);
        }
        new_ptr
    }
}

unsafe impl<const BASE_ORDER: u8, const MAX_ORDER: u8> Allocator
    for BuddySystem<BASE_ORDER, MAX_ORDER>
{
    fn allocate(&self, layout: Layout) -> Result<NonNull<[u8]>, AllocError> {
        unsafe {
            let ptr = self.alloc(layout);
            Ok(NonNull::new(slice::from_raw_parts_mut(ptr, layout.size()))
                .expect("buddy cannot allocate"))
        }
    }

    unsafe fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
        self.dealloc(ptr.as_ptr(), layout);
    }

    unsafe fn grow(
        &self,
        ptr: NonNull<u8>,
        old_layout: Layout,
        new_layout: Layout,
    ) -> Result<NonNull<[u8]>, AllocError> {
        self.grow_impl(ptr, old_layout, new_layout, false)
    }
}

#[cfg(all(target_arch = "x86_64", target_os = "linux"))]
#[dynamic]
static BUDDY_HEAP: BuddySystem<12u8, 22u8> = BuddySystem::<12, 22>::new();

#[cfg(all(target_arch = "x86_64", target_os = "windows"))]
#[dynamic]
static BUDDY_HEAP: BuddySystem<12u8, 22u8> = BuddySystem::<12, 22>::new();

#[cfg(all(target_arch = "aarch64", target_os = "macos"))]
#[dynamic]
static BUDDY_HEAP: BuddySystem<14u8, 22u8> = BuddySystem::<14, 22>::new();

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
        BUDDY_HEAP.alloc(layout)
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        BUDDY_HEAP.dealloc(ptr, layout)
    }
}

/// replicate the alloc crate's API because we want to use them in hashbrown
///
/// # Safety
///
/// The underling function is unsafe
#[inline]
pub unsafe fn alloc(layout: Layout) -> *mut u8 {
    BUDDY_HEAP.alloc(layout)
}

/// # Safety
///
/// The underling function is unsafe
#[inline]
pub unsafe fn dealloc(ptr: *mut u8, layout: Layout) {
    BUDDY_HEAP.dealloc(ptr, layout)
}

/// # Safety
///
/// The underling function is unsafe
#[inline]
pub unsafe fn realloc(ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
    BUDDY_HEAP.realloc(ptr, layout, new_size)
}

/// # Safety
///
/// The underling function is unsafe
#[inline]
pub unsafe fn alloc_zeroed(layout: Layout) -> *mut u8 {
    let ptr = alloc(layout);
    ptr::write_bytes(ptr, 0, next_page_size(layout.size()));
    ptr
}

unsafe impl Allocator for BuddySystemAllocator {
    /// Follow the implementation in
    /// https://github.com/rust-lang/rust/blob/master/library/alloc/src/alloc.rs#L161
    fn allocate(&self, layout: Layout) -> Result<NonNull<[u8]>, AllocError> {
        match layout.size() {
            0 => Ok(NonNull::slice_from_raw_parts(layout.dangling(), 0)),
            // SAFETY: `layout` is non-zero in size,
            size => unsafe {
                let layout = Layout::from_size_align(next_page_size(layout.size()), layout.align())
                    .expect("cannot create layout");
                let raw_ptr = alloc(layout);
                let ptr = NonNull::new(raw_ptr).ok_or(AllocError)?;
                Ok(NonNull::slice_from_raw_parts(ptr, size))
            },
        }
    }

    unsafe fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
        if layout.size() != 0 {
            let layout = Layout::from_size_align(next_page_size(layout.size()), layout.align())
                .expect("cannot create layout");
            BUDDY_HEAP.dealloc(ptr.as_ptr(), layout);
        }
    }

    unsafe fn grow(
        &self,
        ptr: NonNull<u8>,
        old_layout: Layout,
        new_layout: Layout,
    ) -> Result<NonNull<[u8]>, AllocError> {
        BUDDY_HEAP.grow_impl(ptr, old_layout, new_layout, false)
    }
}

fn log2(n: usize) -> u32 {
    let n = n.next_power_of_two();
    (mem::size_of::<usize>() * 8) as u32 - n.leading_zeros() - 1
}

fn next_page_size(heap_size: usize) -> usize {
    if heap_size % PAGE_SIZE == 0 {
        heap_size
    } else {
        // optimize the alignment using the fact that size is power of two
        (heap_size + PAGE_SIZE - 1) & !(PAGE_SIZE - 1)
    }
}

#[cfg(test)]
mod test {
    use super::super::buddy::*;
    use super::*;
    const TEST_BLOCKS: usize = blocks_in_tree(19);
    extern crate std;
    use std::collections::BTreeSet;

    #[cfg(all(target_arch = "x86_64"))]
    #[dynamic]
    static lzbs: BuddySystem<12, 22> = BuddySystem::<12, 22>::new();
    #[cfg(all(target_arch = "x86_64"))]
    type Buddy = BuddySystem<12, 22>;

    #[cfg(all(target_arch = "aarch64", target_os = "macos"))]
    #[dynamic]
    static lzbs: BuddySystem<14, 20> = BuddySystem::<14, 20>::new();
    #[cfg(all(target_arch = "aarch64", target_os = "macos"))]
    type Buddy = BuddySystem<14, 20>;

    #[test]
    fn test_lazy_buddy_system() {
        let layout = Layout::from_size_align(2usize.pow(12), 8).expect("It does not work");
        for _ in 0..10 {
            (*lzbs)
                .allocate(layout)
                .expect("cannot allocate from buddy");
        }
    }

    #[cfg(all(target_arch = "x86_64", target_os = "linux"))]
    #[test]
    fn linux_utility_test() {
        // order
        assert_eq!(Buddy::order(0x100), 0);
        assert_eq!(Buddy::order(0x1000), 0);
        assert_eq!(Buddy::order(0x2000), 1);
        assert_eq!(Buddy::order(0x3000), 2);
        assert_eq!(Buddy::order(0x4000), 2);
        assert_eq!(Buddy::order(0x40000000), 18);
        assert_eq!(Buddy::order(2usize.pow(31)), 19);

        assert_eq!(next_page_size(0x900), 0x1000);
        assert_eq!(next_page_size(5000), 8192);
        assert_eq!(next_page_size(0x2000), 0x2000);
        // #[cfg(all(target_arch = "aarch64", target_os = "macos"))]

        // calculate size by levels
        assert_eq!(2usize.pow(30), Buddy::get_heap_size_by_level(19));

        // heap size
        assert_eq!(Buddy::heap_size(), 2usize.pow(Buddy::MAX_ORDER_SIZE as u32));
    }

    #[test]
    fn tree_size() {
        assert_eq!(mem::size_of::<[Block; TEST_BLOCKS]>(), 0x7ffff);
        assert_eq!(Buddy::tree_size(), 2usize.pow(LEVEL_COUNT as u32) - 1);
        // we can directly calculate the size of `tree` according to the level
        assert_eq!(
            mem::size_of::<[Block; blocks_in_tree(30)]>(),
            2usize.pow(30) - 1
        );
    }

    #[cfg(all(target_arch = "x86_64", target_os = "linux"))]
    #[test]
    fn it_works() {
        let HEAP: SystemAllocator = SystemAllocator::default();

        // 1. tree meta data
        let layout = Layout::from_size_align(next_page_size(Buddy::tree_size()), 8)
            .expect("It does not work");
        let tree_start = unsafe { HEAP.alloc(layout) as usize };

        // 2. heap
        let layout = Layout::from_size_align(Buddy::heap_size(), 8).expect("It does not work");
        let heap_start = unsafe { HEAP.alloc(layout) as usize };

        // 3. buddy system
        let layout = Layout::from_size_align(2usize.pow(12), 8).expect("It does not work");
        let bs = Buddy::new_with_start(HEAP, heap_start, tree_start);

        // allocation
        let ptr0 = unsafe { bs.alloc(layout) };
        assert_eq!(
            ptr0 as usize, heap_start,
            "{:x} vs {:x}",
            ptr0 as usize, heap_start
        );

        // deallocation
        unsafe { bs.dealloc(ptr0, layout) };
    }

    #[cfg(all(target_arch = "x86_64", target_os = "linux"))]
    #[test]
    fn allocation() {
        let HEAP: SystemAllocator = SystemAllocator::default();

        // 1. tree meta data
        let tree_size = next_page_size(Buddy::tree_size());
        let layout = Layout::from_size_align(tree_size, 8).expect("It does not work");
        debug_assert_eq!(tree_size % PAGE_SIZE, 0);
        let tree_start = unsafe { HEAP.alloc(layout) as usize };
        debug_assert_ne!(tree_start, 0);

        // 2. heap
        let layout = Layout::from_size_align(Buddy::heap_size(), 8).expect("It does not work");
        debug_assert_eq!(layout.size(), Buddy::heap_size());
        debug_assert_eq!(layout.size() % PAGE_SIZE, 0);
        let heap_start = unsafe { HEAP.alloc(layout) as usize };
        debug_assert_ne!(heap_start, 0);

        // 3. buddy system
        let bs = Buddy::new_with_start(HEAP, heap_start, tree_start);

        // allocate 0x2000
        let layout = Layout::from_size_align(2usize.pow(13), 8).expect("It does not work");
        let ptr0 = unsafe { bs.alloc(layout) } as usize - heap_start;
        let ptr1 = unsafe { bs.alloc(layout) } as usize - heap_start;
        let ptr2 = unsafe { bs.alloc(layout) } as usize - heap_start;
        let ptr3 = unsafe { bs.alloc(layout) } as usize - heap_start;

        assert_eq!(ptr0, 0, "ptr0: 0x{:x}", ptr0);
        assert_eq!(ptr1, 0x2000, "ptr1: 0x{:x}", ptr1);
        assert_eq!(ptr2, 0x4000, "ptr2: 0x{:x}", ptr2);
        assert_eq!(ptr3, 0x6000, "ptr3: 0x{:x}", ptr3);

        unsafe { bs.dealloc((ptr2 + heap_start) as *mut u8, layout) };
        unsafe { bs.dealloc((ptr3 + heap_start) as *mut u8, layout) };

        let layout = Layout::from_size_align(2usize.pow(14), 8).expect("It does not work");
        let ptr4 = unsafe { bs.alloc(layout) } as usize - heap_start;
        assert_eq!(ptr4, 0x4000, "ptr4: 0x{:x}", ptr4);
    }

    #[cfg(all(target_arch = "x86_64", target_os = "linux"))]
    #[test]
    fn oom_allocation() {
        let HEAP: SystemAllocator = SystemAllocator::default();

        // 1. tree meta data
        let tree_size = next_page_size(Buddy::tree_size());
        let layout = Layout::from_size_align(tree_size, 8).expect("It does not work");
        let tree_start = unsafe { HEAP.alloc(layout) as usize };

        // 2. heap
        let layout = Layout::from_size_align(Buddy::heap_size(), 8).expect("It does not work");
        let heap_start = unsafe { HEAP.alloc(layout) as usize };

        // 3. buddy system
        let bs = Buddy::new_with_start(HEAP, heap_start, tree_start);

        // 4. oom allocation
        let layout = Layout::from_size_align(Buddy::heap_size().checked_mul(2).expect(""), 8)
            .expect("It does not work");
        let ptr0 = unsafe { bs.alloc(layout) } as usize;

        assert_eq!(ptr0, 0);

        // 5. max allocation
        let layout = Layout::from_size_align(Buddy::heap_size(), 8).expect("It does not work");
        let ptr0 = unsafe { bs.alloc(layout) } as usize;
        let ptr_before = ptr0;
        assert_ne!(ptr0, 0);

        // 6. deallocation
        unsafe { bs.dealloc(ptr0 as *mut u8, layout) };
        let layout = Layout::from_size_align(Buddy::heap_size(), 8).expect("It does not work");
        let ptr0 = unsafe { bs.alloc(layout) } as usize;
        assert_ne!(ptr0, 0);
        assert_eq!(ptr0, ptr_before);
    }

    #[cfg(all(target_arch = "x86_64", target_os = "linux"))]
    #[test]
    fn memory_exhaustion_test() {
        let HEAP: SystemAllocator = SystemAllocator::default();

        // 1. tree meta data
        let layout = Layout::from_size_align(1 << (LEVEL_COUNT + 1), 8).expect("It does not work");
        let tree_start = unsafe { HEAP.alloc(layout) as usize };

        // 2. heap
        let layout = Layout::from_size_align(Buddy::heap_size(), 8).expect("It does not work");
        let heap_start = unsafe { HEAP.alloc(layout) as usize };

        // 3. buddy system
        let bs = Buddy::new_with_start(HEAP, heap_start, tree_start);

        let max_blocks = blocks_in_level(Buddy::MAX as u8);
        let mut seen = BTreeSet::new();
        let layout = Layout::from_size_align(2usize.pow(12), 8).expect("It does not work");

        // Note: this test can take a very long time because the number of iteration
        // grows linearly
        for i in 0..max_blocks {
            let addr = unsafe { bs.alloc(layout) } as usize;

            if addr == 0 {
                panic!(
                    "0x{:x}th block cannot be allocated, MAX: 0x{:x}",
                    i, BLOCKS_IN_TREE
                );
            } else {
                // println!("addr: 0x{:x}", addr);
            }

            if seen.contains(&addr) {
                panic!("Allocator (addr: 0x{:x})", addr);
            } else {
                seen.insert(addr);
            }
        }
    }
    #[cfg(all(target_arch = "x86_64", target_os = "linux"))]
    #[test]
    fn test_initializer() {
        let bs = Buddy::new();
        let layout = Layout::from_size_align(2usize.pow(12), 8).expect("It does not work");
        let ptr = bs
            .allocate(layout)
            .expect("cannot allocate from buddy")
            .as_mut_ptr() as usize;
        assert_ne!(ptr, 0);
    }
    #[cfg(all(target_arch = "x86_64", target_os = "linux"))]
    #[test]
    fn test_buddy_zst() {
        let bsa: BuddySystemAllocator = BuddySystemAllocator;
        let layout = Layout::from_size_align(2usize.pow(12), 8).expect("It does not work");
        let ptr = bsa.allocate(layout).expect("cannot allocate from buddy");
        let addr = ptr.as_mut_ptr() as usize;
        assert_ne!(addr, 0, "ALlocation failed: (return value: 0x{:x})", addr);
        unsafe { bsa.deallocate(ptr.as_non_null_ptr(), layout) };
    }
}
