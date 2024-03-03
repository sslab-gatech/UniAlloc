use super::consts::*;
use crate::print;
use core::cmp;
use core::ops::{DerefMut, Index, IndexMut, Range};
use core::ptr::write_bytes;

pub const BLOCKS_IN_TREE: usize = blocks_in_tree(LEVEL_COUNT);

#[derive(Copy, Clone)]
pub struct Block {
    /// (1) not 0: the order of the biggest block under this block - 1.
    /// (2) 0 : used
    pub order_free: u8,
}

impl Block {
    pub const fn new_free(order: u8) -> Self {
        Self {
            order_free: order + 1,
        }
    }

    pub const fn new_used() -> Self {
        Self { order_free: 0 }
    }
}

#[inline]
pub const fn blocks_in_tree(levels: u8) -> usize {
    ((1 << levels) - 1) as usize
}

#[inline]
pub const fn blocks_in_level(level: u8) -> usize {
    blocks_in_tree(level + 1) - blocks_in_tree(level)
}

/// A 1-indexed flatten tree structure
pub mod flat_tree {
    #[inline]
    pub const fn left_child(index: usize) -> usize {
        index << 1
    }

    #[inline]
    pub const fn parent(index: usize) -> usize {
        index >> 1
    }
}

/// A tree of blocks. Contains the flat representation of the tree as a flat array
// TODO i might have a *few* cache misses here, eh?
///
/// # Notes
///
/// * `order` - In this buddy system, the order is a relative order, i.e.,
/// the real order should be `order + BASE_ORDER`
pub struct BuddyTree<B, const BASE_ORDER: u8, const MAX_ORDER: u8>
where
    B: DerefMut<Target = [Block; BLOCKS_IN_TREE]>
        + IndexMut<usize, Output = Block>
        + Index<usize, Output = Block>,
{
    /// Flat array representation of tree. Used with the help of the `flat_tree` crate.
    flat_blocks: B,
}

