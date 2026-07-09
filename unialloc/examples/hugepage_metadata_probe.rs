use std::alloc::Layout;
#[cfg(target_os = "linux")]
use std::fs;
use std::io;
#[cfg(target_os = "linux")]
use std::io::Read;

use unialloc::{
    hugepage_aligned_fallback_supported, hugepage_fallback_alignment,
    hugepage_metadata_side_cache_snapshot, hugepage_mmap_platform_status_snapshot,
    hugepage_mmap_stats_snapshot, semantic_stats_recording_disable, semantic_stats_reset,
    semantic_stats_snapshot, AllocationMetadata, SemanticAlloc, UniAlloc, FLAG_HUGEPAGE_METADATA,
    FLAG_TYPE_ISOLATED,
};

#[derive(Clone, Copy, Debug)]
struct SmapsBacking {
    mapping_found: bool,
    kernel_page_kb: usize,
    mmu_page_kb: usize,
    anon_huge_kb: usize,
    thp_eligible: Option<bool>,
    vmflags_ht: bool,
    vmflags_hg: bool,
    vmflags_nh: bool,
}

impl SmapsBacking {
    fn empty() -> Self {
        Self {
            mapping_found: false,
            kernel_page_kb: 0,
            mmu_page_kb: 0,
            anon_huge_kb: 0,
            thp_eligible: None,
            vmflags_ht: false,
            vmflags_hg: false,
            vmflags_nh: false,
        }
    }

    fn hugepage_backed(self) -> bool {
        self.anon_huge_kb >= 2048
            || self.kernel_page_kb >= 2048
            || self.mmu_page_kb >= 2048
            || self.vmflags_ht
    }
}

#[cfg(target_os = "linux")]
unsafe fn prefault_mapping_for_smaps(address: usize, size: usize) -> usize {
    if address == 0 || size == 0 {
        return 0;
    }
    let mut touched = 0usize;
    let page = 4096usize;
    let mut offset = 0usize;
    while offset < size {
        let ptr = (address + offset) as *mut u8;
        let value = std::ptr::read_volatile(ptr);
        std::ptr::write_volatile(ptr, value);
        touched += 1;
        match offset.checked_add(page) {
            Some(next) => offset = next,
            None => break,
        }
    }
    touched
}

#[cfg(not(target_os = "linux"))]
unsafe fn prefault_mapping_for_smaps(_address: usize, _size: usize) -> usize {
    0
}

#[cfg(target_os = "linux")]
fn parse_smaps_for_address(address: usize) -> io::Result<SmapsBacking> {
    let mut text = String::new();
    fs::File::open("/proc/self/smaps")?.read_to_string(&mut text)?;
    let mut current: Option<SmapsBacking> = None;
    for line in text.lines() {
        if let Some((range, _rest)) = line.split_once(' ') {
            if let Some((start, end)) = range.split_once('-') {
                if let (Ok(start), Ok(end)) = (
                    usize::from_str_radix(start, 16),
                    usize::from_str_radix(end, 16),
                ) {
                    if let Some(backing) = current.take() {
                        return Ok(backing);
                    }
                    if address >= start && address < end {
                        current = Some(SmapsBacking {
                            mapping_found: true,
                            ..SmapsBacking::empty()
                        });
                    }
                    continue;
                }
            }
        }
        let backing = match current.as_mut() {
            Some(backing) => backing,
            None => continue,
        };
        if let Some(value) = line.strip_prefix("KernelPageSize:") {
            backing.kernel_page_kb = parse_kb(value);
        } else if let Some(value) = line.strip_prefix("MMUPageSize:") {
            backing.mmu_page_kb = parse_kb(value);
        } else if let Some(value) = line.strip_prefix("AnonHugePages:") {
            backing.anon_huge_kb = parse_kb(value);
        } else if let Some(value) = line.strip_prefix("THPeligible:") {
            backing.thp_eligible = Some(value.trim().starts_with('1'));
        } else if let Some(value) = line.strip_prefix("VmFlags:") {
            for flag in value.split_whitespace() {
                if flag == "ht" {
                    backing.vmflags_ht = true;
                } else if flag == "hg" {
                    backing.vmflags_hg = true;
                } else if flag == "nh" {
                    backing.vmflags_nh = true;
                }
            }
        }
    }
    Ok(current.unwrap_or_else(SmapsBacking::empty))
}

