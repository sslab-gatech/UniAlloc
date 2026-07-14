//! Oracle-hint feasibility probe for lifetime-aware hugepage placement.
//!
//! This is deliberately a research harness, not a production allocator path.
//! It exercises real mappings while keeping the policy small enough to falsify:
//! long-lived and ephemeral fixed-size objects are either mixed, segregated, or
//! tiered between 2 MiB HugeTLB and ordinary-page arenas.

#[cfg(all(unix, not(feature = "fixed_heap")))]
mod probe {
    use std::env;
    use std::fs;
    use std::ptr;
    use std::time::Instant;

    use unialloc::{
        lifetime_placement_class, mmap_huge_with_backing, AllocationMetadata, HugePageMmapBacking,
        LifetimePlacementClass, LIFETIME_HINT_EPHEMERAL, LIFETIME_HINT_LONG_LIVED,
    };

    const EXTENT_BYTES: usize = 2 * 1024 * 1024;
    const LONG_TYPE_ID: u64 = 0x4c4f_4e47_4c49_5645;
    const EPHEMERAL_TYPE_ID: u64 = 0x4550_4845_4d45_5241;

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum RequestedBacking {
        Ordinary,
        Huge,
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum Policy {
        OrdinaryMixed,
        OrdinarySegregated,
        HugeMixed,
        HugeSegregated,
        Tiered,
    }

    impl Policy {
        fn parse(raw: &str) -> Result<Self, String> {
            match raw {
                "ordinary-mixed" => Ok(Self::OrdinaryMixed),
                "ordinary-segregated" => Ok(Self::OrdinarySegregated),
                "huge-mixed" => Ok(Self::HugeMixed),
                "huge-segregated" => Ok(Self::HugeSegregated),
                "tiered" => Ok(Self::Tiered),
                _ => Err(format!(
                    "unknown policy {raw:?}; expected ordinary-mixed, ordinary-segregated, huge-mixed, huge-segregated, or tiered"
                )),
            }
        }

        fn as_str(self) -> &'static str {
            match self {
                Self::OrdinaryMixed => "ordinary-mixed",
                Self::OrdinarySegregated => "ordinary-segregated",
                Self::HugeMixed => "huge-mixed",
                Self::HugeSegregated => "huge-segregated",
                Self::Tiered => "tiered",
            }
        }

        fn requests_hugepages(self) -> bool {
            matches!(self, Self::HugeMixed | Self::HugeSegregated | Self::Tiered)
        }

        fn arena_index(self, class: LifetimePlacementClass) -> usize {
            match class {
                LifetimePlacementClass::LongLived => 0,
                LifetimePlacementClass::Ephemeral | LifetimePlacementClass::Unknown => match self {
                    Self::OrdinarySegregated | Self::HugeSegregated | Self::Tiered => 1,
                    Self::OrdinaryMixed | Self::HugeMixed => 0,
                },
            }
        }
    }

    #[derive(Clone, Copy, Debug)]
    struct Config {
        policy: Policy,
        extents: usize,
        slot_bytes: usize,
        warmup_passes: usize,
        measured_passes: usize,
        require_hugetlb: bool,
    }

    impl Config {
        fn parse() -> Result<Self, String> {
            let mut config = Self {
                policy: Policy::Tiered,
                extents: 256,
                slot_bytes: 4096,
                warmup_passes: 2,
                measured_passes: 16,
                require_hugetlb: false,
            };
            let mut args = env::args().skip(1);
            while let Some(arg) = args.next() {
                match arg.as_str() {
                    "--policy" => {
                        config.policy = Policy::parse(&next_value(&mut args, "--policy")?)?;
                    }
                    "--extents" => {
                        config.extents =
                            parse_usize(&next_value(&mut args, "--extents")?, "--extents")?;
                    }
                    "--slot-bytes" => {
                        config.slot_bytes =
                            parse_usize(&next_value(&mut args, "--slot-bytes")?, "--slot-bytes")?;
                    }
                    "--warmup-passes" => {
                        config.warmup_passes = parse_usize(
                            &next_value(&mut args, "--warmup-passes")?,
                            "--warmup-passes",
                        )?;
                    }
                    "--passes" => {
                        config.measured_passes =
                            parse_usize(&next_value(&mut args, "--passes")?, "--passes")?;
                    }
                    "--require-hugetlb" => config.require_hugetlb = true,
                    "--help" | "-h" => {
                        println!(
                            "usage: lifetime_hugepage_probe [--policy ordinary-mixed|ordinary-segregated|huge-mixed|huge-segregated|tiered] [--extents N] [--slot-bytes N] [--warmup-passes N] [--passes N] [--require-hugetlb]"
                        );
                        std::process::exit(0);
                    }
                    _ => return Err(format!("unknown argument {arg:?}")),
                }
            }
            config.validate()?;
            Ok(config)
        }

