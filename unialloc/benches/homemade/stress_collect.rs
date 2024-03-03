use core::iter::repeat;
use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion};
use rand::{self, Rng};
use std::time::{Duration, Instant};

const N_INSERTIONS: &'static [usize] = &[5000, 10000, 15000];

fn filter(ratio: u8, idx: usize) -> bool {
    idx % ratio as usize == 0
}

fn collect_test(c: &mut Criterion) {
    let mut group = c.benchmark_group("stressing_collect");
    let g = group.measurement_time(Duration::from_secs(15));

    for i in N_INSERTIONS {
        g.bench_with_input(BenchmarkId::new("Allocator", i), i, |b, &i| {
            b.iter_custom(|iters| {
                let mut total = Duration::from_secs(0);
                for x in 0..iters {
                    let start = Instant::now();
                    let num = (x * 10) % 4096;

                    let chars: Vec<_> = repeat('a').take(num as usize).collect();
                    let mut vv: Vec<char> = chars
                        .iter()
                        .enumerate()
                        .filter_map(|(i, c)| if filter(2, i) { Some(*c) } else { None })
                        .collect();

                    vv.pop();

                    total += start.elapsed();
                }
                total / iters as u32
            })
        });
    }
    group.finish();
}

criterion_group!(benches, collect_test);
