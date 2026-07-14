//! Segment-tree-backed contiguous page bitmap.
//!
//! Every leaf stores one 64-page occupancy word. Internal nodes cache the
//! longest free prefix, suffix, and sub-run. Allocating and freeing a range is
//! therefore `O(log pages)`, and freeing adjacent ranges merges them as part of
//! the normal parent-summary update. A bounded leaf-word scan handles the hot
//! path; the tree supplies the fragmented and cross-word fallback.

use core::alloc::Layout;
use core::cmp::{max, min};
use core::ptr;

const NODE_MIXED: u32 = 0;
const NODE_FREE: u32 = 1;
const NODE_USED: u32 = 2;
const LEAF_PAGES: usize = u64::BITS as usize;
const FAST_LEAF_SCAN_LIMIT: usize = 8;

/// One segment-tree node. Leaves additionally store a 64-page occupancy word;
/// internal nodes use only the run summaries. This keeps metadata near
/// 0.75 bytes/page while supporting fixed heaps of up to `u32::MAX` pages.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
pub struct RunNode {
    prefix_free: u32,
    suffix_free: u32,
    max_free: u32,
    state: u32,
    leaf_bits: u64,
}

impl RunNode {
    pub const EMPTY: Self = Self {
        prefix_free: 0,
        suffix_free: 0,
        max_free: 0,
        state: NODE_USED,
        leaf_bits: u64::MAX,
    };
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RunBitmapError {
    InvalidLayout,
    CapacityExceeded,
    OutOfMemory,
    InvalidPointer,
    RangeAlreadyFree,
    Uninitialized,
}

#[derive(Clone, Copy)]
struct Summary {
    len: usize,
    prefix_free: usize,
    suffix_free: usize,
    max_free: usize,
}

impl Summary {
    #[inline]
    fn from_node(node: RunNode, len: usize) -> Self {
        Self {
            len,
            prefix_free: node.prefix_free as usize,
            suffix_free: node.suffix_free as usize,
            max_free: node.max_free as usize,
        }
    }

    #[inline]
    fn merge(left: Self, right: Self) -> Self {
        let prefix_free = if left.prefix_free == left.len {
            left.len + right.prefix_free
        } else {
            left.prefix_free
        };
        let suffix_free = if right.suffix_free == right.len {
            right.len + left.suffix_free
        } else {
            right.suffix_free
        };
        Self {
            len: left.len + right.len,
            prefix_free,
            suffix_free,
            max_free: max(
                max(left.max_free, right.max_free),
                left.suffix_free + right.prefix_free,
            ),
        }
    }
}

/// A page-run allocator over caller-owned node storage.
///
/// The owning fixed-heap backend serializes this value with a `spin::Mutex`.
/// Keeping the tree itself non-atomic makes every allocation a short, bounded
/// critical section and avoids one atomic operation per tree level.
pub struct SegmentPageAllocator {
    nodes: *mut RunNode,
    nodes_len: usize,
    leaf_capacity: usize,
    page_count: usize,
    base: usize,
    page_size: usize,
    allocated_pages: usize,
    search_word: usize,
}

// The raw node pointer is exclusively accessed through `&mut self`; the global
// instance is protected by a spin mutex.
unsafe impl Send for SegmentPageAllocator {}

impl SegmentPageAllocator {
    pub const fn empty() -> Self {
        Self {
            nodes: ptr::null_mut(),
            nodes_len: 0,
            leaf_capacity: 0,
            page_count: 0,
            base: 0,
            page_size: 0,
            allocated_pages: 0,
            search_word: 0,
        }
    }

    pub fn required_node_count(page_count: usize) -> Option<usize> {
        if page_count == 0 || page_count > u32::MAX as usize {
            return None;
        }
        let words = page_count.checked_add(LEAF_PAGES - 1)? / LEAF_PAGES;
        words.checked_next_power_of_two()?.checked_mul(2)
    }

    pub fn required_layout(page_count: usize) -> Option<Layout> {
        let count = Self::required_node_count(page_count)?;
        Layout::array::<RunNode>(count).ok()
    }

