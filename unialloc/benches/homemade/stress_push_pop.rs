use core::iter::repeat;
use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion};
use rand::{self, Rng};
use slaballoc::SecureTLSAllocator;
use std::alloc::System;
use std::time::{Duration, Instant};

static A: SecureTLSAllocator = SecureTLSAllocator;

const N_INSERTIONS: &'static [usize] = &[5000, 10000, 15000];

fn push_pop_test(c: &mut Criterion) {
    let mut group = c.benchmark_group("stressing_vector");
    let g = group.measurement_time(Duration::from_secs(15));

    for i in N_INSERTIONS {
        // g.bench_with_input(BenchmarkId::new("OurAllocator", i), i, |b, &i| {
        //     b.iter_custom(|iters| {
        //         let mut total = Duration::from_secs(0);
        //         for x in 0..iters {
        //             let start = Instant::now();
        //             // let r: u32 = rand::thread_rng().gen();
        //             // let mut num: usize = (r % 4096) as usize;
        //             let num = (x * 10) % 4096;
        //             // let num = (x * 10) % 8192;

        //             let mut v = Vec::<usize, _>::new_in(A);

        //             for _ in 0..num {
        //                 v.push(0);
        //             }

        //             for _ in 0..num {
        //                 v.pop();
        //             }

        //             total += start.elapsed();
        //         }
        //         total
        //     })
        // });

        g.bench_with_input(BenchmarkId::new("Allocator", i), i, |b, &i| {
            b.iter_custom(|iters| {
                let mut total = Duration::from_secs(0);
                for x in 0..iters {
                    let start = Instant::now();
                    // let r: u32 = rand::thread_rng().gen();
                    // let mut num: usize = (r % 4096) as usize;
                    let num = (x * 10) % 4096;
                    // let num = (x * 10) % 8192;

                    let mut v = Vec::<usize>::new();

                    for _ in 0..num {
                        v.push(0);
                    }

                    for _ in 0..num {
                        v.pop();
                    }

                    total += start.elapsed();
                }
                total / iters as u32
            })
        });
    }
    group.finish();
}

criterion_group!(benches, push_pop_test);