        fn validate(self) -> Result<(), String> {
            if self.extents == 0 || self.extents % 2 != 0 {
                return Err("--extents must be a non-zero even number".into());
            }
            if self.slot_bytes == 0
                || self.slot_bytes < std::mem::size_of::<*mut u8>()
                || EXTENT_BYTES % self.slot_bytes != 0
                || (EXTENT_BYTES / self.slot_bytes) % 2 != 0
            {
                return Err(
                    "--slot-bytes must evenly divide 2 MiB and yield an even slot count".into(),
                );
            }
            if self.measured_passes == 0 {
                return Err("--passes must be non-zero".into());
            }
            Ok(())
        }
    }

    fn next_value(args: &mut impl Iterator<Item = String>, flag: &str) -> Result<String, String> {
        args.next()
            .ok_or_else(|| format!("missing value for {flag}"))
    }

    fn parse_usize(raw: &str, flag: &str) -> Result<usize, String> {
        raw.parse::<usize>()
            .map_err(|_| format!("invalid integer for {flag}: {raw:?}"))
    }

    #[derive(Clone, Copy, Debug, Default)]
    struct MappingCounts {
        hugetlb: usize,
        ordinary: usize,
        fallbacks: usize,
        nohugepage_advice_failures: usize,
    }

    #[derive(Debug)]
    struct Extent {
        ptr: *mut u8,
        live: usize,
        next_slot: usize,
        actual_backing: HugePageMmapBacking,
    }

    impl Extent {
        fn is_hugetlb(&self) -> bool {
            self.actual_backing == HugePageMmapBacking::HugePage
        }

        fn is_mapped(&self) -> bool {
            !self.ptr.is_null()
        }
    }

    #[derive(Debug)]
    struct Arena {
        requested_backing: RequestedBacking,
        slot_bytes: usize,
        extents: Vec<Extent>,
        mapping_counts: MappingCounts,
    }

    impl Arena {
        fn new(requested_backing: RequestedBacking, slot_bytes: usize) -> Self {
            Self {
                requested_backing,
                slot_bytes,
                extents: Vec::new(),
                mapping_counts: MappingCounts::default(),
            }
        }

        fn capacity(&self) -> usize {
            EXTENT_BYTES / self.slot_bytes
        }

        unsafe fn allocate(&mut self) -> Result<(usize, *mut u8), String> {
            let needs_extent = self
                .extents
                .last()
                .map(|extent| extent.next_slot == self.capacity())
                .unwrap_or(true);
            if needs_extent {
                let extent = map_extent(self.requested_backing, &mut self.mapping_counts)?;
                self.extents.push(extent);
            }
            let extent_index = self.extents.len() - 1;
            let extent = &mut self.extents[extent_index];
            let ptr = extent.ptr.add(extent.next_slot * self.slot_bytes);
            extent.next_slot += 1;
            extent.live += 1;
            Ok((extent_index, ptr))
        }

        unsafe fn deallocate(&mut self, extent_index: usize) -> Result<(), String> {
            let extent = self
                .extents
                .get_mut(extent_index)
                .ok_or_else(|| "allocation referenced a missing extent".to_string())?;
            if !extent.is_mapped() || extent.live == 0 {
                return Err("allocation was released twice".into());
            }
            extent.live -= 1;
            Ok(())
        }