    /// Initializes the tree over raw caller-owned storage.
    ///
    /// # Safety
    /// `nodes` must be valid and exclusively owned for `nodes_len` elements for
    /// the entire lifetime of this allocator. The managed address range must
    /// remain valid for `page_count * page_size` bytes.
    pub unsafe fn initialize(
        &mut self,
        base: usize,
        page_count: usize,
        page_size: usize,
        nodes: *mut RunNode,
        nodes_len: usize,
    ) -> Result<(), RunBitmapError> {
        if base == 0
            || page_size == 0
            || !page_size.is_power_of_two()
            || base & (page_size - 1) != 0
            || nodes.is_null()
        {
            return Err(RunBitmapError::InvalidLayout);
        }
        let required =
            Self::required_node_count(page_count).ok_or(RunBitmapError::CapacityExceeded)?;
        if nodes_len < required {
            return Err(RunBitmapError::CapacityExceeded);
        }
        let leaf_capacity = (required / 2)
            .checked_mul(LEAF_PAGES)
            .ok_or(RunBitmapError::CapacityExceeded)?;
        ptr::write_bytes(nodes, 0, required);
        self.nodes = nodes;
        self.nodes_len = required;
        self.leaf_capacity = leaf_capacity;
        self.page_count = page_count;
        self.base = base;
        self.page_size = page_size;
        self.allocated_pages = 0;
        self.search_word = 0;
        self.build(1, 0, leaf_capacity);
        Ok(())
    }

    #[inline]
    pub fn is_initialized(&self) -> bool {
        !self.nodes.is_null() && self.page_count != 0
    }

    #[inline]
    pub fn page_count(&self) -> usize {
        self.page_count
    }

    #[inline]
    pub fn allocated_pages(&self) -> usize {
        self.allocated_pages
    }

    #[inline]
    pub fn largest_free_run(&self) -> usize {
        if self.is_initialized() {
            self.node(1).max_free as usize
        } else {
            0
        }
    }

    pub fn allocate_bytes(&mut self, size: usize, align: usize) -> Result<*mut u8, RunBitmapError> {
        if !self.is_initialized() {
            return Err(RunBitmapError::Uninitialized);
        }
        if size == 0 || align == 0 || !align.is_power_of_two() {
            return Err(RunBitmapError::InvalidLayout);
        }
        if align > self.page_size && align % self.page_size != 0 {
            return Err(RunBitmapError::InvalidLayout);
        }
        let pages = size
            .checked_add(self.page_size - 1)
            .and_then(|rounded| rounded.checked_div(self.page_size))
            .filter(|pages| *pages != 0)
            .ok_or(RunBitmapError::InvalidLayout)?;
        let effective_align = max(align, self.page_size);
        let start = match self.try_allocate_from_leaf_words(pages, effective_align) {
            Some(start) => start,
            None => {
                let start = self
                    .find_aligned(1, 0, self.leaf_capacity, pages, effective_align)
                    .ok_or(RunBitmapError::OutOfMemory)?;
                let end = start
                    .checked_add(pages)
                    .filter(|end| *end <= self.page_count)
                    .ok_or(RunBitmapError::OutOfMemory)?;
                self.assign(1, 0, self.leaf_capacity, start, end, NODE_USED);
                start
            }
        };
        self.allocated_pages = self
            .allocated_pages
            .checked_add(pages)
            .ok_or(RunBitmapError::CapacityExceeded)?;
        let offset = start
            .checked_mul(self.page_size)
            .and_then(|offset| self.base.checked_add(offset))
            .ok_or(RunBitmapError::CapacityExceeded)?;
        Ok(offset as *mut u8)
    }

