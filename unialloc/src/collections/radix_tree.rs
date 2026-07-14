#[cfg(feature = "fixed_heap")]
use crate::freelist::BUMP;
use crate::mm::BackendAllocator as GlobalBackend;
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sys_alloc as system_alloc;
use crate::sc::META_BUMP;
#[cfg(not(feature = "fixed_heap"))]
use crate::sync::PthreadMutex as RadixTreeMutex;
use alloc::boxed::Box;
use core::alloc::{AllocError, Allocator, GlobalAlloc, Layout};
use core::ffi::c_void;
use core::option::Option::Some;
use core::ptr::{null_mut, NonNull};
use core::slice;
use core::sync::atomic::{AtomicPtr, Ordering};
#[cfg(feature = "fixed_heap")]
use spin::Mutex as RadixTreeMutex;

include!(concat!(env!("OUT_DIR"), "/consts.rs"));
#[cfg(not(feature = "fixed_heap"))]
pub type RadixTree = RadixNodeHead<RadixBottomNode>;
#[cfg(feature = "fixed_heap")]
pub type RadixTree = ArrayNode;

const RADIX_PAGE_QUANTUM: usize = 1 << 12;
const RADIX_CHILD_SLOTS: usize = 1 << 18;
const RADIX_KEY_STRIDE: usize = RADIX_PAGE_QUANTUM << 16;

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

#[inline]
fn radix_head_index(k: usize) -> usize {
    let mask = !((1_usize << 46) - 1);
    (mask & k) >> 46
}

#[inline]
fn radix_child_offset(k: usize) -> usize {
    (k >> 28) & (RADIX_CHILD_SLOTS - 1)
}

#[inline]
fn radix_child_slots_remaining(k: usize) -> usize {
    RADIX_CHILD_SLOTS - radix_child_offset(k)
}

#[inline]
fn advance_radix_key(k: usize, slots: usize) -> Result<usize, &'static str> {
    let delta = slots
        .checked_mul(RADIX_KEY_STRIDE)
        .ok_or("radix tree key delta overflow")?;
    k.checked_add(delta).ok_or("radix tree key overflow")
}

fn validate_radix_chunks(k: usize, n: usize) -> Result<(), &'static str> {
    if n == 0 {
        return Err("radix tree range must be nonzero");
    }
    let mut current_key = k;
    let mut remaining = n;
    while remaining != 0 {
        let chunk = core::cmp::min(remaining, radix_child_slots_remaining(current_key));
        remaining -= chunk;
        if remaining != 0 {
            current_key = advance_radix_key(current_key, chunk)?;
        }
    }
    Ok(())
}

#[inline]
fn checked_bottom_range(k: usize, n: usize) -> Result<(usize, usize), &'static str> {
    if n == 0 {
        return Err("radix tree range must be nonzero");
    }
    let idx = radix_head_index(k);
    let end = idx
        .checked_add(n)
        .ok_or("radix tree bottom range overflow")?;
    if end > RADIX_CHILD_SLOTS {
        return Err("radix tree bottom range exceeds node");
    }
    Ok((idx, end))
}

pub fn allocate_node<V: TreeNode>() -> *mut V {
    #[cfg(not(feature = "fixed_heap"))]
    {
        let prot = system_alloc::prots::get_prot(true, true, false);
        let ptr = unsafe {
            #[cfg(feature = "hugepage")]
            {
                system_alloc::mmap_huge(core::mem::size_of::<V>(), prot) as *mut u8
            }
            #[cfg(not(feature = "hugepage"))]
            {
                system_alloc::mmap(core::mem::size_of::<V>(), prot) as *mut u8
            }
        };
        system_alloc::normalize_mmap_result(ptr) as *mut V
    }
    #[cfg(feature = "fixed_heap")]
    {
        #[cfg(test)]
        crate::sc::ensure_fixed_heap_test_initialized();

        let layout = Layout::new::<V>();
        match META_BUMP
            .lock()
            .alloc_aligned(layout.size(), layout.align())
        {
            Ok(ptr) => ptr as *mut V,
            Err(_) => null_mut(),
        }
    }
}
pub fn deallocate_node<V: TreeNode>(ptr: usize) {
    #[cfg(not(feature = "fixed_heap"))]
    unsafe {
        system_alloc::munmap(ptr as *mut u8, core::mem::size_of::<V>())
    }
    #[cfg(feature = "fixed_heap")]
    let _ = ptr;
}