        unsafe fn reclaim_empty(&mut self) -> Result<MappingCounts, String> {
            let mut reclaimed = MappingCounts::default();
            for extent in &mut self.extents {
                if extent.is_mapped() && extent.live == 0 {
                    if libc::munmap(extent.ptr as *mut libc::c_void, EXTENT_BYTES) != 0 {
                        return Err(format!(
                            "munmap failed while reclaiming an extent: {}",
                            std::io::Error::last_os_error()
                        ));
                    }
                    if extent.is_hugetlb() {
                        reclaimed.hugetlb += 1;
                    } else {
                        reclaimed.ordinary += 1;
                    }
                    extent.ptr = ptr::null_mut();
                }
            }
            Ok(reclaimed)
        }

        unsafe fn release_all(&mut self) -> Result<MappingCounts, String> {
            let mut released = MappingCounts::default();
            for extent in &mut self.extents {
                if extent.is_mapped() {
                    if libc::munmap(extent.ptr as *mut libc::c_void, EXTENT_BYTES) != 0 {
                        return Err(format!(
                            "munmap failed while releasing an extent: {}",
                            std::io::Error::last_os_error()
                        ));
                    }
                    if extent.is_hugetlb() {
                        released.hugetlb += 1;
                    } else {
                        released.ordinary += 1;
                    }
                    extent.ptr = ptr::null_mut();
                }
            }
            Ok(released)
        }

        fn retained_counts(&self) -> MappingCounts {
            let mut retained = MappingCounts::default();
            for extent in self.extents.iter().filter(|extent| extent.is_mapped()) {
                if extent.is_hugetlb() {
                    retained.hugetlb += 1;
                } else {
                    retained.ordinary += 1;
                }
            }
            retained
        }

        fn stranded_huge_slots(&self) -> usize {
            self.extents
                .iter()
                .filter(|extent| extent.is_mapped() && extent.is_hugetlb())
                .map(|extent| self.capacity() - extent.live)
                .sum()
        }

        fn stranded_slots(&self) -> usize {
            self.extents
                .iter()
                .filter(|extent| extent.is_mapped())
                .map(|extent| self.capacity() - extent.live)
                .sum()
        }
    }

    impl Drop for Arena {
        fn drop(&mut self) {
            for extent in &mut self.extents {
                if extent.is_mapped() {
                    unsafe {
                        libc::munmap(extent.ptr as *mut libc::c_void, EXTENT_BYTES);
                    }
                    extent.ptr = ptr::null_mut();
                }
            }
        }
    }

    unsafe fn map_extent(
        requested: RequestedBacking,
        counts: &mut MappingCounts,
    ) -> Result<Extent, String> {
        let (ptr, backing) = match requested {
            RequestedBacking::Huge => {
                let result =
                    mmap_huge_with_backing(EXTENT_BYTES, libc::PROT_READ | libc::PROT_WRITE);
                if !result.is_success() {
                    return Err("hugepage mapping and fallback both failed".into());
                }
                if result.backing.is_hugepage() {
                    counts.hugetlb += 1;
                } else {
                    counts.ordinary += 1;
                    counts.fallbacks += 1;
                }
                (result.ptr, result.backing)
            }
            RequestedBacking::Ordinary => {
                let ptr = libc::mmap(
                    ptr::null_mut(),
                    EXTENT_BYTES,
                    libc::PROT_READ | libc::PROT_WRITE,
                    libc::MAP_PRIVATE | libc::MAP_ANONYMOUS,
                    -1,
                    0,
                ) as *mut u8;
                if ptr as *mut libc::c_void == libc::MAP_FAILED || ptr.is_null() {
                    return Err("ordinary mmap failed".into());
                }
                #[cfg(target_os = "linux")]
                if libc::madvise(
                    ptr as *mut libc::c_void,
                    EXTENT_BYTES,
                    libc::MADV_NOHUGEPAGE,
                ) != 0
                {
                    counts.nohugepage_advice_failures += 1;
                }
                counts.ordinary += 1;
                (ptr, HugePageMmapBacking::OrdinaryFallback)
            }
        };
        Ok(Extent {
            ptr,
            live: 0,
            next_slot: 0,
            actual_backing: backing,
        })
    }

