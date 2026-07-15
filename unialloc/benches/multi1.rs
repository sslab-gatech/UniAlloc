use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion};
use rand::{self, Rng};
use std::{
    sync::{Arc, Barrier},
    thread,
    time::{Duration, Instant},
};

#[macro_use]
extern crate alloc;

#[cfg(feature = "bench_scudo")]
mod scudo_runtime;
#[cfg(feature = "bench_tcmalloc")]
mod google_tcmalloc;

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
        use google_tcmalloc::GoogleTcmalloc;
        #[global_allocator]
        static TCMALLOC: GoogleTcmalloc = GoogleTcmalloc;
    } else if #[cfg(feature = "bench_gperftools_legacy")] {
        use gperftools_tcmalloc::TCMalloc;
        #[global_allocator]
        static GPERFTOOLS_TCMALLOC: TCMalloc = TCMalloc;
    } else if #[cfg(feature = "bench_scudo")] {
        // The shared constructor rejects execution unless System resolves to Scudo.
        use std::alloc::System;
        #[global_allocator]
        static SCUDO_SYSTEM: System = System;
    } else {}
}

const N_INSERTIONS: &'static [usize] = &[100, 300, 500, 700, 1000, 3000, 5000, 10000, 15000];

fn insert_remove_multithread(c: &mut Criterion) {
    let mut group = c.benchmark_group("insert_remove_multithreaded");
    // let g = group.measurement_time(Duration::from_secs(15));

    for i in N_INSERTIONS {
        group.bench_with_input(BenchmarkId::new("ThreadBench1", i), i, |b, &i| {
            b.iter_custom(|iters| {
                let mut total = Duration::from_secs(0);
                for _ in 0..iters {
                    let start = Instant::now();
                    thread::spawn(move || {
                        let mut rng = rand::thread_rng();
                        let x: u32 = rng.gen();
                        let len: usize = (x % 257) as usize;
                        let v = vec![0; len];
                    });

                    total += start.elapsed();
                }
                total
            })
        });
    }
    group.finish();
}

criterion_group!(benches, insert_remove_multithread);
criterion_main!(benches);
