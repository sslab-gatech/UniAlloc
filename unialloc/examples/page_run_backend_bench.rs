use std::alloc::{alloc_zeroed, dealloc, Layout};
use std::hint::black_box;
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::{Duration, Instant};

const HEAP_BYTES: usize = 256 * 1024 * 1024;
const FRAGMENT_SLOTS: usize = 1024;
const FRAGMENT_PAGES: usize = 8;
const COALESCED_PAGES: usize = 32;

struct HeapRange {
    ptr: *mut u8,
    layout: Layout,
}

impl HeapRange {
    fn new() -> Self {
        let layout = Layout::from_size_align(HEAP_BYTES, unialloc::PAGE_SIZE)
            .expect("valid benchmark heap layout");
        let ptr = unsafe { alloc_zeroed(layout) };
        assert!(!ptr.is_null(), "failed to reserve benchmark heap");
        Self { ptr, layout }
    }
}

impl Drop for HeapRange {
    fn drop(&mut self) {
        unsafe { dealloc(self.ptr, self.layout) };
    }
}

#[inline]
unsafe fn page_alloc(pages: usize) -> *mut u8 {
    let size = pages * unialloc::PAGE_SIZE;
    let ptr = unialloc::unialloc_alloc(size, unialloc::PAGE_SIZE);
    assert!(
        !ptr.is_null(),
        "fixed-heap allocation failed for {} pages",
        pages
    );
    black_box(ptr)
}

#[inline]
unsafe fn page_free(ptr: *mut u8, pages: usize) {
    unialloc::unialloc_dealloc(ptr, pages * unialloc::PAGE_SIZE, unialloc::PAGE_SIZE);
}

fn elapsed_per_op(elapsed: Duration, operations: usize) -> f64 {
    elapsed.as_secs_f64() * 1_000_000_000.0 / operations as f64
}

fn same_run_reuse(iterations: usize) -> (Duration, usize) {
    let started = Instant::now();
    for _ in 0..iterations {
        unsafe {
            let ptr = page_alloc(FRAGMENT_PAGES);
            page_free(ptr, FRAGMENT_PAGES);
        }
    }
    (started.elapsed(), iterations * 2)
}

fn fragmented_exact_reuse(rounds: usize) -> (Duration, usize) {
    let mut slots = Vec::with_capacity(FRAGMENT_SLOTS);
    for _ in 0..FRAGMENT_SLOTS {
        slots.push(unsafe { page_alloc(FRAGMENT_PAGES) });
    }

    let started = Instant::now();
    for round in 0..rounds {
        let parity = round & 1;
        for idx in (parity..FRAGMENT_SLOTS).step_by(2) {
            unsafe { page_free(slots[idx], FRAGMENT_PAGES) };
        }
        for idx in (parity..FRAGMENT_SLOTS).step_by(2) {
            slots[idx] = unsafe { page_alloc(FRAGMENT_PAGES) };
        }
    }
    let elapsed = started.elapsed();

    for ptr in slots {
        unsafe { page_free(ptr, FRAGMENT_PAGES) };
    }
    (elapsed, rounds * FRAGMENT_SLOTS)
}

fn shuffled_indices(len: usize) -> Vec<usize> {
    let mut indices: Vec<usize> = (0..len).collect();
    let mut random = 0x243f_6a88_u32;
    for idx in (1..len).rev() {
        random = random.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
        indices.swap(idx, random as usize % (idx + 1));
    }
    indices
}

fn coalesce_and_refill(rounds: usize) -> (Duration, usize) {
    let free_order = shuffled_indices(FRAGMENT_SLOTS);
    let coalesced_slots = FRAGMENT_SLOTS * FRAGMENT_PAGES / COALESCED_PAGES;
    let mut small = Vec::with_capacity(FRAGMENT_SLOTS);
    let mut large = Vec::with_capacity(coalesced_slots);
    let started = Instant::now();

    for _ in 0..rounds {
        small.clear();
        for _ in 0..FRAGMENT_SLOTS {
            small.push(unsafe { page_alloc(FRAGMENT_PAGES) });
        }
        for idx in free_order.iter().copied() {
            unsafe { page_free(small[idx], FRAGMENT_PAGES) };
        }

        large.clear();
        for _ in 0..coalesced_slots {
            large.push(unsafe { page_alloc(COALESCED_PAGES) });
        }
        for ptr in large.iter().copied() {
            unsafe { page_free(ptr, COALESCED_PAGES) };
        }
    }

    (
        started.elapsed(),
        rounds * (FRAGMENT_SLOTS * 2 + coalesced_slots * 2),
    )
}

