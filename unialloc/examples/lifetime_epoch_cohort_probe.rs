#![cfg_attr(not(feature = "lifetime_hugepage"), allow(dead_code, unused_imports))]

#[cfg(all(feature = "lifetime_hugepage", not(feature = "fixed_heap")))]
mod probe {
    use std::alloc::Layout;
    use std::env;
    use std::fmt::Write as _;
    use std::hint::black_box;
    use std::ptr::{read_volatile, write_volatile};
    use std::time::Instant;
    use unialloc::{
        lifetime_hugepage_advance_epoch, lifetime_hugepage_configure,
        lifetime_hugepage_stats_reset, lifetime_hugepage_stats_snapshot, AllocationMetadata,
        LifetimeHugepagePolicy, LifetimeHugepageStatsSnapshot, SemanticAlloc, UniAlloc,
        LIFETIME_HINT_LONG_LIVED,
    };

    const SLOT_BYTES: usize = 4096;
    const SLOT_ALIGN: usize = 64;
    const LARGE_OBJECTS: usize = 496;
    const BRIDGE_OBJECTS: usize = 256;
    const TYPE_ID: u64 = 0xE0C0_2026_0714_0001;
    const MODULE_ID: u64 = 0xE0C0_2026_0714_0002;
    const CALLSITE_ID: u64 = 0xE0C0_2026_0714_0003;
    const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
    const FNV_PRIME: u64 = 0x100_0000_01b3;

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum PolicyName {
        LongHuge,
        EpochCohort,
    }

    impl PolicyName {
        fn parse(value: &str) -> Result<Self, String> {
            match value {
                "long-huge" => Ok(Self::LongHuge),
                "epoch-cohort" => Ok(Self::EpochCohort),
                _ => Err(format!("unknown --policy {value}")),
            }
        }

        fn as_str(self) -> &'static str {
            match self {
                Self::LongHuge => "long-huge",
                Self::EpochCohort => "epoch-cohort",
            }
        }

