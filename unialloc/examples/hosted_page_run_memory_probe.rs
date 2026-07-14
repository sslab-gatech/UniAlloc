use std::alloc::{GlobalAlloc, Layout};
use std::fs;
use std::thread;
use std::time::Duration;

use unialloc::UniAlloc;

const FRAGMENT_SLOTS: usize = 1024;
const SLOTS_PER_ARENA: usize = 16;
const FRAGMENT_PAGES: usize = 8;
const COALESCED_PAGES: usize = 32;

static ALLOCATOR: UniAlloc = UniAlloc::new();

#[derive(Clone, Copy, Default)]
struct ProcessMemory {
    status_available: bool,
    smaps_rollup_available: bool,
    stat_available: bool,
    vm_size_kib: usize,
    vm_rss_kib: usize,
    vm_hwm_kib: usize,
    rollup_rss_kib: usize,
    pss_kib: usize,
    private_clean_kib: usize,
    private_dirty_kib: usize,
    anonymous_kib: usize,
    referenced_kib: usize,
    swap_kib: usize,
    minor_faults: usize,
    major_faults: usize,
}

#[derive(Clone, Copy, Default)]
struct BitmapMemory {
    available: bool,
    active_arenas: usize,
    warm_empty_arenas: usize,
    descriptor_count: usize,
    live_allocations: usize,
    mapped_payload_bytes: usize,
    allocated_payload_bytes: usize,
    mapped_tree_bytes: usize,
    mapped_descriptor_bytes: usize,
    mapped_owner_directory_bytes: usize,
}

#[derive(Clone, Copy, Default)]
struct AdaptiveRoutes {
    available: bool,
    freelist_allocations: usize,
    bitmap_allocations: usize,
    bitmap_fallbacks: usize,
    freelist_deallocations: usize,
    owned_deallocations: usize,
    bridge_signals: usize,
    bridge_activations: usize,
    contention_signals: usize,
    contention_activations: usize,
}

fn parse_kib(text: &str, key: &str) -> usize {
    text.lines()
        .find_map(|line| {
            let rest = line.strip_prefix(key)?;
            rest.split_ascii_whitespace().next()?.parse().ok()
        })
        .unwrap_or(0)
}

fn process_memory() -> ProcessMemory {
    let status = fs::read_to_string("/proc/self/status").ok();
    let rollup = fs::read_to_string("/proc/self/smaps_rollup").ok();
    let stat = fs::read_to_string("/proc/self/stat").ok();
    let status_text = status.as_deref().unwrap_or_default();
    let rollup_text = rollup.as_deref().unwrap_or_default();
    let stat_fields: Vec<&str> = stat
        .as_deref()
        .and_then(|text| text.rfind(')').map(|end| &text[end + 1..]))
        .map(|fields| fields.split_ascii_whitespace().collect())
        .unwrap_or_default();
    ProcessMemory {
        status_available: status.is_some(),
        smaps_rollup_available: rollup.is_some(),
        stat_available: stat.is_some(),
        vm_size_kib: parse_kib(status_text, "VmSize:"),
        vm_rss_kib: parse_kib(status_text, "VmRSS:"),
        vm_hwm_kib: parse_kib(status_text, "VmHWM:"),
        rollup_rss_kib: parse_kib(rollup_text, "Rss:"),
        pss_kib: parse_kib(rollup_text, "Pss:"),
        private_clean_kib: parse_kib(rollup_text, "Private_Clean:"),
        private_dirty_kib: parse_kib(rollup_text, "Private_Dirty:"),
        anonymous_kib: parse_kib(rollup_text, "Anonymous:"),
        referenced_kib: parse_kib(rollup_text, "Referenced:"),
        swap_kib: parse_kib(rollup_text, "Swap:"),
        // stat_fields[0] is field 3 (`state`) after removing pid/comm.
        minor_faults: stat_fields
            .get(7)
            .and_then(|value| value.parse().ok())
            .unwrap_or(0),
        major_faults: stat_fields
            .get(9)
            .and_then(|value| value.parse().ok())
            .unwrap_or(0),
    }
}

