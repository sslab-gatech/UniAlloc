#![no_std]
#![allow(unused_imports)]
#![allow(dead_code)]
#![allow(clippy::uninit_assumed_init)]
#![feature(allocator_api)]
#![feature(thread_local)]
#![feature(alloc_layout_extra)]
#![allow(incomplete_features)]
#![feature(ptr_internals)]
#![feature(core_intrinsics)]
#![feature(raw_vec_internals)]
#![feature(rustc_allow_const_fn_unstable)]
#![feature(dropck_eyepatch)]
#![feature(slice_ptr_get)]
#![feature(slice_ptr_len)]
#![feature(asm_const)]
#![feature(libc)]
#![feature(nonnull_slice_from_raw_parts)]
#![feature(const_mut_refs)]
#![feature(generic_const_exprs)]
#![feature(stdsimd)]
#![feature(portable_simd)]

#[macro_use]
mod buddy_system;
mod alloc_api;
// mod arena;
mod bg_thread;
mod bitmap_alloc;
mod cache;
mod collections;
mod error;
mod freelist;
mod mm;
mod mpmc;
mod page;
mod pal;
mod prelude;
mod sc;
mod size_class;
#[cfg(not(feature = "fixed_heap"))]
mod sync;
mod zone;

include!(concat!(env!("OUT_DIR"), "/consts.rs"));
extern crate alloc;

pub use cache::RustAllocator as UniAlloc;
pub use pal::arch::*;

// use core::panic::PanicInfo;

// #[panic_handler]
// fn panic1(info: &PanicInfo) -> ! {
//     println!("{}", info);
//     loop {}
// }
