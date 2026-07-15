//! Eventually-freed lifetime/THP micro-workload.
//!
//! The persistent cohort is allocated before an allocation-heavy loop, touched
//! throughout the loop, and released with normal deallocation after the loop.
//! No object is leaked.  The `ordinary` and `selective-thp` arms use identical
//! allocation metadata and work; only the lifetime arena policy differs.

use std::alloc::{GlobalAlloc, Layout, System};
use std::env;
use std::fs;
use std::hint::black_box;
use std::thread;
use std::time::{Duration, Instant};

use unialloc::{
    lifetime_hugepage_advance_epoch, lifetime_hugepage_configure,
    lifetime_hugepage_configure_with_backend, lifetime_hugepage_stats_reset,
    lifetime_hugepage_stats_snapshot, AllocationMetadata, LifetimeHugepagePolicy,
    LifetimeHugepageStatsSnapshot, LifetimePageBackend, SemanticAlloc, UniAlloc,
    LIFETIME_HINT_EPHEMERAL, LIFETIME_HINT_LONG_LIVED,
};

const SHARED_TYPE_ID: u64 = 0xE7F1_0000_0000_0001;
const MODULE_ID: u64 = 0xE7F1_0000_0000_1000;
const LONG_CALLSITE: u64 = 0xE7F1_0000_0000_2001;
const SHORT_CALLSITE: u64 = 0xE7F1_0000_0000_2002;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Arm {
    System,
    PolicyOff,
    Ordinary,
    SelectiveThp,
    AdaptiveThp,
}

impl Arm {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "system" => Ok(Self::System),
            "policy-off" => Ok(Self::PolicyOff),
            "ordinary" => Ok(Self::Ordinary),
            "selective-thp" => Ok(Self::SelectiveThp),
            "adaptive-thp" => Ok(Self::AdaptiveThp),
            _ => Err(format!("unknown --arm value: {value}")),
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::System => "system",
            Self::PolicyOff => "policy-off",
            Self::Ordinary => "ordinary",
            Self::SelectiveThp => "selective-thp",
            Self::AdaptiveThp => "adaptive-thp",
        }
    }

    fn uses_unialloc(self) -> bool {
        self != Self::System
    }

    fn is_adaptive(self) -> bool {
        self == Self::AdaptiveThp
    }
}

#[derive(Debug)]
struct Config {
    arm: Arm,
    long_objects: usize,
    short_objects: usize,
    object_bytes: usize,
    waves: usize,
    passes_per_wave: usize,
    thp_deadline_ms: u64,
}

impl Config {
    fn parse() -> Result<Self, String> {
        let mut config = Self {
            arm: Arm::System,
            long_objects: 2048,
            short_objects: 2048,
            object_bytes: 4096,
            waves: 8,
            passes_per_wave: 2,
            thp_deadline_ms: 30_000,
        };
        let mut args = env::args().skip(1);
        while let Some(flag) = args.next() {
            let value = args
                .next()
                .ok_or_else(|| format!("missing value after {flag}"))?;
            match flag.as_str() {
                "--arm" => config.arm = Arm::parse(&value)?,
                "--long-objects" => config.long_objects = parse_usize(&flag, &value)?,
                "--short-objects" => config.short_objects = parse_usize(&flag, &value)?,
                "--object-bytes" => config.object_bytes = parse_usize(&flag, &value)?,
                "--waves" => config.waves = parse_usize(&flag, &value)?,
                "--passes-per-wave" => config.passes_per_wave = parse_usize(&flag, &value)?,
                "--thp-deadline-ms" | "--thp-wait-ms" => {
                    config.thp_deadline_ms = value
                        .parse()
                        .map_err(|error| format!("invalid {flag}: {error}"))?
                }
                _ => return Err(format!("unknown argument: {flag}")),
            }
        }
        if config.long_objects == 0
            || config.short_objects == 0
            || config.object_bytes == 0
            || config.waves == 0
            || config.passes_per_wave == 0
        {
            return Err("object counts, size, waves, and passes must be positive".to_owned());
        }
        Layout::from_size_align(config.object_bytes, 64)
            .map_err(|error| format!("invalid object layout: {error}"))?;
        Ok(config)
    }
}

fn parse_usize(flag: &str, value: &str) -> Result<usize, String> {
    value
        .parse()
        .map_err(|error| format!("invalid {flag}: {error}"))
}