fn contended_same_run(threads: usize, iterations_per_thread: usize) -> (Duration, usize) {
    let ready = Arc::new(Barrier::new(threads + 1));
    let started = thread::scope(|scope| {
        for _ in 0..threads {
            let ready = Arc::clone(&ready);
            scope.spawn(move || {
                ready.wait();
                for _ in 0..iterations_per_thread {
                    unsafe {
                        let ptr = page_alloc(FRAGMENT_PAGES);
                        page_free(ptr, FRAGMENT_PAGES);
                    }
                }
            });
        }
        let started = Instant::now();
        ready.wait();
        started
    });
    (started.elapsed(), threads * iterations_per_thread * 2)
}

fn print_result(name: &str, elapsed: Duration, operations: usize) {
    println!(
        "{{\"workload\":\"{}\",\"operations\":{},\"elapsed_ns\":{},\"ns_per_op\":{:.3}}}",
        name,
        operations,
        elapsed.as_nanos(),
        elapsed_per_op(elapsed, operations)
    );
}

fn benchmark<F>(name: &str, trials: usize, mut workload: F)
where
    F: FnMut() -> (Duration, usize),
{
    let mut samples = Vec::with_capacity(trials);
    let mut operations = 0usize;
    for _ in 0..trials {
        let (elapsed, trial_operations) = workload();
        print_result(name, elapsed, trial_operations);
        operations = trial_operations;
        samples.push(elapsed_per_op(elapsed, trial_operations));
    }
    samples.sort_by(|left, right| left.partial_cmp(right).unwrap());
    println!(
        "{{\"summary\":\"median\",\"workload\":\"{}\",\"trials\":{},\"operations_per_trial\":{},\"ns_per_op\":{:.3}}}",
        name,
        trials,
        operations,
        samples[trials / 2]
    );
}

fn main() {
    let mode = std::env::args().nth(1).unwrap_or_else(|| "all".to_owned());
    assert!(
        matches!(
            mode.as_str(),
            "all" | "same" | "fragmented" | "coalesce" | "contended"
        ),
        "usage: page_run_backend_bench [all|same|fragmented|coalesce|contended]"
    );

    let heap = HeapRange::new();
    let initialized = unsafe {
        unialloc::unialloc_fixed_heap_try_init(heap.ptr as usize, HEAP_BYTES, unialloc::PAGE_SIZE)
    };
    assert!(initialized, "fixed heap initialization failed");

    let backend = if cfg!(feature = "bitmap_page_allocator") {
        "segment_bitmap"
    } else {
        "intrusive_freelist"
    };
    println!(
        "{{\"backend\":\"{}\",\"heap_bytes\":{},\"page_size\":{}}}",
        backend,
        HEAP_BYTES,
        unialloc::PAGE_SIZE
    );

    if matches!(mode.as_str(), "all" | "same") {
        // Populate allocator metadata and code paths outside the measured region.
        let _ = same_run_reuse(2_000);
        benchmark("same_run_reuse", 7, || same_run_reuse(200_000));
    }
    if matches!(mode.as_str(), "all" | "fragmented") {
        benchmark("fragmented_exact_reuse", 7, || fragmented_exact_reuse(200));
    }
    if matches!(mode.as_str(), "all" | "coalesce") {
        benchmark("coalesce_and_refill", 7, || coalesce_and_refill(40));
    }
    if matches!(mode.as_str(), "all" | "contended") {
        benchmark("contended_same_run_4t", 7, || contended_same_run(4, 50_000));
    }
}
