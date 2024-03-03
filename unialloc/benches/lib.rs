#![cfg(not(target_os = "android"))]
#![feature(btree_drain_filter)]
#![feature(map_first_last)]
#![feature(repr_simd)]
#![feature(slice_partition_dedup)]
#![feature(test)]

#[macro_use]
extern crate test;
#[macro_use]
extern crate alloc;

cfg_if::cfg_if! {
    if #[cfg(feature = "bench_jemalloc")] {
        use jemallocator::Jemalloc;
        #[global_allocator]
        static JEMALLOC: Jemalloc = Jemalloc;
    } else if #[cfg(feature = "bench_mimalloc")] {
        use mimalloc::MiMalloc;
        #[global_allocator]
        static MIMALLOC: MiMalloc = MiMalloc;
    } else if #[cfg(feature = "bench_tcmalloc")] {
        use tcmalloc::TCMalloc;
        #[global_allocator]
        static TCMALLOC: TCMalloc = TCMalloc;
    } else if #[cfg(feature = "bench_snmalloc")] {
        #[global_allocator]
        static ALLOC: snmalloc_rs::SnMalloc = snmalloc_rs::SnMalloc;
    } else {
        // default -> ourself
        use unialloc::UniAlloc;
        #[global_allocator]
        static OURSELF: UniAlloc = UniAlloc;
    }
}

mod binary_heap;
mod btree;
mod linked_list;
mod slice;
mod str;
mod string;
mod vec;
mod vec_deque;