#[derive(Clone, Copy, Debug, Default)]
struct MemorySnapshot {
    available: bool,
    rss_kib: usize,
    pss_kib: usize,
    private_dirty_kib: usize,
    anon_hugepages_kib: usize,
}

impl MemorySnapshot {
    fn read() -> Self {
        let text = match fs::read_to_string("/proc/self/smaps_rollup") {
            Ok(text) => text,
            Err(_) => return Self::default(),
        };
        Self {
            available: true,
            rss_kib: parse_kib(&text, "Rss:"),
            pss_kib: parse_kib(&text, "Pss:"),
            private_dirty_kib: parse_kib(&text, "Private_Dirty:"),
            anon_hugepages_kib: parse_kib(&text, "AnonHugePages:"),
        }
    }

    fn max(self, other: Self) -> Self {
        Self {
            available: self.available && other.available,
            rss_kib: self.rss_kib.max(other.rss_kib),
            pss_kib: self.pss_kib.max(other.pss_kib),
            private_dirty_kib: self.private_dirty_kib.max(other.private_dirty_kib),
            anon_hugepages_kib: self.anon_hugepages_kib.max(other.anon_hugepages_kib),
        }
    }
}

#[derive(Clone, Debug, Default)]
struct VmaSnapshot {
    available: bool,
    anon_hugepages_kib: usize,
    pss_kib: usize,
    private_dirty_kib: usize,
    vm_flags: String,
}

impl VmaSnapshot {
    fn for_address(address: usize) -> Self {
        let text = match fs::read_to_string("/proc/self/smaps") {
            Ok(text) => text,
            Err(_) => return Self::default(),
        };
        let mut in_target = false;
        let mut result = Self::default();
        for line in text.lines() {
            let head = line.split_whitespace().next().unwrap_or("");
            if let Some((start, end)) = head.split_once('-') {
                if let (Ok(start), Ok(end)) = (
                    usize::from_str_radix(start, 16),
                    usize::from_str_radix(end, 16),
                ) {
                    if in_target {
                        break;
                    }
                    in_target = start <= address && address < end;
                    if in_target {
                        result.available = true;
                    }
                    continue;
                }
            }
            if !in_target {
                continue;
            }
            if line.starts_with("AnonHugePages:") {
                result.anon_hugepages_kib = parse_kib(line, "AnonHugePages:");
            } else if line.starts_with("Pss:") {
                result.pss_kib = parse_kib(line, "Pss:");
            } else if line.starts_with("Private_Dirty:") {
                result.private_dirty_kib = parse_kib(line, "Private_Dirty:");
            } else if let Some(flags) = line.strip_prefix("VmFlags:") {
                result.vm_flags = flags.trim().to_owned();
            }
        }
        result
    }

    fn hugepage_advised(&self) -> bool {
        self.vm_flags.split_whitespace().any(|flag| flag == "hg")
    }

    fn max_backing(mut self, other: Self) -> Self {
        self.available &= other.available;
        self.anon_hugepages_kib = self.anon_hugepages_kib.max(other.anon_hugepages_kib);
        self.pss_kib = self.pss_kib.max(other.pss_kib);
        self.private_dirty_kib = self.private_dirty_kib.max(other.private_dirty_kib);
        if self.vm_flags.is_empty() {
            self.vm_flags = other.vm_flags;
        }
        self
    }
}

fn parse_kib(text: &str, key: &str) -> usize {
    text.lines()
        .find_map(|line| {
            let rest = line.strip_prefix(key)?;
            rest.split_whitespace().next()?.parse().ok()
        })
        .unwrap_or(0)
}

fn status_kib(key: &str) -> usize {
    fs::read_to_string("/proc/self/status")
        .ok()
        .map(|text| parse_kib(&text, key))
        .unwrap_or(0)
}

#[derive(Clone, Copy, Debug, Default)]
struct Faults {
    minor: u64,
    major: u64,
}

fn faults() -> Faults {
    let text = match fs::read_to_string("/proc/self/stat") {
        Ok(text) => text,
        Err(_) => return Faults::default(),
    };
    let tail = match text.rsplit_once(')') {
        Some((_, tail)) => tail,
        None => return Faults::default(),
    };
    let fields: Vec<&str> = tail.split_whitespace().collect();
    Faults {
        // `fields[0]` is proc field 3 (state); minflt and majflt are 10 and 12.
        minor: fields
            .get(7)
            .and_then(|value| value.parse().ok())
            .unwrap_or(0),
        major: fields
            .get(9)
            .and_then(|value| value.parse().ok())
            .unwrap_or(0),
    }
}

