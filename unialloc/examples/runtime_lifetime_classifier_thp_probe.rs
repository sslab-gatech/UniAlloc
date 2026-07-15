//! Host-gated marker-free learned-Long and realized-THP probe.

#[cfg(target_os = "linux")]
mod linux_probe {
    use std::{
        alloc::Layout,
        fs, thread,
        time::{Duration, Instant},
    };
    use unialloc::{
        lifetime_hugepage_configure, lifetime_hugepage_configure_with_backend,
        lifetime_hugepage_stats_reset, lifetime_hugepage_stats_snapshot, AllocationMetadata,
        LifetimeHugepagePolicy, LifetimePageBackend, SemanticAlloc, UniAlloc,
        LIFETIME_HUGEPAGE_EXTENT_BYTES,
    };

    const GLOBAL_THP_ENABLED: &str = "/sys/kernel/mm/transparent_hugepage/enabled";
    const PMD_THP_ENABLED: &str = "/sys/kernel/mm/transparent_hugepage/hugepages-2048kB/enabled";
    const THP_DEFRAG: &str = "/sys/kernel/mm/transparent_hugepage/defrag";
    const PMD_SIZE: &str = "/sys/kernel/mm/transparent_hugepage/hpage_pmd_size";
    const KHUGEPAGED_ALLOC_SLEEP: &str =
        "/sys/kernel/mm/transparent_hugepage/khugepaged/alloc_sleep_millisecs";
    const KHUGEPAGED_SCAN_SLEEP: &str =
        "/sys/kernel/mm/transparent_hugepage/khugepaged/scan_sleep_millisecs";
    const SMAPS: &str = "/proc/self/smaps";
    const LONG_SAMPLES: usize = 8;
    const PRESSURE_OBJECTS: usize = (8 * 1024 * 1024) / 4096;
    const EXTENT_OBJECTS: usize = 480;

    #[derive(Debug)]
    struct Region {
        start: usize,
        end: usize,
        anon_huge_kib: usize,
        kernel_page_kib: usize,
        mmu_page_kib: usize,
        vm_flags: String,
    }

    fn read_trimmed(path: &str) -> Result<String, String> {
        fs::read_to_string(path)
            .map(|value| value.trim().to_owned())
            .map_err(|error| format!("cannot read {path}: {error}"))
    }

    fn selected_mode(value: &str) -> Option<&str> {
        value
            .split_whitespace()
            .find_map(|word| word.strip_prefix('[')?.strip_suffix(']'))
    }

    fn effective_pmd_thp(global: &str, per_size: Option<&str>) -> bool {
        match per_size.and_then(selected_mode) {
            Some("always" | "madvise") => true,
            Some("inherit") | None => {
                matches!(selected_mode(global), Some("always" | "madvise"))
            }
            Some(_) => false,
        }
    }

    fn parse_kib(line: &str) -> usize {
        line.split_whitespace()
            .nth(1)
            .and_then(|value| value.parse().ok())
            .unwrap_or(0)
    }

    fn region_for(address: usize) -> Result<Option<Region>, String> {
        let text = read_trimmed(SMAPS)?;
        let mut current: Option<Region> = None;
        for line in text.lines() {
            let head = line.split_whitespace().next().unwrap_or("");
            if let Some((start, end)) = head.split_once('-') {
                if let (Ok(start), Ok(end)) = (
                    usize::from_str_radix(start, 16),
                    usize::from_str_radix(end, 16),
                ) {
                    if let Some(region) = current.take() {
                        if region.start <= address && address < region.end {
                            return Ok(Some(region));
                        }
                    }
                    current = Some(Region {
                        start,
                        end,
                        anon_huge_kib: 0,
                        kernel_page_kib: 0,
                        mmu_page_kib: 0,
                        vm_flags: String::new(),
                    });
                    continue;
                }
            }
            if let Some(region) = current.as_mut() {
                if line.starts_with("AnonHugePages:") {
                    region.anon_huge_kib = parse_kib(line);
                } else if line.starts_with("KernelPageSize:") {
                    region.kernel_page_kib = parse_kib(line);
                } else if line.starts_with("MMUPageSize:") {
                    region.mmu_page_kib = parse_kib(line);
                } else if let Some(flags) = line.strip_prefix("VmFlags:") {
                    region.vm_flags = flags.trim().to_owned();
                }
            }
        }
        Ok(current.filter(|region| region.start <= address && address < region.end))
    }