#[cfg(feature = "fixed_heap")]
fn checked_align_up(value: usize, align: usize) -> Option<usize> {
    if align == 0 || !align.is_power_of_two() {
        return None;
    }
    value.checked_add(align - 1).map(|v| v & !(align - 1))
}

impl<V> TreeNode for RadixNodeHead<V>
where
    V: TreeNode,
{
    fn insert(&mut self, k: usize, v: i64, n: usize) -> Result<(), &'static str> {
        validate_radix_chunks(k, n)?;
        let mut current_key = k;
        let mut remaining = n;
        while remaining != 0 {
            let idx = radix_head_index(current_key);
            let chunk = core::cmp::min(remaining, radix_child_slots_remaining(current_key));
            self.insert_child_range(idx, current_key, v, chunk)?;
            remaining -= chunk;
            if remaining != 0 {
                current_key = advance_radix_key(current_key, chunk)?;
            }
        }
        Ok(())
    }

    fn remove(&mut self, k: usize, n: usize) -> Result<(), &'static str> {
        validate_radix_chunks(k, n)?;
        let mut current_key = k;
        let mut remaining = n;
        while remaining != 0 {
            let idx = radix_head_index(current_key);
            let chunk = core::cmp::min(remaining, radix_child_slots_remaining(current_key));
            self.remove_child_range(idx, current_key, chunk)?;
            remaining -= chunk;
            if remaining != 0 {
                current_key = advance_radix_key(current_key, chunk)?;
            }
        }
        Ok(())
    }

    fn get_mut(&mut self, k: usize) -> i64 {
        let idx = radix_head_index(k);
        let node_ptr_ref: &mut AtomicPtr<V> = &mut self.nodes[idx];
        let node_ptr = node_ptr_ref.load(Ordering::Relaxed);
        if node_ptr.is_null() {
            0
        } else {
            let node: &mut V = match unsafe { node_ptr.as_mut() } {
                Some(node) => node,
                None => return 0,
            };
            node.get_mut(k << 18)
        }
    }
}

impl<V> RadixNodeHead<V>
where
    V: TreeNode,
{
    fn insert_child_range(
        &mut self,
        idx: usize,
        k: usize,
        v: i64,
        n: usize,
    ) -> Result<(), &'static str> {
        let node_ptr_ref: &mut AtomicPtr<V> = &mut self.nodes[idx];
        let mut node_ptr = node_ptr_ref.load(Ordering::Relaxed);

        if node_ptr.is_null() {
            node_ptr = allocate_node::<V>();
            if node_ptr.is_null() {
                return Err("radix tree node allocation failed");
            }
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
        let node: &mut V = match unsafe { node_ptr.as_mut() } {
            Some(node) => node,
            None => return Err("radix tree child pointer is null"),
        };
        node.insert(k << 18, v, n)
    }

    fn remove_child_range(&mut self, idx: usize, k: usize, n: usize) -> Result<(), &'static str> {
        let node_ptr_ref: &mut AtomicPtr<V> = &mut self.nodes[idx];
        let node_ptr = node_ptr_ref.load(Ordering::Relaxed);
        if node_ptr.is_null() {
            Ok(())
        } else {
            let node: &mut V = match unsafe { node_ptr.as_mut() } {
                Some(node) => node,
                None => return Err("radix tree child pointer is null"),
            };
            node.remove(k << 18, n)
        }
    }
}

pub struct RadixBottomNode {
    nodes: [i64; 1 << 18],
}

