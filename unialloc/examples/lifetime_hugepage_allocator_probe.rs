#![cfg_attr(not(feature = "lifetime_hugepage"), allow(dead_code, unused_imports))]

#[cfg(all(feature = "lifetime_hugepage", not(feature = "fixed_heap")))]
mod probe {
    use std::alloc::{GlobalAlloc, Layout};
    use std::env;
    use std::hint::black_box;
    use std::time::Instant;
    use unialloc::{
        lifetime_hugepage_advance_epoch, lifetime_hugepage_configure,
        lifetime_hugepage_stats_reset, lifetime_hugepage_stats_snapshot, AllocationMetadata,
        LifetimeHugepagePolicy, LifetimeHugepageStatsSnapshot, SemanticAlloc, UniAlloc,
        LIFETIME_HINT_EPHEMERAL, LIFETIME_HINT_LONG_LIVED,
    };

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum PolicyName {
        RawDefault,
        PolicyOff,
        OrdinarySegregated,
        AllHugeSegregated,
        LongHuge,
        EpochCohort,
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum IdentityMode {
        Exact,
        LifetimeOnly,
    }

    impl IdentityMode {
        fn parse(value: &str) -> Result<Self, String> {
            match value {
                "exact" => Ok(Self::Exact),
                "lifetime-only" => Ok(Self::LifetimeOnly),
                _ => Err(format!("unknown --identity-mode {value}")),
            }
        }

        fn as_str(self) -> &'static str {
            match self {
                Self::Exact => "exact",
                Self::LifetimeOnly => "lifetime-only",
            }
        }
    }

    impl PolicyName {
        fn parse(value: &str) -> Result<Self, String> {
            match value {
                "raw-default" => Ok(Self::RawDefault),
                "policy-off" => Ok(Self::PolicyOff),
                "ordinary-segregated" => Ok(Self::OrdinarySegregated),
                "all-huge-segregated" => Ok(Self::AllHugeSegregated),
                "long-huge" => Ok(Self::LongHuge),
                "epoch-cohort" => Ok(Self::EpochCohort),
                _ => Err(format!("unknown --policy {value}")),
            }
        }

        fn as_str(self) -> &'static str {
            match self {
                Self::RawDefault => "raw-default",
                Self::PolicyOff => "policy-off",
                Self::OrdinarySegregated => "ordinary-segregated",
                Self::AllHugeSegregated => "all-huge-segregated",
                Self::LongHuge => "long-huge",
                Self::EpochCohort => "epoch-cohort",
            }
        }

