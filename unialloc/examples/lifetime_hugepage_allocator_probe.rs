#![cfg_attr(not(feature = "lifetime_hugepage"), allow(dead_code, unused_imports))]

#[cfg(all(feature = "lifetime_hugepage", not(feature = "fixed_heap")))]
mod probe {
    use std::alloc::{GlobalAlloc, Layout};
    use std::env;
    use std::hint::black_box;
    use std::time::Instant;
    use unialloc::{
        lifetime_hugepage_advance_epoch, lifetime_hugepage_configure,
        lifetime_hugepage_configure_with_backend, lifetime_hugepage_stats_reset,
        lifetime_hugepage_stats_snapshot, AllocationMetadata, LifetimeHugepagePolicy,
        LifetimeHugepageStatsSnapshot, LifetimePageBackend, SemanticAlloc, UniAlloc,
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
        AllThpSegregated,
        LongThp,
        EpochCohortThp,
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
                "all-thp-segregated" => Ok(Self::AllThpSegregated),
                "long-thp" => Ok(Self::LongThp),
                "epoch-cohort-thp" => Ok(Self::EpochCohortThp),
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
                Self::AllThpSegregated => "all-thp-segregated",
                Self::LongThp => "long-thp",
                Self::EpochCohortThp => "epoch-cohort-thp",
            }
        }

        fn runtime(self) -> LifetimeHugepagePolicy {
            match self {
                Self::RawDefault => LifetimeHugepagePolicy::Disabled,
                Self::PolicyOff => LifetimeHugepagePolicy::Disabled,
                Self::OrdinarySegregated => LifetimeHugepagePolicy::SegregatedOrdinary,
                Self::AllHugeSegregated | Self::AllThpSegregated => {
                    LifetimeHugepagePolicy::SegregatedHugepage
                }
                Self::LongHuge | Self::LongThp => LifetimeHugepagePolicy::LongLivedHugepage,
                Self::EpochCohort | Self::EpochCohortThp => {
                    LifetimeHugepagePolicy::EpochCohortHugepage
                }
            }
        }

        fn backend(self) -> LifetimePageBackend {
            match self {
                Self::AllThpSegregated | Self::LongThp | Self::EpochCohortThp => {
                    LifetimePageBackend::TransparentHugepage
                }
                _ => LifetimePageBackend::ExplicitHugeTLB,
            }
        }

        fn backend_name(self) -> &'static str {
            match self {
                Self::RawDefault => "system-default",
                Self::AllThpSegregated | Self::LongThp | Self::EpochCohortThp => "thp",
                Self::PolicyOff | Self::OrdinarySegregated => "ordinary-no-thp",
                Self::AllHugeSegregated | Self::LongHuge | Self::EpochCohort => "hugetlb",
            }
        }

        fn runtime_confirmed_placement_available(self) -> bool {
            matches!(
                self,
                Self::AllHugeSegregated | Self::LongHuge | Self::EpochCohort | Self::EpochCohortThp
            )
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
        require_thp: bool,
        require_no_thp: bool,
        disable_process_thp: bool,
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
                require_thp: false,
                require_no_thp: false,
                disable_process_thp: false,
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
                    "--require-thp" => config.require_thp = true,
                    "--require-no-thp" => config.require_no_thp = true,
                    "--disable-process-thp" => config.disable_process_thp = true,
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
                || (config.require_thp && config.require_no_thp)
            {
                return Err("invalid workload geometry or rate".to_string());
            }
            Ok(config)
        }
    }

    fn process_thp_disabled(disable: bool) -> Result<bool, String> {
        #[cfg(target_os = "linux")]
        unsafe {
            const PR_SET_THP_DISABLE: libc::c_int = 41;
            const PR_GET_THP_DISABLE: libc::c_int = 42;
            if disable && libc::prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0) != 0 {
                return Err(format!(
                    "PR_SET_THP_DISABLE failed: {}",
                    std::io::Error::last_os_error()
                ));
            }
            let status = libc::prctl(PR_GET_THP_DISABLE, 0, 0, 0, 0);
            if status < 0 {
                return Err(format!(
                    "PR_GET_THP_DISABLE failed: {}",
                    std::io::Error::last_os_error()
                ));
            }
            Ok(status == 1)
        }
        #[cfg(not(target_os = "linux"))]
        {
            if disable {
                return Err("--disable-process-thp requires Linux".to_string());
            }
            Ok(false)
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
            PolicyName::AllHugeSegregated | PolicyName::AllThpSegregated => PlacementCounts {
                true_positive: predictions
                    .true_positive
                    .saturating_add(predictions.false_negative),
                true_negative: predictions.unknown_short,
                false_positive: predictions
                    .true_negative
                    .saturating_add(predictions.false_positive),
                false_negative: predictions.unknown_long,
            },
            PolicyName::LongHuge
            | PolicyName::EpochCohort
            | PolicyName::LongThp
            | PolicyName::EpochCohortThp => PlacementCounts {
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

    #[derive(Clone, Copy, Default)]
    struct ProcessMemoryKib {
        rss: usize,
        rss_anon: usize,
        anonymous: usize,
        anon_hugepages: usize,
        hugetlb: usize,
        smaps_rollup_available: bool,
        smaps_rollup_parse_success: bool,
    }

    impl ProcessMemoryKib {
        fn effective_resident(self) -> usize {
            self.rss.saturating_add(self.hugetlb)
        }
    }

    fn parse_kib_line(line: &str, prefix: &str) -> Option<usize> {
        line.strip_prefix(prefix)?
            .split_whitespace()
            .next()?
            .parse()
            .ok()
    }

    fn current_memory_kib() -> ProcessMemoryKib {
        let mut memory = ProcessMemoryKib::default();
        if let Ok(status) = std::fs::read_to_string("/proc/self/status") {
            for line in status.lines() {
                memory.rss = parse_kib_line(line, "VmRSS:").unwrap_or(memory.rss);
                memory.rss_anon = parse_kib_line(line, "RssAnon:").unwrap_or(memory.rss_anon);
                memory.hugetlb = parse_kib_line(line, "HugetlbPages:").unwrap_or(memory.hugetlb);
            }
        }
        // AnonHugePages is the proof of anonymous THP backing. RssAnon only
        // reports anonymous residency and is deliberately kept separate.
        if let Ok(rollup) = std::fs::read_to_string("/proc/self/smaps_rollup") {
            memory.smaps_rollup_available = true;
            let mut anonymous_parsed = false;
            let mut anon_hugepages_parsed = false;
            for line in rollup.lines() {
                if let Some(value) = parse_kib_line(line, "Anonymous:") {
                    memory.anonymous = value;
                    anonymous_parsed = true;
                }
                if let Some(value) = parse_kib_line(line, "AnonHugePages:") {
                    memory.anon_hugepages = value;
                    anon_hugepages_parsed = true;
                }
            }
            memory.smaps_rollup_parse_success = anonymous_parsed && anon_hugepages_parsed;
        }
        memory
    }

    #[derive(Clone, Copy, Default)]
    struct ThpVmstat {
        fault_alloc: usize,
        fault_fallback: usize,
        fault_fallback_charge: usize,
        collapse_alloc: usize,
        collapse_alloc_failed: usize,
        split_page: usize,
        split_page_failed: usize,
        split_pmd: usize,
    }

    impl ThpVmstat {
        fn read() -> Self {
            let mut snapshot = Self::default();
            if let Ok(vmstat) = std::fs::read_to_string("/proc/vmstat") {
                for line in vmstat.lines() {
                    let mut fields = line.split_whitespace();
                    let Some(name) = fields.next() else { continue };
                    let Some(value) = fields.next().and_then(|raw| raw.parse::<usize>().ok())
                    else {
                        continue;
                    };
                    match name {
                        "thp_fault_alloc" => snapshot.fault_alloc = value,
                        "thp_fault_fallback" => snapshot.fault_fallback = value,
                        "thp_fault_fallback_charge" => snapshot.fault_fallback_charge = value,
                        "thp_collapse_alloc" => snapshot.collapse_alloc = value,
                        "thp_collapse_alloc_failed" => snapshot.collapse_alloc_failed = value,
                        "thp_split_page" => snapshot.split_page = value,
                        "thp_split_page_failed" => snapshot.split_page_failed = value,
                        "thp_split_pmd" => snapshot.split_pmd = value,
                        _ => {}
                    }
                }
            }
            snapshot
        }

        fn delta(self, before: Self) -> Self {
            Self {
                fault_alloc: self.fault_alloc.saturating_sub(before.fault_alloc),
                fault_fallback: self.fault_fallback.saturating_sub(before.fault_fallback),
                fault_fallback_charge: self
                    .fault_fallback_charge
                    .saturating_sub(before.fault_fallback_charge),
                collapse_alloc: self.collapse_alloc.saturating_sub(before.collapse_alloc),
                collapse_alloc_failed: self
                    .collapse_alloc_failed
                    .saturating_sub(before.collapse_alloc_failed),
                split_page: self.split_page.saturating_sub(before.split_page),
                split_page_failed: self
                    .split_page_failed
                    .saturating_sub(before.split_page_failed),
                split_pmd: self.split_pmd.saturating_sub(before.split_pmd),
            }
        }
    }

    fn thp_enabled_mode() -> &'static str {
        let Ok(enabled) = std::fs::read_to_string("/sys/kernel/mm/transparent_hugepage/enabled")
        else {
            return "unknown";
        };
        if enabled.contains("[always]") {
            "always"
        } else if enabled.contains("[madvise]") {
            "madvise"
        } else if enabled.contains("[never]") {
            "never"
        } else {
            "unknown"
        }
    }

    fn thp_evidence_json(
        baseline: ProcessMemoryKib,
        peak: ProcessMemoryKib,
        post_epoch: ProcessMemoryKib,
        steady: ProcessMemoryKib,
        wave_peak: ProcessMemoryKib,
        wave_peak_observed: bool,
        wave_max_anon_hugepages: usize,
        wave_smaps_rollup_available: bool,
        wave_smaps_rollup_parse_success: bool,
        final_memory: ProcessMemoryKib,
        vmstat: ThpVmstat,
        require_thp: bool,
        require_no_thp: bool,
    ) -> String {
        let smaps_rollup_available = baseline.smaps_rollup_available
            && peak.smaps_rollup_available
            && post_epoch.smaps_rollup_available
            && steady.smaps_rollup_available
            && (!wave_peak_observed || wave_smaps_rollup_available)
            && final_memory.smaps_rollup_available;
        let smaps_rollup_parse_success = baseline.smaps_rollup_parse_success
            && peak.smaps_rollup_parse_success
            && post_epoch.smaps_rollup_parse_success
            && steady.smaps_rollup_parse_success
            && (!wave_peak_observed || wave_smaps_rollup_parse_success)
            && final_memory.smaps_rollup_parse_success;
        let max_anon_hugepages = peak
            .anon_hugepages
            .max(post_epoch.anon_hugepages)
            .max(steady.anon_hugepages)
            .max(wave_max_anon_hugepages);
        let observed = smaps_rollup_parse_success && max_anon_hugepages > baseline.anon_hugepages;
        let backing_requirement = if require_thp {
            observed
        } else if require_no_thp {
            !observed
        } else {
            true
        };
        let gate_passed = (!require_thp && !require_no_thp)
            || (smaps_rollup_available && smaps_rollup_parse_success && backing_requirement);
        let delta = max_anon_hugepages.saturating_sub(baseline.anon_hugepages);
        let peak_delta_anonymous = peak.anonymous.saturating_sub(baseline.anonymous);
        let coverage = ratio(delta, peak_delta_anonymous);
        let peak_effective_resident = peak.effective_resident();
        let post_epoch_effective_resident = post_epoch.effective_resident();
        let steady_effective_resident = steady.effective_resident();
        let max_effective_resident = peak_effective_resident
            .max(post_epoch_effective_resident)
            .max(steady_effective_resident)
            .max(wave_peak.effective_resident());
        format!(
            "\"thp_enabled_mode\":\"{}\",\"smaps_rollup_available\":{},\"smaps_rollup_parse_success\":{},\"thp_required\":{},\"no_thp_required\":{},\"thp_actual_backing_observed\":{},\"thp_backing_gate_passed\":{},\"baseline_anon_hugepages_kib\":{},\"peak_anon_hugepages_kib\":{},\"post_epoch_anon_hugepages_kib\":{},\"steady_anon_hugepages_kib\":{},\"wave_peak_anon_hugepages_kib\":{},\"wave_max_anon_hugepages_kib\":{},\"final_anon_hugepages_kib\":{},\"max_anon_hugepages_delta_kib\":{},\"peak_anonymous_kib\":{},\"peak_process_thp_delta_coverage\":{:.9},\"post_epoch_rss_kib\":{},\"post_epoch_anon_kib\":{},\"post_epoch_hugetlb_kib\":{},\"wave_peak_observed\":{},\"wave_peak_rss_kib\":{},\"wave_peak_anon_kib\":{},\"wave_peak_hugetlb_kib\":{},\"wave_peak_effective_resident_kib\":{},\"peak_effective_resident_kib\":{},\"post_epoch_effective_resident_kib\":{},\"steady_effective_resident_kib\":{},\"max_effective_resident_kib\":{},\"overall_max_effective_resident_kib\":{},\"vmstat_scope\":\"system-wide-delta\",\"vmstat_thp_fault_alloc_delta\":{},\"vmstat_thp_fault_fallback_delta\":{},\"vmstat_thp_fault_fallback_charge_delta\":{},\"vmstat_thp_collapse_alloc_delta\":{},\"vmstat_thp_collapse_alloc_failed_delta\":{},\"vmstat_thp_split_page_delta\":{},\"vmstat_thp_split_page_failed_delta\":{},\"vmstat_thp_split_pmd_delta\":{}",
            thp_enabled_mode(),
            smaps_rollup_available,
            smaps_rollup_parse_success,
            require_thp,
            require_no_thp,
            observed,
            gate_passed,
            baseline.anon_hugepages,
            peak.anon_hugepages,
            post_epoch.anon_hugepages,
            steady.anon_hugepages,
            wave_peak.anon_hugepages,
            wave_max_anon_hugepages,
            final_memory.anon_hugepages,
            delta,
            peak.anonymous,
            coverage,
            post_epoch.rss,
            post_epoch.rss_anon,
            post_epoch.hugetlb,
            wave_peak_observed,
            wave_peak.rss,
            wave_peak.rss_anon,
            wave_peak.hugetlb,
            wave_peak.effective_resident(),
            peak_effective_resident,
            post_epoch_effective_resident,
            steady_effective_resident,
            max_effective_resident,
            max_effective_resident,
            vmstat.fault_alloc,
            vmstat.fault_fallback,
            vmstat.fault_fallback_charge,
            vmstat.collapse_alloc,
            vmstat.collapse_alloc_failed,
            vmstat.split_page,
            vmstat.split_page_failed,
            vmstat.split_pmd,
        )
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
            "\"{prefix}_current_extents\":{},\"{prefix}_hugetlb_extents\":{},\"{prefix}_ordinary_extents\":{},\"{prefix}_thp_extents\":{},\"{prefix}_thp_collapse_confirmed_extents\":{},\"{prefix}_identity_regions\":{},\"{prefix}_live_objects\":{},\"{prefix}_live_ephemeral_objects\":{},\"{prefix}_live_long_lived_objects\":{},\"{prefix}_live_slot_bytes\":{},\"{prefix}_retained_bytes\":{},\"{prefix}_reusable_unassigned_region_bytes\":{},\"{prefix}_cohort_pinned_unassigned_region_bytes\":{},\"{prefix}_assigned_region_slack_bytes\":{},\"{prefix}_retained_slack_bytes\":{},\"{prefix}_stranded_bytes\":{}",
            stats.current_extents,
            stats.current_hugetlb_extents,
            stats.current_ordinary_extents,
            stats.current_thp_extents,
            stats.current_thp_collapse_confirmed_extents,
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

    fn thp_stats_json(stats: LifetimeHugepageStatsSnapshot) -> String {
        format!(
            "\"thp_extent_mappings\":{},\"thp_candidate_extent_mappings\":{},\"thp_advice_attempts\":{},\"thp_advice_successes\":{},\"thp_advice_failures\":{},\"thp_collapse_eligible_extents\":{},\"thp_collapse_low_occupancy_skips\":{},\"thp_collapse_attempts\":{},\"thp_collapse_successes\":{},\"thp_collapse_failures\":{},\"thp_collapse_last_error_code\":{},\"lifetime_peak_thp_extents\":{},\"lifetime_peak_thp_collapse_confirmed_extents\":{}",
            stats.thp_extent_mappings,
            stats.thp_candidate_extent_mappings,
            stats.thp_advice_attempts,
            stats.thp_advice_successes,
            stats.thp_advice_errors,
            stats.thp_collapse_eligible_extents,
            stats.thp_collapse_low_occupancy_skips,
            stats.thp_collapse_attempts,
            stats.thp_collapse_successes,
            stats.thp_collapse_errors,
            stats.thp_collapse_last_error_code,
            stats.peak_thp_extents,
            stats.peak_thp_collapse_confirmed_extents,
        )
    }

    fn runtime_confirmed_placement_json(
        policy: PolicyName,
        stats: LifetimeHugepageStatsSnapshot,
    ) -> String {
        if !policy.runtime_confirmed_placement_available() {
            return "\"runtime_confirmed_placement_available\":false,\"runtime_confirmed_placement_basis\":null,\"runtime_confirmed_placement_success_rate\":null,\"runtime_confirmed_placement_failure_rate\":null,\"runtime_confirmed_placement_precision\":null,\"runtime_confirmed_placement_recall\":null".to_string();
        }
        let success = stats
            .placement_true_positive_objects
            .saturating_add(stats.placement_true_negative_objects);
        let failure = stats
            .placement_false_positive_objects
            .saturating_add(stats.placement_false_negative_objects);
        let precision_denominator = stats
            .placement_true_positive_objects
            .saturating_add(stats.placement_false_positive_objects);
        let recall_denominator = stats
            .placement_true_positive_objects
            .saturating_add(stats.placement_false_negative_objects);
        format!(
            "\"runtime_confirmed_placement_available\":true,\"runtime_confirmed_placement_basis\":\"hugetlb-or-epoch-collapse\",\"runtime_confirmed_placement_success_rate\":{:.9},\"runtime_confirmed_placement_failure_rate\":{:.9},\"runtime_confirmed_placement_precision\":{:.9},\"runtime_confirmed_placement_recall\":{:.9}",
            ratio(success, stats.runtime_validated_objects),
            ratio(failure, stats.runtime_validated_objects),
            ratio(stats.placement_true_positive_objects, precision_denominator),
            ratio(stats.placement_true_positive_objects, recall_denominator),
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
                .saturating_add(stats.current_thp_extents)
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
        let process_thp_disabled = process_thp_disabled(config.disable_process_thp)?;
        let layout = Layout::from_size_align(config.slot_bytes, 64)
            .map_err(|_| "unsupported slot layout".to_string())?;
        let long_objects = ((config.objects as f64) * config.long_fraction).round() as usize;
        let ephemeral_objects = config.objects.saturating_sub(long_objects);
        if long_objects == 0 || ephemeral_objects == 0 {
            return Err("both truth classes need objects".to_string());
        }

        if !lifetime_hugepage_stats_reset()
            || !lifetime_hugepage_configure_with_backend(
                config.policy.runtime(),
                config.policy.backend(),
            )
        {
            return Err("lifetime arena was already active".to_string());
        }
        let baseline_memory = current_memory_kib();
        let baseline_vmstat = ThpVmstat::read();

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
        let peak_memory = current_memory_kib();

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
        let mut thp_backend_vma_byte_epochs = first_boundary
            .current_thp_extents
            .saturating_mul(unialloc::LIFETIME_HUGEPAGE_EXTENT_BYTES);
        let mut epoch_advances = 0usize;
        let first_epoch_advance_ns = if !matches!(
            config.policy,
            PolicyName::RawDefault | PolicyName::PolicyOff
        ) {
            let epoch_advance_start = Instant::now();
            lifetime_hugepage_advance_epoch();
            epoch_advances += 1;
            epoch_advance_start.elapsed().as_nanos()
        } else {
            0
        };
        let mut epoch_advance_ns = first_epoch_advance_ns;
        // Runtime-survival THP promotion happens synchronously at an epoch
        // boundary. Sample immediately so later reclaim/splitting cannot erase
        // the point at which the process held anonymous THP backing.
        let post_epoch_memory = current_memory_kib();

        // Additional waves exercise production free-list and complete-extent
        // release behavior without changing the persistent long set.
        let mut wave_allocations = 0usize;
        let mut wave_peak_memory = ProcessMemoryKib::default();
        let mut wave_peak_observed = false;
        let mut wave_max_anon_hugepages = 0usize;
        let mut wave_smaps_rollup_available = true;
        let mut wave_smaps_rollup_parse_success = true;
        let mut wave_evidence_sampling_ns = 0u128;
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
            // Capture transient resident and THP peaks while the wave is live;
            // release-side snapshots cannot recover this evidence.
            let evidence_start = Instant::now();
            let wave_memory = current_memory_kib();
            wave_smaps_rollup_available &= wave_memory.smaps_rollup_available;
            wave_smaps_rollup_parse_success &= wave_memory.smaps_rollup_parse_success;
            wave_max_anon_hugepages = wave_max_anon_hugepages.max(wave_memory.anon_hugepages);
            if !wave_peak_observed
                || wave_memory.effective_resident() > wave_peak_memory.effective_resident()
            {
                wave_peak_memory = wave_memory;
                wave_peak_observed = true;
            }
            wave_evidence_sampling_ns =
                wave_evidence_sampling_ns.saturating_add(evidence_start.elapsed().as_nanos());
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
            thp_backend_vma_byte_epochs = thp_backend_vma_byte_epochs.saturating_add(
                boundary
                    .current_thp_extents
                    .saturating_mul(unialloc::LIFETIME_HUGEPAGE_EXTENT_BYTES),
            );
            let advance_ns = if !matches!(
                config.policy,
                PolicyName::RawDefault | PolicyName::PolicyOff
            ) {
                let advance_start = Instant::now();
                lifetime_hugepage_advance_epoch();
                epoch_advances += 1;
                advance_start.elapsed().as_nanos()
            } else {
                0
            };
            epoch_advance_ns = epoch_advance_ns.saturating_add(advance_ns);
            black_box(wave);
        }
        let wave_ns = wave_start
            .elapsed()
            .as_nanos()
            .saturating_sub(wave_evidence_sampling_ns);
        let steady = lifetime_hugepage_stats_snapshot();
        let steady_memory = current_memory_kib();

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
        let final_memory = current_memory_kib();
        let vmstat_delta = ThpVmstat::read().delta(baseline_vmstat);
        let max_anon_hugepages = peak_memory
            .anon_hugepages
            .max(post_epoch_memory.anon_hugepages)
            .max(steady_memory.anon_hugepages)
            .max(wave_max_anon_hugepages);
        let smaps_rollup_available = baseline_memory.smaps_rollup_available
            && peak_memory.smaps_rollup_available
            && post_epoch_memory.smaps_rollup_available
            && steady_memory.smaps_rollup_available
            && (!wave_peak_observed || wave_smaps_rollup_available)
            && final_memory.smaps_rollup_available;
        let smaps_rollup_parse_success = baseline_memory.smaps_rollup_parse_success
            && peak_memory.smaps_rollup_parse_success
            && post_epoch_memory.smaps_rollup_parse_success
            && steady_memory.smaps_rollup_parse_success
            && (!wave_peak_observed || wave_smaps_rollup_parse_success)
            && final_memory.smaps_rollup_parse_success;
        let thp_actual_backing_observed =
            smaps_rollup_parse_success && max_anon_hugepages > baseline_memory.anon_hugepages;
        let thp_backing_requirement = if config.require_thp {
            thp_actual_backing_observed
        } else if config.require_no_thp {
            !thp_actual_backing_observed
        } else {
            true
        };
        let thp_backing_gate_passed = (!config.require_thp && !config.require_no_thp)
            || (smaps_rollup_available && smaps_rollup_parse_success && thp_backing_requirement);
        let byte_epoch_accounting_consistent = retained_byte_epochs
            == hugetlb_byte_epochs
                .saturating_add(ordinary_byte_epochs)
                .saturating_add(thp_backend_vma_byte_epochs);
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
                PolicyName::AllThpSegregated | PolicyName::LongThp | PolicyName::EpochCohortThp => {
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
            && byte_epoch_accounting_consistent
            && thp_backing_gate_passed
            && (!config.disable_process_thp || process_thp_disabled)
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
        let policy_intent_placement_success_rate = ratio(
            effective_placement
                .true_positive
                .saturating_add(effective_placement.true_negative),
            total_allocations,
        );
        let policy_intent_placement_failure_rate = ratio(
            effective_placement
                .false_positive
                .saturating_add(effective_placement.false_negative),
            total_allocations,
        );
        let policy_intent_placement_precision = ratio(
            effective_placement.true_positive,
            effective_placement
                .true_positive
                .saturating_add(effective_placement.false_positive),
        );
        let policy_intent_placement_recall = ratio(
            effective_placement.true_positive,
            effective_placement
                .true_positive
                .saturating_add(effective_placement.false_negative),
        );
        if !effective_placement_closed {
            return Err("effective placement confusion matrix did not close".to_string());
        }

        println!(
            "{{\"source\":\"lifetime_hugepage_allocator_probe\",\"passed\":{},\"accounting_consistent\":{},\"policy\":\"{}\",\"backend\":\"{}\",\"process_thp_disable_requested\":{},\"process_thp_disabled\":{},\"identity_mode\":\"{}\",\"types_per_truth\":{},\"objects\":{},\"total_allocations\":{},\"wave_allocations\":{},\"slot_bytes\":{},\"arena_slot_bytes\":{},\"long_objects\":{},\"ephemeral_objects\":{},\"false_long\":{},\"false_short\":{},\"false_long_rate\":{:.6},\"false_short_rate\":{:.6},\"confidence_threshold\":{},\"correct_confidence\":{},\"error_confidence\":{},\"confidence_overlap_rate\":{:.6},\"unknown_rate\":{:.6},\"prediction_trace_digest\":\"{:016x}\",\"static_classified\":{},\"static_unknown\":{},\"static_unknown_long\":{},\"static_unknown_short\":{},\"static_tp_objects\":{},\"static_tp_bytes\":{},\"static_tn_objects\":{},\"static_tn_bytes\":{},\"static_fp_objects\":{},\"static_fp_bytes\":{},\"static_fn_objects\":{},\"static_fn_bytes\":{},\"classification_coverage\":{:.9},\"classification_success_rate\":{:.9},\"classification_failure_rate\":{:.9},\"classification_precision\":{:.9},\"classification_recall\":{:.9},\"effective_placement_tp_objects\":{},\"effective_placement_tp_bytes\":{},\"effective_placement_tn_objects\":{},\"effective_placement_tn_bytes\":{},\"effective_placement_fp_objects\":{},\"effective_placement_fp_bytes\":{},\"effective_placement_fn_objects\":{},\"effective_placement_fn_bytes\":{},\"placement_success_rate\":{:.9},\"placement_failure_rate\":{:.9},\"placement_precision\":{:.9},\"placement_recall\":{:.9},\"placement_metric_semantics\":\"policy-intent\",\"policy_intent_placement_success_rate\":{:.9},\"policy_intent_placement_failure_rate\":{:.9},\"policy_intent_placement_precision\":{:.9},\"policy_intent_placement_recall\":{:.9},\"ephemeral_waves\":{},\"allocation_ns\":{},\"allocation_ns_per_object\":{:.6},\"ephemeral_release_ns\":{},\"first_epoch_advance_ns\":{},\"epoch_advance_ns\":{},\"retained_byte_epochs\":{},\"hugetlb_byte_epochs\":{},\"ordinary_byte_epochs\":{},\"thp_backend_vma_byte_epochs\":{},\"byte_epoch_accounting_consistent\":{},\"wave_evidence_sampling_ns\":{},\"wave_ns\":{},\"touch_ns\":{},\"touches\":{},\"ns_per_touch\":{:.6},\"teardown_ns\":{},\"checksum\":{},\"peak_rss_kib\":{},\"peak_anon_kib\":{},\"peak_hugetlb_kib\":{},\"steady_rss_kib\":{},\"steady_anon_kib\":{},\"steady_hugetlb_kib\":{},\"routed_allocations\":{},\"routed_deallocations\":{},\"unknown_bypasses\":{},\"unsupported_layout_bypasses\":{},\"allocation_fallbacks\":{},\"slot_reuse_hits\":{},\"slot_bump_allocations\":{},\"identity_region_assignments\":{},\"identity_region_releases\":{},\"ordinary_extent_mappings\":{},\"hugetlb_extent_mappings\":{},\"hugetlb_fallback_extent_mappings\":{},\"mapping_failures\":{},\"nohugepage_advice_failures\":{},\"extent_unmaps\":{},\"extent_unmap_failures\":{},{},{},{},{},{},{},{},\"own_mappings_released\":{}}}",
            passed,
            stats_consistent(peak) && stats_consistent(steady) && stats_consistent(final_stats),
            config.policy.as_str(),
            config.policy.backend_name(),
            config.disable_process_thp,
            process_thp_disabled,
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
            policy_intent_placement_success_rate,
            policy_intent_placement_failure_rate,
            policy_intent_placement_precision,
            policy_intent_placement_recall,
            policy_intent_placement_success_rate,
            policy_intent_placement_failure_rate,
            policy_intent_placement_precision,
            policy_intent_placement_recall,
            config.ephemeral_waves,
            allocation_ns,
            allocation_ns as f64 / config.objects as f64,
            ephemeral_release_ns,
            first_epoch_advance_ns,
            epoch_advance_ns,
            retained_byte_epochs,
            hugetlb_byte_epochs,
            ordinary_byte_epochs,
            thp_backend_vma_byte_epochs,
            byte_epoch_accounting_consistent,
            wave_evidence_sampling_ns,
            wave_ns,
            touch_ns,
            total_touches,
            touch_ns as f64 / total_touches as f64,
            teardown_ns,
            checksum,
            peak_memory.rss,
            peak_memory.rss_anon,
            peak_memory.hugetlb,
            steady_memory.rss,
            steady_memory.rss_anon,
            steady_memory.hugetlb,
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
            thp_evidence_json(
                baseline_memory,
                peak_memory,
                post_epoch_memory,
                steady_memory,
                wave_peak_memory,
                wave_peak_observed,
                wave_max_anon_hugepages,
                wave_smaps_rollup_available,
                wave_smaps_rollup_parse_success,
                final_memory,
                vmstat_delta,
                config.require_thp,
                config.require_no_thp,
            ),
            thp_stats_json(final_stats),
            runtime_confirmed_placement_json(config.policy, final_stats),
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