    pub fn deallocate_bytes(&mut self, ptr: *mut u8, size: usize) -> Result<(), RunBitmapError> {
        if !self.is_initialized() {
            return Err(RunBitmapError::Uninitialized);
        }
        let addr = ptr as usize;
        if ptr.is_null() || size == 0 || addr < self.base {
            return Err(RunBitmapError::InvalidPointer);
        }
        let offset = addr - self.base;
        if offset % self.page_size != 0 {
            return Err(RunBitmapError::InvalidPointer);
        }
        let pages = size
            .checked_add(self.page_size - 1)
            .and_then(|rounded| rounded.checked_div(self.page_size))
            .filter(|pages| *pages != 0)
            .ok_or(RunBitmapError::InvalidLayout)?;
        let start = offset / self.page_size;
        let end = start
            .checked_add(pages)
            .filter(|end| *end <= self.page_count)
            .ok_or(RunBitmapError::InvalidPointer)?;
        if start / LEAF_PAGES == (end - 1) / LEAF_PAGES {
            let word_idx = start / LEAF_PAGES;
            let leaf_idx = self.materialize_leaf(word_idx);
            let local_left = start % LEAF_PAGES;
            let local_right = local_left + pages;
            let mask = Self::range_mask(local_left, local_right);
            if self.node(leaf_idx).leaf_bits & mask != mask {
                return Err(RunBitmapError::RangeAlreadyFree);
            }
            self.node_mut(leaf_idx).leaf_bits &= !mask;
            self.recompute_leaf(leaf_idx);
            self.pull_leaf_ancestors(leaf_idx);
            self.search_word = min(self.search_word, word_idx);
            self.allocated_pages = self
                .allocated_pages
                .checked_sub(pages)
                .ok_or(RunBitmapError::RangeAlreadyFree)?;
            return Ok(());
        }
        let summary = self
            .query(1, 0, self.leaf_capacity, start, end)
            .ok_or(RunBitmapError::InvalidPointer)?;
        if summary.max_free != 0 {
            return Err(RunBitmapError::RangeAlreadyFree);
        }
        self.assign(1, 0, self.leaf_capacity, start, end, NODE_FREE);
        self.search_word = min(self.search_word, start / LEAF_PAGES);
        self.allocated_pages = self
            .allocated_pages
            .checked_sub(pages)
            .ok_or(RunBitmapError::RangeAlreadyFree)?;
        Ok(())
    }

    fn try_allocate_from_leaf_words(&mut self, pages: usize, align: usize) -> Option<usize> {
        if pages > LEAF_PAGES {
            return None;
        }
        let words = (self.page_count + LEAF_PAGES - 1) / LEAF_PAGES;
        if words == 0 {
            return None;
        }
        let scans = min(words, FAST_LEAF_SCAN_LIMIT);
        for offset in 0..scans {
            let word_idx = (self.search_word + offset) % words;
            let leaf_idx = self.materialize_leaf(word_idx);
            let left = word_idx * LEAF_PAGES;
            let right = left + LEAF_PAGES;
            if let Some(start) = self.find_aligned_in_leaf(leaf_idx, left, right, pages, align) {
                let local = start - left;
                let mask = Self::range_mask(local, local + pages);
                self.node_mut(leaf_idx).leaf_bits |= mask;
                self.recompute_leaf(leaf_idx);
                self.pull_leaf_ancestors(leaf_idx);
                if word_idx == self.search_word && self.node(leaf_idx).max_free == 0 {
                    self.search_word = (word_idx + 1) % words;
                }
                return Some(start);
            }
        }
        None
    }

    fn materialize_leaf(&mut self, word_idx: usize) -> usize {
        let mut idx = 1usize;
        let mut left = 0usize;
        let mut right = self.leaf_capacity;
        let page = word_idx * LEAF_PAGES;
        while right - left > LEAF_PAGES {
            self.push(idx, left, right);
            let mid = left + (right - left) / 2;
            if page < mid {
                idx *= 2;
                right = mid;
            } else {
                idx = idx * 2 + 1;
                left = mid;
            }
        }
        idx
    }

    fn pull_leaf_ancestors(&mut self, mut idx: usize) {
        let mut child_len = LEAF_PAGES;
        while idx > 1 {
            idx /= 2;
            self.pull(idx, child_len, child_len);
            child_len *= 2;
        }
    }

    fn build(&mut self, idx: usize, left: usize, right: usize) {
        if right <= self.page_count {
            self.set_node(idx, right - left, NODE_FREE);
            return;
        }
        if left >= self.page_count {
            self.set_node(idx, right - left, NODE_USED);
            return;
        }
        if right - left == LEAF_PAGES {
            let valid_pages = self.page_count - left;
            let valid_mask = Self::low_bits_mask(valid_pages);
            self.node_mut(idx).leaf_bits = !valid_mask;
            self.recompute_leaf(idx);
            return;
        }
        let mid = left + (right - left) / 2;
        self.build(idx * 2, left, mid);
        self.build(idx * 2 + 1, mid, right);
        self.pull(idx, mid - left, right - mid);
    }