#[derive(Clone, Copy)]
struct Object {
    pointer: *mut u8,
    metadata: AllocationMetadata,
}

struct WorkloadAllocator {
    arm: Arm,
    unialloc: UniAlloc,
}

impl WorkloadAllocator {
    fn new(arm: Arm) -> Self {
        Self {
            arm,
            unialloc: UniAlloc::new(),
        }
    }

    unsafe fn allocate(
        &self,
        layout: Layout,
        metadata: AllocationMetadata,
    ) -> Result<Object, String> {
        let pointer = match self.arm {
            Arm::System => System.alloc(layout),
            Arm::PolicyOff | Arm::Ordinary | Arm::SelectiveThp | Arm::AdaptiveThp => {
                self.unialloc.alloc_with_metadata(layout, metadata)
            }
        };
        if pointer.is_null() {
            return Err("allocation failed".to_owned());
        }
        Ok(Object { pointer, metadata })
    }

    unsafe fn deallocate(&self, object: Object, layout: Layout) {
        match self.arm {
            Arm::System => System.dealloc(object.pointer, layout),
            Arm::PolicyOff | Arm::Ordinary | Arm::SelectiveThp | Arm::AdaptiveThp => self
                .unialloc
                .dealloc_with_metadata(object.pointer, layout, object.metadata),
        }
    }
}

fn metadata(type_id: u64, callsite: u64, lifetime_hint: u16) -> AllocationMetadata {
    AllocationMetadata::for_type(type_id)
        .with_module(MODULE_ID)
        .with_callsite(callsite)
        .with_lifetime_hint(lifetime_hint)
}

unsafe fn initialize(object: Object, bytes: usize, seed: u8) {
    std::ptr::write_bytes(object.pointer, seed, bytes);
}

unsafe fn touch(cohort: &[Object], bytes: usize, salt: usize) -> u64 {
    let mut checksum = 0u64;
    for (index, object) in cohort.iter().enumerate() {
        let offset = (index.wrapping_mul(131).wrapping_add(salt)) % bytes;
        let pointer = object.pointer.add(offset);
        let previous = std::ptr::read_volatile(pointer);
        let next = previous.wrapping_add(1);
        std::ptr::write_volatile(pointer, next);
        checksum = checksum.wrapping_add(next as u64).rotate_left(5);
    }
    checksum
}

fn configure(arm: Arm) -> Result<(), String> {
    if !arm.uses_unialloc() {
        return Ok(());
    }
    if !lifetime_hugepage_stats_reset() {
        return Err("lifetime allocator has live state before the run".to_owned());
    }
    let policy = match arm {
        Arm::PolicyOff => LifetimeHugepagePolicy::Disabled,
        Arm::Ordinary => LifetimeHugepagePolicy::SegregatedOrdinary,
        Arm::SelectiveThp => LifetimeHugepagePolicy::LongLivedHugepage,
        Arm::AdaptiveThp => LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
        Arm::System => unreachable!(),
    };
    if !lifetime_hugepage_configure_with_backend(policy, LifetimePageBackend::TransparentHugepage) {
        return Err("failed to configure lifetime allocator".to_owned());
    }
    Ok(())
}

fn empty_stats() -> LifetimeHugepageStatsSnapshot {
    lifetime_hugepage_stats_snapshot()
}

unsafe fn train_adaptive_long_site(
    allocator: &WorkloadAllocator,
    layout: Layout,
    long_metadata: AllocationMetadata,
    pressure_metadata: AllocationMetadata,
) -> Result<LifetimeHugepageStatsSnapshot, String> {
    const TRAINING_SAMPLES: usize = 8;
    const LONG_AGE_BYTES: usize = 8 * 1024 * 1024;
    let pressure_allocations = (LONG_AGE_BYTES + layout.size() - 1) / layout.size();
    for sample in 0..TRAINING_SAMPLES {
        let survivor = allocator.allocate(layout, long_metadata)?;
        initialize(survivor, layout.size(), sample as u8);
        for pressure_index in 0..pressure_allocations {
            let pressure = allocator.allocate(layout, pressure_metadata)?;
            initialize(pressure, layout.size(), pressure_index as u8);
            allocator.deallocate(pressure, layout);
        }
        allocator.deallocate(survivor, layout);
    }
    let trained = lifetime_hugepage_stats_snapshot();
    if trained.adaptive_long_sites < 1 || trained.adaptive_long_observations < TRAINING_SAMPLES {
        return Err(format!(
            "adaptive training failed: long_sites={} long_observations={}",
            trained.adaptive_long_sites, trained.adaptive_long_observations
        ));
    }
    if !lifetime_hugepage_stats_reset() {
        return Err("adaptive telemetry reset failed after training".to_owned());
    }
    Ok(trained)
}