    #[derive(Clone, Copy, Debug)]
    struct Allocation {
        arena_index: usize,
        extent_index: usize,
        ptr: *mut u8,
    }

    #[derive(Debug)]
    struct Placement {
        policy: Policy,
        arenas: Vec<Arena>,
        routed_long: usize,
        routed_ephemeral: usize,
        routed_unknown: usize,
    }

    impl Placement {
        fn new(policy: Policy, slot_bytes: usize) -> Self {
            let arenas = match policy {
                Policy::OrdinaryMixed => {
                    vec![Arena::new(RequestedBacking::Ordinary, slot_bytes)]
                }
                Policy::OrdinarySegregated => vec![
                    Arena::new(RequestedBacking::Ordinary, slot_bytes),
                    Arena::new(RequestedBacking::Ordinary, slot_bytes),
                ],
                Policy::HugeMixed => vec![Arena::new(RequestedBacking::Huge, slot_bytes)],
                Policy::HugeSegregated => vec![
                    Arena::new(RequestedBacking::Huge, slot_bytes),
                    Arena::new(RequestedBacking::Huge, slot_bytes),
                ],
                Policy::Tiered => vec![
                    Arena::new(RequestedBacking::Huge, slot_bytes),
                    Arena::new(RequestedBacking::Ordinary, slot_bytes),
                ],
            };
            Self {
                policy,
                arenas,
                routed_long: 0,
                routed_ephemeral: 0,
                routed_unknown: 0,
            }
        }

        unsafe fn allocate(&mut self, metadata: AllocationMetadata) -> Result<Allocation, String> {
            let class = lifetime_placement_class(metadata.lifetime_hint);
            match class {
                LifetimePlacementClass::LongLived => {
                    self.routed_long += 1;
                }
                LifetimePlacementClass::Ephemeral => {
                    self.routed_ephemeral += 1;
                }
                LifetimePlacementClass::Unknown => {
                    self.routed_unknown += 1;
                }
            }
            let arena_index = self.policy.arena_index(class);
            let (extent_index, ptr) = self.arenas[arena_index].allocate()?;
            Ok(Allocation {
                arena_index,
                extent_index,
                ptr,
            })
        }

        unsafe fn deallocate(&mut self, allocation: Allocation) -> Result<(), String> {
            self.arenas[allocation.arena_index].deallocate(allocation.extent_index)
        }

        unsafe fn reclaim_empty(&mut self) -> Result<MappingCounts, String> {
            let mut total = MappingCounts::default();
            for arena in &mut self.arenas {
                let counts = arena.reclaim_empty()?;
                total.hugetlb += counts.hugetlb;
                total.ordinary += counts.ordinary;
            }
            Ok(total)
        }

        unsafe fn release_all(&mut self) -> Result<MappingCounts, String> {
            let mut total = MappingCounts::default();
            for arena in &mut self.arenas {
                let counts = arena.release_all()?;
                total.hugetlb += counts.hugetlb;
                total.ordinary += counts.ordinary;
            }
            Ok(total)
        }

        fn peak_counts(&self) -> MappingCounts {
            let mut total = MappingCounts::default();
            for arena in &self.arenas {
                total.hugetlb += arena.mapping_counts.hugetlb;
                total.ordinary += arena.mapping_counts.ordinary;
                total.fallbacks += arena.mapping_counts.fallbacks;
                total.nohugepage_advice_failures += arena.mapping_counts.nohugepage_advice_failures;
            }
            total
        }

        fn retained_counts(&self) -> MappingCounts {
            let mut total = MappingCounts::default();
            for arena in &self.arenas {
                let counts = arena.retained_counts();
                total.hugetlb += counts.hugetlb;
                total.ordinary += counts.ordinary;
            }
            total
        }

        fn stranded_huge_slots(&self) -> usize {
            self.arenas.iter().map(Arena::stranded_huge_slots).sum()
        }

        fn stranded_slots(&self) -> usize {
            self.arenas.iter().map(Arena::stranded_slots).sum()
        }
    }

