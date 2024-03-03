#![feature(allocator_api)]

extern crate alloc;

use std::alloc::{GlobalAlloc, Layout};
use unialloc;
use unialloc::UniAlloc;

#[global_allocator]
static A: UniAlloc = UniAlloc;

fn main() {
    let heap_size: usize = 50usize * (1 << 12);
    unsafe {
        let heap = libc::mmap(
            core::ptr::null_mut(),
            heap_size,
            libc::PROT_READ | libc::PROT_WRITE,
            libc::MAP_PRIVATE | libc::MAP_ANONYMOUS,
            -1,
            0,
        ) as *mut u8;
        A.init(heap as usize, heap_size / 2, 1usize << 12);
    }
    let mut vvv = Vec::new();
    for i in 1..1024 {
        unsafe {
            let layout = Layout::from_size_align(i % 256 + 1, 8).unwrap();
            // assert!(layout.size() < 100);
            let x = A.alloc(layout);
            vvv.push(i);
        }
    }
    unsafe {
        A.extend(heap_size / 2, 1usize << 12);
    }
    let mut vvvv = Vec::new();
    for i in 1..1024 {
        unsafe {
            let layout = Layout::from_size_align(i % 1024 + 1, 8).unwrap();
            // assert!(layout.size() < 100);
            let x = A.alloc(layout);
            vvvv.push(i);
        }
    }
}
