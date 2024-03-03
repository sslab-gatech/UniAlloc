use crate::error::Result;
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

    pub fn reserve(&mut self, additional: usize) {
        self.list.reserve(additional);
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
        // unlink from the linklist
        self.remove_node(self.list.len() - 1);
        let last_item = self.list.pop();

        if let Some(tuple) = last_item {
            let res = tuple.2;
            return Some(res);
        }

        None
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
        Ok(self.list[idx].0)
    }

    /// Sets the `prev` item
    #[inline]
    fn set_prev(&mut self, curr: usize, new_prev: usize) {
        self.list[curr].0 = new_prev;
    }

    /// Gets the `next` item
    #[inline]
    fn get_next(&self, idx: usize) -> Result<usize> {
        Ok(self.list[idx].1)
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

    #[test]
    fn arr_linklist_new() {
        let l = ArrayLinkedList::<usize>::new();
    }
}
