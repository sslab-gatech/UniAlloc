use std::hint::black_box;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::Instant;
use unialloc::alloc_api::type_isolation::{
    __unialloc_typeiso_pointer_filter_bench_count, __unialloc_typeiso_pointer_filter_bench_index,
    __unialloc_typeiso_pointer_filter_bench_query,
    __unialloc_typeiso_pointer_filter_bench_round_trip,
};

const CPU_MASK_BYTES: usize = 128;
const POINTER: usize = 0x3000;

#[cfg(target_os = "linux")]
fn pin_current_thread(cpu: usize) {
    extern "C" {
        fn sched_setaffinity(pid: i32, cpusetsize: usize, mask: *const u8) -> i32;
    }

    assert!(cpu < CPU_MASK_BYTES * 8, "CPU index exceeds benchmark mask");
    let mut mask = [0u8; CPU_MASK_BYTES];
    mask[cpu / 8] |= 1u8 << (cpu % 8);
    let result = unsafe { sched_setaffinity(0, mask.len(), mask.as_ptr()) };
    if result != 0 {
        panic!(
            "sched_setaffinity({cpu}) failed: {}",
            std::io::Error::last_os_error()
        );
    }
}

#[cfg(not(target_os = "linux"))]
fn pin_current_thread(_cpu: usize) {
    panic!("typeiso_pointer_filter_bench requires Linux sched_setaffinity");
}

#[derive(Clone, Copy)]
enum Work {
    RecoveryRoundTrip,
    RetainedRoundTrip,
    NegativeQuery,
}

fn run_worker(work: Work, iterations: usize) -> usize {
    match work {
        Work::RecoveryRoundTrip => {
            __unialloc_typeiso_pointer_filter_bench_round_trip(POINTER, false, iterations);
            0
        }
        Work::RetainedRoundTrip => {
            __unialloc_typeiso_pointer_filter_bench_round_trip(POINTER, true, iterations);
            0
        }
        Work::NegativeQuery => __unialloc_typeiso_pointer_filter_bench_query(POINTER, iterations),
    }
}

fn main() {
    let mut args = std::env::args().skip(1);
    let scenario = args.next().expect("missing scenario");
    let iterations: usize = args
        .next()
        .expect("missing iterations")
        .parse()
        .expect("iterations must be an integer");
    let first_cpu: usize = args
        .next()
        .expect("missing first CPU")
        .parse()
        .expect("first CPU must be an integer");
    let second_cpu: usize = args
        .next()
        .expect("missing second CPU")
        .parse()
        .expect("second CPU must be an integer");
    let coordinator_cpu: usize = args
        .next()
        .expect("missing coordinator CPU")
        .parse()
        .expect("coordinator CPU must be an integer");
    assert!(args.next().is_none(), "unexpected extra arguments");
    assert!(iterations > 0, "iterations must be positive");
    pin_current_thread(coordinator_cpu);

    let (work, cpus): (Vec<Work>, Vec<usize>) = match scenario.as_str() {
        "mixed_same_hash_2t" => {
            assert_ne!(first_cpu, second_cpu, "worker CPUs must differ");
            (
                vec![Work::RecoveryRoundTrip, Work::RetainedRoundTrip],
                vec![first_cpu, second_cpu],
            )
        }
        "single_recovery_1t" => (vec![Work::RecoveryRoundTrip], vec![first_cpu]),
        "single_retained_1t" => (vec![Work::RetainedRoundTrip], vec![first_cpu]),
        "negative_query_1t" => (vec![Work::NegativeQuery], vec![first_cpu]),
        _ => panic!("unknown scenario: {}", scenario),
    };

    assert_eq!(
        __unialloc_typeiso_pointer_filter_bench_count(POINTER, false),
        0,
        "recovery benchmark counter must start empty"
    );
    assert_eq!(
        __unialloc_typeiso_pointer_filter_bench_count(POINTER, true),
        0,
        "retained benchmark counter must start empty"
    );

    let worker_count = work.len();
    let ready = Arc::new(Barrier::new(worker_count + 1));
    let finished = Arc::new(Barrier::new(worker_count + 1));
    let start = Arc::new(AtomicBool::new(false));
    let mut workers = Vec::with_capacity(worker_count);
    for (work, cpu) in work.into_iter().zip(cpus) {
        let ready = Arc::clone(&ready);
        let finished = Arc::clone(&finished);
        let start = Arc::clone(&start);
        workers.push(thread::spawn(move || {
            pin_current_thread(cpu);
            ready.wait();
            while !start.load(Ordering::Acquire) {
                std::hint::spin_loop();
            }
            let query_hits = black_box(run_worker(work, iterations));
            finished.wait();
            query_hits
        }));
    }

    ready.wait();
    let started = Instant::now();
    start.store(true, Ordering::Release);
    finished.wait();
    let elapsed_ns = started.elapsed().as_nanos();
    let query_hits: usize = workers
        .into_iter()
        .map(|worker| worker.join().expect("pointer-filter worker failed"))
        .sum();
    let total_iterations = iterations * worker_count;
    assert_eq!(query_hits, 0, "negative query unexpectedly found an owner");
    assert_eq!(
        __unialloc_typeiso_pointer_filter_bench_count(POINTER, false),
        0,
        "recovery benchmark counter did not retire"
    );
    assert_eq!(
        __unialloc_typeiso_pointer_filter_bench_count(POINTER, true),
        0,
        "retained benchmark counter did not retire"
    );

    println!(
        "{{\"source\":\"typeiso_pointer_filter_bench\",\"scenario\":\"{}\",\"iterations_per_worker\":{},\"worker_count\":{},\"total_iterations\":{},\"first_cpu\":{},\"second_cpu\":{},\"coordinator_cpu\":{},\"recovery_index\":{},\"retained_index\":{},\"elapsed_ns\":{},\"ns_per_iteration\":{:.6},\"query_hits\":{},\"final_count\":0}}",
        scenario,
        iterations,
        worker_count,
        total_iterations,
        first_cpu,
        second_cpu,
        coordinator_cpu,
        __unialloc_typeiso_pointer_filter_bench_index(POINTER, false),
        __unialloc_typeiso_pointer_filter_bench_index(POINTER, true),
        elapsed_ns,
        elapsed_ns as f64 / total_iterations as f64,
        query_hits,
    );
}