impl TreeNode for RadixBottomNode {
    fn insert(&mut self, k: usize, v: i64, n: usize) -> Result<(), &'static str> {
        let (idx, end) = checked_bottom_range(k, n)?;
        for slot in &mut self.nodes[idx..end] {
            *slot = v;
        }
        Ok(())
    }

    fn remove(&mut self, k: usize, n: usize) -> Result<(), &'static str> {
        let (idx, end) = checked_bottom_range(k, n)?;
        for slot in &mut self.nodes[idx..end] {
            *slot = 0;
        }
        Ok(())
    }

    fn get_mut(&mut self, k: usize) -> i64 {
        let idx = radix_head_index(k);
        self.nodes.get(idx).copied().unwrap_or(0)
    }
}
#[cfg(not(feature = "fixed_heap"))]
static RDPTR: AtomicPtr<RadixTree> = AtomicPtr::new(null_mut());
static RD_TREE_LOCK: RadixTreeMutex<()> = RadixTreeMutex::new(());

#[cfg(not(feature = "fixed_heap"))]
fn try_get_rd_tree_unlocked() -> Result<&'static mut RadixTree, AllocError> {
    let mut ptr = RDPTR.load(Ordering::Relaxed);
    if ptr.is_null() {
        ptr = allocate_node::<RadixTree>();
        if ptr.is_null() {
            return Err(AllocError);
        }
        let res = RDPTR.compare_exchange(null_mut(), ptr, Ordering::AcqRel, Ordering::Acquire);
        if let Err(eptr) = res {
            deallocate_node::<RadixTree>(ptr as usize);
            ptr = eptr;
        }
    }
    match unsafe { ptr.as_mut() } {
        Some(tree) => Ok(tree),
        None => Err(AllocError),
    }
}
#[cfg(feature = "fixed_heap")]
pub static mut RDTREE: RadixTree = RadixTree::new();
#[cfg(feature = "fixed_heap")]
fn try_get_rd_tree_unlocked() -> Result<&'static mut RadixTree, AllocError> {
    // `RDTREE` is a single fixed-heap metadata root.  All public access goes
    // through `try_with_rd_tree`, which holds `RD_TREE_LOCK`; use `addr_of_mut!`
    // first so Rust 2024 does not create an implicit reference to a
    // `static mut` before that aliasing boundary is established.
    #[cfg(unialloc_addr_of_mut_static_mut_is_safe)]
    let ptr = core::ptr::addr_of_mut!(RDTREE);
    #[cfg(not(unialloc_addr_of_mut_static_mut_is_safe))]
    let ptr = unsafe { core::ptr::addr_of_mut!(RDTREE) };
    match unsafe { ptr.as_mut() } {
        Some(tree) => Ok(tree),
        None => Err(AllocError),
    }
}

pub fn try_with_rd_tree<R>(f: impl FnOnce(&mut RadixTree) -> R) -> Result<R, AllocError> {
    #[cfg(all(
        feature = "adaptive_bitmap_page_allocator",
        not(feature = "fixed_heap")
    ))]
    let _guard = match RD_TREE_LOCK.try_lock() {
        Some(guard) => guard,
        None => {
            crate::adaptive_bitmap_alloc::note_freelist_contention();
            RD_TREE_LOCK.lock()
        }
    };
    #[cfg(not(all(
        feature = "adaptive_bitmap_page_allocator",
        not(feature = "fixed_heap")
    )))]
    let _guard = RD_TREE_LOCK.lock();
    Ok(f(try_get_rd_tree_unlocked()?))
}

pub fn with_rd_tree<R>(f: impl FnOnce(&mut RadixTree) -> R) -> R {
    try_with_rd_tree(f).ok().expect("radix tree init failed")
}

pub struct ArrayNode {
    /// Base in the normalized radix-key space used by `align_12k`.
    ///
    /// The fixed-heap backend uses an array instead of the mmap-backed radix
    /// tree.  Keys passed into this tree are already normalized to 4 KiB
    /// granularity (`align_12k(addr) << 16`), even when the platform page size
    /// is larger (for example 16 KiB on macOS/aarch64).  Keeping this base in
    /// that same key space avoids underflowing or aliasing page metadata on
    /// larger-page platforms.
    base: usize,
    /// Raw heap-range start used for checked extension sizing.
    range_start: usize,
    /// Number of OS pages covered by `nodes`.
    page_count: usize,
    nodes: *mut i64,
}