fn run(config: &Config) -> Result<(), String> {
    configure(config.arm)?;
    let allocator = WorkloadAllocator::new(config.arm);
    let layout = Layout::from_size_align(config.object_bytes, 64)
        .map_err(|error| format!("invalid layout: {error}"))?;
    let (long_hint, short_hint) = if config.arm.is_adaptive() {
        (0, 0)
    } else {
        (LIFETIME_HINT_LONG_LIVED, LIFETIME_HINT_EPHEMERAL)
    };
    let long_metadata = metadata(SHARED_TYPE_ID, LONG_CALLSITE, long_hint);
    let short_metadata = metadata(SHARED_TYPE_ID, SHORT_CALLSITE, short_hint);
    let training = if config.arm.is_adaptive() {
        unsafe { train_adaptive_long_site(&allocator, layout, long_metadata, short_metadata)? }
    } else {
        empty_stats()
    };
    let baseline_memory = MemorySnapshot::read();
    let baseline_faults = faults();
    let mut long = Vec::with_capacity(config.long_objects);
    let mut setup_short = Vec::with_capacity(config.long_objects);
    let started = Instant::now();
    let mut checksum = 0u64;

    unsafe {
        for index in 0..config.long_objects {
            let object = allocator.allocate(layout, long_metadata)?;
            initialize(object, config.object_bytes, index as u8);
            long.push(object);
            // Same type/layout, different callsite and lifetime. Interleaving
            // exposes lifetime mixing in policy-off allocators while the
            // lifetime policies can physically segregate the two cohorts.
            let short = allocator.allocate(layout, short_metadata)?;
            initialize(short, config.object_bytes, !index as u8);
            setup_short.push(short);
        }
        checksum ^= touch(&setup_short, config.object_bytes, 0);
        for object in setup_short.drain(..) {
            allocator.deallocate(object, layout);
        }
    }
    if config.arm.uses_unialloc() {
        lifetime_hugepage_advance_epoch();
    }

    for wave in 0..config.waves {
        let mut short = Vec::with_capacity(config.short_objects);
        unsafe {
            for index in 0..config.short_objects {
                let object = allocator.allocate(layout, short_metadata)?;
                initialize(object, config.object_bytes, (wave ^ index) as u8);
                short.push(object);
            }
            checksum ^= touch(&short, config.object_bytes, wave);
            for object in short.drain(..) {
                allocator.deallocate(object, layout);
            }
            for pass in 0..config.passes_per_wave {
                checksum ^= touch(
                    &long,
                    config.object_bytes,
                    wave.wrapping_mul(config.passes_per_wave) + pass,
                );
            }
        }
        if config.arm.uses_unialloc() {
            lifetime_hugepage_advance_epoch();
        }
    }
    let work_elapsed_ns = started.elapsed().as_nanos();
    let work_end_memory = MemorySnapshot::read();
    let long_address = long
        .first()
        .map(|object| object.pointer as usize)
        .unwrap_or(0);
    let work_end_long_vma = VmaSnapshot::for_address(long_address);
    let pre_drop = if config.arm.uses_unialloc() {
        lifetime_hugepage_stats_snapshot()
    } else {
        empty_stats()
    };

    // Poll THP-requesting arms until the pointer-containing VMA proves backing
    // or the claim-grade deadline expires. This observation time stays outside
    // `work_elapsed_ns`; control arms need no asynchronous THP wait.
    let observation_started = Instant::now();
    let wait_deadline = observation_started + Duration::from_millis(config.thp_deadline_ms);
    let mut max_memory = work_end_memory;
    let mut max_long_vma = work_end_long_vma.clone();
    while matches!(config.arm, Arm::SelectiveThp | Arm::AdaptiveThp)
        && max_long_vma.anon_hugepages_kib == 0
        && Instant::now() < wait_deadline
    {
        max_memory = max_memory.max(MemorySnapshot::read());
        max_long_vma = max_long_vma.max_backing(VmaSnapshot::for_address(long_address));
        thread::sleep(Duration::from_millis(10));
    }
    let observation_elapsed_ms = observation_started.elapsed().as_millis();
    // `steady_*` is the matched memory sample used by the summary. Take it
    // after the THP observation window so selective rows cannot pair a
    // pre-collapse PSS value with post-collapse mechanism evidence.
    let steady_memory = MemorySnapshot::read();
    let steady_long_vma = VmaSnapshot::for_address(long_address);
    max_memory = max_memory.max(steady_memory);
    max_long_vma = max_long_vma.max_backing(steady_long_vma);

    unsafe {
        for object in long.drain(..) {
            allocator.deallocate(object, layout);
        }
    }
    let final_faults = faults();
    let post_drop = if config.arm.uses_unialloc() {
        lifetime_hugepage_stats_snapshot()
    } else {
        empty_stats()
    };
    if config.arm.uses_unialloc() {
        if !post_drop.all_mappings_released || post_drop.live_objects != 0 {
            return Err("lifetime arena retained live mappings after normal drops".to_owned());
        }
        if !lifetime_hugepage_configure(LifetimeHugepagePolicy::Disabled) {
            return Err("failed to disable lifetime allocator".to_owned());
        }
    }
    black_box(checksum);
    let expected_allocations = config.long_objects * 2 + config.short_objects * config.waves;
    let actual_thp_delta = max_memory
        .anon_hugepages_kib
        .saturating_sub(baseline_memory.anon_hugepages_kib);

    println!(
        concat!(
            "{{\"schema_version\":2,\"source\":\"eventually_freed_lifetime_workload\",",
            "\"arm\":\"{}\",\"work_elapsed_ns\":{},\"checksum\":{},",
            "\"long_objects\":{},\"short_objects_per_wave\":{},\"object_bytes\":{},",
            "\"waves\":{},\"passes_per_wave\":{},\"setup_short_objects\":{},",
            "\"expected_workload_allocations\":{},",
            "\"normal_long_drops\":{},\"leaked_objects\":0,",
            "\"thp_deadline_ms\":{},\"thp_observation_elapsed_ms\":{},",
            "\"minor_faults_delta\":{},\"major_faults_delta\":{},\"peak_rss_kib\":{},",
            "\"smaps_available\":{},\"baseline_rss_kib\":{},\"steady_rss_kib\":{},",
            "\"steady_pss_kib\":{},\"steady_private_dirty_kib\":{},",
            "\"steady_sample_phase\":\"post_thp_observation\",",
            "\"baseline_anon_hugepages_kib\":{},\"steady_anon_hugepages_kib\":{},",
            "\"max_anon_hugepages_kib\":{},\"actual_anon_hugepages_delta_kib\":{},",
            "\"long_vma\":{{\"available\":{},\"anon_hugepages_kib\":{},",
            "\"pss_kib\":{},\"private_dirty_kib\":{},\"hugepage_advised\":{},",
            "\"vm_flags\":\"{}\"}},",
            "\"long_site\":{{\"callsite\":{},\"type_id\":{},\"module_id\":{},",
            "\"size\":{},\"align\":64,\"ground_truth\":\"long_then_normal_drop\"}},",
            "\"short_site\":{{\"callsite\":{},\"type_id\":{},\"module_id\":{},",
            "\"size\":{},\"align\":64,\"ground_truth\":\"same_epoch_short\"}},",
            "\"pre_drop\":{{\"routed_allocations\":{},\"live_objects\":{},",
            "\"live_long_lived_objects\":{},\"live_ephemeral_objects\":{},",
            "\"ordinary_extent_mappings\":{},\"thp_extent_mappings\":{},",
            "\"thp_advice_attempts\":{},\"thp_advice_successes\":{},",
            "\"phase_advances\":{},\"adaptive_long_routed_allocations\":{},",
            "\"adaptive_short_bypassed_allocations\":{}}},",
            "\"post_drop\":{{\"routed_deallocations\":{},\"live_objects\":{},",
            "\"runtime_validated_objects\":{},\"runtime_validated_bytes\":{},",
            "\"predictor_true_positive_objects\":{},\"predictor_true_negative_objects\":{},",
            "\"predictor_false_positive_objects\":{},\"predictor_false_negative_objects\":{},",
            "\"all_mappings_released\":{}}},",
            "\"adaptive_training\":{{\"performed\":{},\"long_sites\":{},",
            "\"long_observations\":{},\"decisive_observation_bytes\":{}}},",
            "\"claim_boundary\":\"oracle policy arms and runtime-adaptive microbenchmark; compiler layer unmeasured; no real-application claim\"}}"
        ),
        config.arm.name(),
        work_elapsed_ns,
        checksum,
        config.long_objects,
        config.short_objects,
        config.object_bytes,
        config.waves,
        config.passes_per_wave,
        config.long_objects,
        expected_allocations,
        config.long_objects,
        config.thp_deadline_ms,
        observation_elapsed_ms,
        final_faults.minor.saturating_sub(baseline_faults.minor),
        final_faults.major.saturating_sub(baseline_faults.major),
        status_kib("VmHWM:"),
        baseline_memory.available
            && work_end_memory.available
            && steady_memory.available
            && max_memory.available,
        baseline_memory.rss_kib,
        steady_memory.rss_kib,
        steady_memory.pss_kib,
        steady_memory.private_dirty_kib,
        baseline_memory.anon_hugepages_kib,
        steady_memory.anon_hugepages_kib,
        max_memory.anon_hugepages_kib,
        actual_thp_delta,
        max_long_vma.available,
        max_long_vma.anon_hugepages_kib,
        max_long_vma.pss_kib,
        max_long_vma.private_dirty_kib,
        max_long_vma.hugepage_advised(),
        max_long_vma.vm_flags,
        LONG_CALLSITE,
        SHARED_TYPE_ID,
        MODULE_ID,
        config.object_bytes,
        SHORT_CALLSITE,
        SHARED_TYPE_ID,
        MODULE_ID,
        config.object_bytes,
        pre_drop.routed_allocations,
        pre_drop.live_objects,
        pre_drop.live_long_lived_objects,
        pre_drop.live_ephemeral_objects,
        pre_drop.ordinary_extent_mappings,
        pre_drop.thp_extent_mappings,
        pre_drop.thp_advice_attempts,
        pre_drop.thp_advice_successes,
        pre_drop.phase_advances,
        pre_drop.adaptive_long_routed_allocations,
        pre_drop.adaptive_short_bypassed_allocations,
        post_drop.routed_deallocations,
        post_drop.live_objects,
        post_drop.runtime_validated_objects,
        post_drop.runtime_validated_bytes,
        post_drop.predictor_true_positive_objects,
        post_drop.predictor_true_negative_objects,
        post_drop.predictor_false_positive_objects,
        post_drop.predictor_false_negative_objects,
        post_drop.all_mappings_released,
        config.arm.is_adaptive(),
        training.adaptive_long_sites,
        training.adaptive_long_observations,
        training.adaptive_decisive_observation_bytes,
    );
    Ok(())
}

