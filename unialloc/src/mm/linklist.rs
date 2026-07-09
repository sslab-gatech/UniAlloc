use crate::*;
use core::mem::align_of;

#[derive(Clone, Copy)]
pub struct Linklist {
    pub link: usize,
    pub length: usize,
}

impl Linklist {
    pub const fn new() -> Self {
        Self { link: 0, length: 0 }
    }

    /// Push a node onto the freelist without allocator-side ownership checks.
    ///
    /// The caller still owns the storage invariant: `ptr` must point to a
    /// writable `usize` word inside a free object.  This helper rejects null,
    /// unaligned, or overflowing pushes before touching the node so corrupted
    /// metadata cannot inflate the logical length.
    pub fn push_unchecked(&mut self, ptr: *mut u8) {
        let next_length = match self.length.checked_add(1) {
            Some(length) if !ptr.is_null() && (ptr as usize) % align_of::<usize>() == 0 => length,
            _ => return,
        };
        let target = ptr as *mut usize;
        let current = self.link;
        unsafe {
            *target = current;
        }

        self.link = target as usize;
        self.length = next_length;
    }

    #[inline]
    fn node_is_word_aligned(addr: usize) -> bool {
        addr != 0 && addr % align_of::<usize>() == 0
    }

    #[inline]
    fn read_next_word(addr: usize) -> Option<usize> {
        if !Self::node_is_word_aligned(addr) {
            return None;
        }
        Some(unsafe { *(addr as *const usize) })
    }

    #[inline]
    fn clear_corrupt(&mut self) -> *mut u8 {
        self.link = 0;
        self.length = 0;
        core::ptr::null_mut::<u8>()
    }

    /// Push a node onto the freelist using the default non-authenticated path.
    ///
    /// The pointer must reference writable storage large enough to hold one
    /// machine word; the list stores the next pointer in that word.
    pub fn push(&mut self, ptr: *mut u8) {
        self.push_unchecked(ptr);
    }

    fn pop_head(&mut self, result: usize) -> *mut u8 {
        if result == 0 {
            debug_assert_eq!(self.length, 0);
            self.length = 0;
            return core::ptr::null_mut::<u8>();
        }
        let next = match Self::read_next_word(result) {
            Some(next) => next,
            None => return self.clear_corrupt(),
        };
        if next != 0 && !Self::node_is_word_aligned(next) {
            return self.clear_corrupt();
        }
        self.link = next;
        self.length = match self.length.checked_sub(1) {
            Some(length) => length,
            None => return self.clear_corrupt(),
        };
        result as *mut u8
    }

    fn unlink_aligned_after(
        &mut self,
        mut pre: usize,
        align_mask: usize,
        search_bound: usize,
    ) -> *mut u8 {
        let mut now = match Self::read_next_word(pre) {
            Some(next) => next,
            None => return self.clear_corrupt(),
        };
        let mut visited = 1usize;
        while now != 0 && visited < search_bound {
            if !Self::node_is_word_aligned(now) {
                return self.clear_corrupt();
            }
            if now & align_mask == 0 {
                let next = match Self::read_next_word(now) {
                    Some(next) => next,
                    None => return self.clear_corrupt(),
                };
                if next != 0 && !Self::node_is_word_aligned(next) {
                    return self.clear_corrupt();
                }
                unsafe { *(pre as *mut usize) = next };
                self.length = match self.length.checked_sub(1) {
                    Some(length) => length,
                    None => return self.clear_corrupt(),
                };
                return now as *mut u8;
            }
            pre = now;
            now = match Self::read_next_word(pre) {
                Some(next) => next,
                None => return self.clear_corrupt(),
            };
            visited += 1;
        }
        // If the declared length is exhausted but the link chain still
        // continues, the list is cyclic or the length metadata is stale.  Keep
        // the allocator fail-closed: clear this freelist instead of preserving
        // a corrupt cycle for the next allocation path.
        if now != 0 {
            return self.clear_corrupt();
        }
        core::ptr::null_mut::<u8>()
    }

    pub fn pop_unchecked_aligned(&mut self, align: usize) -> *mut u8 {
        if align == 0 || !align.is_power_of_two() {
            return core::ptr::null_mut::<u8>();
        }
        if self.length == 0 {
            debug_assert_eq!(self.length, 0);
            return core::ptr::null_mut::<u8>();
        }

        let result = self.link;
        if result == 0 {
            debug_assert_eq!(self.length, 0);
            self.length = 0;
            return core::ptr::null_mut::<u8>();
        }
        if !Self::node_is_word_aligned(result) {
            return self.clear_corrupt();
        }
        let align_mask = align - 1;
        if result & align_mask == 0 {
            self.pop_head(result)
        } else {
            self.unlink_aligned_after(result, align_mask, self.length)
        }
    }

    /// Pop the most recent node without requiring a stricter alignment match.
    pub fn pop(&mut self) -> *mut u8 {
        self.pop_unchecked_aligned(1)
    }

