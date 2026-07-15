//! Marker-free ownership workload for compiler heap-lifetime inference.

use std::hint::black_box;
use std::mem;
use std::thread;
use std::time::Instant;

use unialloc::{
    lifetime_hugepage_configure, lifetime_hugepage_stats_reset, lifetime_hugepage_stats_snapshot,
    semantic_stats_recording_enable, semantic_stats_reset, semantic_stats_snapshot,
    LifetimeHugepagePolicy, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const PAYLOAD_BYTES: usize = 4096;
const RESULT_PREFIX: &str = "UNIALLOC_COMPILER_HEAP_LIFETIME_RESULT=";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Arm {
    Default,
    RuntimeAdaptive,
    HintsOrdinary,
    HintsThp,
}

impl Arm {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "default" => Ok(Self::Default),
            "runtime-adaptive" => Ok(Self::RuntimeAdaptive),
            "hints-ordinary" => Ok(Self::HintsOrdinary),
            "hints-thp" => Ok(Self::HintsThp),
            _ => Err(format!("unknown arm {value:?}")),
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::Default => "default",
            Self::RuntimeAdaptive => "runtime-adaptive",
            Self::HintsOrdinary => "hints-ordinary",
            Self::HintsThp => "hints-thp",
        }
    }
}

#[derive(Default)]
struct SmapsRollup {
    rss_kib: usize,
    pss_kib: usize,
    private_dirty_kib: usize,
    anon_hugepages_kib: usize,
    hugetlb_kib: usize,
}

fn parse_kib(line: &str) -> usize {
    line.split_whitespace()
        .nth(1)
        .and_then(|value| value.parse().ok())
        .unwrap_or(0)
}

fn smaps_rollup() -> SmapsRollup {
    let mut sample = SmapsRollup::default();
    let Ok(text) = std::fs::read_to_string("/proc/self/smaps_rollup") else {
        return sample;
    };
    for line in text.lines() {
        if line.starts_with("Rss:") {
            sample.rss_kib = parse_kib(line);
        } else if line.starts_with("Pss:") {
            sample.pss_kib = parse_kib(line);
        } else if line.starts_with("Private_Dirty:") {
            sample.private_dirty_kib = parse_kib(line);
        } else if line.starts_with("AnonHugePages:") {
            sample.anon_hugepages_kib = parse_kib(line);
        } else if line.starts_with("Hugetlb:") {
            sample.hugetlb_kib = parse_kib(line);
        }
    }
    sample
}

#[inline(never)]
fn exact_local_drop() {
    let owner = Box::new([0_u8; PAYLOAD_BYTES]);
    drop(owner);
}

#[inline(never)]
fn exact_move_chain_drop() {
    let first = Box::new([0_u8; PAYLOAD_BYTES]);
    let second = first;
    let third = second;
    drop(third);
}

#[inline(never)]
fn exact_mem_forget() {
    let owner = Box::new([0_u8; PAYLOAD_BYTES]);
    mem::forget(owner);
}

#[inline(never)]
fn exact_box_leak(value: u8) -> &'static mut [u8; PAYLOAD_BYTES] {
    let owner = Box::new([value; PAYLOAD_BYTES]);
    Box::leak(owner)
}

#[inline(never)]
fn ambiguous_fallback(branch: bool) {
    let owner = Box::new([0_u8; PAYLOAD_BYTES]);
    if branch {
        drop(owner);
    } else {
        mem::forget(owner);
    }
}

fn mix(mut state: u64, value: u64) -> u64 {
    state ^= value.wrapping_add(0x9e37_79b9_7f4a_7c15);
    state = state.rotate_left(17).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    state ^ (state >> 31)
}

fn configure(arm: Arm) -> Result<(), String> {
    if !lifetime_hugepage_stats_reset() {
        return Err("lifetime arena was not clean before configuration".to_owned());
    }
    let configured = match arm {
        Arm::Default => lifetime_hugepage_configure(LifetimeHugepagePolicy::Disabled),
        Arm::RuntimeAdaptive => {
            lifetime_hugepage_configure(LifetimeHugepagePolicy::AdaptiveRuntimeHugepage)
        }
        Arm::HintsOrdinary => {
            lifetime_hugepage_configure(LifetimeHugepagePolicy::CompilerInferredOrdinary)
        }
        Arm::HintsThp => {
            lifetime_hugepage_configure(LifetimeHugepagePolicy::CompilerInferredHugepage)
        }
    };
    if configured {
        Ok(())
    } else {
        Err("lifetime policy configuration was rejected".to_owned())
    }
}