    fn find_aligned(
        &mut self,
        idx: usize,
        left: usize,
        right: usize,
        pages: usize,
        align: usize,
    ) -> Option<usize> {
        let node = self.node(idx);
        if (node.max_free as usize) < pages {
            return None;
        }
        if node.state == NODE_FREE {
            return self.aligned_start_in(left, right, pages, align);
        }
        if right - left == LEAF_PAGES {
            return self.find_aligned_in_leaf(idx, left, right, pages, align);
        }

        self.push(idx, left, right);
        let mid = left + (right - left) / 2;
        if let Some(found) = self.find_aligned(idx * 2, left, mid, pages, align) {
            return Some(found);
        }

        let left_node = self.node(idx * 2);
        let right_node = self.node(idx * 2 + 1);
        let cross_left = mid - left_node.suffix_free as usize;
        let cross_right = mid + right_node.prefix_free as usize;
        if cross_right - cross_left >= pages {
            if let Some(found) = self.aligned_start_in(cross_left, cross_right, pages, align) {
                return Some(found);
            }
        }
        self.find_aligned(idx * 2 + 1, mid, right, pages, align)
    }

    fn aligned_start_in(
        &self,
        left: usize,
        right: usize,
        pages: usize,
        align: usize,
    ) -> Option<usize> {
        let start_addr = left
            .checked_mul(self.page_size)
            .and_then(|offset| self.base.checked_add(offset))?;
        let aligned_addr = start_addr.checked_add(align - 1)? & !(align - 1);
        let offset = aligned_addr.checked_sub(self.base)?;
        if offset % self.page_size != 0 {
            return None;
        }
        let start = offset / self.page_size;
        start
            .checked_add(pages)
            .filter(|end| start >= left && *end <= right)
            .map(|_| start)
    }

    fn assign(
        &mut self,
        idx: usize,
        left: usize,
        right: usize,
        query_left: usize,
        query_right: usize,
        state: u32,
    ) {
        if query_left <= left && right <= query_right {
            self.set_node(idx, right - left, state);
            return;
        }
        if right - left == LEAF_PAGES {
            let local_left = query_left.saturating_sub(left);
            let local_right = min(query_right, right) - left;
            let mask = Self::range_mask(local_left, local_right);
            if state == NODE_USED {
                self.node_mut(idx).leaf_bits |= mask;
            } else {
                self.node_mut(idx).leaf_bits &= !mask;
            }
            self.recompute_leaf(idx);
            return;
        }
        self.push(idx, left, right);
        let mid = left + (right - left) / 2;
        if query_left < mid {
            self.assign(idx * 2, left, mid, query_left, query_right, state);
        }
        if query_right > mid {
            self.assign(idx * 2 + 1, mid, right, query_left, query_right, state);
        }
        self.pull(idx, mid - left, right - mid);
    }

    fn query(
        &mut self,
        idx: usize,
        left: usize,
        right: usize,
        query_left: usize,
        query_right: usize,
    ) -> Option<Summary> {
        if query_right <= left || right <= query_left {
            return None;
        }
        if query_left <= left && right <= query_right {
            return Some(Summary::from_node(self.node(idx), right - left));
        }
        if right - left == LEAF_PAGES {
            let local_left = query_left.saturating_sub(left);
            let local_right = min(query_right, right) - left;
            return Some(self.leaf_range_summary(idx, local_left, local_right));
        }
        self.push(idx, left, right);
        let mid = left + (right - left) / 2;
        let left_summary = self.query(idx * 2, left, mid, query_left, query_right);
        let right_summary = self.query(idx * 2 + 1, mid, right, query_left, query_right);
        match (left_summary, right_summary) {
            (Some(left), Some(right)) => Some(Summary::merge(left, right)),
            (Some(summary), None) | (None, Some(summary)) => Some(summary),
            (None, None) => None,
        }
    }

    #[inline]
    fn push(&mut self, idx: usize, left: usize, right: usize) {
        let state = self.node(idx).state;
        if state == NODE_MIXED || right - left <= LEAF_PAGES {
            return;
        }
        let mid = left + (right - left) / 2;
        self.set_node(idx * 2, mid - left, state);
        self.set_node(idx * 2 + 1, right - mid, state);
        self.node_mut(idx).state = NODE_MIXED;
    }

    #[inline]
    fn pull(&mut self, idx: usize, left_len: usize, right_len: usize) {
        let left = Summary::from_node(self.node(idx * 2), left_len);
        let right = Summary::from_node(self.node(idx * 2 + 1), right_len);
        let merged = Summary::merge(left, right);
        let state = if merged.max_free == merged.len {
            NODE_FREE
        } else if merged.max_free == 0 {
            NODE_USED
        } else {
            NODE_MIXED
        };
        *self.node_mut(idx) = RunNode {
            prefix_free: merged.prefix_free as u32,
            suffix_free: merged.suffix_free as u32,
            max_free: merged.max_free as u32,
            state,
            leaf_bits: 0,
        };
    }