    fn json_string(value: &str) -> String {
        let mut encoded = String::with_capacity(value.len() + 2);
        encoded.push('"');
        for character in value.chars() {
            match character {
                '"' => encoded.push_str("\\\""),
                '\\' => encoded.push_str("\\\\"),
                '\n' => encoded.push_str("\\n"),
                '\r' => encoded.push_str("\\r"),
                '\t' => encoded.push_str("\\t"),
                value if value.is_control() => {
                    use std::fmt::Write;
                    write!(encoded, "\\u{:04x}", value as u32).unwrap();
                }
                value => encoded.push(value),
            }
        }
        encoded.push('"');
        encoded
    }

    fn skip(reason: &str) {
        println!(
            "{{\"schema_version\":1,\"verdict\":\"SKIP\",\"reason\":{}}}",
            json_string(reason)
        );
    }

    pub fn run() -> Result<(), String> {
        let global_thp = match read_trimmed(GLOBAL_THP_ENABLED) {
            Ok(value) => value,
            Err(reason) => {
                skip(&reason);
                return Ok(());
            }
        };
        let per_size_thp = read_trimmed(PMD_THP_ENABLED).ok();
        if !effective_pmd_thp(&global_thp, per_size_thp.as_deref()) {
            skip("effective 2 MiB THP mode is neither always nor madvise");
            return Ok(());
        }
        let pmd_size = read_trimmed(PMD_SIZE)?
            .parse::<usize>()
            .map_err(|error| format!("invalid PMD size: {error}"))?;
        if pmd_size != LIFETIME_HUGEPAGE_EXTENT_BYTES {
            skip("host PMD size differs from the allocator extent size");
            return Ok(());
        }
        fs::File::open(SMAPS).map_err(|error| format!("cannot open {SMAPS}: {error}"))?;

        let defrag = read_trimmed(THP_DEFRAG).unwrap_or_default();
        let alloc_sleep_ms = read_trimmed(KHUGEPAGED_ALLOC_SLEEP).unwrap_or_default();
        let scan_sleep_ms = read_trimmed(KHUGEPAGED_SCAN_SLEEP).unwrap_or_default();
        let alloc = UniAlloc::new();
        let layout = Layout::from_size_align(4096, 64).unwrap();
        let long = AllocationMetadata::for_type(0xADAA_0001)
            .with_lifetime_hint(0)
            .with_callsite(0xADAA_1001);
        let pressure = AllocationMetadata::for_type(0xADAA_0002)
            .with_lifetime_hint(0)
            .with_callsite(0xADAA_1002);

        if !lifetime_hugepage_stats_reset()
            || !lifetime_hugepage_configure_with_backend(
                LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
                LifetimePageBackend::TransparentHugepage,
            )
        {
            return Err("cannot configure a clean adaptive THP arena".to_owned());
        }
        unsafe {
            for _ in 0..LONG_SAMPLES {
                let survivor = alloc.alloc_with_metadata(layout, long);
                if survivor.is_null() {
                    return Err("survivor allocation failed during training".to_owned());
                }
                for _ in 0..PRESSURE_OBJECTS {
                    let pointer = alloc.alloc_with_metadata(layout, pressure);
                    if pointer.is_null() {
                        return Err("pressure allocation failed during training".to_owned());
                    }
                    alloc.dealloc_with_metadata(pointer, layout, pressure);
                }
                alloc.dealloc_with_metadata(survivor, layout, long);
            }
        }
        let trained = lifetime_hugepage_stats_snapshot();
        if trained.adaptive_long_sites != 1
            || trained.adaptive_long_observations < LONG_SAMPLES
            || trained.phase_advances != 0
            || !trained.all_mappings_released
        {
            return Err(format!(
                "training contract failed: long_sites={} long_observations={} phase_advances={} released={}",
                trained.adaptive_long_sites,
                trained.adaptive_long_observations,
                trained.phase_advances,
                trained.all_mappings_released
            ));
        }

        if !lifetime_hugepage_stats_reset() {
            return Err("cannot start the application measurement window".to_owned());
        }
        let mut live = Vec::with_capacity(EXTENT_OBJECTS);
        unsafe {
            for _ in 0..EXTENT_OBJECTS {
                let pointer = alloc.alloc_with_metadata(layout, long);
                if pointer.is_null() {
                    return Err("learned-Long allocation failed".to_owned());
                }
                std::ptr::write_bytes(pointer, 0xa5, layout.size());
                live.push(pointer);
            }
        }
        let routed = lifetime_hugepage_stats_snapshot();
        if routed.adaptive_long_routed_allocations != EXTENT_OBJECTS
            || routed.current_thp_extents != 1
            || routed.thp_extent_mappings != 1
            || routed.thp_advice_successes < 1
        {
            return Err(format!(
                "learned-Long route failed: routed={} current_thp={} mappings={} advice={}",
                routed.adaptive_long_routed_allocations,
                routed.current_thp_extents,
                routed.thp_extent_mappings,
                routed.thp_advice_successes
            ));
        }

        let extent_base = (live[0] as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
        let started = Instant::now();
        let (region, verdict) = loop {
            let region = region_for(extent_base)?
                .ok_or_else(|| "learned-Long extent VMA disappeared".to_owned())?;
            if !region.vm_flags.split_whitespace().any(|flag| flag == "hg") {
                return Err("learned-Long VMA lacks MADV_HUGEPAGE advice".to_owned());
            }
            if region.anon_huge_kib >= LIFETIME_HUGEPAGE_EXTENT_BYTES / 1024 {
                break (region, "PASS");
            }
            if started.elapsed() >= Duration::from_secs(30) {
                break (region, "INCONCLUSIVE");
            }
            thread::sleep(Duration::from_millis(100));
        };

        unsafe {
            for pointer in live {
                alloc.dealloc_with_metadata(pointer, layout, long);
            }
        }
        let released = lifetime_hugepage_stats_snapshot();
        if !released.all_mappings_released || released.adaptive_live_trailers != 0 {
            return Err("probe cleanup left allocator mappings or trailers live".to_owned());
        }
        if !lifetime_hugepage_configure(LifetimeHugepagePolicy::Disabled) {
            return Err("probe could not disable the adaptive policy".to_owned());
        }

        println!(
            concat!(
                "{{\"schema_version\":1,\"verdict\":\"{}\",",
                "\"semantic_epoch_calls\":{},\"adaptive_pressure_epoch_advances\":{},",
                "\"adaptive_long_sites\":{},\"adaptive_long_observations\":{},",
                "\"adaptive_long_routed_allocations\":{},\"thp_extent_mappings\":{},",
                "\"thp_advice_successes\":{},\"vma_start\":\"0x{:x}\",",
                "\"vma_end\":\"0x{:x}\",\"anon_huge_pages_kib\":{},",
                "\"kernel_page_kib\":{},\"mmu_page_kib\":{},\"vm_flags\":{},",
                "\"poll_elapsed_ms\":{},\"all_mappings_released\":{},",
                "\"host\":{{\"global_thp_enabled\":{},\"pmd_thp_enabled\":{},",
                "\"defrag\":{},\"pmd_size_bytes\":{},",
                "\"khugepaged_alloc_sleep_ms\":{},\"khugepaged_scan_sleep_ms\":{}}}}}"
            ),
            verdict,
            trained.phase_advances,
            trained.adaptive_epoch_advances,
            trained.adaptive_long_sites,
            trained.adaptive_long_observations,
            routed.adaptive_long_routed_allocations,
            routed.thp_extent_mappings,
            routed.thp_advice_successes,
            region.start,
            region.end,
            region.anon_huge_kib,
            region.kernel_page_kib,
            region.mmu_page_kib,
            json_string(&region.vm_flags),
            started.elapsed().as_millis(),
            released.all_mappings_released,
            json_string(&global_thp),
            per_size_thp
                .as_deref()
                .map(json_string)
                .unwrap_or_else(|| "null".to_owned()),
            json_string(&defrag),
            pmd_size,
            json_string(&alloc_sleep_ms),
            json_string(&scan_sleep_ms),
        );
        Ok(())
    }
}

#[cfg(target_os = "linux")]
fn main() {
    if let Err(error) = linux_probe::run() {
        eprintln!("runtime lifetime classifier THP probe failed: {error}");
        std::process::exit(1);
    }
}

#[cfg(not(target_os = "linux"))]
fn main() {
    println!(
        "{\"schema_version\":1,\"verdict\":\"SKIP\",\"reason\":\"Linux /proc and THP sysfs are required\"}"
    );
}