fn parse_args() -> Result<(Arm, usize, usize, u64, u64), String> {
    let mut arm = None;
    let mut iterations = 4096usize;
    let mut touch_passes = 64usize;
    let mut thp_settle_ms = 0u64;
    let mut seed = 0x2026_0715_u64;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--arm" => {
                let value = args.next().ok_or("--arm requires a value")?;
                arm = Some(Arm::parse(&value)?);
            }
            "--iterations" => {
                let value = args.next().ok_or("--iterations requires a value")?;
                iterations = value
                    .parse()
                    .map_err(|error| format!("invalid --iterations: {error}"))?;
            }
            "--touch-passes" => {
                let value = args.next().ok_or("--touch-passes requires a value")?;
                touch_passes = value
                    .parse()
                    .map_err(|error| format!("invalid --touch-passes: {error}"))?;
            }
            "--thp-settle-ms" => {
                let value = args.next().ok_or("--thp-settle-ms requires a value")?;
                thp_settle_ms = value
                    .parse()
                    .map_err(|error| format!("invalid --thp-settle-ms: {error}"))?;
            }
            "--seed" => {
                let value = args.next().ok_or("--seed requires a value")?;
                seed = value
                    .parse()
                    .map_err(|error| format!("invalid --seed: {error}"))?;
            }
            _ => return Err(format!("unknown argument {arg:?}")),
        }
    }
    if iterations == 0 || touch_passes == 0 {
        return Err("--iterations and --touch-passes must be positive".to_owned());
    }
    Ok((
        arm.ok_or("--arm is required")?,
        iterations,
        touch_passes,
        thp_settle_ms,
        seed,
    ))
}

fn greatest_common_divisor(mut left: usize, mut right: usize) -> usize {
    while right != 0 {
        let remainder = left % right;
        left = right;
        right = remainder;
    }
    left
}

fn coprime_stride(length: usize) -> usize {
    if length <= 1 {
        return 1;
    }
    let mut stride = (length / 2) | 1;
    while greatest_common_divisor(stride, length) != 1 {
        stride = stride.saturating_add(2);
    }
    stride
}