#[cfg(feature = "hosted_bitmap_page_allocator")]
fn bitmap_memory() -> BitmapMemory {
    let snapshot = unialloc::hosted_bitmap_page_run_snapshot();
    BitmapMemory {
        available: true,
        active_arenas: snapshot.active_arenas,
        warm_empty_arenas: snapshot.warm_empty_arenas,
        descriptor_count: snapshot.descriptor_count,
        live_allocations: snapshot.live_allocations,
        mapped_payload_bytes: snapshot.mapped_payload_bytes,
        allocated_payload_bytes: snapshot.allocated_payload_bytes,
        mapped_tree_bytes: snapshot.mapped_tree_bytes,
        mapped_descriptor_bytes: snapshot.mapped_descriptor_bytes,
        mapped_owner_directory_bytes: snapshot.mapped_owner_directory_bytes,
    }
}

#[cfg(not(feature = "hosted_bitmap_page_allocator"))]
fn bitmap_memory() -> BitmapMemory {
    BitmapMemory::default()
}

#[cfg(all(feature = "adaptive_bitmap_page_allocator", feature = "stats"))]
fn adaptive_routes() -> AdaptiveRoutes {
    let snapshot = unialloc::adaptive_page_run_stats_snapshot();
    AdaptiveRoutes {
        available: true,
        freelist_allocations: snapshot.freelist_allocations,
        bitmap_allocations: snapshot.bitmap_allocations,
        bitmap_fallbacks: snapshot.bitmap_fallbacks,
        freelist_deallocations: snapshot.freelist_deallocations,
        owned_deallocations: snapshot.owned_deallocations,
        bridge_signals: snapshot.bridge_signals,
        bridge_activations: snapshot.bridge_activations,
        contention_signals: snapshot.contention_signals,
        contention_activations: snapshot.contention_activations,
    }
}

#[cfg(not(all(feature = "adaptive_bitmap_page_allocator", feature = "stats")))]
fn adaptive_routes() -> AdaptiveRoutes {
    AdaptiveRoutes::default()
}

