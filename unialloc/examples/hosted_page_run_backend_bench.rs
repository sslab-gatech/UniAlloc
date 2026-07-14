use std::alloc::{GlobalAlloc, Layout};
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::{Duration, Instant};

use unialloc::UniAlloc;

const FRAGMENT_SLOTS: usize = 1024;
const SLOTS_PER_ARENA: usize = 16;
const FRAGMENT_PAGES: usize = 8;
const COALESCED_PAGES: usize = 32;

static ALLOCATOR: UniAlloc = UniAlloc::new();

#[inline]
unsafe fn page_alloc(pages: usize) -> *mut u8 {
    let layout = Layout::from_size_align(pages * unialloc::PAGE_SIZE, unialloc::PAGE_SIZE)
        .expect("valid page-run layout");
    let ptr = GlobalAlloc::alloc(&ALLOCATOR, layout);
    assert!(!ptr.is_null(), "allocation failed for {} pages", pages);
    std::ptr::read_volatile(&ptr)
}

#[inline]
unsafe fn page_free(ptr: *mut u8, pages: usize) {
    let layout = Layout::from_size_align(pages * unialloc::PAGE_SIZE, unialloc::PAGE_SIZE)
        .expect("valid page-run layout");
    GlobalAlloc::dealloc(&ALLOCATOR, ptr, layout);
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

/// Keeps one 8-page guard live in each expected 512KiB arena. That isolates
/// adjacent-run reconstruction from whole-empty-arena retirement.
fn guarded_coalesce_and_refill(rounds: usize) -> (Duration, usize) {
    let free_order: Vec<usize> = shuffled_indices(FRAGMENT_SLOTS)
        .into_iter()
        .filter(|idx| idx % SLOTS_PER_ARENA != 0)
        .collect();
    let arena_count = FRAGMENT_SLOTS / SLOTS_PER_ARENA;
    let coalesced_slots_per_arena = (SLOTS_PER_ARENA - 1) * FRAGMENT_PAGES / COALESCED_PAGES;
    let coalesced_slots = arena_count * coalesced_slots_per_arena;
    let mut small = Vec::with_capacity(FRAGMENT_SLOTS);
    let mut large = Vec::with_capacity(coalesced_slots);
    for _ in 0..FRAGMENT_SLOTS {
        small.push(unsafe { page_alloc(FRAGMENT_PAGES) });
    }

    let mut cycle = || {
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
        for idx in free_order.iter().copied() {
            small[idx] = unsafe { page_alloc(FRAGMENT_PAGES) };
        }
    };
    for _ in 0..10 {
        cycle();
    }

    let started = Instant::now();
    for _ in 0..rounds {
        cycle();
    }
    let elapsed = started.elapsed();
    for ptr in small {
        unsafe { page_free(ptr, FRAGMENT_PAGES) };
    }

    let operations_per_round = free_order.len() * 2 + coalesced_slots * 2;
    (elapsed, rounds * operations_per_round)
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
            "all" | "single" | "same" | "fragmented" | "coalesce" | "contended"
        ),
        "usage: hosted_page_run_backend_bench [all|single|same|fragmented|coalesce|contended]"
    );
    println!(
        "{{\"backend\":\"{}\",\"page_size\":{}}}",
        unialloc::platform_allocator_backend(),
        unialloc::PAGE_SIZE
    );

    if matches!(mode.as_str(), "all" | "single" | "same") {
        let _ = same_run_reuse(2_000);
        benchmark("same_run_reuse", 7, || same_run_reuse(200_000));
    }
    if matches!(mode.as_str(), "all" | "single" | "fragmented") {
        benchmark("fragmented_exact_reuse", 7, || fragmented_exact_reuse(100));
    }
    if matches!(mode.as_str(), "all" | "single" | "coalesce") {
        for _ in 0..10 {
            let _ = guarded_coalesce_and_refill(30);
        }
        benchmark("guarded_coalesce_and_refill", 7, || {
            guarded_coalesce_and_refill(30)
        });
    }
    if matches!(mode.as_str(), "all" | "contended") {
        benchmark("contended_same_run_4t", 7, || contended_same_run(4, 50_000));
    }

    #[cfg(all(
        feature = "adaptive_bitmap_page_allocator",
        feature = "stats",
        not(feature = "fixed_heap")
    ))]
    {
        let stats = unialloc::adaptive_page_run_stats_snapshot();
        println!(
            "{{\"adaptive_stats\":{{\"freelist_allocations\":{},\"bitmap_allocations\":{},\"bitmap_fallbacks\":{},\"freelist_deallocations\":{},\"owned_deallocations\":{},\"bridge_signals\":{},\"bridge_activations\":{},\"contention_signals\":{},\"contention_activations\":{}}}}}",
            stats.freelist_allocations,
            stats.bitmap_allocations,
            stats.bitmap_fallbacks,
            stats.freelist_deallocations,
            stats.owned_deallocations,
            stats.bridge_signals,
            stats.bridge_activations,
            stats.contention_signals,
            stats.contention_activations,
        );
    }
}