    fn hugepages_free() -> Option<usize> {
        let text = fs::read_to_string("/proc/meminfo").ok()?;
        text.lines().find_map(|line| {
            line.strip_prefix("HugePages_Free:")
                .and_then(|value| value.trim().parse::<usize>().ok())
        })
    }

    fn shuffle<T>(values: &mut [T]) {
        let mut state = 0xd1b5_4a32_d192_ed03u64;
        for i in (1..values.len()).rev() {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            values.swap(i, (state as usize) % (i + 1));
        }
    }

    unsafe fn build_pointer_cycle(pointers: &[*mut u8]) -> *mut u8 {
        for index in 0..pointers.len() {
            let next = pointers[(index + 1) % pointers.len()];
            ptr::write(pointers[index].cast::<*mut u8>(), next);
        }
        pointers[0]
    }

    unsafe fn touch_pointer_cycle(
        mut current: *mut u8,
        count: usize,
        checksum: &mut u64,
    ) -> *mut u8 {
        for _ in 0..count {
            current = ptr::read_volatile(current.cast::<*mut u8>());
            *checksum = checksum.wrapping_add((current as usize >> 12) as u64);
        }
        current
    }

    pub fn run() -> Result<(), String> {
        let config = Config::parse()?;
        let slots_per_extent = EXTENT_BYTES / config.slot_bytes;
        let total_objects = config
            .extents
            .checked_mul(slots_per_extent)
            .ok_or_else(|| "object count overflow".to_string())?;
        let long_metadata =
            AllocationMetadata::for_type(LONG_TYPE_ID).with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);
        let ephemeral_metadata = AllocationMetadata::for_type(EPHEMERAL_TYPE_ID)
            .with_lifetime_hint(LIFETIME_HINT_EPHEMERAL);
        let hugepages_before = hugepages_free();
        let mut placement = Placement::new(config.policy, config.slot_bytes);
        let mut long_pointers = Vec::with_capacity(total_objects / 2);
        let mut ephemeral_allocations = Vec::with_capacity(total_objects / 2);

        let allocation_start = Instant::now();
        for index in 0..total_objects {
            let long_lived = index % 2 == 0;
            let metadata = if long_lived {
                long_metadata
            } else {
                ephemeral_metadata
            };
            let allocation = unsafe { placement.allocate(metadata)? };
            unsafe { ptr::write_volatile(allocation.ptr, (index as u8).wrapping_add(1)) };
            if long_lived {
                long_pointers.push(allocation.ptr);
            } else {
                ephemeral_allocations.push(allocation);
            }
        }
        let allocation_ns = allocation_start.elapsed().as_nanos();

        let peak_counts = placement.peak_counts();
        if config.require_hugetlb && peak_counts.fallbacks != 0 {
            return Err(format!(
                "{} requested huge extents fell back to ordinary mappings",
                peak_counts.fallbacks
            ));
        }
        let hugepages_at_peak = hugepages_free();
        let ephemeral_release_start = Instant::now();
        for allocation in ephemeral_allocations.drain(..) {
            unsafe { placement.deallocate(allocation)? };
        }
        let reclaimed = unsafe { placement.reclaim_empty()? };
        let ephemeral_release_ns = ephemeral_release_start.elapsed().as_nanos();
        let hugepages_after_ephemeral_reclaim = hugepages_free();
        let retained = placement.retained_counts();
        let stranded_huge_slots = placement.stranded_huge_slots();
        let stranded_slots = placement.stranded_slots();