        fn runtime(self) -> LifetimeHugepagePolicy {
            match self {
                Self::LongHuge => LifetimeHugepagePolicy::LongLivedHugepage,
                Self::EpochCohort => LifetimeHugepagePolicy::EpochCohortHugepage,
            }
        }
    }

    struct Config {
        policy: PolicyName,
        warmup_cycles: usize,
        measured_cycles: usize,
        seed: u64,
        require_hugetlb: bool,
    }

    impl Default for Config {
        fn default() -> Self {
            Self {
                policy: PolicyName::LongHuge,
                warmup_cycles: 2,
                measured_cycles: 8,
                seed: 0x2026_0714,
                require_hugetlb: false,
            }
        }
    }

    impl Config {
        fn parse() -> Result<Self, String> {
            let mut config = Self::default();
            let mut args = env::args().skip(1);
            while let Some(arg) = args.next() {
                let value = |args: &mut std::iter::Skip<env::Args>, name: &str| {
                    args.next()
                        .ok_or_else(|| format!("missing value for {name}"))
                };
                match arg.as_str() {
                    "--policy" => config.policy = PolicyName::parse(&value(&mut args, &arg)?)?,
                    "--warmup-cycles" => {
                        config.warmup_cycles = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --warmup-cycles".to_string())?
                    }
                    "--measured-cycles" | "--cycles" => {
                        config.measured_cycles = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --measured-cycles".to_string())?
                    }
                    "--seed" => {
                        config.seed = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --seed".to_string())?
                    }
                    "--require-hugetlb" => config.require_hugetlb = true,
                    _ => return Err(format!("unknown argument {arg}")),
                }
            }
            if config.measured_cycles == 0 {
                return Err("--measured-cycles must be positive".to_string());
            }
            Ok(config)
        }
    }

    #[derive(Clone, Copy)]
    struct Allocation {
        ptr: *mut u8,
        sentinel: u64,
    }

    struct Cohort {
        values: Vec<Allocation>,
        birth_epoch: usize,
        cohort_id: u64,
    }

    #[derive(Clone, Copy, Default)]
    struct BoundaryAuc {
        retained_byte_epochs: u128,
        hugetlb_byte_epochs: u128,
        pinned_byte_epochs: u128,
        live_byte_epochs: u128,
    }

    impl BoundaryAuc {
        fn add(&mut self, stats: LifetimeHugepageStatsSnapshot) {
            self.retained_byte_epochs = self
                .retained_byte_epochs
                .saturating_add(stats.retained_bytes as u128);
            self.hugetlb_byte_epochs = self.hugetlb_byte_epochs.saturating_add(
                stats
                    .current_hugetlb_extents
                    .saturating_mul(unialloc::LIFETIME_HUGEPAGE_EXTENT_BYTES)
                    as u128,
            );
            self.pinned_byte_epochs = self
                .pinned_byte_epochs
                .saturating_add(stats.cohort_pinned_unassigned_region_bytes as u128);
            self.live_byte_epochs = self
                .live_byte_epochs
                .saturating_add(stats.live_slot_bytes as u128);
        }

        fn include(&mut self, other: Self) {
            self.retained_byte_epochs = self
                .retained_byte_epochs
                .saturating_add(other.retained_byte_epochs);
            self.hugetlb_byte_epochs = self
                .hugetlb_byte_epochs
                .saturating_add(other.hugetlb_byte_epochs);
            self.pinned_byte_epochs = self
                .pinned_byte_epochs
                .saturating_add(other.pinned_byte_epochs);
            self.live_byte_epochs = self.live_byte_epochs.saturating_add(other.live_byte_epochs);
        }
    }

    #[derive(Default)]
    struct Timings {
        bridge_allocation_ns: u128,
        large_allocation_ns: u128,
        large_release_ns: u128,
        bridge_release_ns: u128,
        epoch_advance_ns: u128,
        measured_lifecycle_ns: u128,
        measured_observed_cycle_ns: u128,
    }

    struct TraceDigest(u64);

    impl TraceDigest {
        fn new(seed: u64, warmup_cycles: usize, measured_cycles: usize) -> Self {
            let mut digest = Self(FNV_OFFSET);
            digest.record(0x434f_484f_5254_5631);
            digest.record(seed);
            digest.record(warmup_cycles as u64);
            digest.record(measured_cycles as u64);
            digest.record(SLOT_BYTES as u64);
            digest.record(LARGE_OBJECTS as u64);
            digest.record(BRIDGE_OBJECTS as u64);
            digest
        }

        fn record(&mut self, value: u64) {
            for byte in value.to_le_bytes() {
                self.0 ^= byte as u64;
                self.0 = self.0.wrapping_mul(FNV_PRIME);
            }
        }
    }

    fn splitmix64(mut value: u64) -> u64 {
        value = value.wrapping_add(0x9e37_79b9_7f4a_7c15);
        value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        value ^ (value >> 31)
    }

    fn cohort_metadata() -> AllocationMetadata {
        AllocationMetadata::for_type(TYPE_ID)
            .with_module(MODULE_ID)
            .with_callsite(CALLSITE_ID)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED)
    }

    unsafe fn allocate_cohort(
        alloc: &UniAlloc,
        layout: Layout,
        count: usize,
        cohort_id: u64,
        birth_epoch: usize,
        seed: u64,
        sentinel_touches: &mut usize,
        checksum: &mut u64,
    ) -> Result<Cohort, String> {
        let metadata = cohort_metadata();
        let mut values = Vec::with_capacity(count);
        for ordinal in 0..count {
            let ptr = alloc.alloc_with_metadata(layout, metadata);
            if ptr.is_null() {
                return Err(format!(
                    "allocation failed for cohort {cohort_id} object {ordinal}"
                ));
            }
            let sentinel = splitmix64(
                seed ^ cohort_id.rotate_left(21) ^ (ordinal as u64).wrapping_mul(FNV_PRIME),
            );
            write_volatile(ptr.cast::<u64>(), sentinel);
            write_volatile(
                ptr.add(SLOT_BYTES - std::mem::size_of::<u64>())
                    .cast::<u64>(),
                !sentinel,
            );
            *sentinel_touches = sentinel_touches.saturating_add(2);
            *checksum = checksum.wrapping_add(sentinel).rotate_left(7);
            values.push(Allocation { ptr, sentinel });
        }
        Ok(Cohort {
            values,
            birth_epoch,
            cohort_id,
        })
    }

    unsafe fn release_cohort(
        alloc: &UniAlloc,
        layout: Layout,
        cohort: &mut Cohort,
        current_epoch: usize,
        sentinel_touches: &mut usize,
        checksum: &mut u64,
    ) -> Result<(), String> {
        if current_epoch == cohort.birth_epoch {
            return Err(format!(
                "cohort {} was released in its allocation epoch {current_epoch}",
                cohort.cohort_id
            ));
        }
        let metadata = cohort_metadata();
        for allocation in cohort.values.drain(..) {
            let head = read_volatile(allocation.ptr.cast::<u64>());
            let tail = read_volatile(
                allocation
                    .ptr
                    .add(SLOT_BYTES - std::mem::size_of::<u64>())
                    .cast::<u64>(),
            );
            if head != allocation.sentinel || tail != !allocation.sentinel {
                return Err(format!("sentinel mismatch in cohort {}", cohort.cohort_id));
            }
            *sentinel_touches = sentinel_touches.saturating_add(2);
            *checksum = checksum.wrapping_add(head ^ tail).rotate_left(11);
            alloc.dealloc_with_metadata(allocation.ptr, layout, metadata);
        }
        Ok(())
    }

    fn record_allocation_trace(
        trace: &mut TraceDigest,
        cohort_id: u64,
        birth_epoch: usize,
        count: usize,
        seed: u64,
    ) {
        trace.record(0xA110_C000_0000_0001);
        trace.record(cohort_id);
        trace.record(birth_epoch as u64);
        trace.record(count as u64);
        for ordinal in 0..count {
            trace.record(splitmix64(
                seed ^ cohort_id.rotate_left(21) ^ (ordinal as u64).wrapping_mul(FNV_PRIME),
            ));
        }
    }

    fn record_release_trace(trace: &mut TraceDigest, cohort: &Cohort, current_epoch: usize) {
        trace.record(0xF3EE_C000_0000_0001);
        trace.record(cohort.cohort_id);
        trace.record(cohort.birth_epoch as u64);
        trace.record(current_epoch as u64);
        trace.record(cohort.values.len() as u64);
    }

    fn advance_epoch(trace: &mut TraceDigest, event: u64) -> (usize, u128) {
        trace.record(event);
        let start = Instant::now();
        let epoch = lifetime_hugepage_advance_epoch();
        let elapsed = start.elapsed().as_nanos();
        trace.record(epoch as u64);
        (epoch, elapsed)
    }

    fn stats_consistent(stats: LifetimeHugepageStatsSnapshot) -> bool {
        let regions_per_extent = unialloc::LIFETIME_HUGEPAGE_EXTENT_BYTES
            / unialloc::LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
        let total_regions = stats.current_extents.saturating_mul(regions_per_extent);
        stats.current_extents
            == stats
                .current_hugetlb_extents
                .saturating_add(stats.current_ordinary_extents)
            && stats.retained_bytes
                == stats
                    .current_extents
                    .saturating_mul(unialloc::LIFETIME_HUGEPAGE_EXTENT_BYTES)
            && stats.live_objects == stats.live_long_lived_objects
            && stats.live_ephemeral_objects == 0
            && stats.live_slot_bytes == stats.live_objects.saturating_mul(SLOT_BYTES)
            && stats.current_identity_regions <= total_regions
            && stats
                .reusable_unassigned_region_bytes
                .saturating_add(stats.cohort_pinned_unassigned_region_bytes)
                == total_regions
                    .saturating_sub(stats.current_identity_regions)
                    .saturating_mul(unialloc::LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES)
            && stats.assigned_region_slack_bytes
                == stats
                    .current_identity_regions
                    .saturating_mul(unialloc::LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES)
                    .saturating_sub(stats.live_slot_bytes)
            && stats.retained_slack_bytes
                == stats
                    .reusable_unassigned_region_bytes
                    .saturating_add(stats.cohort_pinned_unassigned_region_bytes)
                    .saturating_add(stats.assigned_region_slack_bytes)
    }

    fn boundary_consistent(stats: LifetimeHugepageStatsSnapshot, live_objects: usize) -> bool {
        stats_consistent(stats)
            && stats.live_objects == live_objects
            && stats.live_slot_bytes == live_objects.saturating_mul(SLOT_BYTES)
    }

    fn delta(after: usize, before: usize) -> usize {
        after.saturating_sub(before)
    }

    fn field<T: std::fmt::Display>(output: &mut String, first: &mut bool, name: &str, value: T) {
        if !*first {
            output.push(',');
        }
        *first = false;
        write!(output, "\"{name}\":{value}").expect("String writes cannot fail");
    }

    fn string_field(output: &mut String, first: &mut bool, name: &str, value: &str) {
        if !*first {
            output.push(',');
        }
        *first = false;
        write!(output, "\"{name}\":\"{value}\"").expect("String writes cannot fail");
    }

    fn bool_field(output: &mut String, first: &mut bool, name: &str, value: bool) {
        field(output, first, name, if value { "true" } else { "false" });
    }

    fn auc_fields(output: &mut String, first: &mut bool, prefix: &str, auc: BoundaryAuc) {
        field(
            output,
            first,
            &format!("{prefix}_retained_byte_epochs"),
            auc.retained_byte_epochs,
        );
        field(
            output,
            first,
            &format!("{prefix}_hugetlb_byte_epochs"),
            auc.hugetlb_byte_epochs,
        );
        field(
            output,
            first,
            &format!("{prefix}_pinned_byte_epochs"),
            auc.pinned_byte_epochs,
        );
        field(
            output,
            first,
            &format!("{prefix}_live_byte_epochs"),
            auc.live_byte_epochs,
        );
    }

    pub fn run() -> Result<(), String> {
        let config = Config::parse()?;
        let slots_per_region = unialloc::LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES / SLOT_BYTES;
        let regions_per_extent = unialloc::LIFETIME_HUGEPAGE_EXTENT_BYTES
            / unialloc::LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
        let geometry_gate_passed = unialloc::LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES % SLOT_BYTES
            == 0
            && LARGE_OBJECTS % slots_per_region == 0
            && BRIDGE_OBJECTS % slots_per_region == 0
            && LARGE_OBJECTS / slots_per_region == 31
            && BRIDGE_OBJECTS / slots_per_region == 16
            && regions_per_extent == 32;
        if !geometry_gate_passed {
            return Err(
                "allocator constants no longer match the rolling-cohort geometry".to_string(),
            );
        }

        let layout = Layout::from_size_align(SLOT_BYTES, SLOT_ALIGN)
            .map_err(|_| "invalid fixed workload layout".to_string())?;
        if !lifetime_hugepage_stats_reset() || !lifetime_hugepage_configure(config.policy.runtime())
        {
            return Err("lifetime arena was already active".to_string());
        }

        let alloc = UniAlloc::new();
        let total_cycles = config.warmup_cycles.saturating_add(config.measured_cycles);
        let total_allocations = LARGE_OBJECTS.saturating_add(
            total_cycles.saturating_mul(LARGE_OBJECTS.saturating_add(BRIDGE_OBJECTS)),
        );
        let measured_allocation_objects = config
            .measured_cycles
            .saturating_mul(LARGE_OBJECTS.saturating_add(BRIDGE_OBJECTS));
        let mut trace = TraceDigest::new(config.seed, config.warmup_cycles, config.measured_cycles);
        let mut sentinel_touches = 0usize;
        let mut checksum = 0u64;
        let mut boundaries_consistent = true;
        let mut overlap_auc = BoundaryAuc::default();
        let mut target_auc = BoundaryAuc::default();
        let mut reset_auc = BoundaryAuc::default();
        let mut timings = Timings::default();

        let mut current_epoch = lifetime_hugepage_stats_snapshot().current_epoch;
        record_allocation_trace(
            &mut trace,
            0x1000_0000,
            current_epoch,
            LARGE_OBJECTS,
            config.seed,
        );
        let initial_allocation_start = Instant::now();
        let mut large = unsafe {
            allocate_cohort(
                &alloc,
                layout,
                LARGE_OBJECTS,
                0x1000_0000,
                current_epoch,
                config.seed,
                &mut sentinel_touches,
                &mut checksum,
            )?
        };
        let initial_allocation_ns = initial_allocation_start.elapsed().as_nanos();
        let (advanced_epoch, initial_advance_ns) = advance_epoch(&mut trace, 0xE000_0000);
        current_epoch = advanced_epoch;

        let mut measurement_baseline = None;
        for cycle in 0..total_cycles {
            let measured = cycle >= config.warmup_cycles;
            if measured && measurement_baseline.is_none() {
                measurement_baseline = Some(lifetime_hugepage_stats_snapshot());
            }
            let bridge_id = 0x2000_0000u64.wrapping_add(cycle as u64);
            record_allocation_trace(
                &mut trace,
                bridge_id,
                current_epoch,
                BRIDGE_OBJECTS,
                config.seed,
            );
            let cycle_start = Instant::now();
            let bridge_allocation_start = Instant::now();
            let mut bridge = unsafe {
                allocate_cohort(
                    &alloc,
                    layout,
                    BRIDGE_OBJECTS,
                    bridge_id,
                    current_epoch,
                    config.seed,
                    &mut sentinel_touches,
                    &mut checksum,
                )?
            };
            let bridge_allocation_ns = bridge_allocation_start.elapsed().as_nanos();
            let overlap = lifetime_hugepage_stats_snapshot();
            boundaries_consistent &=
                boundary_consistent(overlap, LARGE_OBJECTS.saturating_add(BRIDGE_OBJECTS));
            let (advanced_epoch, overlap_advance_ns) =
                advance_epoch(&mut trace, 0xE100_0000u64.wrapping_add(cycle as u64));
            current_epoch = advanced_epoch;

            record_release_trace(&mut trace, &large, current_epoch);
            let large_release_start = Instant::now();
            unsafe {
                release_cohort(
                    &alloc,
                    layout,
                    &mut large,
                    current_epoch,
                    &mut sentinel_touches,
                    &mut checksum,
                )?
            };
            let large_release_ns = large_release_start.elapsed().as_nanos();
            let target = lifetime_hugepage_stats_snapshot();
            boundaries_consistent &= boundary_consistent(target, BRIDGE_OBJECTS);
            let (advanced_epoch, target_advance_ns) =
                advance_epoch(&mut trace, 0xE200_0000u64.wrapping_add(cycle as u64));
            current_epoch = advanced_epoch;

            let next_large_id = 0x3000_0000u64.wrapping_add(cycle as u64);
            record_allocation_trace(
                &mut trace,
                next_large_id,
                current_epoch,
                LARGE_OBJECTS,
                config.seed,
            );
            let large_allocation_start = Instant::now();
            large = unsafe {
                allocate_cohort(
                    &alloc,
                    layout,
                    LARGE_OBJECTS,
                    next_large_id,
                    current_epoch,
                    config.seed,
                    &mut sentinel_touches,
                    &mut checksum,
                )?
            };
            let large_allocation_ns = large_allocation_start.elapsed().as_nanos();
            record_release_trace(&mut trace, &bridge, current_epoch);
            let bridge_release_start = Instant::now();
            unsafe {
                release_cohort(
                    &alloc,
                    layout,
                    &mut bridge,
                    current_epoch,
                    &mut sentinel_touches,
                    &mut checksum,
                )?
            };
            let bridge_release_ns = bridge_release_start.elapsed().as_nanos();
            let reset = lifetime_hugepage_stats_snapshot();
            boundaries_consistent &= boundary_consistent(reset, LARGE_OBJECTS);
            let (advanced_epoch, reset_advance_ns) =
                advance_epoch(&mut trace, 0xE300_0000u64.wrapping_add(cycle as u64));
            current_epoch = advanced_epoch;

            if measured {
                overlap_auc.add(overlap);
                target_auc.add(target);
                reset_auc.add(reset);
                timings.bridge_allocation_ns = timings
                    .bridge_allocation_ns
                    .saturating_add(bridge_allocation_ns);
                timings.large_allocation_ns = timings
                    .large_allocation_ns
                    .saturating_add(large_allocation_ns);
                timings.large_release_ns =
                    timings.large_release_ns.saturating_add(large_release_ns);
                timings.bridge_release_ns =
                    timings.bridge_release_ns.saturating_add(bridge_release_ns);
                timings.epoch_advance_ns = timings
                    .epoch_advance_ns
                    .saturating_add(overlap_advance_ns)
                    .saturating_add(target_advance_ns)
                    .saturating_add(reset_advance_ns);
                timings.measured_observed_cycle_ns = timings
                    .measured_observed_cycle_ns
                    .saturating_add(cycle_start.elapsed().as_nanos());
            }
            black_box(cycle);
        }

        let measurement_end = lifetime_hugepage_stats_snapshot();
        let measurement_baseline = measurement_baseline
            .ok_or_else(|| "measurement baseline was not captured".to_string())?;
        record_release_trace(&mut trace, &large, current_epoch);
        let final_release_start = Instant::now();
        unsafe {
            release_cohort(
                &alloc,
                layout,
                &mut large,
                current_epoch,
                &mut sentinel_touches,
                &mut checksum,
            )?
        };
        let final_release_ns = final_release_start.elapsed().as_nanos();
        let final_stats = lifetime_hugepage_stats_snapshot();

        let expected_phase_advances = 1usize.saturating_add(total_cycles.saturating_mul(3));
        let release_gate_passed = final_stats.all_mappings_released
            && final_stats.current_extents == 0
            && final_stats.current_identity_regions == 0
            && final_stats.live_objects == 0
            && final_stats.live_slot_bytes == 0
            && final_stats.routed_allocations == total_allocations
            && final_stats.routed_deallocations == total_allocations
            && final_stats.routed_allocations
                == final_stats
                    .slot_bump_allocations
                    .saturating_add(final_stats.slot_reuse_hits)
            && final_stats.identity_region_assignments == final_stats.identity_region_releases
            && final_stats.extent_unmaps
                == final_stats
                    .hugetlb_extent_mappings
                    .saturating_add(final_stats.ordinary_extent_mappings)
                    .saturating_add(final_stats.hugetlb_fallback_extent_mappings)
            && final_stats.extent_unmap_failures == 0;
        let runtime_validation_gate_passed = final_stats.runtime_validated_objects
            == total_allocations
            && final_stats.runtime_validated_bytes == total_allocations.saturating_mul(SLOT_BYTES)
            && final_stats.runtime_validation_excluded_objects == 0
            && final_stats.runtime_validation_excluded_bytes == 0
            && final_stats.predictor_true_positive_objects == total_allocations
            && final_stats.predictor_true_positive_bytes
                == total_allocations.saturating_mul(SLOT_BYTES)
            && final_stats.predictor_true_negative_objects == 0
            && final_stats.predictor_false_positive_objects == 0
            && final_stats.predictor_false_negative_objects == 0
            && final_stats
                .placement_true_positive_objects
                .saturating_add(final_stats.placement_false_negative_objects)
                == total_allocations
            && final_stats
                .placement_true_positive_bytes
                .saturating_add(final_stats.placement_false_negative_bytes)
                == total_allocations.saturating_mul(SLOT_BYTES)
            && final_stats.placement_true_negative_objects == 0
            && final_stats.placement_false_positive_objects == 0
            && final_stats.phase_advances == expected_phase_advances;
        let hugetlb_gate_passed = final_stats.hugetlb_extent_mappings != 0
            && final_stats.hugetlb_fallback_extent_mappings == 0
            && final_stats.ordinary_extent_mappings == 0
            && final_stats.mapping_failures == 0
            && final_stats.allocation_fallbacks == 0
            && final_stats.unsupported_layout_bypasses == 0
            && final_stats.unknown_bypasses == 0
            && final_stats.nohugepage_advice_failures == 0
            && final_stats.placement_true_positive_objects == total_allocations
            && final_stats.placement_true_positive_bytes
                == total_allocations.saturating_mul(SLOT_BYTES)
            && final_stats.placement_false_negative_objects == 0;
        let accounting_consistent = boundaries_consistent
            && stats_consistent(measurement_baseline)
            && stats_consistent(measurement_end)
            && stats_consistent(final_stats);
        let passed = geometry_gate_passed
            && release_gate_passed
            && runtime_validation_gate_passed
            && accounting_consistent
            && (!config.require_hugetlb || hugetlb_gate_passed);

        // The primary boundary integral covers every post-release observation:
        // the bridge-only target and the next-large reset. Keep the overlap
        // snapshot in a separate full-cycle integral so both denominators are
        // explicit in the evaluation artifact.
        let mut all_boundary_auc = target_auc;
        all_boundary_auc.include(reset_auc);
        let mut full_cycle_auc = overlap_auc;
        full_cycle_auc.include(all_boundary_auc);
        let measured_allocation_ns = timings
            .bridge_allocation_ns
            .saturating_add(timings.large_allocation_ns);
        let measured_release_ns = timings
            .large_release_ns
            .saturating_add(timings.bridge_release_ns);
        timings.measured_lifecycle_ns = measured_allocation_ns
            .saturating_add(measured_release_ns)
            .saturating_add(timings.epoch_advance_ns);
        let measured_lifecycle_ns_per_object =
            timings.measured_lifecycle_ns as f64 / measured_allocation_objects as f64;

        let mut output = String::from("{");
        let mut first = true;
        string_field(
            &mut output,
            &mut first,
            "source",
            "lifetime_epoch_cohort_probe",
        );
        bool_field(&mut output, &mut first, "passed", passed);
        bool_field(
            &mut output,
            &mut first,
            "geometry_gate_passed",
            geometry_gate_passed,
        );
        bool_field(
            &mut output,
            &mut first,
            "accounting_consistent",
            accounting_consistent,
        );
        bool_field(
            &mut output,
            &mut first,
            "release_gate_passed",
            release_gate_passed,
        );
        bool_field(
            &mut output,
            &mut first,
            "runtime_validation_gate_passed",
            runtime_validation_gate_passed,
        );
        bool_field(
            &mut output,
            &mut first,
            "hugetlb_gate_passed",
            hugetlb_gate_passed,
        );
        bool_field(
            &mut output,
            &mut first,
            "hugetlb_required",
            config.require_hugetlb,
        );
        string_field(&mut output, &mut first, "policy", config.policy.as_str());
        field(
            &mut output,
            &mut first,
            "warmup_cycles",
            config.warmup_cycles,
        );
        field(
            &mut output,
            &mut first,
            "measured_cycles",
            config.measured_cycles,
        );
        field(&mut output, &mut first, "seed", config.seed);
        string_field(
            &mut output,
            &mut first,
            "trace_digest",
            &format!("{:016x}", trace.0),
        );
        field(&mut output, &mut first, "slot_bytes", SLOT_BYTES);
        field(
            &mut output,
            &mut first,
            "slots_per_region",
            slots_per_region,
        );
        field(
            &mut output,
            &mut first,
            "regions_per_extent",
            regions_per_extent,
        );
        field(&mut output, &mut first, "large_objects", LARGE_OBJECTS);
        field(
            &mut output,
            &mut first,
            "large_regions",
            LARGE_OBJECTS / slots_per_region,
        );
        field(&mut output, &mut first, "bridge_objects", BRIDGE_OBJECTS);
        field(
            &mut output,
            &mut first,
            "bridge_regions",
            BRIDGE_OBJECTS / slots_per_region,
        );
        field(
            &mut output,
            &mut first,
            "total_allocations",
            total_allocations,
        );
        field(
            &mut output,
            &mut first,
            "measured_allocation_objects",
            measured_allocation_objects,
        );
        auc_fields(&mut output, &mut first, "overlap", overlap_auc);
        auc_fields(&mut output, &mut first, "target", target_auc);
        auc_fields(&mut output, &mut first, "reset", reset_auc);
        auc_fields(&mut output, &mut first, "all_boundary", all_boundary_auc);
        string_field(
            &mut output,
            &mut first,
            "all_boundary_definition",
            "target_plus_reset",
        );
        auc_fields(&mut output, &mut first, "full_cycle", full_cycle_auc);
        string_field(
            &mut output,
            &mut first,
            "full_cycle_definition",
            "overlap_plus_target_plus_reset",
        );
        field(
            &mut output,
            &mut first,
            "initial_allocation_ns",
            initial_allocation_ns,
        );
        field(
            &mut output,
            &mut first,
            "initial_epoch_advance_ns",
            initial_advance_ns,
        );
        field(
            &mut output,
            &mut first,
            "measured_bridge_allocation_ns",
            timings.bridge_allocation_ns,
        );
        field(
            &mut output,
            &mut first,
            "measured_large_allocation_ns",
            timings.large_allocation_ns,
        );
        field(
            &mut output,
            &mut first,
            "measured_allocation_ns",
            measured_allocation_ns,
        );
        field(
            &mut output,
            &mut first,
            "measured_large_release_ns",
            timings.large_release_ns,
        );
        field(
            &mut output,
            &mut first,
            "measured_bridge_release_ns",
            timings.bridge_release_ns,
        );
        field(
            &mut output,
            &mut first,
            "measured_release_ns",
            measured_release_ns,
        );
        field(
            &mut output,
            &mut first,
            "measured_epoch_advance_ns",
            timings.epoch_advance_ns,
        );
        field(
            &mut output,
            &mut first,
            "measured_lifecycle_ns",
            timings.measured_lifecycle_ns,
        );
        field(
            &mut output,
            &mut first,
            "measured_observed_cycle_ns",
            timings.measured_observed_cycle_ns,
        );
        field(
            &mut output,
            &mut first,
            "measured_lifecycle_ns_per_object",
            format_args!("{measured_lifecycle_ns_per_object:.6}"),
        );
        field(
            &mut output,
            &mut first,
            "final_release_ns",
            final_release_ns,
        );
        field(
            &mut output,
            &mut first,
            "sentinel_touches",
            sentinel_touches,
        );
        field(&mut output, &mut first, "checksum", checksum);
        field(
            &mut output,
            &mut first,
            "measured_hugetlb_extent_mappings",
            delta(
                measurement_end.hugetlb_extent_mappings,
                measurement_baseline.hugetlb_extent_mappings,
            ),
        );
        field(
            &mut output,
            &mut first,
            "measured_epoch_cohort_extent_mappings",
            delta(
                measurement_end.epoch_cohort_extent_mappings,
                measurement_baseline.epoch_cohort_extent_mappings,
            ),
        );
        field(
            &mut output,
            &mut first,
            "measured_extent_unmaps",
            delta(
                measurement_end.extent_unmaps,
                measurement_baseline.extent_unmaps,
            ),
        );
        field(
            &mut output,
            &mut first,
            "measured_identity_region_assignments",
            delta(
                measurement_end.identity_region_assignments,
                measurement_baseline.identity_region_assignments,
            ),
        );
        field(
            &mut output,
            &mut first,
            "measured_identity_region_releases",
            delta(
                measurement_end.identity_region_releases,
                measurement_baseline.identity_region_releases,
            ),
        );
        field(
            &mut output,
            &mut first,
            "hugetlb_extent_mappings",
            final_stats.hugetlb_extent_mappings,
        );
        field(
            &mut output,
            &mut first,
            "hugetlb_fallback_extent_mappings",
            final_stats.hugetlb_fallback_extent_mappings,
        );
        field(
            &mut output,
            &mut first,
            "ordinary_extent_mappings",
            final_stats.ordinary_extent_mappings,
        );
        field(
            &mut output,
            &mut first,
            "epoch_cohort_extent_mappings",
            final_stats.epoch_cohort_extent_mappings,
        );
        field(
            &mut output,
            &mut first,
            "extent_unmaps",
            final_stats.extent_unmaps,
        );
        field(
            &mut output,
            &mut first,
            "mapping_failures",
            final_stats.mapping_failures,
        );
        field(
            &mut output,
            &mut first,
            "allocation_fallbacks",
            final_stats.allocation_fallbacks,
        );
        field(
            &mut output,
            &mut first,
            "extent_unmap_failures",
            final_stats.extent_unmap_failures,
        );
        field(
            &mut output,
            &mut first,
            "unsupported_layout_bypasses",
            final_stats.unsupported_layout_bypasses,
        );
        field(
            &mut output,
            &mut first,
            "unknown_bypasses",
            final_stats.unknown_bypasses,
        );
        field(
            &mut output,
            &mut first,
            "nohugepage_advice_failures",
            final_stats.nohugepage_advice_failures,
        );
        field(
            &mut output,
            &mut first,
            "routed_allocations",
            final_stats.routed_allocations,
        );
        field(
            &mut output,
            &mut first,
            "routed_deallocations",
            final_stats.routed_deallocations,
        );
        field(
            &mut output,
            &mut first,
            "identity_region_assignments",
            final_stats.identity_region_assignments,
        );
        field(
            &mut output,
            &mut first,
            "identity_region_releases",
            final_stats.identity_region_releases,
        );
        field(
            &mut output,
            &mut first,
            "slot_bump_allocations",
            final_stats.slot_bump_allocations,
        );
        field(
            &mut output,
            &mut first,
            "slot_reuse_hits",
            final_stats.slot_reuse_hits,
        );
        field(
            &mut output,
            &mut first,
            "runtime_validated_objects",
            final_stats.runtime_validated_objects,
        );
        field(
            &mut output,
            &mut first,
            "runtime_validated_bytes",
            final_stats.runtime_validated_bytes,
        );
        field(
            &mut output,
            &mut first,
            "runtime_validation_excluded_objects",
            final_stats.runtime_validation_excluded_objects,
        );
        field(
            &mut output,
            &mut first,
            "runtime_validation_excluded_bytes",
            final_stats.runtime_validation_excluded_bytes,
        );
        field(
            &mut output,
            &mut first,
            "predictor_tp_objects",
            final_stats.predictor_true_positive_objects,
        );
        field(
            &mut output,
            &mut first,
            "predictor_tp_bytes",
            final_stats.predictor_true_positive_bytes,
        );
        field(
            &mut output,
            &mut first,
            "predictor_tn_objects",
            final_stats.predictor_true_negative_objects,
        );
        field(
            &mut output,
            &mut first,
            "predictor_fp_objects",
            final_stats.predictor_false_positive_objects,
        );
        field(
            &mut output,
            &mut first,
            "predictor_fn_objects",
            final_stats.predictor_false_negative_objects,
        );
        field(
            &mut output,
            &mut first,
            "placement_tp_objects",
            final_stats.placement_true_positive_objects,
        );
        field(
            &mut output,
            &mut first,
            "placement_tp_bytes",
            final_stats.placement_true_positive_bytes,
        );
        field(
            &mut output,
            &mut first,
            "placement_tn_objects",
            final_stats.placement_true_negative_objects,
        );
        field(
            &mut output,
            &mut first,
            "placement_fp_objects",
            final_stats.placement_false_positive_objects,
        );
        field(
            &mut output,
            &mut first,
            "placement_fn_objects",
            final_stats.placement_false_negative_objects,
        );
        field(
            &mut output,
            &mut first,
            "phase_advances",
            final_stats.phase_advances,
        );
        field(
            &mut output,
            &mut first,
            "expected_phase_advances",
            expected_phase_advances,
        );
        field(
            &mut output,
            &mut first,
            "final_current_extents",
            final_stats.current_extents,
        );
        field(
            &mut output,
            &mut first,
            "final_live_objects",
            final_stats.live_objects,
        );
        bool_field(
            &mut output,
            &mut first,
            "own_mappings_released",
            final_stats.all_mappings_released,
        );
        output.push('}');
        println!("{output}");
        black_box(checksum);

        if !lifetime_hugepage_configure(LifetimeHugepagePolicy::Disabled) {
            return Err("arena policy could not be disabled after teardown".to_string());
        }
        if !passed {
            return Err("rolling cohort lifecycle or HugeTLB gate failed".to_string());
        }
        Ok(())
    }
}

#[cfg(all(feature = "lifetime_hugepage", not(feature = "fixed_heap")))]
fn main() {
    if let Err(error) = probe::run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

#[cfg(not(all(feature = "lifetime_hugepage", not(feature = "fixed_heap"))))]
fn main() {
    eprintln!("build with --features lifetime_hugepage on a hosted target");
    std::process::exit(2);
}