fn checkpoint(mode: &str, name: &str, requested_live_payload_bytes: usize) {
    // Let synchronous unmap/madvise effects settle before reading both procfs
    // files. This is outside every performance benchmark and exists only to
    // make memory checkpoints easier to compare across isolated processes.
    thread::sleep(Duration::from_millis(10));
    let process = process_memory();
    let bitmap = bitmap_memory();
    let routes = adaptive_routes();
    let thread_cache = unialloc::thread_cache_footprint_snapshot();
    #[cfg(feature = "stats")]
    let zone = unialloc::zone_retained_empty_slab_snapshot();
    #[cfg(not(feature = "stats"))]
    let zone = ZoneMemory::default();
    #[cfg(feature = "stats")]
    let zone = ZoneMemory {
        available: true,
        initialized: zone.initialized,
        retained_os_pages: zone.observed_retained_os_pages,
        busy_class_count: zone.busy_class_count,
        budget_os_pages: zone.budget_os_pages,
    };

    println!(
        concat!(
            "{{\"source\":\"hosted_page_run_memory_probe\",",
            "\"backend\":\"{}\",\"mode\":\"{}\",\"checkpoint\":\"{}\",",
            "\"page_size\":{},\"requested_live_payload_bytes\":{},",
            "\"status_available\":{},\"smaps_rollup_available\":{},",
            "\"stat_available\":{},",
            "\"vm_size_kib\":{},\"vm_rss_kib\":{},\"vm_hwm_kib\":{},",
            "\"rollup_rss_kib\":{},\"pss_kib\":{},",
            "\"private_clean_kib\":{},\"private_dirty_kib\":{},",
            "\"anonymous_kib\":{},\"referenced_kib\":{},\"swap_kib\":{},",
            "\"minor_faults\":{},\"major_faults\":{},",
            "\"thread_cache_initialized\":{},\"thread_cache_retained_bytes\":{},",
            "\"thread_cache_accounting_exact\":{},",
            "\"zone_snapshot_available\":{},\"zone_initialized\":{},",
            "\"zone_retained_os_pages\":{},\"zone_busy_class_count\":{},",
            "\"zone_budget_os_pages\":{},",
            "\"bitmap_snapshot_available\":{},\"bitmap_active_arenas\":{},",
            "\"bitmap_warm_empty_arenas\":{},\"bitmap_descriptor_count\":{},",
            "\"bitmap_live_allocations\":{},\"bitmap_mapped_payload_bytes\":{},",
            "\"bitmap_allocated_payload_bytes\":{},\"bitmap_mapped_tree_bytes\":{},",
            "\"bitmap_mapped_descriptor_bytes\":{},",
            "\"bitmap_mapped_owner_directory_bytes\":{},",
            "\"adaptive_stats_available\":{},\"adaptive_freelist_allocations\":{},",
            "\"adaptive_bitmap_allocations\":{},\"adaptive_bitmap_fallbacks\":{},",
            "\"adaptive_freelist_deallocations\":{},",
            "\"adaptive_owned_deallocations\":{},\"adaptive_bridge_signals\":{},",
            "\"adaptive_bridge_activations\":{},",
            "\"adaptive_contention_signals\":{},",
            "\"adaptive_contention_activations\":{}}}"
        ),
        unialloc::platform_allocator_backend(),
        mode,
        name,
        unialloc::PAGE_SIZE,
        requested_live_payload_bytes,
        process.status_available,
        process.smaps_rollup_available,
        process.stat_available,
        process.vm_size_kib,
        process.vm_rss_kib,
        process.vm_hwm_kib,
        process.rollup_rss_kib,
        process.pss_kib,
        process.private_clean_kib,
        process.private_dirty_kib,
        process.anonymous_kib,
        process.referenced_kib,
        process.swap_kib,
        process.minor_faults,
        process.major_faults,
        thread_cache.cache_initialized,
        thread_cache.exact_cached_object_bytes,
        thread_cache.accounting_matches_exact,
        zone.available,
        zone.initialized,
        zone.retained_os_pages,
        zone.busy_class_count,
        zone.budget_os_pages,
        bitmap.available,
        bitmap.active_arenas,
        bitmap.warm_empty_arenas,
        bitmap.descriptor_count,
        bitmap.live_allocations,
        bitmap.mapped_payload_bytes,
        bitmap.allocated_payload_bytes,
        bitmap.mapped_tree_bytes,
        bitmap.mapped_descriptor_bytes,
        bitmap.mapped_owner_directory_bytes,
        routes.available,
        routes.freelist_allocations,
        routes.bitmap_allocations,
        routes.bitmap_fallbacks,
        routes.freelist_deallocations,
        routes.owned_deallocations,
        routes.bridge_signals,
        routes.bridge_activations,
        routes.contention_signals,
        routes.contention_activations,
    );
}

#[derive(Clone, Copy, Default)]
struct ZoneMemory {
    available: bool,
    initialized: bool,
    retained_os_pages: usize,
    busy_class_count: usize,
    budget_os_pages: usize,
}

#[inline]
unsafe fn page_alloc(pages: usize) -> *mut u8 {
    let layout = Layout::from_size_align(pages * unialloc::PAGE_SIZE, unialloc::PAGE_SIZE)
        .expect("valid page-run layout");
    let ptr = GlobalAlloc::alloc(&ALLOCATOR, layout);
    assert!(!ptr.is_null(), "allocation failed for {} pages", pages);
    ptr
}

#[inline]
unsafe fn page_free(ptr: *mut u8, pages: usize) {
    let layout = Layout::from_size_align(pages * unialloc::PAGE_SIZE, unialloc::PAGE_SIZE)
        .expect("valid page-run layout");
    GlobalAlloc::dealloc(&ALLOCATOR, ptr, layout);
}

