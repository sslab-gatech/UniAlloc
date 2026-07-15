//! Process-isolated Type Isolation allocator hot-path probe.

use std::hint::black_box;
use std::time::Instant;
use unialloc::alloc_api::type_isolation::{
    __unialloc_semantic_scope_pop, __unialloc_semantic_scope_push,
    __unialloc_semantic_scope_push_local,
};

#[global_allocator]
static ALLOC: unialloc::UniAlloc = unialloc::UniAlloc;

const TYPE_ID: u64 = 0x5459_5045_4953_4f31;
const MODULE_ID: u64 = 0x4d4f_4455_4c45_3031;
const ALLOC_SITE: u64 = 0x414c_4c4f_4300_0001;
const DROP_SITE: u64 = 0x4452_4f50_0000_0002;
const FLAGS: u32 = 1;
const CPU_MASK_BYTES: usize = 128;

#[cfg(target_os = "linux")]
fn pin_current_thread(cpu: usize) {
    extern "C" {
        fn sched_setaffinity(pid: i32, cpusetsize: usize, mask: *const u8) -> i32;
    }

    assert!(cpu < CPU_MASK_BYTES * 8, "CPU index exceeds probe mask");
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
    panic!("this probe requires Linux sched_setaffinity");
}

#[inline(always)]
fn push_conservative(site: u64) {
    __unialloc_semantic_scope_push(TYPE_ID, MODULE_ID, FLAGS, site);
}

#[inline(always)]
fn push_local(site: u64) {
    __unialloc_semantic_scope_push_local(TYPE_ID, MODULE_ID, FLAGS, site);
}

#[inline(always)]
fn one_box(i: u64) -> Box<u64> {
    Box::new(black_box(i))
}

#[inline(always)]
fn one_box64(i: u64) -> Box<[u8; 64]> {
    let mut value = [0u8; 64];
    value[0] = i as u8;
    Box::new(black_box(value))
}

#[inline(never)]
fn run(scenario: &str, iterations: u64) -> u128 {
    let recovery_live = if scenario == "raw64_under_global_recovery" {
        let mut live = Vec::with_capacity(1024);
        for i in 0..1024u64 {
            push_conservative(ALLOC_SITE.wrapping_add(i));
            live.push(one_box(i));
            __unialloc_semantic_scope_pop();
        }
        Some(live)
    } else {
        None
    };
    let started = Instant::now();
    match scenario {
        "raw64_under_global_recovery" => {
            for i in 0..iterations {
                let value = one_box64(i);
                black_box(&*value);
                drop(value);
            }
        }
        "raw64" => {
            for i in 0..iterations {
                let value = one_box64(i);
                black_box(&*value);
                drop(value);
            }
        }
        "raw" => {
            for i in 0..iterations {
                let value = one_box(i);
                black_box(&*value);
                drop(value);
            }
        }
        "conservative_same" => {
            for i in 0..iterations {
                push_conservative(ALLOC_SITE);
                let value = one_box(i);
                black_box(&*value);
                drop(value);
                __unialloc_semantic_scope_pop();
            }
        }
        "local_same" => {
            for i in 0..iterations {
                push_local(ALLOC_SITE);
                let value = one_box(i);
                black_box(&*value);
                drop(value);
                __unialloc_semantic_scope_pop();
            }
        }
        "conservative_split" => {
            for i in 0..iterations {
                push_conservative(ALLOC_SITE);
                let value = one_box(i);
                __unialloc_semantic_scope_pop();
                black_box(&*value);
                push_conservative(DROP_SITE);
                drop(value);
                __unialloc_semantic_scope_pop();
            }
        }
        "conservative_unscoped_drop" => {
            for i in 0..iterations {
                push_conservative(ALLOC_SITE);
                let value = one_box(i);
                __unialloc_semantic_scope_pop();
                black_box(&*value);
                drop(value);
            }
        }
        _ => panic!("unknown scenario: {}", scenario),
    }
    let elapsed = started.elapsed().as_nanos();
    black_box(&recovery_live);
    drop(recovery_live);
    elapsed
}

fn main() {
    let mut args = std::env::args().skip(1);
    let scenario = args.next().expect("missing scenario");
    let iterations: u64 = args
        .next()
        .expect("missing iterations")
        .parse()
        .expect("iterations must be an integer");
    let cpu: usize = args
        .next()
        .expect("missing CPU")
        .parse()
        .expect("CPU must be an integer");
    assert!(args.next().is_none(), "unexpected extra arguments");
    assert!(iterations > 0, "iterations must be positive");
    pin_current_thread(cpu);
    let elapsed_ns = run(&scenario, iterations);
    println!(
        "{{\"source\":\"typeiso_allocator_hotpath\",\"scenario\":\"{}\",\"iterations\":{},\"cpu\":{},\"elapsed_ns\":{},\"ns_per_iteration\":{:.6}}}",
        scenario,
        iterations,
        cpu,
        elapsed_ns,
        elapsed_ns as f64 / iterations as f64,
    );
}