fn run() -> Result<(), String> {
    let (arm, iterations, touch_passes, thp_settle_ms, seed) = parse_args()?;
    configure(arm)?;
    semantic_stats_reset();
    semantic_stats_recording_enable();

    let mut leaked = Vec::<*mut u8>::with_capacity(iterations);
    let allocation_start = Instant::now();
    let mut checksum = seed;
    for index in 0..iterations {
        exact_local_drop();
        checksum = mix(checksum, (index as u64) << 3);

        exact_move_chain_drop();
        checksum = mix(checksum, ((index as u64) << 3) | 1);

        exact_mem_forget();
        checksum = mix(checksum, ((index as u64) << 3) | 2);

        leaked.push(exact_box_leak((index as u8).wrapping_add(1)).as_mut_ptr());
        checksum = mix(checksum, ((index as u64) << 3) | 3);

        ambiguous_fallback(index & 1 == 0);
        checksum = mix(checksum, ((index as u64) << 3) | 4);
    }
    let allocation_ns = allocation_start.elapsed().as_nanos();

    let link_start = Instant::now();
    let stride = coprime_stride(leaked.len());
    for index in 0..leaked.len() {
        let next = leaked[(index + stride) % leaked.len()];
        unsafe {
            std::ptr::write_unaligned(leaked[index] as *mut usize, next as usize);
        }
    }
    let link_ns = link_start.elapsed().as_nanos();

    if thp_settle_ms != 0 {
        thread::sleep(std::time::Duration::from_millis(thp_settle_ms));
    }
    let pre_touch_smaps = smaps_rollup();

    let touch_start = Instant::now();
    let mut current = leaked[0];
    for _ in 0..touch_passes.saturating_mul(leaked.len()) {
        unsafe {
            current = std::ptr::read_unaligned(current as *const usize) as *mut u8;
            checksum = mix(
                checksum,
                u64::from(std::ptr::read_volatile(current.add(64))),
            );
        }
    }
    let touch_ns = touch_start.elapsed().as_nanos();
    black_box(checksum);
    black_box(current);
    let workload_ns = allocation_ns
        .saturating_add(link_ns)
        .saturating_add(touch_ns);

    let semantic = semantic_stats_snapshot();
    let lifetime = lifetime_hugepage_stats_snapshot();
    let smaps = smaps_rollup();
    println!(
        concat!(
            "{}{{\"schema_version\":1,\"source\":\"compiler_heap_lifetime_workload\",",
            "\"passed\":true,\"arm\":\"{}\",\"iterations\":{},",
            "\"allocation_operations\":{},\"touch_operations\":{},\"operations\":{},",
            "\"payload_bytes\":{},\"touch_passes\":{},\"thp_settle_ms\":{},",
            "\"checksum\":{},\"allocation_ns\":{},\"link_ns\":{},",
            "\"touch_ns\":{},\"workload_ns\":{},",
            "\"policy\":{},\"backend\":{},",
            "\"semantic_total_allocations\":{},\"semantic_typed_allocations\":{},",
            "\"semantic_fallback_allocations\":{},",
            "\"routed_allocations\":{},\"routed_deallocations\":{},",
            "\"unknown_bypasses\":{},\"ordinary_extent_mappings\":{},",
            "\"thp_extent_mappings\":{},\"thp_advice_attempts\":{},",
            "\"thp_advice_successes\":{},\"thp_advice_errors\":{},",
            "\"thp_collapse_attempts\":{},\"thp_collapse_successes\":{},",
            "\"mapping_failures\":{},\"nohugepage_advice_failures\":{},",
            "\"peak_extents\":{},\"current_extents\":{},",
            "\"live_objects\":{},\"live_slot_bytes\":{},",
            "\"compiler_inferred_unknown_or_unproven_bypasses\":{},",
            "\"compiler_inferred_proven_ephemeral_bypasses\":{},",
            "\"compiler_inferred_bypass_requested_bytes\":{},",
            "\"compiler_inferred_arena_lock_acquisitions\":{},",
            "\"compiler_inferred_direct_long_routes\":{},",
            "\"compiler_inferred_direct_long_routes_without_trailer\":{},",
            "\"compiler_inferred_direct_long_requested_bytes\":{},",
            "\"compiler_inferred_direct_long_slot_bytes\":{},",
            "\"compiler_inferred_direct_long_deallocations\":{},",
            "\"compiler_inferred_density_promotion_attempts\":{},",
            "\"compiler_inferred_density_promotion_successes\":{},",
            "\"compiler_inferred_density_promotion_errors\":{},",
            "\"smaps_rss_kib\":{},\"smaps_pss_kib\":{},",
            "\"smaps_private_dirty_kib\":{},\"pre_touch_anon_hugepages_kib\":{},",
            "\"smaps_anon_hugepages_kib\":{},",
            "\"smaps_hugetlb_kib\":{}}}"
        ),
        RESULT_PREFIX,
        arm.name(),
        iterations,
        iterations.saturating_mul(5),
        touch_passes.saturating_mul(iterations),
        iterations
            .saturating_mul(5)
            .saturating_add(touch_passes.saturating_mul(iterations)),
        PAYLOAD_BYTES,
        touch_passes,
        thp_settle_ms,
        checksum,
        allocation_ns,
        link_ns,
        touch_ns,
        workload_ns,
        lifetime.policy as u8,
        lifetime.backend as u8,
        semantic.total_allocations,
        semantic.typed_allocations,
        semantic.fallback_allocations,
        lifetime.routed_allocations,
        lifetime.routed_deallocations,
        lifetime.unknown_bypasses,
        lifetime.ordinary_extent_mappings,
        lifetime.thp_extent_mappings,
        lifetime.thp_advice_attempts,
        lifetime.thp_advice_successes,
        lifetime.thp_advice_errors,
        lifetime.thp_collapse_attempts,
        lifetime.thp_collapse_successes,
        lifetime.mapping_failures,
        lifetime.nohugepage_advice_failures,
        lifetime.peak_extents,
        lifetime.current_extents,
        lifetime.live_objects,
        lifetime.live_slot_bytes,
        lifetime.compiler_inferred_unknown_or_unproven_bypasses,
        lifetime.compiler_inferred_proven_ephemeral_bypasses,
        lifetime.compiler_inferred_bypass_requested_bytes,
        lifetime.compiler_inferred_arena_lock_acquisitions,
        lifetime.compiler_inferred_direct_long_routes,
        lifetime.compiler_inferred_direct_long_routes_without_trailer,
        lifetime.compiler_inferred_direct_long_requested_bytes,
        lifetime.compiler_inferred_direct_long_slot_bytes,
        lifetime.compiler_inferred_direct_long_deallocations,
        lifetime.compiler_inferred_density_promotion_attempts,
        lifetime.compiler_inferred_density_promotion_successes,
        lifetime.compiler_inferred_density_promotion_errors,
        smaps.rss_kib,
        smaps.pss_kib,
        smaps.private_dirty_kib,
        pre_touch_smaps.anon_hugepages_kib,
        smaps.anon_hugepages_kib,
        smaps.hugetlb_kib,
    );
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("compiler heap-lifetime workload failed: {error}");
        std::process::exit(2);
    }
}
