//! RUSTSEC-2022-0063 / linked_list_allocator 0.10.1.
//!
//! The advisory states that initializing a heap smaller than three machine
//! words can write allocator metadata beyond the supplied region. This public-
//! API adapter supplies an eight-byte heap on x86_64. ASan reports a heap-
//! buffer-overflow in 0.10.1; 0.10.2 rejects the same input with its new
//! minimum-size assertion before writing metadata outside the allocation.

use core::mem::MaybeUninit;
use linked_list_allocator::Heap;

fn main() {
    let mut memory = Box::new([MaybeUninit::<u8>::uninit(); 8]);
    let mut heap = Heap::empty();
    unsafe {
        heap.init(memory.as_mut_ptr().cast(), memory.len());
    }
    std::hint::black_box(&heap);
    drop(memory);
}