impl ArrayNode {
    pub const fn new() -> Self {
        Self {
            base: 0,
            range_start: 0,
            page_count: 0,
            nodes: null_mut(),
        }
    }

    pub fn init_with_range(&mut self, start: usize, array: *mut i64, page_count: usize) {
        self.base = crate::sc::align_12k(start);
        self.range_start = start;
        self.page_count = page_count;
        self.nodes = array;
        if !array.is_null() && page_count != 0 {
            unsafe { core::ptr::write_bytes(array, 0, page_count) };
        }
    }

    #[cfg(feature = "fixed_heap")]
    fn initialized_shape(&self) -> Option<(usize, usize)> {
        if self.range_start == 0 || self.page_count == 0 || self.nodes.is_null() {
            return None;
        }
        Some((self.range_start, self.page_count))
    }

    fn checked_array_layout(
        &self,
        end: usize,
        page_size: usize,
    ) -> Result<(usize, Layout), AllocError> {
        if page_size == 0 || !page_size.is_power_of_two() || end < self.range_start {
            return Err(AllocError);
        }
        let span = end.checked_sub(self.range_start).ok_or(AllocError)?;
        if span == 0 || span % page_size != 0 {
            return Err(AllocError);
        }
        let page_count = span.checked_div(page_size).ok_or(AllocError)?;
        if page_count == 0 {
            return Err(AllocError);
        }
        let bytes = page_count
            .checked_mul(core::mem::size_of::<i64>())
            .ok_or(AllocError)?;
        let layout =
            Layout::from_size_align(bytes, core::mem::align_of::<i64>()).map_err(|_| AllocError)?;
        Ok((page_count, layout))
    }

    pub unsafe fn extend_with_range(
        &mut self,
        size: usize,
        new_end: usize,
        page_size: usize,
    ) -> Result<(usize, usize), AllocError> {
        let previous_end = new_end.checked_sub(size).ok_or(AllocError)?;
        let (prev_count, _) = self.checked_array_layout(previous_end, page_size)?;
        let (new_count, new_layout) = self.checked_array_layout(new_end, page_size)?;
        if self.nodes.is_null() || prev_count != self.page_count || new_count < prev_count {
            return Err(AllocError);
        }
        let prev = self.nodes;
        let array = {
            #[cfg(feature = "fixed_heap")]
            {
                // `extend_with_range` is called while `RD_TREE_LOCK` is held.
                // Do not route this allocation through `GlobalBackend`: the
                // backend may consult the radix tree/free list and would try to
                // re-enter the same lock.  Carve the replacement radix backing
                // directly from the fixed-heap page-run bump instead.
                //
                // Commit the page-run bump's larger checkpoint only together
                // with this metadata allocation.  If shape/layout validation
                // above fails, or the allocation cannot fit, the fixed-heap
                // object range is not published without matching radix
                // metadata.
                match crate::freelist::BUMP
                    .lock()
                    .alloc_with_extended_checkpoint(new_layout.size(), new_end)
                {
                    Ok(ptr) => ptr as *mut i64,
                    Err(_) => null_mut(),
                }
            }
            #[cfg(not(feature = "fixed_heap"))]
            {
                GlobalBackend.alloc(new_layout) as *mut i64
            }
        };
        if array.is_null() {
            return Err(AllocError);
        }
        core::ptr::copy(prev, array, prev_count);
        core::ptr::write_bytes(array.add(prev_count), 0, new_count - prev_count);
        self.nodes = array;
        self.page_count = new_count;
        Ok((prev as usize, prev_count))
    }

    fn checked_page_range(&self, k: usize, n: usize) -> Result<(usize, usize), &'static str> {
        if self.nodes.is_null() {
            return Err("fixed-heap radix array is not initialized");
        }
        if n == 0 {
            return Err("fixed-heap radix range must be nonzero");
        }