#[cfg(not(target_os = "linux"))]
fn parse_smaps_for_address(_address: usize) -> io::Result<SmapsBacking> {
    Ok(SmapsBacking::empty())
}

#[cfg(target_os = "linux")]
fn parse_kb(value: &str) -> usize {
    value
        .split_whitespace()
        .next()
        .and_then(|raw| raw.parse::<usize>().ok())
        .unwrap_or(0)
}

fn json_bool(value: bool) -> &'static str {
    if value {
        "true"
    } else {
        "false"
    }
}

fn main() {
    semantic_stats_reset();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(
        4 * std::mem::size_of::<usize>(),
        std::mem::align_of::<usize>(),
    )
    .expect("valid probe layout");
    let metadata = AllocationMetadata::for_type(0x4855_4745_5041_4745)
        .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);

    let first = unsafe { alloc.alloc_with_metadata(layout, metadata) };
    let second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
    if first.is_null() || second.is_null() {
        println!(
            "{{\"source\":\"hugepage_metadata_probe\",\"passed\":false,\"error\":\"alloc_with_metadata returned null\"}}"
        );
        unsafe {
            if !first.is_null() {
                alloc.dealloc_with_metadata(first, layout, metadata);
            }
            if !second.is_null() {
                alloc.dealloc_with_metadata(second, layout, metadata);
            }
        }
        std::process::exit(2);
    }
    unsafe {
        first.write(0xA5);
        first.add(layout.size() - 1).write(0x5A);
        second.write(0xB6);
        second.add(layout.size() - 1).write(0x6B);
        // The allocator keeps a single hugepage-metadata object in the inline
        // TLS cache.  Freeing a second object materializes the real
        // mmap_huge-backed side-cache table, so the probe samples that mapping
        // before consuming the cached objects below.
        alloc.dealloc_with_metadata(first, layout, metadata);
        alloc.dealloc_with_metadata(second, layout, metadata);
    }

    let side_cache = hugepage_metadata_side_cache_snapshot();
    let stats = hugepage_mmap_stats_snapshot();
    let status = hugepage_mmap_platform_status_snapshot();
    let (side_cache_allocated, side_cache_address, side_cache_mapping_size, backing_name) =
        if let Some(snapshot) = side_cache {
            (
                snapshot.allocated,
                snapshot.address,
                snapshot.mapping_size,
                snapshot.backing.as_str(),
            )
        } else {
            (false, 0, 0, "unmapped")
        };
    let prefaulted_pages =
        unsafe { prefault_mapping_for_smaps(side_cache_address, side_cache_mapping_size) };
    #[cfg(target_os = "linux")]
    if prefaulted_pages > 0 {
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    let smaps =
        parse_smaps_for_address(side_cache_address).unwrap_or_else(|_| SmapsBacking::empty());

    let reused_one = unsafe { alloc.alloc_with_metadata(layout, metadata) };
    let reused_two = unsafe { alloc.alloc_with_metadata(layout, metadata) };
    let reused_same_ptr = !reused_one.is_null()
        && !reused_two.is_null()
        && ((reused_one == first && reused_two == second)
            || (reused_one == second && reused_two == first));
    unsafe {
        if !reused_one.is_null() {
            reused_one.write(0xC3);
            reused_one.add(layout.size() - 1).write(0x3C);
            alloc.dealloc_with_metadata(reused_one, layout, metadata);
        }
        if !reused_two.is_null() {
            reused_two.write(0xD4);
            reused_two.add(layout.size() - 1).write(0x4D);
            alloc.dealloc_with_metadata(reused_two, layout, metadata);
        }
    }
    let semantic = semantic_stats_snapshot();
    semantic_stats_recording_disable();

    let fallback_alignment = hugepage_fallback_alignment();
    // The allocator first asks the PAL for a hugepage-sized metadata mapping.
    // On hosts without real huge pages the allocator may then compact-remap the
    // side-cache to a smaller ordinary mapping to reduce footprint.  Report both
    // facts separately so C005 diagnostics do not confuse "the hugepage-sized
    // fallback path is supported" with "the final compact mapping is itself
    // eligible for a hugepage-aligned fallback".
    let aligned_fallback_supported = hugepage_aligned_fallback_supported(fallback_alignment);
    let side_cache_mapping_aligned_fallback_supported =
        hugepage_aligned_fallback_supported(side_cache_mapping_size);

    let hugepage_backed = backing_name == "hugepage" || smaps.hugepage_backed();
    let validated = !first.is_null()
        && !second.is_null()
        && !reused_one.is_null()
        && !reused_two.is_null()
        && side_cache_allocated
        && stats.attempts > 0
        && semantic.typed_cache_inserts > 0
        && semantic.typed_cache_hits > 0;
    let claim_grade = validated && hugepage_backed;

    println!(
        "{{\"source\":\"hugepage_metadata_probe\",\"passed\":{},\"claim_grade\":{},\"hugepage_backed\":{},\"side_cache_allocated\":{},\"side_cache_address\":{},\"side_cache_mapping_size\":{},\"backing\":\"{}\",\"mmap_attempts\":{},\"mmap_successes\":{},\"mmap_fallbacks\":{},\"mmap_aligned_fallbacks\":{},\"mmap_advised_fallbacks\":{},\"mmap_fallback_failures\":{},\"mmap_hugetlb_syscalls\":{},\"mmap_hugetlb_skips\":{},\"last_error_stage\":\"{}\",\"last_error_code\":{},\"last_error_name\":\"{}\",\"reused_same_ptr\":{},\"typed_cache_inserts\":{},\"typed_cache_hits\":{},\"fallback_alignment\":{},\"aligned_fallback_supported\":{},\"side_cache_mapping_aligned_fallback_supported\":{},\"linux_smaps_mapping_found\":{},\"linux_smaps_kernel_page_kb\":{},\"linux_smaps_mmu_page_kb\":{},\"linux_smaps_anon_huge_kb\":{},\"linux_smaps_thp_eligible\":{},\"linux_smaps_vmflags_ht\":{},\"linux_smaps_vmflags_hg\":{},\"linux_smaps_vmflags_nh\":{},\"linux_prefaulted_pages\":{}}}",
        json_bool(validated),
        json_bool(claim_grade),
        json_bool(hugepage_backed),
        json_bool(side_cache_allocated),
        side_cache_address,
        side_cache_mapping_size,
        backing_name,
        stats.attempts,
        stats.successes,
        stats.fallbacks,
        stats.aligned_fallbacks,
        stats.advised_fallbacks,
        stats.fallback_failures,
        stats.hugetlb_mmap_syscalls,
        stats.hugetlb_mmap_skips,
        status.last_error_stage.as_str(),
        status.last_error_code,
        status.last_error_code_name(),
        json_bool(reused_same_ptr),
        semantic.typed_cache_inserts,
        semantic.typed_cache_hits,
        fallback_alignment,
        json_bool(aligned_fallback_supported),
        json_bool(side_cache_mapping_aligned_fallback_supported),
        json_bool(smaps.mapping_found),
        smaps.kernel_page_kb,
        smaps.mmu_page_kb,
        smaps.anon_huge_kb,
        match smaps.thp_eligible {
            Some(true) => "true",
            Some(false) => "false",
            None => "null",
        },
        json_bool(smaps.vmflags_ht),
        json_bool(smaps.vmflags_hg),
        json_bool(smaps.vmflags_nh),
        prefaulted_pages,
    );

    if validated {
        std::process::exit(0);
    }
    std::process::exit(1);
}
