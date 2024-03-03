#![feature(allocator_api)]
#[macro_use]
extern crate bencher;

use bencher::Bencher;
use rand::{self, Rng};
use std::iter::repeat;
use std::{
    sync::{Arc, Barrier},
    thread,
    time::{Duration, Instant},
};

#[macro_use]
extern crate alloc;
cfg_if::cfg_if! {
    if #[cfg(feature = "bench_jemalloc")] {
        use jemallocator::Jemalloc;
        #[global_allocator]
        static JEMALLOC: Jemalloc = Jemalloc;
    } else if #[cfg(feature = "bench_ourself")] {
        use slaballoc::SecureTLSAllocator;
        #[global_allocator]
        static OURSELF: SecureTLSAllocator = SecureTLSAllocator;
    } else if #[cfg(feature = "bench_mimalloc")] {
        use mimalloc::MiMalloc;
        #[global_allocator]
        static MIMALLOC: MiMalloc = MiMalloc;
    } else if #[cfg(feature = "bench_tcmalloc")] {
        use tcmalloc::TCMalloc;
        #[global_allocator]
        static TCMALLOC: TCMalloc = TCMalloc;
    } else {}
}

const N_THREADS: &'static [usize] = &[512];

use alloc::alloc::{alloc, dealloc};
use core::alloc::{GlobalAlloc, Layout};

macro_rules! bench_gen {
    ($bench_name: ident, $inner: ident, $len: expr) => {
        fn $bench_name(b: &mut Bencher) {
            b.iter(|| $inner($len));
        }
    };
}

fn insert_remove_multithread(n_threads: usize) {
    let mut children = vec![];
    for _ in 0..n_threads {
        children.push(thread::spawn(move || {
            let mut v = vec![0; 8];
            v[0] = 3;

            let mut v2 = vec![0, 128];
            v2[0] = 3;

            let mut v3 = vec![0, 1024];
            v3[0] = 3;

            {
                let mut v2 = vec![0, 128];
                v2[0] = 3;
            }

            let mut vvv = Vec::new();
            let mut vvvv = Vec::new();
            let mut vvvvv = Vec::new();

            for i in 1..1024 {
                unsafe {
                    let layout = Layout::from_size_align(i % 64, 8).unwrap();
                    let x = alloc(layout);
                    vvv.push(x);
                }
            }

            for i in 1..1024 {
                unsafe {
                    let layout = Layout::from_size_align(i % 64, 8).unwrap();
                    dealloc(vvv[i - 1], layout);
                }
            }

            for i in 1..4096 {
                unsafe {
                    let layout = Layout::from_size_align(i % 512, 8).unwrap();
                    let x = alloc(layout);
                    vvvv.push(x);
                }
            }

            for i in 1..4096 {
                unsafe {
                    let layout = Layout::from_size_align(i % 512, 8).unwrap();
                    dealloc(vvvv[i - 1], layout);
                }
            }

            for i in 1..8192 {
                unsafe {
                    let layout = Layout::from_size_align(i % 1024, 8).unwrap();
                    let x = alloc(layout);
                    vvvvv.push(x);
                }
            }

            for i in 1..8192 {
                unsafe {
                    let layout = Layout::from_size_align(i % 1024, 8).unwrap();
                    dealloc(vvvvv[i - 1], layout);
                }
            }
        }));
    }
    for child in children {
        // Wait for the thread to finish. Returns a result.
        let _ = child.join();
    }
}

bench_gen!(multi2_4, insert_remove_multithread, 4);
bench_gen!(multi2_8, insert_remove_multithread, 8);
bench_gen!(multi2_16, insert_remove_multithread, 16);
bench_gen!(multi2_32, insert_remove_multithread, 32);
bench_gen!(multi2_64, insert_remove_multithread, 64);
bench_gen!(multi2_128, insert_remove_multithread, 128);
bench_gen!(multi2_256, insert_remove_multithread, 256);
bench_gen!(multi2_512, insert_remove_multithread, 512);
bench_gen!(multi2_1024, insert_remove_multithread, 1024);

benchmark_group!(
    benches,
    multi2_4,
    multi2_8,
    multi2_16,
    multi2_32,
    multi2_64,
    multi2_128,
    multi2_256,
    multi2_512,
    multi2_1024,
);

benchmark_main!(benches);