        shuffle(&mut long_pointers);
        let mut checksum = 0u64;
        let mut current = unsafe { build_pointer_cycle(&long_pointers) };
        for _ in 0..config.warmup_passes {
            current = unsafe { touch_pointer_cycle(current, long_pointers.len(), &mut checksum) };
        }
        let start = Instant::now();
        for _ in 0..config.measured_passes {
            current = unsafe { touch_pointer_cycle(current, long_pointers.len(), &mut checksum) };
        }
        checksum ^= current as usize as u64;
        let touch_ns = start.elapsed().as_nanos();
        let measured_touches = long_pointers.len() * config.measured_passes;
        let ns_per_touch = touch_ns as f64 / measured_touches as f64;
        let huge_retained_slots = retained.hugetlb * slots_per_extent;
        let huge_fragmentation_ratio = if huge_retained_slots == 0 {
            0.0
        } else {
            stranded_huge_slots as f64 / huge_retained_slots as f64
        };
        let retained_slots = (retained.hugetlb + retained.ordinary) * slots_per_extent;
        let total_fragmentation_ratio = if retained_slots == 0 {
            0.0
        } else {
            stranded_slots as f64 / retained_slots as f64
        };
        let routed_long = placement.routed_long;
        let routed_ephemeral = placement.routed_ephemeral;
        let routed_unknown = placement.routed_unknown;
        let routing_passed = routed_long == long_pointers.len()
            && routed_ephemeral == total_objects / 2
            && routed_unknown == 0;