// Kept as ordinary Rust ownership evidence for optional compiler-pass audits.
// The persistent Box cohort survives an allocation-heavy loop and is dropped
// normally at function exit.  The measured arms above use raw allocation so
// allocator work stays matched across System and UniAlloc.
#[allow(dead_code)]
#[inline(never)]
fn compiler_observation_shape(objects: usize, bytes: usize, waves: usize) -> u64 {
    let mut persistent: Vec<Box<[u8]>> = (0..objects)
        .map(|index| vec![index as u8; bytes].into_boxed_slice())
        .collect();
    let mut checksum = 0u64;
    for wave in 0..waves {
        let ephemeral: Vec<Box<[u8]>> = (0..objects)
            .map(|index| vec![(wave ^ index) as u8; bytes].into_boxed_slice())
            .collect();
        checksum ^= ephemeral.iter().map(|value| value[0] as u64).sum::<u64>();
        for value in &mut persistent {
            value[wave % bytes] = value[wave % bytes].wrapping_add(1);
            checksum = checksum.wrapping_add(value[wave % bytes] as u64);
        }
        drop(ephemeral);
    }
    drop(persistent);
    checksum
}

fn main() {
    let result = Config::parse().and_then(|config| run(&config));
    if let Err(error) = result {
        eprintln!("eventually_freed_lifetime_workload: {error}");
        std::process::exit(2);
    }
}