    #[inline]
    fn set_node(&mut self, idx: usize, len: usize, state: u32) {
        debug_assert!(idx < self.nodes_len);
        let free_len = if state == NODE_FREE { len as u32 } else { 0 };
        *self.node_mut(idx) = RunNode {
            prefix_free: free_len,
            suffix_free: free_len,
            max_free: free_len,
            state,
            leaf_bits: if len == LEAF_PAGES && state == NODE_USED {
                u64::MAX
            } else {
                0
            },
        };
    }

    fn find_aligned_in_leaf(
        &self,
        idx: usize,
        left: usize,
        right: usize,
        pages: usize,
        align: usize,
    ) -> Option<usize> {
        if pages > LEAF_PAGES {
            return None;
        }
        let mut start = self.aligned_start_in(left, right, pages, align)?;
        let step_pages = max(1, align / self.page_size);
        debug_assert!(step_pages.is_power_of_two());
        let used = self.node(idx).leaf_bits;
        if used == 0 {
            return Some(start);
        }
        while start.checked_add(pages)? <= right {
            let local = start - left;
            let mask = Self::range_mask(local, local + pages);
            let conflicts = used & mask;
            if conflicts == 0 {
                return Some(start);
            }
            start = Self::advance_past_leaf_conflict(start, local, conflicts, step_pages)?;
        }
        None
    }

    #[inline(always)]
    fn advance_past_leaf_conflict(
        start: usize,
        local: usize,
        conflicts: u64,
        step_pages: usize,
    ) -> Option<usize> {
        // Every candidate up to the last occupied page in this window still
        // overlaps that page. Jump past it, rounded to the next aligned
        // candidate, rather than retrying every page.
        let last_conflict = LEAF_PAGES - 1 - conflicts.leading_zeros() as usize;
        let conflict_delta = last_conflict - local;
        let advance = conflict_delta.checked_add(step_pages)? & !(step_pages - 1);
        start.checked_add(advance)
    }

    #[inline]
    fn low_bits_mask(bits: usize) -> u64 {
        if bits >= LEAF_PAGES {
            u64::MAX
        } else if bits == 0 {
            0
        } else {
            (1_u64 << bits) - 1
        }
    }

    #[inline]
    fn range_mask(left: usize, right: usize) -> u64 {
        debug_assert!(left <= right && right <= LEAF_PAGES);
        Self::low_bits_mask(right) & !Self::low_bits_mask(left)
    }

    fn recompute_leaf(&mut self, idx: usize) {
        let used = self.node(idx).leaf_bits;
        let summary = Self::summary_for_bits(used, LEAF_PAGES);
        let state = if used == 0 {
            NODE_FREE
        } else if used == u64::MAX {
            NODE_USED
        } else {
            NODE_MIXED
        };
        *self.node_mut(idx) = RunNode {
            prefix_free: summary.prefix_free as u32,
            suffix_free: summary.suffix_free as u32,
            max_free: summary.max_free as u32,
            state,
            leaf_bits: used,
        };
    }

    fn leaf_range_summary(&self, idx: usize, left: usize, right: usize) -> Summary {
        let len = right - left;
        let bits = (self.node(idx).leaf_bits >> left) | !Self::low_bits_mask(len);
        Self::summary_for_bits(bits, len)
    }

    fn summary_for_bits(used: u64, len: usize) -> Summary {
        debug_assert!(len <= LEAF_PAGES);
        if len == 0 {
            return Summary {
                len: 0,
                prefix_free: 0,
                suffix_free: 0,
                max_free: 0,
            };
        }
        let free = !used & Self::low_bits_mask(len);
        let prefix_free = min(free.trailing_ones() as usize, len);
        let suffix_free = if len == LEAF_PAGES {
            free.leading_ones() as usize
        } else {
            (free << (LEAF_PAGES - len)).leading_ones() as usize
        };
        let mut remaining = free;
        let mut max_free = 0usize;
        while remaining != 0 {
            let skipped = remaining.trailing_zeros() as usize;
            let shifted = remaining >> skipped;
            let run = shifted.trailing_ones() as usize;
            max_free = max(max_free, run);
            if run >= LEAF_PAGES - skipped {
                break;
            }
            remaining &= !Self::range_mask(skipped, skipped + run);
        }
        Summary {
            len,
            prefix_free,
            suffix_free,
            max_free,
        }
    }