        let key_page = k >> 16;
        let offset = key_page
            .checked_sub(self.base)
            .ok_or("fixed-heap radix key precedes heap base")?;
        if offset % RADIX_PAGE_QUANTUM != 0 {
            return Err("fixed-heap radix key is not page-normalized");
        }
        let start = offset / RADIX_PAGE_QUANTUM;
        let end = start
            .checked_add(n)
            .ok_or("fixed-heap radix range overflow")?;
        if end > self.page_count {
            return Err("fixed-heap radix range exceeds initialized heap");
        }
        Ok((start, end))
    }

    fn nodes_slice(&mut self) -> &mut [i64] {
        unsafe { core::slice::from_raw_parts_mut(self.nodes, self.page_count) }
    }
}

/// Allocate a private fixed-heap radix map with the same address shape as the
/// global fixed-heap map.
///
/// The hosted radix tree lazily mmaps child nodes, so `allocate_node::<RadixTree>`
/// is enough there.  In `fixed_heap`, `RadixTree` is an `ArrayNode`: it must know
/// the heap start and must own a backing `i64` array before the bitmap slab
/// backend can insert page-to-slab mappings.  Keep this constructor in the
/// radix-tree module so callers do not depend on `ArrayNode`'s fields.
///
/// The initial fixed-heap metadata bump is sized for global roots only.  Private
/// maps are cold, opt-in `separate_sc_backend` metadata, so they are carved from
/// the fixed page-run bump as whole-page blocks, matching the existing radix
/// backing extension path instead of growing permanent hot allocator roots.
#[cfg(feature = "fixed_heap")]
pub fn allocate_fixed_heap_radix_tree_from_global_shape() -> *mut RadixTree {
    #[cfg(test)]
    crate::sc::ensure_fixed_heap_test_initialized();

    let (range_start, page_count) = match try_with_rd_tree(|rd| rd.initialized_shape()) {
        Ok(Some(shape)) => shape,
        _ => return null_mut(),
    };

    let backing_bytes = match page_count.checked_mul(core::mem::size_of::<i64>()) {
        Some(bytes) => bytes,
        None => return null_mut(),
    };
    let backing_offset = match checked_align_up(
        core::mem::size_of::<RadixTree>(),
        core::mem::align_of::<i64>(),
    ) {
        Some(offset) => offset,
        None => return null_mut(),
    };
    let block_bytes = match backing_offset.checked_add(backing_bytes) {
        Some(bytes) => bytes,
        None => return null_mut(),
    };
    let block_align = core::cmp::max(
        core::mem::align_of::<RadixTree>(),
        core::mem::align_of::<i64>(),
    );

    if block_align > crate::PAGE_SIZE {
        return null_mut();
    }
    let block = match BUMP.lock().alloc(block_bytes) {
        Ok(ptr) => ptr,
        Err(_) => return null_mut(),
    };
    let tree = block as *mut RadixTree;
    let backing = unsafe { block.add(backing_offset) as *mut i64 };
    unsafe {
        core::ptr::write(tree, RadixTree::new());
        (*tree).init_with_range(range_start, backing, page_count);
    }
    tree
}

