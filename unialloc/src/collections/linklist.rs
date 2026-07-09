use crate::error::{AllocError, Result};
use crate::prelude::*;
use alloc::vec::Vec;
use core::slice::SliceIndex;
use GlobalBackend as LinkedlistAllocator;

/// A double linkedlist implemented using vector
/// (prev, next, item)
///
/// We use [`usize`] as indices because slide indices are of type `usize`
/// or ranges of `usize`.
///
/// This implementation requires an external indices tracking method
pub struct ArrayLinkedList<T> {
    list: Vec<(usize, usize, T), LinkedlistAllocator>,
}

pub trait Linkedlist {
    type Item;

    fn tail(&self) -> usize;

    fn push(&mut self, item: Self::Item);
    fn pop(&mut self) -> Option<Self::Item>;

    fn remove_node(&mut self, idx: usize);
    fn insert_to_next(&mut self, base: usize, idx: usize);
    fn insert_to_prev(&mut self, base: usize, idx: usize);
    fn get_prev(&self, idx: usize) -> Result<usize>;
    fn set_prev(&mut self, curr: usize, new_prev: usize);
    fn get_next(&self, idx: usize) -> Result<usize>;
    fn set_next(&mut self, curr: usize, new_next: usize);
}

impl<T> ArrayLinkedList<T> {
    pub const fn new() -> Self {
        Self {
            list: Vec::<(usize, usize, T), LinkedlistAllocator>::new_in(LinkedlistAllocator),
        }
    }

    pub fn capacity(&self) -> usize {
        self.list.capacity()
    }

    pub fn try_reserve(&mut self, additional: usize) -> Result<()> {
        self.list
            .try_reserve(additional)
            .map_err(|_| AllocError::ENOMEM)
    }

    pub fn reserve(&mut self, additional: usize) {
        self.try_reserve(additional)
            .expect("ArrayLinkedList reserve failed")
    }

    pub fn len(&self) -> usize {
        self.list.len()
    }

    pub fn get_mut(&mut self, idx: usize) -> Option<&mut T> {
        let tuple = self.list.get_mut(idx);
        if let Some(elem) = tuple {
            return Some(&mut elem.2);
        }
        None
    }

    pub fn get(&self, idx: usize) -> Option<&T> {
        let tuple = self.list.get(idx);
        if let Some(elem) = tuple {
            return Some(&elem.2);
        }
        None
    }

    /// Resets the `prev` and `next` links to itself
    #[inline]
    pub fn reset_links(&mut self, idx: usize) {
        self.list[idx].0 = idx;
        self.list[idx].1 = idx;
    }

    #[inline]
    fn checked_links(&self, idx: usize) -> Result<(usize, usize)> {
        self.list
            .get(idx)
            .map(|node| (node.0, node.1))
            .ok_or(AllocError::EFATAL)
    }

    #[inline]
    pub fn try_reset_links(&mut self, idx: usize) -> Result<()> {
        let node = self.list.get_mut(idx).ok_or(AllocError::EFATAL)?;
        node.0 = idx;
        node.1 = idx;
        Ok(())
    }

    pub fn try_remove_node(&mut self, idx: usize) -> Result<()> {
        let (prev_idx, next_idx) = self.checked_links(idx)?;
        self.checked_links(prev_idx)?;
        self.checked_links(next_idx)?;

        self.list[next_idx].0 = prev_idx;
        self.list[prev_idx].1 = next_idx;

        self.list[idx].0 = idx;
        self.list[idx].1 = idx;
        Ok(())
    }

    pub fn try_insert_to_next(&mut self, base: usize, idx: usize) -> Result<()> {
        let (_, next_idx) = self.checked_links(base)?;
        self.checked_links(idx)?;
        self.checked_links(next_idx)?;

        self.list[next_idx].0 = idx;
        self.list[base].1 = idx;

        self.list[idx].1 = next_idx;
        self.list[idx].0 = base;
        Ok(())
    }

    pub fn try_insert_to_prev(&mut self, base: usize, idx: usize) -> Result<()> {
        let (prev_idx, _) = self.checked_links(base)?;
        self.checked_links(idx)?;
        self.checked_links(prev_idx)?;

        self.list[prev_idx].1 = idx;
        self.list[base].0 = idx;

        self.list[idx].1 = base;
        self.list[idx].0 = prev_idx;
        Ok(())
    }
}

impl<T> Linkedlist for ArrayLinkedList<T> {
    type Item = T;

    fn tail(&self) -> usize {
        self.list.len() - 1
    }