    #[inline]
    fn node(&self, idx: usize) -> RunNode {
        debug_assert!(idx < self.nodes_len);
        unsafe { *self.nodes.add(idx) }
    }

    #[inline]
    fn node_mut(&mut self, idx: usize) -> &mut RunNode {
        debug_assert!(idx < self.nodes_len);
        unsafe { &mut *self.nodes.add(idx) }
    }
}

#[cfg(feature = "bitmap_page_allocator")]
pub(crate) static PAGE_RUN_BITMAP: spin::Mutex<SegmentPageAllocator> =
    spin::Mutex::new(SegmentPageAllocator::empty());

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::vec;
    use alloc::vec::Vec;

    const BASE: usize = 0x10_0000;
    const PAGE: usize = 4096;

    fn allocator(pages: usize) -> (SegmentPageAllocator, Vec<RunNode>) {
        let nodes_len = SegmentPageAllocator::required_node_count(pages).unwrap();
        let mut nodes = vec![RunNode::EMPTY; nodes_len];
        let mut allocator = SegmentPageAllocator::empty();
        unsafe {
            allocator
                .initialize(BASE, pages, PAGE, nodes.as_mut_ptr(), nodes.len())
                .unwrap();
        }
        (allocator, nodes)
    }

    #[test]
    fn adjacent_frees_form_a_larger_run_without_explicit_coalescing() {
        let (mut allocator, _nodes) = allocator(16);
        let first = allocator.allocate_bytes(PAGE * 3, PAGE).unwrap();
        let second = allocator.allocate_bytes(PAGE * 2, PAGE).unwrap();
        assert_eq!(second as usize, first as usize + PAGE * 3);
        allocator.deallocate_bytes(first, PAGE * 3).unwrap();
        allocator.deallocate_bytes(second, PAGE * 2).unwrap();
        assert_eq!(allocator.largest_free_run(), 16);
        assert_eq!(allocator.allocate_bytes(PAGE * 5, PAGE).unwrap(), first);
    }

    #[test]
    fn fragmented_first_fit_uses_cross_child_summary() {
        let (mut allocator, _nodes) = allocator(17);
        let a = allocator.allocate_bytes(PAGE * 3, PAGE).unwrap();
        let b = allocator.allocate_bytes(PAGE * 5, PAGE).unwrap();
        let c = allocator.allocate_bytes(PAGE * 2, PAGE).unwrap();
        allocator.deallocate_bytes(b, PAGE * 5).unwrap();
        let reused = allocator.allocate_bytes(PAGE * 4, PAGE).unwrap();
        assert_eq!(reused, b);
        assert_eq!(a as usize, BASE);
        assert_eq!(c as usize, BASE + PAGE * 8);
    }

    #[test]
    fn fragmented_first_fit_uses_cross_leaf_summary() {
        let (mut allocator, _nodes) = allocator(128);
        let pages: Vec<_> = (0..128)
            .map(|_| allocator.allocate_bytes(PAGE, PAGE).unwrap())
            .collect();
        for ptr in pages[8..12]
            .iter()
            .chain(&pages[24..30])
            .chain(&pages[60..68])
        {
            allocator.deallocate_bytes(*ptr, PAGE).unwrap();
        }

        assert_eq!(allocator.largest_free_run(), 8);
        assert_eq!(
            allocator.allocate_bytes(PAGE * 8, PAGE).unwrap() as usize,
            BASE + PAGE * 60
        );
    }

    #[test]
    fn over_page_alignment_finds_an_aligned_subrun() {
        let (mut allocator, _nodes) = allocator(32);
        let prefix = allocator.allocate_bytes(PAGE, PAGE).unwrap();
        let aligned = allocator.allocate_bytes(PAGE * 3, PAGE * 8).unwrap();
        assert_eq!(prefix as usize, BASE);
        assert_eq!(aligned as usize % (PAGE * 8), 0);
        assert_eq!(aligned as usize, BASE + PAGE * 8);
    }