        drop(long_pointers);
        let teardown_released = unsafe { placement.release_all()? };
        let own_mappings_released = reclaimed.hugetlb + teardown_released.hugetlb
            == peak_counts.hugetlb
            && reclaimed.ordinary + teardown_released.ordinary == peak_counts.ordinary;
        drop(placement);
        let hugepages_after_drop = hugepages_free();
        let pool_restored = match (hugepages_before, hugepages_after_drop) {
            (Some(before), Some(after)) => before == after,
            _ => !cfg!(target_os = "linux"),
        };
        let passed = routing_passed
            && peak_counts.nohugepage_advice_failures == 0
            && own_mappings_released
            && (!config.require_hugetlb
                || (peak_counts.fallbacks == 0
                    && (!config.policy.requests_hugepages() || peak_counts.hugetlb > 0)));
        println!(
            concat!(
                "{{\"source\":\"lifetime_hugepage_probe\",",
                "\"passed\":{},\"policy\":\"{}\",\"oracle_hints\":true,",
                "\"access_pattern\":\"random-dependent-pointer-cycle\",",
                "\"extent_bytes\":{},\"slot_bytes\":{},\"configured_extents\":{},",
                "\"total_objects\":{},\"long_objects\":{},\"ephemeral_objects\":{},",
                "\"routed_long\":{},\"routed_ephemeral\":{},\"routed_unknown\":{},",
                "\"peak_hugetlb_extents\":{},\"peak_ordinary_extents\":{},",
                "\"fallback_extents\":{},\"reclaimed_hugetlb_extents\":{},",
                "\"reclaimed_ordinary_extents\":{},\"teardown_hugetlb_extents\":{},",
                "\"teardown_ordinary_extents\":{},\"own_mappings_released\":{},",
                "\"retained_hugetlb_extents\":{},",
                "\"retained_ordinary_extents\":{},\"stranded_huge_slots\":{},",
                "\"stranded_slots\":{},\"retained_bytes\":{},\"live_bytes\":{},",
                "\"stranded_bytes\":{},\"huge_fragmentation_ratio\":{:.6},",
                "\"total_fragmentation_ratio\":{:.6},\"warmup_passes\":{},",
                "\"allocation_ns\":{},\"allocation_ns_per_object\":{:.6},",
                "\"ephemeral_release_ns\":{},\"release_ns_per_object\":{:.6},",
                "\"measured_passes\":{},\"measured_touches\":{},\"touch_ns\":{},",
                "\"ns_per_touch\":{:.6},\"checksum\":{},",
                "\"nohugepage_advice_failures\":{},\"hugepages_free_before\":{},",
                "\"hugepages_free_at_peak\":{},\"hugepages_free_after_ephemeral_reclaim\":{},",
                "\"hugepages_free_after_drop\":{},\"hugetlb_pool_restored\":{}}}"
            ),
            passed,
            config.policy.as_str(),
            EXTENT_BYTES,
            config.slot_bytes,
            config.extents,
            total_objects,
            total_objects / 2,
            total_objects / 2,
            routed_long,
            routed_ephemeral,
            routed_unknown,
            peak_counts.hugetlb,
            peak_counts.ordinary,
            peak_counts.fallbacks,
            reclaimed.hugetlb,
            reclaimed.ordinary,
            teardown_released.hugetlb,
            teardown_released.ordinary,
            own_mappings_released,
            retained.hugetlb,
            retained.ordinary,
            stranded_huge_slots,
            stranded_slots,
            retained_slots * config.slot_bytes,
            (total_objects / 2) * config.slot_bytes,
            stranded_slots * config.slot_bytes,
            huge_fragmentation_ratio,
            total_fragmentation_ratio,
            config.warmup_passes,
            allocation_ns,
            allocation_ns as f64 / total_objects as f64,
            ephemeral_release_ns,
            ephemeral_release_ns as f64 / (total_objects / 2) as f64,
            config.measured_passes,
            measured_touches,
            touch_ns,
            ns_per_touch,
            checksum,
            peak_counts.nohugepage_advice_failures,
            json_option(hugepages_before),
            json_option(hugepages_at_peak),
            json_option(hugepages_after_ephemeral_reclaim),
            json_option(hugepages_after_drop),
            pool_restored,
        );
        if passed {
            Ok(())
        } else {
            Err("probe invariants failed".into())
        }
    }

    fn json_option(value: Option<usize>) -> String {
        value
            .map(|value| value.to_string())
            .unwrap_or_else(|| "null".into())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn policy_routes_unknown_to_the_conservative_arena() {
            assert_eq!(
                Policy::Tiered.arena_index(LifetimePlacementClass::LongLived),
                0
            );
            assert_eq!(
                Policy::Tiered.arena_index(LifetimePlacementClass::Ephemeral),
                1
            );
            assert_eq!(
                Policy::Tiered.arena_index(LifetimePlacementClass::Unknown),
                1
            );
            assert_eq!(
                Policy::HugeMixed.arena_index(LifetimePlacementClass::Unknown),
                0
            );
        }

        #[test]
        fn segregated_ordinary_arenas_reclaim_the_ephemeral_extent() {
            let slot_bytes = 4096;
            let slots_per_extent = EXTENT_BYTES / slot_bytes;
            let total_objects = slots_per_extent * 2;
            let long_metadata = AllocationMetadata::for_type(LONG_TYPE_ID)
                .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);
            let ephemeral_metadata = AllocationMetadata::for_type(EPHEMERAL_TYPE_ID)
                .with_lifetime_hint(LIFETIME_HINT_EPHEMERAL);
            let mut placement = Placement::new(Policy::OrdinarySegregated, slot_bytes);
            let mut ephemeral = Vec::with_capacity(slots_per_extent);

            for index in 0..total_objects {
                let metadata = if index % 2 == 0 {
                    long_metadata
                } else {
                    ephemeral_metadata
                };
                let allocation = unsafe { placement.allocate(metadata).expect("map extent") };
                if index % 2 != 0 {
                    ephemeral.push(allocation);
                }
            }
            assert_eq!(placement.peak_counts().ordinary, 2);

            for allocation in ephemeral {
                unsafe { placement.deallocate(allocation).expect("release object") };
            }
            let reclaimed = unsafe { placement.reclaim_empty().expect("reclaim extent") };
            let retained = placement.retained_counts();
            assert_eq!(reclaimed.ordinary, 1);
            assert_eq!(retained.ordinary, 1);
            assert_eq!(placement.stranded_slots(), 0);

            let teardown = unsafe { placement.release_all().expect("release retained extent") };
            assert_eq!(teardown.ordinary, 1);
            assert_eq!(reclaimed.ordinary + teardown.ordinary, 2);
        }
    }
}

#[cfg(all(unix, not(feature = "fixed_heap")))]
fn main() {
    if let Err(error) = probe::run() {
        eprintln!("lifetime_hugepage_probe: {error}");
        std::process::exit(2);
    }
}

#[cfg(any(not(unix), feature = "fixed_heap"))]
fn main() {
    eprintln!(
        "lifetime_hugepage_probe requires a Unix mapping backend without the fixed_heap feature"
    );
    std::process::exit(2);
}