    pub fn length(&self) -> usize {
        self.length
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_test_link_word(slot: *mut usize, next: usize) {
        // The freelist reads this test fixture through raw pointers.  Writing
        // through the same raw-memory contract avoids latest rustc flagging the
        // setup as an unused ordinary assignment.
        unsafe {
            slot.write(next);
        }
    }

    #[test]
    fn freelist_push_pop_test() {
        let mut list = Linklist::new();
        let mut chunks = [0xdeadbeefusize, 0xcafeusize];
        let first = chunks.as_mut_ptr() as *mut u8;
        let second = unsafe { chunks.as_mut_ptr().add(1) as *mut u8 };
        list.push(first);
        assert_eq!(chunks[0], 0);
        list.push(second);
        assert_eq!(chunks[1], first as usize);
        assert_eq!(list.length(), 2);
        assert_eq!(list.pop(), second);
        assert_eq!(list.pop(), first);
        assert_eq!(list.pop(), core::ptr::null_mut::<u8>());
        assert_eq!(list.length(), 0);
    }

    #[test]
    fn null_push_is_ignored_without_corrupting_length() {
        let mut list = Linklist::new();
        list.push(core::ptr::null_mut());
        assert_eq!(list.length(), 0);
        assert_eq!(list.pop(), core::ptr::null_mut::<u8>());
    }

    #[test]
    fn unaligned_push_is_ignored_without_touching_storage() {
        let mut list = Linklist::new();
        let mut storage = [0xA5u8; core::mem::size_of::<usize>() + 1];
        let unaligned = storage.as_mut_ptr().wrapping_add(1);
        if (unaligned as usize) % core::mem::align_of::<usize>() == 0 {
            return;
        }
        list.push(unaligned);
        assert_eq!(list.length(), 0);
        assert_eq!(list.link, 0);
        assert!(storage.iter().all(|byte| *byte == 0xA5));
    }

    #[test]
    fn invalid_alignment_pop_is_noop() {
        let mut list = Linklist::new();
        let mut chunk = 0usize;
        let ptr = &mut chunk as *mut usize as *mut u8;
        list.push(ptr);
        assert_eq!(list.pop_unchecked_aligned(3), core::ptr::null_mut::<u8>());
        assert_eq!(list.length(), 1);
        assert_eq!(list.pop(), ptr);
        assert_eq!(list.length(), 0);
    }

    #[test]
    fn aligned_pop_skips_until_matching_node() {
        #[repr(align(16))]
        struct AlignedWords([usize; 4]);

        let mut list = Linklist::new();
        let mut chunks = AlignedWords([0usize; 4]);
        let base = chunks.0.as_mut_ptr();
        let aligned = unsafe { base.add(2) as *mut u8 };
        let misaligned_to_16 = unsafe { base.add(1) as *mut u8 };
        list.push(aligned);
        list.push(misaligned_to_16);
        assert_eq!(list.pop_unchecked_aligned(16), aligned);
        assert_eq!(list.pop(), misaligned_to_16);
    }

    #[test]
    fn push_rejects_length_overflow_without_mutating_node() {
        let mut list = Linklist {
            link: 0x1234,
            length: usize::MAX,
        };
        let mut node = 0xfeedusize;
        let ptr = (&mut node as *mut usize).cast::<u8>();

        list.push(ptr);

        assert_eq!(list.link, 0x1234);
        assert_eq!(list.length(), usize::MAX);
        assert_eq!(node, 0xfeed);
    }

    #[test]
    fn aligned_pop_clears_cycle_when_declared_length_is_exhausted() {
        #[repr(align(16))]
        struct AlignedWords([usize; 4]);

        let mut chunks = AlignedWords([0usize; 4]);
        let misaligned_to_16 = unsafe { chunks.0.as_mut_ptr().add(1) as usize };
        chunks.0[1] = misaligned_to_16;
        let mut list = Linklist {
            link: misaligned_to_16,
            length: 1,
        };

        assert_eq!(list.pop_unchecked_aligned(16), core::ptr::null_mut::<u8>());
        assert_eq!(list.link, 0);
        assert_eq!(list.length(), 0);
        assert_eq!(chunks.0[1], misaligned_to_16);
    }

    #[test]
    fn aligned_pop_preserves_valid_unmatched_list_after_full_scan() {
        const STRICT_ALIGN: usize = 64;
        #[repr(align(16))]
        struct AlignedWords([usize; 10]);

        let mut list = Linklist::new();
        let mut chunks = AlignedWords([0usize; 10]);
        let mut unmatched = [core::ptr::null_mut::<u8>(); 2];
        let mut found = 0usize;
        for word in chunks.0.iter_mut() {
            let ptr = (word as *mut usize).cast::<u8>();
            if (ptr as usize) % STRICT_ALIGN != 0 {
                unmatched[found] = ptr;
                found += 1;
                if found == unmatched.len() {
                    break;
                }
            }
        }
        assert_eq!(found, unmatched.len());

        let first = unmatched[0];
        let second = unmatched[1];
        list.push(first);
        list.push(second);

        assert_eq!(
            list.pop_unchecked_aligned(STRICT_ALIGN),
            core::ptr::null_mut::<u8>()
        );
        assert_eq!(list.length(), 2);
        assert_eq!(list.pop(), second);
        assert_eq!(list.pop(), first);
    }

    #[test]
    fn pop_clears_corrupt_misaligned_head_without_deref() {
        let mut list = Linklist {
            link: 0x1001,
            length: 1,
        };

        assert_eq!(list.pop(), core::ptr::null_mut::<u8>());
        assert_eq!(list.link, 0);
        assert_eq!(list.length(), 0);
    }

    #[test]
    fn aligned_pop_clears_corrupt_misaligned_next_without_deref() {
        #[repr(align(16))]
        struct AlignedWords([usize; 2]);

        let mut chunks = AlignedWords([0usize; 2]);
        let head = chunks.0.as_mut_ptr() as usize;
        write_test_link_word(chunks.0.as_mut_ptr(), 0x1001);
        let mut list = Linklist {
            link: head,
            length: 2,
        };

        assert_eq!(list.pop_unchecked_aligned(32), core::ptr::null_mut::<u8>());
        assert_eq!(list.link, 0);
        assert_eq!(list.length(), 0);
    }
}