    #[test]
    fn leaf_search_skips_conflicts_to_the_first_complete_run() {
        let (mut allocator, _nodes) = allocator(64);
        let pages: Vec<_> = (0..64)
            .map(|_| allocator.allocate_bytes(PAGE, PAGE).unwrap())
            .collect();
        for ptr in pages[8..16].iter().chain(&pages[24..32]) {
            allocator.deallocate_bytes(*ptr, PAGE).unwrap();
        }

        assert_eq!(
            allocator.allocate_bytes(PAGE * 8, PAGE).unwrap() as usize,
            BASE + PAGE * 8
        );
    }

    #[test]
    fn conflict_skip_preserves_over_page_alignment() {
        let (mut allocator, _nodes) = allocator(64);
        let pages: Vec<_> = (0..64)
            .map(|_| allocator.allocate_bytes(PAGE, PAGE).unwrap())
            .collect();
        for ptr in &pages[9..24] {
            allocator.deallocate_bytes(*ptr, PAGE).unwrap();
        }

        assert_eq!(
            allocator.allocate_bytes(PAGE * 4, PAGE * 8).unwrap() as usize,
            BASE + PAGE * 16
        );
    }

    #[test]
    fn multiword_run_allocation_and_free_restore_root_summary() {
        let (mut allocator, _nodes) = allocator(257);
        let prefix = allocator.allocate_bytes(PAGE * 7, PAGE).unwrap();
        let run = allocator.allocate_bytes(PAGE * 130, PAGE).unwrap();
        assert_eq!(prefix as usize, BASE);
        assert_eq!(run as usize, BASE + PAGE * 7);
        allocator.deallocate_bytes(run, PAGE * 130).unwrap();
        allocator.deallocate_bytes(prefix, PAGE * 7).unwrap();
        assert_eq!(allocator.largest_free_run(), 257);
        assert_eq!(allocator.allocated_pages(), 0);
    }

    #[test]
    fn deallocation_rejects_double_free_and_foreign_ranges() {
        let (mut allocator, _nodes) = allocator(8);
        let ptr = allocator.allocate_bytes(PAGE * 2, PAGE).unwrap();
        allocator.deallocate_bytes(ptr, PAGE * 2).unwrap();
        assert_eq!(
            allocator.deallocate_bytes(ptr, PAGE * 2),
            Err(RunBitmapError::RangeAlreadyFree)
        );
        assert_eq!(
            allocator.deallocate_bytes((BASE - PAGE) as *mut u8, PAGE),
            Err(RunBitmapError::InvalidPointer)
        );
    }

    #[test]
    fn randomized_operations_match_naive_bitmap_model() {
        const PAGES: usize = 257;
        let (mut allocator, _nodes) = allocator(PAGES);
        let mut used = vec![false; PAGES];
        let mut live: Vec<(usize, usize)> = Vec::new();
        let mut random = 0x9e37_79b9_u32;

        for _ in 0..10_000 {
            random = random.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            let should_free = !live.is_empty() && random & 3 == 0;
            if should_free {
                let live_idx = random as usize % live.len();
                let (start, pages) = live.swap_remove(live_idx);
                allocator
                    .deallocate_bytes((BASE + start * PAGE) as *mut u8, pages * PAGE)
                    .unwrap();
                for slot in &mut used[start..start + pages] {
                    *slot = false;
                }
                continue;
            }

            let pages = 1 + (random as usize % 9);
            let align_pages = 1usize << ((random >> 8) as usize % 4);
            let expected = (0..=PAGES.saturating_sub(pages)).find(|start| {
                (BASE + start * PAGE) % (align_pages * PAGE) == 0
                    && used[*start..*start + pages]
                        .iter()
                        .all(|occupied| !*occupied)
            });
            let actual = allocator.allocate_bytes(pages * PAGE, align_pages * PAGE);
            match (expected, actual) {
                (Some(_), Ok(ptr)) => {
                    let start = (ptr as usize - BASE) / PAGE;
                    assert_eq!(ptr as usize % (align_pages * PAGE), 0);
                    assert!(start + pages <= PAGES);
                    assert!(used[start..start + pages].iter().all(|occupied| !*occupied));
                    for slot in &mut used[start..start + pages] {
                        *slot = true;
                    }
                    live.push((start, pages));
                }
                (None, Err(RunBitmapError::OutOfMemory)) => {}
                pair => panic!("segment tree diverged from naive model: {:?}", pair),
            }
        }
    }
}
