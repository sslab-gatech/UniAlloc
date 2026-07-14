use std::collections::VecDeque;
use std::time::Instant;

#[cfg(feature = "bench_scudo")]
mod scudo_runtime;

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
        static SNMALLOC: snmalloc_rs::SnMalloc = snmalloc_rs::SnMalloc;
    } else if #[cfg(feature = "bench_scudo")] {
        // The scudo_runtime constructor verifies System's allocation ABI is
        // actually interposed by Scudo before this benchmark can execute.
        use std::alloc::System;
        #[global_allocator]
        static SCUDO_SYSTEM: System = System;
    } else if #[cfg(feature = "bench_ptmalloc")] {
        use std::alloc::System;
        #[global_allocator]
        static SYSTEM: System = System;
    } else {
        #[cfg(feature = "fixed_heap")]
        use std::alloc::System;
        #[cfg(not(feature = "fixed_heap"))]
        use unialloc::UniAlloc;
        #[cfg(feature = "fixed_heap")]
        #[global_allocator]
        static SYSTEM_FOR_FIXED_HEAP_HARNESS: System = System;
        #[cfg(not(feature = "fixed_heap"))]
        #[global_allocator]
        static OURSELF: UniAlloc = UniAlloc;
    }
}

const VECDEQUE_LEN: i32 = 100000;
const WARMUP_N: usize = 100;
const BENCH_N: usize = 1000;

fn main() {
    let a: VecDeque<i32> = (0..VECDEQUE_LEN).collect();
    let b: VecDeque<i32> = (0..VECDEQUE_LEN).collect();

    for _ in 0..WARMUP_N {
        let mut c = a.clone();
        let mut d = b.clone();
        c.append(&mut d);
    }

    let mut durations = Vec::with_capacity(BENCH_N);

    for _ in 0..BENCH_N {
        let mut c = a.clone();
        let mut d = b.clone();
        let before = Instant::now();
        c.append(&mut d);
        let after = Instant::now();
        durations.push(after.duration_since(before));
    }

    let l = durations.len();
    durations.sort();

    assert!(BENCH_N % 2 == 0);
    let median = (durations[(l / 2) - 1] + durations[l / 2]) / 2;
    println!(
        "\ncustom-bench vec_deque_append {:?} ns/iter\n",
        median.as_nanos()
    );
}