#[inline]
unsafe fn touch_pages(ptr: *mut u8, pages: usize) {
    for page in 0..pages {
        ptr.add(page * unialloc::PAGE_SIZE)
            .write_volatile((page as u8).wrapping_add(1));
    }
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

fn retention_probe() {
    let mode = "retention";
    checkpoint(mode, "process_start", 0);
    let mut slots = Vec::with_capacity(FRAGMENT_SLOTS);
    for _ in 0..FRAGMENT_SLOTS {
        slots.push(unsafe { page_alloc(FRAGMENT_PAGES) });
    }
    let full_bytes = FRAGMENT_SLOTS * FRAGMENT_PAGES * unialloc::PAGE_SIZE;
    checkpoint(mode, "allocated_untouched", full_bytes);
    for ptr in slots.iter().copied() {
        unsafe { touch_pages(ptr, FRAGMENT_PAGES) };
    }
    checkpoint(mode, "allocated_touched", full_bytes);

    for idx in (0..FRAGMENT_SLOTS).step_by(2) {
        unsafe { page_free(slots[idx], FRAGMENT_PAGES) };
    }
    checkpoint(mode, "half_freed", full_bytes / 2);
    for idx in (1..FRAGMENT_SLOTS).step_by(2) {
        unsafe { page_free(slots[idx], FRAGMENT_PAGES) };
    }
    checkpoint(mode, "all_freed", 0);
}

fn cold_probe() {
    let mode = "cold";
    checkpoint(mode, "process_start", 0);
    let ptr = unsafe { page_alloc(FRAGMENT_PAGES) };
    let live_bytes = FRAGMENT_PAGES * unialloc::PAGE_SIZE;
    checkpoint(mode, "first_live_untouched", live_bytes);
    unsafe { touch_pages(ptr, FRAGMENT_PAGES) };
    checkpoint(mode, "first_live_touched", live_bytes);
    unsafe { page_free(ptr, FRAGMENT_PAGES) };
    checkpoint(mode, "all_freed", 0);
}

fn coalesce_probe() {
    let mode = "coalesce";
    checkpoint(mode, "process_start", 0);
    let mut small = Vec::with_capacity(FRAGMENT_SLOTS);
    for _ in 0..FRAGMENT_SLOTS {
        let ptr = unsafe { page_alloc(FRAGMENT_PAGES) };
        unsafe { touch_pages(ptr, FRAGMENT_PAGES) };
        small.push(ptr);
    }
    let small_bytes = FRAGMENT_SLOTS * FRAGMENT_PAGES * unialloc::PAGE_SIZE;
    checkpoint(mode, "small_live_touched", small_bytes);

    let free_order: Vec<usize> = shuffled_indices(FRAGMENT_SLOTS)
        .into_iter()
        .filter(|idx| idx % SLOTS_PER_ARENA != 0)
        .collect();
    for idx in free_order.iter().copied() {
        unsafe { page_free(small[idx], FRAGMENT_PAGES) };
    }
    let guard_count = FRAGMENT_SLOTS / SLOTS_PER_ARENA;
    let guard_bytes = guard_count * FRAGMENT_PAGES * unialloc::PAGE_SIZE;
    checkpoint(mode, "fragmented_guards_live", guard_bytes);

    let coalesced_per_arena = (SLOTS_PER_ARENA - 1) * FRAGMENT_PAGES / COALESCED_PAGES;
    let coalesced_count = guard_count * coalesced_per_arena;
    let mut large = Vec::with_capacity(coalesced_count);
    for _ in 0..coalesced_count {
        let ptr = unsafe { page_alloc(COALESCED_PAGES) };
        unsafe { touch_pages(ptr, COALESCED_PAGES) };
        large.push(ptr);
    }
    let large_bytes = coalesced_count * COALESCED_PAGES * unialloc::PAGE_SIZE;
    checkpoint(mode, "coalesced_live_touched", guard_bytes + large_bytes);

    for ptr in large {
        unsafe { page_free(ptr, COALESCED_PAGES) };
    }
    checkpoint(mode, "large_freed_guards_live", guard_bytes);
    for idx in (0..FRAGMENT_SLOTS).step_by(SLOTS_PER_ARENA) {
        unsafe { page_free(small[idx], FRAGMENT_PAGES) };
    }
    checkpoint(mode, "all_freed", 0);
}

fn main() {
    let mode = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "retention".to_owned());
    assert!(
        matches!(mode.as_str(), "cold" | "retention" | "coalesce"),
        "usage: hosted_page_run_memory_probe [cold|retention|coalesce]"
    );
    match mode.as_str() {
        "cold" => cold_probe(),
        "retention" => retention_probe(),
        "coalesce" => coalesce_probe(),
        _ => unreachable!(),
    }
}