    /// Pushes an item into the internal vector,
    /// The `prev` and `next` point to itself
    fn push(&mut self, item: Self::Item) {
        let new_idx = self.list.len();
        self.list.push((new_idx, new_idx, item));
    }

    /// Pops the last item from the internal vector
    /// The item is unlinked from the linklist
    fn pop(&mut self) -> Option<Self::Item> {
        let last_idx = self.list.len().checked_sub(1)?;
        // unlink from the linklist before removing the backing slot
        self.remove_node(last_idx);
        self.list.pop().map(|tuple| tuple.2)
    }

    /// Removes a node from linklist
    /// By default, the node's `prev` and `next` point to itself
    fn remove_node(&mut self, idx: usize) {
        let prev_idx = self.list[idx].0;
        let next_idx = self.list[idx].1;

        self.set_prev(next_idx, prev_idx);
        self.set_next(prev_idx, next_idx);

        self.set_prev(idx, idx);
        self.set_next(idx, idx);
    }

    /// Inserts a new node to the linklist.
    ///
    /// The new node will be inserted to the next item of `base`
    fn insert_to_next(&mut self, base: usize, idx: usize) {
        let next_idx = self.list[base].1;

        self.set_prev(next_idx, idx);
        self.set_next(base, idx);

        self.set_next(idx, next_idx);
        self.set_prev(idx, base);
    }

    /// Inserts a new node to the linklist
    ///
    /// The new node will be inserted to the prev item of `base`
    fn insert_to_prev(&mut self, base: usize, idx: usize) {
        let prev_idx = self.list[base].0;

        self.set_next(prev_idx, idx);
        self.set_prev(base, idx);

        self.set_next(idx, base);
        self.set_prev(idx, prev_idx);
    }

    /// Gets the `prev` item
    #[inline]
    fn get_prev(&self, idx: usize) -> Result<usize> {
        self.list
            .get(idx)
            .map(|node| node.0)
            .ok_or(AllocError::EFATAL)
    }

    /// Sets the `prev` item
    #[inline]
    fn set_prev(&mut self, curr: usize, new_prev: usize) {
        self.list[curr].0 = new_prev;
    }

    /// Gets the `next` item
    #[inline]
    fn get_next(&self, idx: usize) -> Result<usize> {
        self.list
            .get(idx)
            .map(|node| node.1)
            .ok_or(AllocError::EFATAL)
    }

    /// Sets the `next` item
    #[inline]
    fn set_next(&mut self, curr: usize, new_next: usize) {
        self.list[curr].1 = new_next;
    }
}

#[cfg(test)]
mod test {
    use super::*;

    #[cfg(feature = "fixed_heap")]
    fn init_test_allocator() -> spin::MutexGuard<'static, ()> {
        crate::sc::fixed_heap_test_guard()
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn init_test_allocator() {}

    #[test]
    fn arr_linklist_new() {
        let _fixed_heap_guard = init_test_allocator();
        let list = ArrayLinkedList::<usize>::new();
        assert_eq!(list.len(), 0);
    }

    #[test]
    fn pop_empty_returns_none_without_underflow() {
        let _fixed_heap_guard = init_test_allocator();
        let mut list = ArrayLinkedList::<usize>::new();

        assert_eq!(list.pop(), None);
        assert_eq!(list.len(), 0);
    }

    #[test]
    fn try_reserve_reports_impossible_growth_without_mutation() {
        let _fixed_heap_guard = init_test_allocator();
        let mut list = ArrayLinkedList::<usize>::new();
        list.push(1);
        let len_before = list.len();
        let capacity_before = list.capacity();

        let err = list
            .try_reserve(usize::MAX)
            .expect_err("impossible metadata growth should report ENOMEM");

        assert_eq!(err.to_raw_errno(), AllocError::ENOMEM.to_raw_errno());
        assert_eq!(list.len(), len_before);
        assert_eq!(list.capacity(), capacity_before);
    }

    #[test]
    fn checked_remove_rejects_invalid_index_without_panic() {
        let _fixed_heap_guard = init_test_allocator();
        let mut list = ArrayLinkedList::<usize>::new();
        list.push(1);

        let err = list
            .try_remove_node(1)
            .expect_err("invalid list index must fail closed");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(list.len(), 1);
    }

    #[test]
    fn checked_insert_rejects_invalid_base_without_panic() {
        let _fixed_heap_guard = init_test_allocator();
        let mut list = ArrayLinkedList::<usize>::new();
        list.push(1);
        list.push(2);

        let err = list
            .try_insert_to_prev(9, 1)
            .expect_err("invalid base must not index out of bounds");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(list.len(), 2);
    }
}