/// # Explanation
///
/// A tree with all four free orders
///
/// ```ignore
///          3             -- order 3
///    2           2       -- order 2
///  1   1       1   1     -- order 1
/// 0 0 0 0     0 0 0 0    -- order 0
/// ```
impl<B, const BASE_ORDER: u8, const MAX_ORDER: u8> BuddyTree<B, BASE_ORDER, MAX_ORDER>
where
    B: DerefMut<Target = [Block; BLOCKS_IN_TREE]>
        + IndexMut<usize, Output = Block>
        + Index<usize, Output = Block>,
{
    const MAX_ORDER_SIZE: usize = (BASE_ORDER + MAX_ORDER) as usize;

    /// initilize the buddy tree
    ///
    /// # Arguments
    ///
    /// * usable - A set of range used to specify which range is usable
    /// * flat_blocks - A memory chunk used to store the tree
    pub fn new<T>(_usable: T, flat_blocks: B) -> Self
    where
        T: Iterator<Item = Range<usize>> + Clone,
    {
        let mut tree = Self { flat_blocks };

        // Set blocks at order 0 (level = MAX_ORDER) in the holes to used & set
        // their parents accordingly. This is implemented by checking if the block falls
        // completely within a usable memory area.
        // #[allow(unused_variables)]
        // let mut block_begin: usize = 0;
        //
        // //  The bottom-level
        //
        // for block_index in (1 << MAX_ORDER)..(1 << (MAX_ORDER + 1)) {
        //     // let block_end = block_begin + (1 << BASE_ORDER) - 1;
        //
        //     // if !(usable.clone())
        //     //     .any(|area| (area.contains(&block_begin) && area.contains(&block_end)))
        //     // {
        //     //     *tree.block_mut(block_index - 1) = Block::new_used();
        //     // } else {
        //     //     *tree.block_mut(block_index - 1) = Block::new_free(0);
        //     // }
        //
        //     *tree.block_mut(block_index - 1) = Block::new_free(0);
        //
        //     block_begin += 1 << (BASE_ORDER);
        // }
        //
        // let mut start: usize = 1 << (MAX_ORDER - 1);
        // for order in 1..=MAX_ORDER {
        //     for node_index in start..(start + blocks_in_level(MAX_ORDER - order)) {
        //         tree.update_block(node_index, order);
        //     }
        //
        //     start >>= 1;
        // }

        // WARNING: it is hardcoded for 16GB for 4k PAGE_SIZE based systems
        #[cfg(all(target_arch = "x86_64"))]
        unsafe {
            let dst = tree.block_mut(0) as *mut Block as *mut u8;
            write_bytes(dst.add(0), 23, 1);
            write_bytes(dst.add(1), 22, 2);
            write_bytes(dst.add(3), 21, 4);
            write_bytes(dst.add(7), 20, 8);
            write_bytes(dst.add(15), 19, 16);
            write_bytes(dst.add(31), 18, 32);
            write_bytes(dst.add(63), 17, 64);
            write_bytes(dst.add(127), 16, 128);
            write_bytes(dst.add(255), 15, 256);
            write_bytes(dst.add(511), 14, 512);
            write_bytes(dst.add(1023), 13, 1024);
            write_bytes(dst.add(2047), 12, 2048);
            write_bytes(dst.add(4095), 11, 4096);
            write_bytes(dst.add(8191), 10, 8192);
            write_bytes(dst.add(16383), 9, 16384);
            write_bytes(dst.add(32767), 8, 32768);
            write_bytes(dst.add(65535), 7, 65536);
            write_bytes(dst.add(131071), 6, 131072);
            write_bytes(dst.add(262143), 5, 262144);
            write_bytes(dst.add(524287), 4, 524288);
            write_bytes(dst.add(1048575), 3, 1048576);
            write_bytes(dst.add(2097151), 2, 2097152);
            write_bytes(dst.add(4194303), 1, 4194304);
        }

        #[cfg(all(target_arch = "aarch64", target_os = "macos"))]
        // WARNING: it is hardcoded for 16k PAGE_SIZE based systems
        unsafe {
            let dst = tree.block_mut(0) as *mut Block as *mut u8;
            write_bytes(dst.add(0), 21, 1);
            write_bytes(dst.add(1), 20, 2);
            write_bytes(dst.add(3), 19, 4);
            write_bytes(dst.add(7), 18, 8);
            write_bytes(dst.add(15), 17, 16);
            write_bytes(dst.add(31), 16, 32);
            write_bytes(dst.add(63), 15, 64);
            write_bytes(dst.add(127), 14, 128);
            write_bytes(dst.add(255), 13, 256);
            write_bytes(dst.add(511), 12, 512);
            write_bytes(dst.add(1023), 11, 1024);
            write_bytes(dst.add(2047), 10, 2048);
            write_bytes(dst.add(4095), 9, 4096);
            write_bytes(dst.add(8191), 8, 8192);
            write_bytes(dst.add(16383), 7, 16384);
            write_bytes(dst.add(32767), 6, 32768);
            write_bytes(dst.add(65535), 5, 65536);
            write_bytes(dst.add(131071), 4, 131072);
            write_bytes(dst.add(262143), 3, 262144);
            write_bytes(dst.add(524287), 2, 524288);
            write_bytes(dst.add(1048575), 1, 1048576);
        }

        tree
    }

    pub fn print_tree(&self) {
        let start: usize = 1 << (MAX_ORDER - 1);
        for order in 0..MAX_ORDER {
            for node_index in start..(start + blocks_in_level(MAX_ORDER - order)) {
                print!("{} ", self.block(node_index).order_free);
            }
        }
    }

    // pub fn serialize_tree(&self) {
    //     let sz = core::mem::size_of::<[Block; BLOCKS_IN_TREE]>();
    //     println!("{:x} bytes of array:", sz);
    //     let view = &self.flat_blocks[0] as *const _ as *const u8;
    //     let mut last = unsafe { view.add(0).read() };
    //     let mut cnt = 1;
    //     let mut sum = 0;
    //     for i in 1..sz {
    //         // print!("0x{:02x}, ", unsafe { view.add(i).read() });
    //         let x = unsafe { view.add(i).read() };
    //         if x == last {
    //             cnt += 1;
    //         } else {
    //             // println!("{}: {}", last, cnt);
    //             // println!("write_bytes(dst.add({}), {}, {});", sum, last, cnt);
    //             sum += cnt;
    //             cnt = 1;
    //             last = x;
    //         }
    //     }
    //     // println!("{}: {}", last, cnt);
    //     // println!("write_bytes(dst.add({}), {}, {});", sum, last, cnt);
    //     sum += cnt;
    //     // println!("sum: 0x{:x}", sum);
    // }

    pub const fn blocks_in_level(order: u8) -> usize {
        (1 << (BASE_ORDER + order) as usize) / (1 << (BASE_ORDER as usize))
    }

    #[inline]
    fn block_mut(&mut self, index: usize) -> &mut Block {
        debug_assert!(
            index < blocks_in_tree(LEVEL_COUNT),
            "index: 0x{:x}, blocks_in_tree: 0x{:x}",
            index,
            blocks_in_tree(LEVEL_COUNT)
        );
        &mut self.flat_blocks[index]
    }

    #[inline]
    fn block(&self, index: usize) -> &Block {
        debug_assert!(index < blocks_in_tree(LEVEL_COUNT));
        &self.flat_blocks[index]
    }

    /// Allocate a chunk of memory with desired order
    ///
    /// returns a pointer relative to the tree
    pub fn allocate(&mut self, desired_order: u8) -> Option<*const u8> {
        // self.serialize_tree();
        let root = self.block_mut(0);

        // If the root node has no orders free, or if it does not have the desired order free
        if root.order_free == 0 || (root.order_free - 1) < desired_order {
            return None;
        }

        let mut addr: usize = 0;
        let mut node_index = 1;

        let max_level = MAX_ORDER - desired_order;

        for level in 0..max_level {
            let left_child_index = flat_tree::left_child(node_index);
            let left_child = self.block(left_child_index - 1);

            let o = left_child.order_free;
            // If the child is not used (o!=0) or (desired_order in o-1)
            // Due to the +1 offset, we need to subtract 1 from 0:
            // However, (o - 1) >= desired_order can be simplified to o > desired_order
            node_index = if o != 0 && o > desired_order {
                left_child_index
            } else {
                // Move over to the right: if the parent had a free order and the left didn't,
                // the right must, or the parent is invalid and does not uphold invariants
                // Since the address is moving from the left hand side, we need to increase it
                // Block size in bytes = 2^(BASE_ORDER + order)
                // We also only want to allocate on the order of the child, hence subtracting 1
                addr += 1 << ((Self::MAX_ORDER_SIZE - level as usize - 1) as usize);
                left_child_index + 1
            };
        }

        let block = self.block_mut(node_index - 1);
        block.order_free = 0;

        // Iterate upwards and set parents accordingly
        // TODO: we can call self.update_blocks_above directly
        for _ in 0..max_level {
            // Treat as right index because we need to be 0 indexed here!
            // If we exclude the last bit, we'll always get an even number (the left node while 1 indexed)
            let right_index = node_index & !1;
            node_index = flat_tree::parent(node_index);

            let left = self.block(right_index - 1).order_free;
            let right = self.block(right_index).order_free;

            self.block_mut(node_index - 1).order_free = cmp::max(left, right);
        }

        Some(addr as *const u8)
    }

    /// Deallocating a block of memory
    ///
    /// * `ptr` - a pointer `relative` to the buddy tree
    /// * `order` - the corresponding order
    pub fn deallocate(&mut self, ptr: *mut u8, order: u8) {
        //TODO: check whether the pointer and order are match
        debug_assert!(order <= MAX_ORDER, "Block order > maximum order!");
        let level = MAX_ORDER - order;
        let level_offset = blocks_in_tree(level);
        let index = level_offset + ((ptr as usize) >> (order + BASE_ORDER)) + 1;

        debug_assert!(
            index < BLOCKS_IN_TREE,
            "Block index {} out of bounds!",
            index
        );

        debug_assert_eq!(
            self.block(index - 1).order_free,
            0,
            "[-] double-free detected! Block to free (index {}) must be used!",
            index,
        );

        self.block_mut(index - 1).order_free = order + 1;

        self.update_blocks_above(index, order);
    }

    /// Update a block from its children
    #[inline]
    fn update_block(&mut self, node_index: usize, order: u8) {
        debug_assert!(
            order != 0,
            "Order 0 does not have children and thus cannot be updated from them!"
        );

        debug_assert!(node_index != 0, "Node index 0 is invalid in 1 index tree!");

        // The ZERO indexed left child index
        let left_index = flat_tree::left_child(node_index) - 1;

        let left = self.block(left_index).order_free;
        let right = self.block(left_index + 1).order_free;

        if (left == order) && (right == order) {
            // Merge blocks
            self.block_mut(node_index - 1).order_free = order + 1;
        } else {
            self.block_mut(node_index - 1).order_free = cmp::max(left, right);
        }
    }

    #[inline]
    fn update_blocks_above(&mut self, index: usize, order: u8) {
        let mut node_index = index;

        // Iterate upwards and set parents accordingly
        for order in order + 1..=MAX_ORDER {
            node_index = flat_tree::parent(node_index);
            self.update_block(node_index, order);
        }
    }
}
