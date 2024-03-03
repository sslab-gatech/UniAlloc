#![cfg(not(target_os = "android"))]
#![feature(btree_drain_filter)]
#![feature(map_first_last)]
#![feature(repr_simd)]
#![feature(slice_partition_dedup)]
#![feature(test)]
use std::collections::VecDeque;
use test::{black_box, Bencher};

extern crate test;
include!("allocator.rs");

/// Returns a `rand::Rng` seeded with a consistent seed.
///
/// This is done to avoid introducing nondeterminism in benchmark results.
fn bench_rng() -> rand_xorshift::XorShiftRng {
    const SEED: [u8; 16] = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15];
    rand::SeedableRng::from_seed(SEED)
}

#[bench]
fn bench_new(b: &mut Bencher) {
    b.iter(|| {
        let ring: VecDeque<i32> = VecDeque::new();
        black_box(ring);
    })
}

#[bench]
fn bench_grow_1025(b: &mut Bencher) {
    b.iter(|| {
        let mut deq = VecDeque::new();
        for i in 0..1025 {
            deq.push_front(i);
        }
        black_box(deq);
    })
}

#[bench]
fn bench_iter_1000(b: &mut Bencher) {
    let ring: VecDeque<_> = (0..1000).collect();

    b.iter(|| {
        let mut sum = 0;
        for &i in &ring {
            sum += i;
        }
        black_box(sum);
    })
}

#[bench]
fn bench_mut_iter_1000(b: &mut Bencher) {
    let mut ring: VecDeque<_> = (0..1000).collect();

    b.iter(|| {
        let mut sum = 0;
        for i in &mut ring {
            sum += *i;
        }
        black_box(sum);
    })
}

#[bench]
fn bench_try_fold(b: &mut Bencher) {
    let ring: VecDeque<_> = (0..1000).collect();

    b.iter(|| black_box(ring.iter().try_fold(0, |a, b| Some(a + b))))
}

#[bench]
fn bench_from_array_1000(b: &mut Bencher) {
    const N: usize = 1000;
    let mut array: [usize; N] = [0; N];

    for i in 0..N {
        array[i] = i;
    }

    b.iter(|| {
        let deq: VecDeque<_> = array.into();
        black_box(deq);
    })
}

#[bench]
fn bench_extend_bytes(b: &mut Bencher) {
    let mut ring: VecDeque<u8> = VecDeque::with_capacity(1000);
    let input: &[u8] = &[128; 512];

    b.iter(|| {
        ring.clear();
        ring.extend(black_box(input));
    });
}

#[bench]
fn bench_extend_vec(b: &mut Bencher) {
    let mut ring: VecDeque<u8> = VecDeque::with_capacity(1000);
    let input = vec![128; 512];

    b.iter(|| {
        ring.clear();

        let input = input.clone();
        ring.extend(black_box(input));
    });
}