impl TreeNode for ArrayNode {
    fn insert(&mut self, k: usize, v: i64, n: usize) -> Result<(), &'static str> {
        let (start, end) = self.checked_page_range(k, n)?;
        let nodes = self.nodes_slice();
        for slot in &mut nodes[start..end] {
            *slot = v;
        }
        Ok(())
    }

    fn remove(&mut self, k: usize, n: usize) -> Result<(), &'static str> {
        let (start, end) = self.checked_page_range(k, n)?;
        let nodes = self.nodes_slice();
        for slot in &mut nodes[start..end] {
            *slot = 0;
        }
        Ok(())
    }

    fn get_mut(&mut self, k: usize) -> i64 {
        let (start, _) = match self.checked_page_range(k, 1) {
            Ok(range) => range,
            Err(_) => return 0,
        };
        self.nodes_slice()[start]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::cmp::Ordering;

    #[cfg(not(feature = "fixed_heap"))]
    pub type RadixTree = RadixNodeHead<RadixBottomNode>;

    #[test]
    fn array_node_extend_rejects_underflow() {
        let mut node = ArrayNode::new();
        node.init_with_range(4096, core::ptr::null_mut(), 0);
        let result = unsafe { node.extend_with_range(8192, 4096, PAGE_SIZE) };
        assert!(result.is_err());
        assert!(node.nodes.is_null());
    }

    #[test]
    fn array_node_layout_rejects_bad_page_size_and_overflow() {
        let node = ArrayNode {
            base: 0,
            range_start: usize::MAX - PAGE_SIZE / 2,
            page_count: 0,
            nodes: core::ptr::null_mut(),
        };
        assert!(node.checked_array_layout(usize::MAX, 0).is_err());
        assert!(node.checked_array_layout(usize::MAX, 3).is_err());
        assert!(node.checked_array_layout(usize::MAX, PAGE_SIZE).is_err());
    }

    #[test]
    fn array_node_uses_normalized_radix_key_space_on_large_pages() {
        let page_count = 4;
        let mut backing = [0_i64; 4];
        let start = 0x1_0000_0000usize;
        let mut node = ArrayNode::new();
        node.init_with_range(start, backing.as_mut_ptr(), page_count);

        let second_page = crate::sc::align_12k(start + PAGE_SIZE) << 16;
        node.insert(second_page, 7, 1).expect("insert second page");

        assert_eq!(node.get_mut(crate::sc::align_12k(start) << 16), 0);
        assert_eq!(node.get_mut(second_page), 7);
        assert_eq!(
            node.get_mut(crate::sc::align_12k(start + page_count * PAGE_SIZE) << 16),
            0
        );
        assert!(node
            .insert(crate::sc::align_12k(start - PAGE_SIZE) << 16, 1, 1)
            .is_err());
    }

    #[cfg(all(unix, not(feature = "fixed_heap")))]
    #[test]
    fn radix_node_head_splits_ranges_across_child_boundary() {
        let rd = unsafe { allocate_node::<RadixTree>().as_mut().expect("radix tree") };
        let start = (RADIX_CHILD_SLOTS - 2) * RADIX_KEY_STRIDE;
        rd.insert(start, 11, 4).expect("cross-node insert");

        assert_eq!(rd.get_mut(start - RADIX_KEY_STRIDE), 0);
        assert_eq!(rd.get_mut(start), 11);
        assert_eq!(rd.get_mut(start + RADIX_KEY_STRIDE), 11);
        assert_eq!(rd.get_mut(start + 2 * RADIX_KEY_STRIDE), 11);
        assert_eq!(rd.get_mut(start + 3 * RADIX_KEY_STRIDE), 11);
        assert_eq!(rd.get_mut(start + 4 * RADIX_KEY_STRIDE), 0);

        rd.remove(start + RADIX_KEY_STRIDE, 2)
            .expect("cross-node remove");
        assert_eq!(rd.get_mut(start), 11);
        assert_eq!(rd.get_mut(start + RADIX_KEY_STRIDE), 0);
        assert_eq!(rd.get_mut(start + 2 * RADIX_KEY_STRIDE), 0);
        assert_eq!(rd.get_mut(start + 3 * RADIX_KEY_STRIDE), 11);
    }

    #[cfg(all(unix, not(feature = "fixed_heap")))]
    #[test]
    fn radix_node_head_prevalidates_overflowing_insert_without_partial_mapping() {
        let rd = unsafe { allocate_node::<RadixTree>().as_mut().expect("radix tree") };
        let key = usize::MAX - (RADIX_KEY_STRIDE / 2);

        assert_eq!(radix_child_slots_remaining(key), 1);
        assert!(rd.insert(key, 77, 2).is_err());
        assert_eq!(
            rd.get_mut(key),
            0,
            "overflowing multi-chunk insert must not leave the first chunk mapped"
        );
    }

    #[cfg(all(unix, not(feature = "fixed_heap")))]
    #[test]
    fn radix_node_head_prevalidates_overflowing_remove_without_partial_clear() {
        let rd = unsafe { allocate_node::<RadixTree>().as_mut().expect("radix tree") };
        let key = usize::MAX - (RADIX_KEY_STRIDE / 2);

        assert_eq!(radix_child_slots_remaining(key), 1);
        rd.insert(key, 77, 1).expect("single final chunk insert");
        assert!(rd.remove(key, 2).is_err());
        assert_eq!(
            rd.get_mut(key),
            77,
            "overflowing multi-chunk remove must not clear the first chunk"
        );
    }

    #[cfg(all(unix, not(feature = "fixed_heap")))]
    #[test]
    fn radix_bottom_node_rejects_cross_node_range_without_panic() {
        let bottom = unsafe {
            allocate_node::<RadixBottomNode>()
                .as_mut()
                .expect("bottom node")
        };
        let last_slot_key = (RADIX_CHILD_SLOTS - 1) << 46;
        assert!(bottom.insert(last_slot_key, 1, 2).is_err());
        assert!(bottom.remove(last_slot_key, 2).is_err());
        assert_eq!(bottom.get_mut(last_slot_key), 0);
    }

    #[test]
    fn allocated_radix_node_respects_type_alignment() {
        #[cfg(not(feature = "fixed_heap"))]
        {
            let ptr = allocate_node::<RadixBottomNode>();
            assert!(!ptr.is_null());
            assert_eq!((ptr as usize) % core::mem::align_of::<RadixBottomNode>(), 0);
            deallocate_node::<RadixBottomNode>(ptr as usize);
        }
        #[cfg(feature = "fixed_heap")]
        {
            let _fixed_heap_guard = crate::sc::fixed_heap_test_guard();
            try_with_rd_tree(|rd| {
                let ptr = rd as *mut RadixTree as usize;
                assert_ne!(ptr, 0);
                assert_eq!(ptr % core::mem::align_of::<RadixTree>(), 0);
                assert!(!rd.nodes.is_null());
                assert_ne!(rd.page_count, 0);
            })
            .expect("fixed-heap radix tree");
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn allocated_radix_bottom_node_respects_type_alignment() {
        let ptr = allocate_node::<RadixBottomNode>();
        assert!(!ptr.is_null());
        assert_eq!((ptr as usize) % core::mem::align_of::<RadixBottomNode>(), 0);
        deallocate_node::<RadixBottomNode>(ptr as usize);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn rd_simple_test() {
        let rd = unsafe {
            allocate_node::<RadixTree>()
                .as_mut()
                .expect("hosted radix tree allocation should succeed")
        };
        let mut last = PAGE_SIZE * 4094;
        match 12_u32.cmp(&PAGE_SIZE.trailing_zeros()) {
            Ordering::Greater => last <<= 12 - PAGE_SIZE.trailing_zeros(),
            Ordering::Less => last >>= PAGE_SIZE.trailing_zeros() - 12,
            Ordering::Equal => {}
        };
        rd.insert(last << 16, 4, 2).expect("insert first range");
        rd.insert((last + 2 * 4096) << 16, 3, 2)
            .expect("insert second range");
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

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn rd_simple_test() {
        let _fixed_heap_guard = crate::sc::fixed_heap_test_guard();
        try_with_rd_tree(|rd| {
            assert!(!rd.nodes.is_null());
            assert!(rd.page_count >= 4);
            let slot = core::cmp::min(rd.page_count / 2, rd.page_count - 3);
            let key_page = rd
                .base
                .checked_add(
                    slot.checked_mul(RADIX_PAGE_QUANTUM)
                        .expect("slot key offset"),
                )
                .expect("slot key page");
            let key = key_page.checked_shl(16).expect("radix key");
            let next_key = key.checked_add(RADIX_KEY_STRIDE).expect("next radix key");
            let end_key = next_key
                .checked_add(RADIX_KEY_STRIDE)
                .expect("end radix key");

            let _ = rd.remove(key, 3);
            assert_eq!(rd.get_mut(key), 0);
            rd.insert(key, 4, 2).expect("insert fixed-heap range");
            assert_eq!(rd.get_mut(key), 4);
            assert_eq!(rd.get_mut(next_key), 4);
            assert_eq!(rd.get_mut(end_key), 0);
            rd.remove(key, 2).expect("remove fixed-heap range");
            assert_eq!(rd.get_mut(key), 0);
            assert_eq!(rd.get_mut(next_key), 0);
        })
        .expect("fixed-heap radix tree");
    }
}