        fn runtime(self) -> LifetimeHugepagePolicy {
            match self {
                Self::RawDefault => LifetimeHugepagePolicy::Disabled,
                Self::PolicyOff => LifetimeHugepagePolicy::Disabled,
                Self::OrdinarySegregated => LifetimeHugepagePolicy::SegregatedOrdinary,
                Self::AllHugeSegregated => LifetimeHugepagePolicy::SegregatedHugepage,
                Self::LongHuge => LifetimeHugepagePolicy::LongLivedHugepage,
                Self::EpochCohort => LifetimeHugepagePolicy::EpochCohortHugepage,
            }
        }
    }

    struct Config {
        policy: PolicyName,
        identity_mode: IdentityMode,
        types_per_truth: usize,
        objects: usize,
        slot_bytes: usize,
        long_fraction: f64,
        false_long_rate: f64,
        false_short_rate: f64,
        confidence_threshold: u8,
        correct_confidence: u8,
        error_confidence: u8,
        confidence_overlap_rate: f64,
        unknown_rate: f64,
        ephemeral_waves: usize,
        warmup_passes: usize,
        measured_passes: usize,
        seed: u64,
        require_hugetlb: bool,
    }

    impl Default for Config {
        fn default() -> Self {
            Self {
                policy: PolicyName::LongHuge,
                identity_mode: IdentityMode::Exact,
                types_per_truth: 1,
                objects: 262_144,
                slot_bytes: 4096,
                long_fraction: 0.5,
                false_long_rate: 0.0,
                false_short_rate: 0.0,
                confidence_threshold: 0,
                correct_confidence: 95,
                error_confidence: 40,
                confidence_overlap_rate: 0.0,
                unknown_rate: 0.0,
                ephemeral_waves: 1,
                warmup_passes: 2,
                measured_passes: 8,
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
                    "--identity-mode" => {
                        config.identity_mode = IdentityMode::parse(&value(&mut args, &arg)?)?
                    }
                    "--types-per-truth" => {
                        config.types_per_truth = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --types-per-truth".to_string())?
                    }
                    "--objects" => {
                        config.objects = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --objects".to_string())?
                    }
                    "--slot-bytes" => {
                        config.slot_bytes = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --slot-bytes".to_string())?
                    }
                    "--long-fraction" => {
                        config.long_fraction = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --long-fraction".to_string())?
                    }
                    "--false-long-rate" => {
                        config.false_long_rate = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --false-long-rate".to_string())?
                    }
                    "--false-short-rate" => {
                        config.false_short_rate = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --false-short-rate".to_string())?
                    }
                    "--confidence-threshold" => {
                        config.confidence_threshold = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --confidence-threshold".to_string())?
                    }
                    "--correct-confidence" => {
                        config.correct_confidence = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --correct-confidence".to_string())?
                    }
                    "--error-confidence" => {
                        config.error_confidence = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --error-confidence".to_string())?
                    }
                    "--confidence-overlap-rate" => {
                        config.confidence_overlap_rate = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --confidence-overlap-rate".to_string())?
                    }
                    "--unknown-rate" => {
                        config.unknown_rate = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --unknown-rate".to_string())?
                    }
                    "--ephemeral-waves" => {
                        config.ephemeral_waves = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --ephemeral-waves".to_string())?
                    }
                    "--warmup-passes" => {
                        config.warmup_passes = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --warmup-passes".to_string())?
                    }
                    "--passes" => {
                        config.measured_passes = value(&mut args, &arg)?
                            .parse()
                            .map_err(|_| "invalid --passes".to_string())?
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
            if config.objects < 2
                || config.types_per_truth == 0
                || config.slot_bytes < std::mem::size_of::<usize>()
                || config.long_fraction <= 0.0
                || config.long_fraction >= 1.0
                || !(0.0..=1.0).contains(&config.false_long_rate)
                || !(0.0..=1.0).contains(&config.false_short_rate)
                || config.confidence_threshold > 100
                || !(1..=100).contains(&config.correct_confidence)
                || !(1..=100).contains(&config.error_confidence)
                || !(0.0..=1.0).contains(&config.confidence_overlap_rate)
                || !(0.0..=1.0).contains(&config.unknown_rate)
                || config.ephemeral_waves == 0
                || config.measured_passes == 0
            {
                return Err("invalid workload geometry or rate".to_string());
            }
            Ok(config)
        }
    }

    #[derive(Clone, Copy)]
    struct Allocation {
        ptr: *mut u8,
        metadata: AllocationMetadata,
        semantic: bool,
    }

    struct PredictionCounts {
        classified: usize,
        unknown: usize,
        unknown_long: usize,
        unknown_short: usize,
        true_positive: usize,
        true_negative: usize,
        false_positive: usize,
        false_negative: usize,
        trace_digest: u64,
    }

    impl Default for PredictionCounts {
        fn default() -> Self {
            Self {
                classified: 0,
                unknown: 0,
                unknown_long: 0,
                unknown_short: 0,
                true_positive: 0,
                true_negative: 0,
                false_positive: 0,
                false_negative: 0,
                trace_digest: 0xcbf2_9ce4_8422_2325,
            }
        }
    }

    impl PredictionCounts {
        fn record(
            &mut self,
            truth_long: bool,
            predicted_long: bool,
            confidence: u8,
            forced_unknown: bool,
            classified: bool,
        ) {
            let event = u64::from(truth_long)
                | (u64::from(predicted_long) << 1)
                | (u64::from(confidence) << 8)
                | (u64::from(forced_unknown) << 16);
            self.trace_digest ^= event;
            self.trace_digest = self.trace_digest.wrapping_mul(0x100_0000_01b3);
            if !classified {
                self.unknown += 1;
                if truth_long {
                    self.unknown_long += 1;
                } else {
                    self.unknown_short += 1;
                }
                return;
            }
            self.classified += 1;
            match (predicted_long, truth_long) {
                (true, true) => self.true_positive += 1,
                (false, false) => self.true_negative += 1,
                (true, false) => self.false_positive += 1,
                (false, true) => self.false_negative += 1,
            }
        }

        fn successes(&self) -> usize {
            self.true_positive.saturating_add(self.true_negative)
        }

        fn failures(&self) -> usize {
            self.false_positive.saturating_add(self.false_negative)
        }
    }

    struct Prediction {
        metadata: AllocationMetadata,
        classified: bool,
        predicted_long: bool,
    }

    struct PlacementCounts {
        true_positive: usize,
        true_negative: usize,
        false_positive: usize,
        false_negative: usize,
    }

    fn effective_placement_counts(
        policy: PolicyName,
        predictions: &PredictionCounts,
    ) -> PlacementCounts {
        match policy {
            PolicyName::RawDefault | PolicyName::PolicyOff | PolicyName::OrdinarySegregated => {
                PlacementCounts {
                    true_positive: 0,
                    true_negative: predictions
                        .true_negative
                        .saturating_add(predictions.false_positive)
                        .saturating_add(predictions.unknown_short),
                    false_positive: 0,
                    false_negative: predictions
                        .true_positive
                        .saturating_add(predictions.false_negative)
                        .saturating_add(predictions.unknown_long),
                }
            }
            PolicyName::AllHugeSegregated => PlacementCounts {
                true_positive: predictions
                    .true_positive
                    .saturating_add(predictions.false_negative),
                true_negative: predictions.unknown_short,
                false_positive: predictions
                    .true_negative
                    .saturating_add(predictions.false_positive),
                false_negative: predictions.unknown_long,
            },
            PolicyName::LongHuge | PolicyName::EpochCohort => PlacementCounts {
                true_positive: predictions.true_positive,
                true_negative: predictions
                    .true_negative
                    .saturating_add(predictions.unknown_short),
                false_positive: predictions.false_positive,
                false_negative: predictions
                    .false_negative
                    .saturating_add(predictions.unknown_long),
            },
        }
    }

    struct XorShift64(u64);

    impl XorShift64 {
        fn new(seed: u64) -> Self {
            Self(if seed == 0 {
                0x9e37_79b9_7f4a_7c15
            } else {
                seed
            })
        }

        fn next(&mut self) -> u64 {
            let mut value = self.0;
            value ^= value << 13;
            value ^= value >> 7;
            value ^= value << 17;
            self.0 = value;
            value
        }

        fn event(&mut self, probability: f64) -> bool {
            // Every decision consumes exactly one draw so changing one sweep
            // probability cannot shift the label/confidence/Unknown streams
            // that follow it.
            let sample = (self.next() >> 11) as f64 / ((1u64 << 53) as f64);
            if probability <= 0.0 {
                return false;
            }
            if probability >= 1.0 {
                return true;
            }
            sample < probability
        }

        fn shuffle<T>(&mut self, values: &mut [T]) {
            let mut idx = values.len();
            while idx > 1 {
                idx -= 1;
                let swap = self.next() as usize % (idx + 1);
                values.swap(idx, swap);
            }
        }
    }

    fn prediction(
        truth_long: bool,
        predicted_long: bool,
        confidence: u8,
        confidence_threshold: u8,
        force_unknown: bool,
        slot_bytes: usize,
        identity_mode: IdentityMode,
        type_index: usize,
    ) -> Prediction {
        let identity_bits = match identity_mode {
            IdentityMode::Exact => ((type_index as u64) << 1) | u64::from(truth_long),
            IdentityMode::LifetimeOnly => 0,
        };
        let classified = !force_unknown && confidence >= confidence_threshold;
        let metadata = AllocationMetadata::for_type(
            0x1A11_0000_0000_0000 ^ (slot_bytes as u64).rotate_left(17) ^ identity_bits,
        )
        .with_lifetime_hint(if !classified {
            0
        } else if predicted_long {
            LIFETIME_HINT_LONG_LIVED
        } else {
            LIFETIME_HINT_EPHEMERAL
        });
        Prediction {
            metadata,
            classified,
            predicted_long,
        }
    }

    fn classify_prediction(
        config: &Config,
        rng: &mut XorShift64,
        counts: &mut PredictionCounts,
        truth_long: bool,
        predicted_long: bool,
        type_index: usize,
    ) -> Prediction {
        let confidence_matches_outcome =
            (truth_long == predicted_long) != rng.event(config.confidence_overlap_rate);
        let confidence = if confidence_matches_outcome {
            config.correct_confidence
        } else {
            config.error_confidence
        };
        let forced_unknown = rng.event(config.unknown_rate);
        let prediction = prediction(
            truth_long,
            predicted_long,
            confidence,
            config.confidence_threshold,
            forced_unknown,
            config.slot_bytes,
            config.identity_mode,
            type_index,
        );
        counts.record(
            truth_long,
            prediction.predicted_long,
            confidence,
            forced_unknown,
            prediction.classified,
        );
        prediction
    }

    fn current_memory_kib() -> (usize, usize, usize) {
        let mut rss = 0usize;
        let mut anon_huge = 0usize;
        let mut hugetlb = 0usize;
        if let Ok(status) = std::fs::read_to_string("/proc/self/status") {
            for line in status.lines() {
                let parse = |prefix: &str| -> Option<usize> {
                    line.strip_prefix(prefix)?
                        .split_whitespace()
                        .next()?
                        .parse()
                        .ok()
                };
                rss = parse("VmRSS:").unwrap_or(rss);
                anon_huge = parse("RssAnon:").unwrap_or(anon_huge);
                hugetlb = parse("HugetlbPages:").unwrap_or(hugetlb);
            }
        }
        (rss, anon_huge, hugetlb)
    }

    unsafe fn allocate_one(
        alloc: &UniAlloc,
        layout: Layout,
        metadata: AllocationMetadata,
        semantic: bool,
    ) -> Result<Allocation, String> {
        let ptr = if semantic {
            alloc.alloc_with_metadata(layout, metadata)
        } else {
            GlobalAlloc::alloc(alloc, layout)
        };
        if ptr.is_null() {
            return Err("payload allocation failed".to_string());
        }
        // Fault the slot in and leave room for the dependent pointer chain.
        ptr.write(0xa5);
        Ok(Allocation {
            ptr,
            metadata,
            semantic,
        })
    }

    unsafe fn release_all(alloc: &UniAlloc, layout: Layout, values: &mut Vec<Allocation>) {
        for value in values.drain(..) {
            if value.semantic {
                alloc.dealloc_with_metadata(value.ptr, layout, value.metadata);
            } else {
                GlobalAlloc::dealloc(alloc, value.ptr, layout);
            }
        }
    }

    fn stats_json(prefix: &str, stats: LifetimeHugepageStatsSnapshot) -> String {
        format!(
            "\"{prefix}_current_extents\":{},\"{prefix}_hugetlb_extents\":{},\"{prefix}_ordinary_extents\":{},\"{prefix}_identity_regions\":{},\"{prefix}_live_objects\":{},\"{prefix}_live_ephemeral_objects\":{},\"{prefix}_live_long_lived_objects\":{},\"{prefix}_live_slot_bytes\":{},\"{prefix}_retained_bytes\":{},\"{prefix}_reusable_unassigned_region_bytes\":{},\"{prefix}_cohort_pinned_unassigned_region_bytes\":{},\"{prefix}_assigned_region_slack_bytes\":{},\"{prefix}_retained_slack_bytes\":{},\"{prefix}_stranded_bytes\":{}",
            stats.current_extents,
            stats.current_hugetlb_extents,
            stats.current_ordinary_extents,
            stats.current_identity_regions,
            stats.live_objects,
            stats.live_ephemeral_objects,
            stats.live_long_lived_objects,
            stats.live_slot_bytes,
            stats.retained_bytes,
            stats.reusable_unassigned_region_bytes,
            stats.cohort_pinned_unassigned_region_bytes,
            stats.assigned_region_slack_bytes,
            stats.retained_slack_bytes,
            stats.stranded_bytes,
        )
    }

    fn validation_json(stats: LifetimeHugepageStatsSnapshot) -> String {
        format!(
            "\"runtime_validated_objects\":{},\"runtime_validated_bytes\":{},\"runtime_validation_excluded_objects\":{},\"runtime_validation_excluded_bytes\":{},\"runtime_delayed_free_excluded_objects\":{},\"runtime_delayed_free_excluded_bytes\":{},\"runtime_mixed_epoch_excluded_objects\":{},\"runtime_mixed_epoch_excluded_bytes\":{},\"unknown_bypass_requested_bytes\":{},\"predictor_tp_objects\":{},\"predictor_tp_bytes\":{},\"predictor_tn_objects\":{},\"predictor_tn_bytes\":{},\"predictor_fp_objects\":{},\"predictor_fp_bytes\":{},\"predictor_fn_objects\":{},\"predictor_fn_bytes\":{},\"placement_tp_objects\":{},\"placement_tp_bytes\":{},\"placement_tn_objects\":{},\"placement_tn_bytes\":{},\"placement_fp_objects\":{},\"placement_fp_bytes\":{},\"placement_fn_objects\":{},\"placement_fn_bytes\":{},\"current_epoch\":{},\"phase_advances\":{},\"epoch_cohort_extent_mappings\":{}",
            stats.runtime_validated_objects,
            stats.runtime_validated_bytes,
            stats.runtime_validation_excluded_objects,
            stats.runtime_validation_excluded_bytes,
            stats.runtime_delayed_free_excluded_objects,
            stats.runtime_delayed_free_excluded_bytes,
            stats.runtime_mixed_epoch_excluded_objects,
            stats.runtime_mixed_epoch_excluded_bytes,
            stats.unknown_bypass_requested_bytes,
            stats.predictor_true_positive_objects,
            stats.predictor_true_positive_bytes,
            stats.predictor_true_negative_objects,
            stats.predictor_true_negative_bytes,
            stats.predictor_false_positive_objects,
            stats.predictor_false_positive_bytes,
            stats.predictor_false_negative_objects,
            stats.predictor_false_negative_bytes,
            stats.placement_true_positive_objects,
            stats.placement_true_positive_bytes,
            stats.placement_true_negative_objects,
            stats.placement_true_negative_bytes,
            stats.placement_false_positive_objects,
            stats.placement_false_positive_bytes,
            stats.placement_false_negative_objects,
            stats.placement_false_negative_bytes,
            stats.current_epoch,
            stats.phase_advances,
            stats.epoch_cohort_extent_mappings,
        )
    }

    fn ratio(numerator: usize, denominator: usize) -> f64 {
        if denominator == 0 {
            0.0
        } else {
            numerator as f64 / denominator as f64
        }
    }

    fn stats_consistent(stats: LifetimeHugepageStatsSnapshot) -> bool {
        let total_regions = stats.current_extents.saturating_mul(
            unialloc::LIFETIME_HUGEPAGE_EXTENT_BYTES
                / unialloc::LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES,
        );
        stats.current_extents
            == stats
                .current_hugetlb_extents
                .saturating_add(stats.current_ordinary_extents)
            && stats.live_objects
                == stats
                    .live_ephemeral_objects
                    .saturating_add(stats.live_long_lived_objects)
            && stats.current_identity_regions <= total_regions
            && stats.live_slot_bytes
                <= stats
                    .current_identity_regions
                    .saturating_mul(unialloc::LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES)
            && stats.retained_bytes
                == stats
                    .current_extents
                    .saturating_mul(unialloc::LIFETIME_HUGEPAGE_EXTENT_BYTES)
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
            && stats.stranded_bytes == stats.retained_slack_bytes
            && stats.runtime_validated_objects
                == stats
                    .predictor_true_positive_objects
                    .saturating_add(stats.predictor_true_negative_objects)
                    .saturating_add(stats.predictor_false_positive_objects)
                    .saturating_add(stats.predictor_false_negative_objects)
            && stats.runtime_validated_objects
                == stats
                    .placement_true_positive_objects
                    .saturating_add(stats.placement_true_negative_objects)
                    .saturating_add(stats.placement_false_positive_objects)
                    .saturating_add(stats.placement_false_negative_objects)
            && stats.runtime_validated_bytes
                == stats
                    .predictor_true_positive_bytes
                    .saturating_add(stats.predictor_true_negative_bytes)
                    .saturating_add(stats.predictor_false_positive_bytes)
                    .saturating_add(stats.predictor_false_negative_bytes)
            && stats.runtime_validated_bytes
                == stats
                    .placement_true_positive_bytes
                    .saturating_add(stats.placement_true_negative_bytes)
                    .saturating_add(stats.placement_false_positive_bytes)
                    .saturating_add(stats.placement_false_negative_bytes)
            && stats.runtime_validation_excluded_objects
                == stats
                    .runtime_delayed_free_excluded_objects
                    .saturating_add(stats.runtime_mixed_epoch_excluded_objects)
            && stats.runtime_validation_excluded_bytes
                == stats
                    .runtime_delayed_free_excluded_bytes
                    .saturating_add(stats.runtime_mixed_epoch_excluded_bytes)
    }

    pub fn run() -> Result<(), String> {
        let config = Config::parse()?;
        let layout = Layout::from_size_align(config.slot_bytes, 64)
            .map_err(|_| "unsupported slot layout".to_string())?;
        let long_objects = ((config.objects as f64) * config.long_fraction).round() as usize;
        let ephemeral_objects = config.objects.saturating_sub(long_objects);
        if long_objects == 0 || ephemeral_objects == 0 {
            return Err("both truth classes need objects".to_string());
        }

        if !lifetime_hugepage_stats_reset() || !lifetime_hugepage_configure(config.policy.runtime())
        {
            return Err("lifetime arena was already active".to_string());
        }

        let alloc = UniAlloc::new();
        let mut rng = XorShift64::new(config.seed);
        let mut long = Vec::with_capacity(long_objects);
        let mut ephemeral = Vec::with_capacity(ephemeral_objects);
        let mut false_long = 0usize;
        let mut false_short = 0usize;
        let mut predictions = PredictionCounts::default();
        let mut long_remaining = long_objects;
        let mut ephemeral_remaining = ephemeral_objects;
        let mut long_ordinal = 0usize;
        let mut ephemeral_ordinal = 0usize;

        let allocation_start = Instant::now();
        unsafe {
            while long_remaining != 0 || ephemeral_remaining != 0 {
                if long_remaining != 0 {
                    let assigned_long = !rng.event(config.false_short_rate);
                    false_short += (!assigned_long) as usize;
                    let prediction = classify_prediction(
                        &config,
                        &mut rng,
                        &mut predictions,
                        true,
                        assigned_long,
                        long_ordinal % config.types_per_truth,
                    );
                    long.push(allocate_one(
                        &alloc,
                        layout,
                        prediction.metadata,
                        config.policy != PolicyName::RawDefault,
                    )?);
                    long_remaining -= 1;
                    long_ordinal += 1;
                }
                if ephemeral_remaining != 0 {
                    let assigned_long = rng.event(config.false_long_rate);
                    false_long += assigned_long as usize;
                    let prediction = classify_prediction(
                        &config,
                        &mut rng,
                        &mut predictions,
                        false,
                        assigned_long,
                        ephemeral_ordinal % config.types_per_truth,
                    );
                    ephemeral.push(allocate_one(
                        &alloc,
                        layout,
                        prediction.metadata,
                        config.policy != PolicyName::RawDefault,
                    )?);
                    ephemeral_remaining -= 1;
                    ephemeral_ordinal += 1;
                }
            }
        }
        let allocation_ns = allocation_start.elapsed().as_nanos();
        let peak = lifetime_hugepage_stats_snapshot();
        let arena_slot_bytes = if peak.live_objects == 0 {
            config.slot_bytes
        } else {
            peak.live_slot_bytes / peak.live_objects
        };
        let (peak_rss_kib, peak_anon_kib, peak_hugetlb_kib) = current_memory_kib();

        let ephemeral_release_start = Instant::now();
        unsafe { release_all(&alloc, layout, &mut ephemeral) };
        let ephemeral_release_ns = ephemeral_release_start.elapsed().as_nanos();
        let first_boundary = lifetime_hugepage_stats_snapshot();
        let mut retained_byte_epochs = first_boundary.retained_bytes;
        let mut hugetlb_byte_epochs = first_boundary
            .current_hugetlb_extents
            .saturating_mul(unialloc::LIFETIME_HUGEPAGE_EXTENT_BYTES);
        let mut ordinary_byte_epochs = first_boundary
            .current_ordinary_extents
            .saturating_mul(unialloc::LIFETIME_HUGEPAGE_EXTENT_BYTES);
        let epoch_advance_start = Instant::now();
        let mut epoch_advances = 0usize;
        if !matches!(
            config.policy,
            PolicyName::RawDefault | PolicyName::PolicyOff
        ) {
            lifetime_hugepage_advance_epoch();
            epoch_advances += 1;
        }
        let mut epoch_advance_ns = epoch_advance_start.elapsed().as_nanos();

        // Additional waves exercise production free-list and complete-extent
        // release behavior without changing the persistent long set.
        let mut wave_allocations = 0usize;
        let wave_start = Instant::now();
        for wave in 1..config.ephemeral_waves {
            for wave_ordinal in 0..ephemeral_objects {
                let assigned_long = rng.event(config.false_long_rate);
                false_long += assigned_long as usize;
                let prediction = classify_prediction(
                    &config,
                    &mut rng,
                    &mut predictions,
                    false,
                    assigned_long,
                    wave_ordinal % config.types_per_truth,
                );
                ephemeral.push(unsafe {
                    allocate_one(
                        &alloc,
                        layout,
                        prediction.metadata,
                        config.policy != PolicyName::RawDefault,
                    )?
                });
            }
            wave_allocations += ephemeral_objects;
            unsafe { release_all(&alloc, layout, &mut ephemeral) };
            let boundary = lifetime_hugepage_stats_snapshot();
            retained_byte_epochs = retained_byte_epochs.saturating_add(boundary.retained_bytes);
            hugetlb_byte_epochs = hugetlb_byte_epochs.saturating_add(
                boundary
                    .current_hugetlb_extents
                    .saturating_mul(unialloc::LIFETIME_HUGEPAGE_EXTENT_BYTES),
            );
            ordinary_byte_epochs = ordinary_byte_epochs.saturating_add(
                boundary
                    .current_ordinary_extents
                    .saturating_mul(unialloc::LIFETIME_HUGEPAGE_EXTENT_BYTES),
            );
            let advance_start = Instant::now();
            if !matches!(
                config.policy,
                PolicyName::RawDefault | PolicyName::PolicyOff
            ) {
                lifetime_hugepage_advance_epoch();
                epoch_advances += 1;
            }
            epoch_advance_ns = epoch_advance_ns.saturating_add(advance_start.elapsed().as_nanos());
            black_box(wave);
        }
        let wave_ns = wave_start.elapsed().as_nanos();
        let steady = lifetime_hugepage_stats_snapshot();
        let (steady_rss_kib, steady_anon_kib, steady_hugetlb_kib) = current_memory_kib();

        let mut chain: Vec<*mut u8> = long.iter().map(|entry| entry.ptr).collect();
        rng.shuffle(&mut chain);
        unsafe {
            for idx in 0..chain.len() {
                let next = chain[(idx + 1) % chain.len()] as usize;
                (chain[idx] as *mut usize).write(next);
            }
        }
        let mut cursor = chain[0];
        let touches_per_pass = chain.len();
        unsafe {
            for _ in 0..config.warmup_passes {
                for _ in 0..touches_per_pass {
                    cursor = (cursor as *const usize).read() as *mut u8;
                }
            }
        }
        let touch_start = Instant::now();
        let mut checksum = 0usize;
        unsafe {
            for _ in 0..config.measured_passes {
                for _ in 0..touches_per_pass {
                    cursor = (cursor as *const usize).read() as *mut u8;
                    checksum = checksum.wrapping_add(cursor as usize);
                }
            }
        }
        let touch_ns = touch_start.elapsed().as_nanos();
        black_box(checksum);

        let teardown_start = Instant::now();
        unsafe { release_all(&alloc, layout, &mut long) };
        let teardown_ns = teardown_start.elapsed().as_nanos();
        let final_stats = lifetime_hugepage_stats_snapshot();
        let total_allocations = config.objects.saturating_add(wave_allocations);
        let runtime_confusion_matches_static = final_stats.predictor_true_positive_objects
            == predictions.true_positive
            && final_stats.predictor_true_negative_objects == predictions.true_negative
            && final_stats.predictor_false_positive_objects == predictions.false_positive
            && final_stats.predictor_false_negative_objects == predictions.false_negative;
        let runtime_prediction_matches = matches!(
            config.policy,
            PolicyName::RawDefault | PolicyName::PolicyOff
        ) || (final_stats
            .runtime_validated_objects
            .saturating_add(final_stats.runtime_validation_excluded_objects)
            == predictions.classified
            && final_stats.unknown_bypasses == predictions.unknown
            && final_stats.phase_advances == epoch_advances
            && match config.identity_mode {
                IdentityMode::Exact => {
                    final_stats.runtime_validation_excluded_objects == 0
                        && runtime_confusion_matches_static
                }
                IdentityMode::LifetimeOnly => {
                    final_stats.runtime_validation_excluded_objects != 0
                        || runtime_confusion_matches_static
                }
            });
        let static_confusion_closed = predictions.classified
            == predictions
                .successes()
                .saturating_add(predictions.failures())
            && total_allocations == predictions.classified.saturating_add(predictions.unknown);

        let hugetlb_required = config.require_hugetlb
            && match config.policy {
                PolicyName::AllHugeSegregated => predictions.classified != 0,
                PolicyName::LongHuge | PolicyName::EpochCohort => {
                    predictions
                        .true_positive
                        .saturating_add(predictions.false_positive)
                        != 0
                }
                PolicyName::RawDefault | PolicyName::PolicyOff | PolicyName::OrdinarySegregated => {
                    false
                }
            };
        let passed = final_stats.all_mappings_released
            && stats_consistent(peak)
            && stats_consistent(steady)
            && stats_consistent(final_stats)
            && final_stats.live_objects == 0
            && final_stats.current_extents == 0
            && final_stats.current_identity_regions == 0
            && final_stats.identity_region_assignments == final_stats.identity_region_releases
            && final_stats.routed_allocations == final_stats.routed_deallocations
            && final_stats.routed_allocations
                == final_stats
                    .slot_bump_allocations
                    .saturating_add(final_stats.slot_reuse_hits)
            && final_stats.extent_unmap_failures == 0
            && final_stats.nohugepage_advice_failures == 0
            && static_confusion_closed
            && runtime_prediction_matches
            && (!hugetlb_required
                || (final_stats.hugetlb_extent_mappings != 0
                    && final_stats.hugetlb_fallback_extent_mappings == 0));
        let total_touches = touches_per_pass.saturating_mul(config.measured_passes);
        let classification_coverage = ratio(predictions.classified, total_allocations);
        let classification_success_rate = ratio(predictions.successes(), predictions.classified);
        let classification_failure_rate = ratio(predictions.failures(), predictions.classified);
        let classification_precision = ratio(
            predictions.true_positive,
            predictions
                .true_positive
                .saturating_add(predictions.false_positive),
        );
        let classification_recall = ratio(
            predictions.true_positive,
            predictions
                .true_positive
                .saturating_add(predictions.false_negative),
        );
        let effective_placement = effective_placement_counts(config.policy, &predictions);
        let effective_placement_closed = total_allocations
            == effective_placement
                .true_positive
                .saturating_add(effective_placement.true_negative)
                .saturating_add(effective_placement.false_positive)
                .saturating_add(effective_placement.false_negative);
        let placement_success_rate = ratio(
            effective_placement
                .true_positive
                .saturating_add(effective_placement.true_negative),
            total_allocations,
        );
        let placement_failure_rate = ratio(
            effective_placement
                .false_positive
                .saturating_add(effective_placement.false_negative),
            total_allocations,
        );
        let placement_precision = ratio(
            effective_placement.true_positive,
            effective_placement
                .true_positive
                .saturating_add(effective_placement.false_positive),
        );
        let placement_recall = ratio(
            effective_placement.true_positive,
            effective_placement
                .true_positive
                .saturating_add(effective_placement.false_negative),
        );
        if !effective_placement_closed {
            return Err("effective placement confusion matrix did not close".to_string());
        }

        println!(
            "{{\"source\":\"lifetime_hugepage_allocator_probe\",\"passed\":{},\"accounting_consistent\":{},\"policy\":\"{}\",\"identity_mode\":\"{}\",\"types_per_truth\":{},\"objects\":{},\"total_allocations\":{},\"wave_allocations\":{},\"slot_bytes\":{},\"arena_slot_bytes\":{},\"long_objects\":{},\"ephemeral_objects\":{},\"false_long\":{},\"false_short\":{},\"false_long_rate\":{:.6},\"false_short_rate\":{:.6},\"confidence_threshold\":{},\"correct_confidence\":{},\"error_confidence\":{},\"confidence_overlap_rate\":{:.6},\"unknown_rate\":{:.6},\"prediction_trace_digest\":\"{:016x}\",\"static_classified\":{},\"static_unknown\":{},\"static_unknown_long\":{},\"static_unknown_short\":{},\"static_tp_objects\":{},\"static_tp_bytes\":{},\"static_tn_objects\":{},\"static_tn_bytes\":{},\"static_fp_objects\":{},\"static_fp_bytes\":{},\"static_fn_objects\":{},\"static_fn_bytes\":{},\"classification_coverage\":{:.9},\"classification_success_rate\":{:.9},\"classification_failure_rate\":{:.9},\"classification_precision\":{:.9},\"classification_recall\":{:.9},\"effective_placement_tp_objects\":{},\"effective_placement_tp_bytes\":{},\"effective_placement_tn_objects\":{},\"effective_placement_tn_bytes\":{},\"effective_placement_fp_objects\":{},\"effective_placement_fp_bytes\":{},\"effective_placement_fn_objects\":{},\"effective_placement_fn_bytes\":{},\"placement_success_rate\":{:.9},\"placement_failure_rate\":{:.9},\"placement_precision\":{:.9},\"placement_recall\":{:.9},\"ephemeral_waves\":{},\"allocation_ns\":{},\"allocation_ns_per_object\":{:.6},\"ephemeral_release_ns\":{},\"epoch_advance_ns\":{},\"retained_byte_epochs\":{},\"hugetlb_byte_epochs\":{},\"ordinary_byte_epochs\":{},\"wave_ns\":{},\"touch_ns\":{},\"touches\":{},\"ns_per_touch\":{:.6},\"teardown_ns\":{},\"checksum\":{},\"peak_rss_kib\":{},\"peak_anon_kib\":{},\"peak_hugetlb_kib\":{},\"steady_rss_kib\":{},\"steady_anon_kib\":{},\"steady_hugetlb_kib\":{},\"routed_allocations\":{},\"routed_deallocations\":{},\"unknown_bypasses\":{},\"unsupported_layout_bypasses\":{},\"allocation_fallbacks\":{},\"slot_reuse_hits\":{},\"slot_bump_allocations\":{},\"identity_region_assignments\":{},\"identity_region_releases\":{},\"ordinary_extent_mappings\":{},\"hugetlb_extent_mappings\":{},\"hugetlb_fallback_extent_mappings\":{},\"mapping_failures\":{},\"nohugepage_advice_failures\":{},\"extent_unmaps\":{},\"extent_unmap_failures\":{},{},{},{},{},\"own_mappings_released\":{}}}",
            passed,
            stats_consistent(peak) && stats_consistent(steady) && stats_consistent(final_stats),
            config.policy.as_str(),
            config.identity_mode.as_str(),
            config.types_per_truth,
            config.objects,
            total_allocations,
            wave_allocations,
            config.slot_bytes,
            arena_slot_bytes,
            long_objects,
            ephemeral_objects,
            false_long,
            false_short,
            config.false_long_rate,
            config.false_short_rate,
            config.confidence_threshold,
            config.correct_confidence,
            config.error_confidence,
            config.confidence_overlap_rate,
            config.unknown_rate,
            predictions.trace_digest,
            predictions.classified,
            predictions.unknown,
            predictions.unknown_long,
            predictions.unknown_short,
            predictions.true_positive,
            predictions.true_positive.saturating_mul(config.slot_bytes),
            predictions.true_negative,
            predictions.true_negative.saturating_mul(config.slot_bytes),
            predictions.false_positive,
            predictions.false_positive.saturating_mul(config.slot_bytes),
            predictions.false_negative,
            predictions.false_negative.saturating_mul(config.slot_bytes),
            classification_coverage,
            classification_success_rate,
            classification_failure_rate,
            classification_precision,
            classification_recall,
            effective_placement.true_positive,
            effective_placement
                .true_positive
                .saturating_mul(config.slot_bytes),
            effective_placement.true_negative,
            effective_placement
                .true_negative
                .saturating_mul(config.slot_bytes),
            effective_placement.false_positive,
            effective_placement
                .false_positive
                .saturating_mul(config.slot_bytes),
            effective_placement.false_negative,
            effective_placement
                .false_negative
                .saturating_mul(config.slot_bytes),
            placement_success_rate,
            placement_failure_rate,
            placement_precision,
            placement_recall,
            config.ephemeral_waves,
            allocation_ns,
            allocation_ns as f64 / config.objects as f64,
            ephemeral_release_ns,
            epoch_advance_ns,
            retained_byte_epochs,
            hugetlb_byte_epochs,
            ordinary_byte_epochs,
            wave_ns,
            touch_ns,
            total_touches,
            touch_ns as f64 / total_touches as f64,
            teardown_ns,
            checksum,
            peak_rss_kib,
            peak_anon_kib,
            peak_hugetlb_kib,
            steady_rss_kib,
            steady_anon_kib,
            steady_hugetlb_kib,
            final_stats.routed_allocations,
            final_stats.routed_deallocations,
            final_stats.unknown_bypasses,
            final_stats.unsupported_layout_bypasses,
            final_stats.allocation_fallbacks,
            final_stats.slot_reuse_hits,
            final_stats.slot_bump_allocations,
            final_stats.identity_region_assignments,
            final_stats.identity_region_releases,
            final_stats.ordinary_extent_mappings,
            final_stats.hugetlb_extent_mappings,
            final_stats.hugetlb_fallback_extent_mappings,
            final_stats.mapping_failures,
            final_stats.nohugepage_advice_failures,
            final_stats.extent_unmaps,
            final_stats.extent_unmap_failures,
            validation_json(final_stats),
            stats_json("peak", peak),
            stats_json("steady", steady),
            stats_json("final", final_stats),
            final_stats.all_mappings_released,
        );

        if !lifetime_hugepage_configure(LifetimeHugepagePolicy::Disabled) {
            return Err("arena policy could not be disabled after teardown".to_string());
        }
        if !passed {
            return Err("allocator lifecycle or HugeTLB gate failed".to_string());
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
