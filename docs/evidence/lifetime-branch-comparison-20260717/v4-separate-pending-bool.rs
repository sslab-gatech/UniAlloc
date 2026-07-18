//! Feature-gated payload arenas driven by exact lifetime metadata or runtime
//! allocation-site survival.
//!
//! Classified objects carry one of the stable ABI values Unknown/Ephemeral/
//! LongLived. A process-wide logical epoch records whether each routed object
//! dies within its allocation phase or survives a phase boundary, producing
//! object- and rounded-slot-byte-weighted predictor and placement confusion
//! matrices. Regions reused across epochs by a non-cohort policy are excluded
//! when individual object epochs cannot be recovered without side metadata. The
//! epoch-cohort policy also keeps different birth epochs out of the same 2 MiB
//! extent. The adaptive policy learns site survival from later allocation
//! pressure and uses static lifetime metadata only as a weak prior.
//!
//! The implementation deliberately owns all bookkeeping in fixed process
//! tables. Mapping or releasing an extent therefore cannot recurse through the
//! allocator whose payload path it is implementing.

use super::type_isolation::{
    compiler_bounded_lifetime_placement_class, lifetime_placement_class, AllocationMetadata,
    LifetimePlacementClass, FLAG_DELAYED_FREE, LIFETIME_HINT_DYNAMIC_BUFFER_OBSERVE,
    LIFETIME_HINT_EPHEMERAL, LIFETIME_HINT_LONG_LIVED,
};
use crate::pal::sys_alloc::{self, prots, HugePageMmapBacking};
use crate::size_class::{get_size_class_tuple, SizeClass, TOTAL_SIZE_CLASS};
use core::alloc::Layout;
use core::mem::size_of;
use core::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use spin::Mutex;

pub const LIFETIME_HUGEPAGE_EXTENT_BYTES: usize = 2 * 1024 * 1024;
pub const LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES: usize = 64 * 1024;

// The configured research host owns 4,096 persistent 2 MiB pages (8 GiB).
// Keep the descriptor bound explicit so allocator metadata cannot recursively
// grow with the payload working set.
const MAX_EXTENTS: usize = 4096;
const EXTENT_LOOKUP_SLOTS: usize = MAX_EXTENTS * 2;
const EXTENT_LOOKUP_MASK: usize = EXTENT_LOOKUP_SLOTS - 1;
const MAX_SLOT_ALIGN: usize = 32 * 1024;
const MAX_SLOT_BYTES: usize = 64 * 1024;
const IDENTITY_REGIONS_PER_EXTENT: usize =
    LIFETIME_HUGEPAGE_EXTENT_BYTES / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
const ALIGN_CLASS_COUNT: usize = 16;
const LIFETIME_CLASS_COUNT: usize = 2;
const BUCKET_COUNT: usize = LIFETIME_CLASS_COUNT * TOTAL_SIZE_CLASS * ALIGN_CLASS_COUNT;
const NONE: usize = usize::MAX;
const TOMBSTONE: usize = usize::MAX - 1;
// Preregistered feasibility threshold for the first runtime-validation
// experiment. It is an evaluation parameter, not a claimed universal optimum.
const THP_PROMOTION_MIN_LIVE_BYTES: usize = LIFETIME_HUGEPAGE_EXTENT_BYTES / 2;
// Adaptive THP placement admits provisional Long priors to candidate extents,
// but only runtime-confirmed Long slots contribute to promotion. Requiring
// three quarters of one extent keeps the backing decision occupancy-driven.
const ADAPTIVE_THP_PROMOTION_MIN_CONFIRMED_LIVE_BYTES: usize =
    LIFETIME_HUGEPAGE_EXTENT_BYTES * 3 / 4;

// Marker-free lifetime learning uses allocation pressure instead of attempting
// to infer application-specific semantic phases. One epoch is one 2 MiB THP's
// worth of allocation pressure. Production adaptive mode retains its original
// eligible-site payload clock. Evaluation force-track mode uses requested bytes
// from every observed allocation generation, including raw, unsupported, and
// missing-identity fallbacks. Objects released before 2 MiB of later pressure
// are short, objects surviving at least 8 MiB are long, and the interval between
// those thresholds is deliberately censored.
const ADAPTIVE_EPOCH_BYTES: u64 = LIFETIME_HUGEPAGE_EXTENT_BYTES as u64;
const ADAPTIVE_LONG_AGE_BYTES: u64 = 4 * ADAPTIVE_EPOCH_BYTES;
const ADAPTIVE_SITE_SLOTS: usize = 4096;
const ADAPTIVE_OBSERVATION_SLOTS: usize = 4096;
const ADAPTIVE_OBSERVATION_PROBE_LIMIT: usize = 16;
/// Maximum number of exact runtime lifetime sites retained in the fixed table.
pub const LIFETIME_ADAPTIVE_SITE_SNAPSHOT_CAPACITY: usize = ADAPTIVE_OBSERVATION_SLOTS;
/// Version of [`LifetimeAdaptiveSiteSnapshot`]'s public field contract.
pub const LIFETIME_ADAPTIVE_SITE_SNAPSHOT_ABI_VERSION: u32 = 2;
const ADAPTIVE_SITE_PROBE_LIMIT: usize = 16;
const ADAPTIVE_MIN_DECISIVE_SAMPLES: u32 = 8;
const ADAPTIVE_PRIOR_VALIDATION_SAMPLES: u32 = 4;
const ADAPTIVE_MIN_SAMPLES_AFTER_TRANSITION: u32 = 4;
const ADAPTIVE_COUNTER_DECAY_INTERVAL: u32 = 256;
const ADAPTIVE_COLD_MAX_INFLIGHT: u32 = 8;
const ADAPTIVE_LIVE_SURVIVOR_SLOTS_PER_SITE: usize = ADAPTIVE_COLD_MAX_INFLIGHT as usize;
const ADAPTIVE_LIVE_SURVIVOR_ACTIVE_WORDS: usize = ADAPTIVE_SITE_SLOTS / u64::BITS as usize;
const ADAPTIVE_SHORT_SAMPLE_INTERVAL: u32 = 256;
// Observation-only borrowed Vec reserve candidates target the resident buffer
// band found in the real-workload audit. Smaller metadata objects cannot fill
// extents efficiently; larger allocations belong to the allocator's large
// object path. Keep this gate on the actual runtime Layout.
const DYNAMIC_BUFFER_OBSERVE_MIN_BYTES: usize = 4 * 1024;
const DYNAMIC_BUFFER_OBSERVE_MAX_BYTES: usize = 32 * 1024;
// Direct compiler placement is limited to layouts the arena can actually
// serve. Keeping this distinct from the broader observation band avoids
// acquiring ARENA only to discover a Large size class.
const COMPILER_DIRECTED_LONG_MIN_BYTES: usize = 4 * 1024;
const COMPILER_DIRECTED_LONG_MAX_BYTES: usize = crate::size_class::MAX_SIZE;
// Bound the extra fullness comparison work on the allocation hot path.
// The existing available-list search still traverses incompatible entries for
// correctness; adaptive Long placement compares at most 32 usable candidates.
const ADAPTIVE_FULLNESS_CANDIDATE_LIMIT: usize = 32;
// Lifetime-routed workloads repeatedly drain the same small geometry set.
// Keep one bounded process-local hot set so a fully empty extent can be reused
// without repeating mmap/munmap and THP-advice work on every operation.
const LIFETIME_EMPTY_EXTENT_RETENTION_CAPACITY: usize = 16;
const ADAPTIVE_TRAILER_ACTIVE: u16 = 1;
const ADAPTIVE_TRAILER_CONFIRMED_LONG: u16 = 1 << 1;
const ADAPTIVE_TRAILER_SURVIVAL_RECORDED: u16 = 1 << 2;
const ADAPTIVE_TRAILER_KNOWN_FLAGS: u16 =
    ADAPTIVE_TRAILER_ACTIVE | ADAPTIVE_TRAILER_CONFIRMED_LONG | ADAPTIVE_TRAILER_SURVIVAL_RECORDED;

#[inline]
fn adaptive_lifetime_outcome(age_bytes: u64) -> Option<bool> {
    if age_bytes < ADAPTIVE_EPOCH_BYTES {
        Some(false)
    } else if age_bytes >= ADAPTIVE_LONG_AGE_BYTES {
        Some(true)
    } else {
        None
    }
}

/// Physical page backend used for placements selected by the lifetime policy.
///
/// Placement policy and backing mechanism are configured independently so the
/// same lifetime experiment can compare explicit HugeTLB against Linux THP.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum LifetimePageBackend {
    ExplicitHugeTLB = 0,
    TransparentHugepage = 1,
}

impl LifetimePageBackend {
    #[inline]
    fn from_usize(value: usize) -> Self {
        match value {
            1 => Self::TransparentHugepage,
            _ => Self::ExplicitHugeTLB,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum LifetimeHugepagePolicy {
    Disabled = 0,
    SegregatedOrdinary = 1,
    LongLivedHugepage = 2,
    SegregatedHugepage = 3,
    /// Apply the binary lifetime policy while keeping allocations from
    /// different runtime epochs out of the same 2 MiB extent. Static
    /// confidence abstention happens before this ABI and arrives as Unknown.
    EpochCohortHugepage = 4,
    /// Learn allocation-site survival from allocator-observed byte pressure.
    /// Tracked Cold and sampled Short allocations use ordinary
    /// `MADV_NOHUGEPAGE` extents, model-directed bypasses use the base allocator,
    /// and Long placements share delayed THP candidates. Only runtime-confirmed
    /// Long occupancy can cross the density gate and request `MADV_HUGEPAGE`.
    AdaptiveRuntimeHugepage = 5,
    /// Trust only the bounded process-long compiler oracle. Local-Drop facts,
    /// legacy hints, and unclassified allocations bypass before the arena lock;
    /// bounded-long oracle allocations route into density-gated THP candidate
    /// extents without adaptive state or per-object observation trailers.
    CompilerInferredHugepage = 6,
    /// Use the same bounded-oracle admission and slot geometry as
    /// `CompilerInferredHugepage`, while keeping every routed bounded-long
    /// extent on ordinary pages with `MADV_NOHUGEPAGE`. This is the matched
    /// THP-off control for isolating page-backing effects from hint packing.
    CompilerInferredOrdinary = 7,
    /// Use the adaptive classifier, observation trailers, pressure clock, and
    /// placement cohorts while forcing every routed cohort onto ordinary
    /// `MADV_NOHUGEPAGE` extents. This is the matched THP-off control for
    /// `AdaptiveRuntimeHugepage`.
    AdaptiveRuntimeOrdinary = 8,
    /// Trust the compiler's exact Long hint only for medium layouts supported
    /// by the arena. Admitted Long allocations use density-gated THP
    /// candidates; Short, Unknown, observation-only, and out-of-band layouts
    /// stay on the base allocator before the arena lock. This policy carries no
    /// runtime classifier, pressure clock, observation trailer, or
    /// deallocation-time validation.
    CompilerDirectedHugepage = 9,
    /// Matched THP-off control for `CompilerDirectedHugepage`. The compiler's
    /// same admitted medium Long allocations use ordinary pages under
    /// `MADV_NOHUGEPAGE`; every other allocation stays on the base allocator.
    CompilerDirectedOrdinary = 10,
    /// Learn and place from allocator-observed survival only. Exact compiler
    /// site identity remains available to the online classifier, while the
    /// static lifetime hint is normalized to Unknown for prediction and arena
    /// identity. This same-binary runtime-profile control pays cold learning,
    /// trailer, pressure-clock, locking, and density-promotion costs.
    AdaptiveRuntimeProfileHugepage = 11,
}

impl LifetimeHugepagePolicy {
    #[inline]
    fn from_usize(value: usize) -> Self {
        match value & POLICY_VALUE_MASK {
            1 => Self::SegregatedOrdinary,
            2 => Self::LongLivedHugepage,
            3 => Self::SegregatedHugepage,
            4 => Self::EpochCohortHugepage,
            5 => Self::AdaptiveRuntimeHugepage,
            6 => Self::CompilerInferredHugepage,
            7 => Self::CompilerInferredOrdinary,
            8 => Self::AdaptiveRuntimeOrdinary,
            9 => Self::CompilerDirectedHugepage,
            10 => Self::CompilerDirectedOrdinary,
            11 => Self::AdaptiveRuntimeProfileHugepage,
            _ => Self::Disabled,
        }
    }

    #[inline]
    fn is_adaptive(self) -> bool {
        matches!(
            self,
            Self::AdaptiveRuntimeHugepage
                | Self::AdaptiveRuntimeOrdinary
                | Self::AdaptiveRuntimeProfileHugepage
        )
    }

    #[inline]
    fn is_adaptive_thp(self) -> bool {
        matches!(
            self,
            Self::AdaptiveRuntimeHugepage | Self::AdaptiveRuntimeProfileHugepage
        )
    }

    #[inline]
    fn ignores_static_lifetime_prior(self) -> bool {
        self == Self::AdaptiveRuntimeProfileHugepage
    }

    #[inline]
    fn is_compiler_directed(self) -> bool {
        matches!(
            self,
            Self::CompilerDirectedHugepage | Self::CompilerDirectedOrdinary
        )
    }

    #[inline]
    fn requested_backing(
        self,
        class: LifetimePlacementClass,
        backend: LifetimePageBackend,
    ) -> Option<RequestedBacking> {
        let requested = match (self, class) {
            (Self::Disabled, _) | (_, LifetimePlacementClass::Unknown) => None,
            (
                Self::SegregatedOrdinary
                | Self::AdaptiveRuntimeOrdinary
                | Self::CompilerDirectedOrdinary,
                _,
            ) => Some(RequestedBacking::Ordinary),
            (
                Self::LongLivedHugepage
                | Self::EpochCohortHugepage
                | Self::AdaptiveRuntimeHugepage
                | Self::AdaptiveRuntimeProfileHugepage
                | Self::CompilerDirectedHugepage,
                LifetimePlacementClass::Ephemeral,
            ) => Some(RequestedBacking::Ordinary),
            (
                Self::CompilerInferredHugepage | Self::CompilerInferredOrdinary,
                LifetimePlacementClass::Ephemeral,
            ) => None,
            (Self::CompilerInferredOrdinary, LifetimePlacementClass::LongLived) => {
                Some(RequestedBacking::Ordinary)
            }
            (
                Self::LongLivedHugepage
                | Self::EpochCohortHugepage
                | Self::AdaptiveRuntimeHugepage
                | Self::AdaptiveRuntimeProfileHugepage
                | Self::CompilerInferredHugepage
                | Self::CompilerDirectedHugepage,
                LifetimePlacementClass::LongLived,
            )
            | (Self::SegregatedHugepage, _) => Some(RequestedBacking::LargePage),
        };
        if backend == LifetimePageBackend::TransparentHugepage
            && matches!(
                self,
                Self::EpochCohortHugepage
                    | Self::AdaptiveRuntimeHugepage
                    | Self::AdaptiveRuntimeProfileHugepage
                    | Self::CompilerInferredHugepage
                    | Self::CompilerDirectedHugepage
            )
            && requested == Some(RequestedBacking::LargePage)
        {
            Some(RequestedBacking::EpochCandidate)
        } else {
            requested
        }
    }

    #[inline]
    fn separates_epoch_extents(self) -> bool {
        self == Self::EpochCohortHugepage
    }
}

/// Interpret only the stable compiler-directed binary hint contract. The
/// bounded-process oracle and observation-only tags keep their independent
/// experiment semantics and therefore abstain in this policy pair.
#[inline]
const fn compiler_directed_lifetime_placement_class(lifetime_hint: u16) -> LifetimePlacementClass {
    match lifetime_hint {
        LIFETIME_HINT_EPHEMERAL => LifetimePlacementClass::Ephemeral,
        LIFETIME_HINT_LONG_LIVED => LifetimePlacementClass::LongLived,
        _ => LifetimePlacementClass::Unknown,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
enum RequestedBacking {
    Ordinary = 1,
    LargePage = 2,
    EpochCandidate = 3,
}

#[inline]
fn requested_backing_for_allocation(
    policy: LifetimeHugepagePolicy,
    class: LifetimePlacementClass,
    backend: LifetimePageBackend,
    adaptive_long_confirmed: bool,
) -> Option<RequestedBacking> {
    if policy.is_adaptive_thp()
        && class == LifetimePlacementClass::LongLived
        && backend == LifetimePageBackend::TransparentHugepage
    {
        Some(RequestedBacking::EpochCandidate)
    } else if policy.is_adaptive()
        && class == LifetimePlacementClass::LongLived
        && !adaptive_long_confirmed
    {
        Some(RequestedBacking::Ordinary)
    } else {
        policy.requested_backing(class, backend)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
enum ActualBacking {
    Unmapped = 0,
    Ordinary = 1,
    Hugetlb = 2,
    HugetlbFallback = 3,
    /// `MADV_HUGEPAGE` failed; the aligned anonymous mapping remains usable.
    ThpAdviceFailed = 4,
    /// The VMA accepted `MADV_HUGEPAGE`; actual THP backing is unverified.
    ThpAdvisedUnverified = 5,
    /// Delayed candidate held under `MADV_NOHUGEPAGE` until runtime validation.
    ThpCandidateNoHugepage = 6,
    /// Delayed candidate whose initial `MADV_NOHUGEPAGE` failed. Its actual
    /// page size remains unverified until the promotion transition runs.
    ThpCandidateNoHugepageAdviceFailed = 7,
    /// `MADV_COLLAPSE` succeeded for this range at a controlled transition.
    /// Linux may split the THP later, so this is a point-in-time observation.
    ThpCollapseSucceededPointInTime = 8,
}

impl ActualBacking {
    #[inline]
    fn is_hugetlb(self) -> bool {
        self == Self::Hugetlb
    }

    #[inline]
    fn is_thp_mapping(self) -> bool {
        matches!(
            self,
            Self::ThpAdviceFailed
                | Self::ThpAdvisedUnverified
                | Self::ThpCandidateNoHugepage
                | Self::ThpCandidateNoHugepageAdviceFailed
                | Self::ThpCollapseSucceededPointInTime
        )
    }

    #[inline]
    fn is_thp_confirmed_at_collapse(self) -> bool {
        self == Self::ThpCollapseSucceededPointInTime
    }

    #[inline]
    fn is_large_page_confirmed(self) -> bool {
        self.is_hugetlb() || self.is_thp_confirmed_at_collapse()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ThpAdviceOutcome {
    NotAttempted,
    Succeeded,
    Failed,
}

#[derive(Clone, Copy)]
struct ExtentMapping {
    base: *mut u8,
    backing: ActualBacking,
    nohugepage_advice_ok: bool,
    thp_advice: ThpAdviceOutcome,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ThpPromotionEligibility {
    Ineligible,
    LowOccupancy,
    Eligible,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
pub struct LifetimeHugepageStatsSnapshot {
    pub policy: LifetimeHugepagePolicy,
    pub backend: LifetimePageBackend,
    pub extent_bytes: usize,
    /// Current process-wide allocation epoch. Epoch 1 is the initial phase.
    pub current_epoch: usize,
    pub phase_advances: usize,
    pub epoch_cohort_extent_mappings: usize,
    pub routed_allocations: usize,
    pub routed_deallocations: usize,
    pub unknown_bypasses: usize,
    pub unknown_bypass_requested_bytes: usize,
    pub unsupported_layout_bypasses: usize,
    pub allocation_fallbacks: usize,
    pub slot_reuse_hits: usize,
    pub slot_bump_allocations: usize,
    pub identity_region_assignments: usize,
    pub identity_region_releases: usize,
    pub ordinary_extent_mappings: usize,
    pub hugetlb_extent_mappings: usize,
    pub hugetlb_fallback_extent_mappings: usize,
    /// Anonymous aligned mappings owned by the THP backend. This counts VMAs,
    /// not confirmed PMD mappings.
    pub thp_extent_mappings: usize,
    /// THP mappings initially held under `MADV_NOHUGEPAGE` for epoch survival
    /// and occupancy validation.
    pub thp_candidate_extent_mappings: usize,
    pub thp_advice_attempts: usize,
    pub thp_advice_successes: usize,
    pub thp_advice_errors: usize,
    /// Surviving candidate observations that met the 50% live-slot threshold.
    pub thp_collapse_eligible_extents: usize,
    pub thp_collapse_low_occupancy_skips: usize,
    pub thp_collapse_attempts: usize,
    /// Point-in-time successful `MADV_COLLAPSE` operations. Later splitting is
    /// possible and requires external smaps/perf validation for steady state.
    pub thp_collapse_successes: usize,
    pub thp_collapse_errors: usize,
    /// Most recent Linux errno returned by `MADV_COLLAPSE`, or zero when no
    /// collapse failure has been observed since the last statistics reset.
    pub thp_collapse_last_error_code: isize,
    pub mapping_failures: usize,
    pub nohugepage_advice_failures: usize,
    pub extent_unmaps: usize,
    pub extent_unmap_failures: usize,
    pub current_extents: usize,
    pub peak_extents: usize,
    pub current_ordinary_extents: usize,
    pub peak_ordinary_extents: usize,
    pub current_hugetlb_extents: usize,
    pub peak_hugetlb_extents: usize,
    pub current_thp_extents: usize,
    pub peak_thp_extents: usize,
    pub current_thp_collapse_confirmed_extents: usize,
    pub peak_thp_collapse_confirmed_extents: usize,
    pub live_objects: usize,
    pub live_ephemeral_objects: usize,
    pub live_long_lived_objects: usize,
    pub live_slot_bytes: usize,
    /// Allocation generations whose allocate/release epochs produced runtime
    /// truth. A moved realloc closes the old generation and starts a new one;
    /// an in-place realloc preserves its original birth epoch.
    pub runtime_validated_objects: usize,
    pub runtime_validated_bytes: usize,
    /// Objects excluded because delayed release or a mixed-epoch region
    /// obscures the individual object's logical death epoch.
    pub runtime_validation_excluded_objects: usize,
    pub runtime_validation_excluded_bytes: usize,
    pub runtime_delayed_free_excluded_objects: usize,
    pub runtime_delayed_free_excluded_bytes: usize,
    pub runtime_mixed_epoch_excluded_objects: usize,
    pub runtime_mixed_epoch_excluded_bytes: usize,
    /// Predictor confusion matrix. "Positive" means predicted long-lived;
    /// byte fields measure rounded arena slot bytes.
    pub predictor_true_positive_objects: usize,
    pub predictor_true_positive_bytes: usize,
    pub predictor_true_negative_objects: usize,
    pub predictor_true_negative_bytes: usize,
    pub predictor_false_positive_objects: usize,
    pub predictor_false_positive_bytes: usize,
    pub predictor_false_negative_objects: usize,
    pub predictor_false_negative_bytes: usize,
    /// Actual-placement confusion matrix. "Positive" means explicit HugeTLB
    /// or a point-in-time successful THP collapse; advice-only THP remains
    /// unverified. Byte fields measure rounded arena slot bytes.
    pub placement_true_positive_objects: usize,
    pub placement_true_positive_bytes: usize,
    pub placement_true_negative_objects: usize,
    pub placement_true_negative_bytes: usize,
    pub placement_false_positive_objects: usize,
    pub placement_false_positive_bytes: usize,
    pub placement_false_negative_objects: usize,
    pub placement_false_negative_bytes: usize,
    pub current_identity_regions: usize,
    pub peak_identity_regions: usize,
    pub retained_bytes: usize,
    /// Bytes held in completely unassigned regions that can serve the current
    /// policy and allocation epoch immediately.
    pub reusable_unassigned_region_bytes: usize,
    /// Unassigned bytes pinned in older epoch-cohort extents. They cannot be
    /// reused by a later cohort while any slot in the extent remains live.
    pub cohort_pinned_unassigned_region_bytes: usize,
    /// Bytes inside assigned regions that are not occupied by live slots.
    pub assigned_region_slack_bytes: usize,
    /// Total retained payload capacity beyond the current live rounded slots.
    pub retained_slack_bytes: usize,
    /// Compatibility alias for `retained_slack_bytes`.
    pub stranded_bytes: usize,
    pub all_mappings_released: bool,
    /// Marker-free classifier input and model telemetry. Pressure counts
    /// eligible exact-site payload capacity for tracked allocations and
    /// model-directed bypasses; slot trailers are excluded.
    pub adaptive_pressure_bytes: u64,
    pub adaptive_pressure_epoch: usize,
    pub adaptive_epoch_advances: usize,
    /// Learned runtime state only. Provisional compiler-prior routing remains
    /// Cold here and is reported separately through static-hint telemetry.
    pub adaptive_site_count: usize,
    pub adaptive_cold_sites: usize,
    pub adaptive_short_sites: usize,
    pub adaptive_long_sites: usize,
    pub adaptive_eligible_allocations: usize,
    pub adaptive_training_allocations: usize,
    pub adaptive_cold_bypassed_allocations: usize,
    pub adaptive_short_routed_allocations: usize,
    pub adaptive_short_bypassed_allocations: usize,
    pub adaptive_long_routed_allocations: usize,
    pub adaptive_missing_identity_bypasses: usize,
    pub adaptive_site_table_bypasses: usize,
    pub adaptive_prior_conflicts: usize,
    /// Evaluation-only exact-layout observation export state. These counters
    /// are maintained in a table separate from the production predictor.
    pub adaptive_observation_recording: bool,
    pub adaptive_observation_site_count: usize,
    pub adaptive_observation_table_bypasses: usize,
    /// Evaluation-only complete-label mode. It routes every eligible tracked
    /// allocation through ordinary-page trailer geometry until either hard
    /// guard is exhausted. Guard bypasses make prevalence claims ineligible.
    pub adaptive_force_track_all: bool,
    pub adaptive_force_track_all_maximum_allocations: usize,
    pub adaptive_force_track_all_maximum_requested_bytes: usize,
    pub adaptive_force_track_all_admitted_allocations: usize,
    pub adaptive_force_track_all_admitted_requested_bytes: usize,
    pub adaptive_force_track_all_guard_bypasses: usize,
    /// Complete-clock accounting for evaluation force-track mode. Total
    /// pressure includes exact semantic allocations plus raw/unattributed
    /// allocations and moved raw reallocations. In-place reallocations create
    /// no new allocation generation and therefore add no pressure.
    pub adaptive_force_track_all_pressure_allocations: usize,
    pub adaptive_force_track_all_pressure_requested_bytes: usize,
    pub adaptive_force_track_all_raw_pressure_allocations: usize,
    pub adaptive_force_track_all_raw_pressure_requested_bytes: usize,
    pub adaptive_force_track_all_raw_reallocation_pressure_allocations: usize,
    pub adaptive_force_track_all_raw_reallocation_pressure_requested_bytes: usize,
    pub adaptive_live_trailers: usize,
    pub adaptive_trailer_corruptions: usize,
    /// Bounded live-survivor sampler telemetry. A live observation means the
    /// object crossed the Long pressure threshold before deallocation. It
    /// updates the predictor once, while its allocation-time placement remains
    /// provisional and receives no retroactive density credit.
    pub adaptive_live_survival_registrations: usize,
    pub adaptive_live_survival_registration_bypasses: usize,
    pub adaptive_live_survival_scans: usize,
    pub adaptive_live_survival_slots_examined: usize,
    pub adaptive_live_survival_observations: usize,
    pub adaptive_live_survival_promotions: usize,
    pub adaptive_live_survival_stale_abstentions: usize,
    pub adaptive_short_observations: usize,
    pub adaptive_long_observations: usize,
    pub adaptive_censored_observations: usize,
    pub adaptive_decisive_observation_bytes: usize,
    pub adaptive_promotions: usize,
    pub adaptive_demotions: usize,
    pub adaptive_prior_corrections: usize,
    /// Prequential learned-prediction matrix. Cold abstentions are excluded;
    /// byte fields use payload capacity and exclude the hidden trailer.
    pub adaptive_predictor_true_positive_objects: usize,
    pub adaptive_predictor_true_positive_bytes: usize,
    pub adaptive_predictor_true_negative_objects: usize,
    pub adaptive_predictor_true_negative_bytes: usize,
    pub adaptive_predictor_false_positive_objects: usize,
    pub adaptive_predictor_false_positive_bytes: usize,
    pub adaptive_predictor_false_negative_objects: usize,
    pub adaptive_predictor_false_negative_bytes: usize,
    /// Allocation-time compiler/static-hint matrix against allocator pressure
    /// truth. Unknown hints are counted separately as abstentions; byte fields
    /// use payload capacity and exclude the hidden trailer.
    pub adaptive_static_hint_true_positive_objects: usize,
    pub adaptive_static_hint_true_positive_bytes: usize,
    pub adaptive_static_hint_true_negative_objects: usize,
    pub adaptive_static_hint_true_negative_bytes: usize,
    pub adaptive_static_hint_false_positive_objects: usize,
    pub adaptive_static_hint_false_positive_bytes: usize,
    pub adaptive_static_hint_false_negative_objects: usize,
    pub adaptive_static_hint_false_negative_bytes: usize,
    pub adaptive_static_hint_abstained_objects: usize,
    pub adaptive_static_hint_abstained_bytes: usize,
    /// Allocations carrying no bounded process-long oracle (including local
    /// Drop facts and legacy hints) that returned to the base allocator before
    /// acquiring the arena lock.
    pub compiler_inferred_unknown_or_unproven_bypasses: usize,
    /// Compatibility counter for the retired 0xA101-as-Short interpretation.
    /// Correct implementations keep this at zero and count local-Drop facts as
    /// unknown/unproven bypasses.
    pub compiler_inferred_proven_ephemeral_bypasses: usize,
    /// Requested bytes covered by compiler-inferred pre-lock bypasses.
    pub compiler_inferred_bypass_requested_bytes: usize,
    /// Compiler-inferred allocations that proceeded far enough to acquire the
    /// arena lock. A bounded-oracle workload should make this equal direct Long
    /// allocation attempts after the pre-lock admission gate.
    pub compiler_inferred_arena_lock_acquisitions: usize,
    /// Successful bounded-long allocations routed directly to the selected long
    /// cohort (THP candidate or matched ordinary control).
    pub compiler_inferred_direct_long_routes: usize,
    /// User-requested and allocator-rounded bytes covered by successful direct
    /// bounded-long routes. These fields support byte-weighted oracle coverage
    /// without reconstructing size classes from object counts.
    pub compiler_inferred_direct_long_requested_bytes: usize,
    pub compiler_inferred_direct_long_slot_bytes: usize,
    /// Deallocations observed for proof-tagged process-long allocations. Exact
    /// mem::forget/Box::leak workloads require this counter to remain zero.
    pub compiler_inferred_direct_long_deallocations: usize,
    /// Successful direct routes whose slot geometry contained no adaptive
    /// observation trailer. This must equal `compiler_inferred_direct_long_routes`.
    pub compiler_inferred_direct_long_routes_without_trailer: usize,
    /// Dense bounded-long extents for which THP advice was attempted.
    pub compiler_inferred_density_promotion_attempts: usize,
    pub compiler_inferred_density_promotion_successes: usize,
    pub compiler_inferred_density_promotion_errors: usize,
    /// Compiler-directed diagnostic telemetry. Collection is explicitly
    /// default-off so the performance arm pays no per-event atomic RMW. The
    /// two bypass fields are updated before the arena lock only while
    /// telemetry is enabled; routed fields are ordinary counters protected by
    /// the arena lock.
    pub compiler_directed_telemetry: bool,
    /// Pre-lock admission bypasses. The legacy `unknown` field name covers
    /// Short hints and out-of-band Long layouts as well as Unknown hints under
    /// the medium-Long-only policy contract.
    pub compiler_directed_unknown_bypasses: usize,
    pub compiler_directed_bypass_requested_bytes: usize,
    pub compiler_directed_arena_lock_acquisitions: usize,
    /// Compatibility fields from the original two-lane prototype. Direct
    /// policies keep these at zero because Short allocations use the base
    /// allocator.
    pub compiler_directed_short_routes: usize,
    pub compiler_directed_short_requested_bytes: usize,
    pub compiler_directed_short_slot_bytes: usize,
    pub compiler_directed_long_routes: usize,
    pub compiler_directed_long_requested_bytes: usize,
    pub compiler_directed_long_slot_bytes: usize,
    pub compiler_directed_deallocations: usize,
    pub compiler_directed_routes_without_trailer: usize,
    pub compiler_directed_density_promotion_attempts: usize,
    pub compiler_directed_density_promotion_successes: usize,
    pub compiler_directed_density_promotion_errors: usize,
    /// Maximum number of fully empty lifetime-routed extents retained for exact
    /// geometry/backing reuse. Kernel unmap failures can temporarily exceed it.
    pub empty_extent_retention_capacity: usize,
    /// Fully empty mapped extents currently held in the bounded reuse LRU.
    pub current_retained_empty_extents: usize,
    pub peak_retained_empty_extents: usize,
    pub current_retained_empty_bytes: usize,
    pub peak_retained_empty_bytes: usize,
    /// Extent-level lifecycle counters. A reuse hit is the first allocation
    /// that reactivates one retained empty extent.
    pub retained_empty_extent_insertions: usize,
    pub retained_empty_extent_reuse_hits: usize,
    pub retained_empty_extent_evictions: usize,
    pub retained_empty_extent_trims: usize,
}

#[derive(Clone, Copy)]
struct SlotGeometry {
    slot_size: usize,
    payload_capacity: usize,
    region_capacity: usize,
    bucket: usize,
    adaptive_trailer: bool,
}

#[derive(Clone, Copy)]
struct AvailableRegion {
    extent_idx: usize,
    region_idx: usize,
}

#[inline]
fn fullness_candidate_is_better(
    candidate_live: usize,
    candidate_reuses_identity: bool,
    incumbent: Option<(usize, bool)>,
) -> bool {
    match incumbent {
        None => true,
        Some((incumbent_live, incumbent_reuses_identity)) => {
            candidate_live > incumbent_live
                || (candidate_live == incumbent_live
                    && candidate_reuses_identity
                    && !incumbent_reuses_identity)
        }
    }
}

#[inline]
fn confirmed_long_live_after_deallocation(
    current: usize,
    confirmed_long_provenance: Option<bool>,
) -> usize {
    match confirmed_long_provenance {
        Some(true) => current.saturating_sub(1),
        Some(false) => current,
        None => 0,
    }
}

#[derive(Clone, Copy)]
struct ConfusionCounters {
    true_positive_objects: usize,
    true_positive_bytes: usize,
    true_negative_objects: usize,
    true_negative_bytes: usize,
    false_positive_objects: usize,
    false_positive_bytes: usize,
    false_negative_objects: usize,
    false_negative_bytes: usize,
}

impl ConfusionCounters {
    const fn new() -> Self {
        Self {
            true_positive_objects: 0,
            true_positive_bytes: 0,
            true_negative_objects: 0,
            true_negative_bytes: 0,
            false_positive_objects: 0,
            false_positive_bytes: 0,
            false_negative_objects: 0,
            false_negative_bytes: 0,
        }
    }

    #[inline]
    fn record(&mut self, predicted_positive: bool, actual_positive: bool, bytes: usize) {
        match (predicted_positive, actual_positive) {
            (true, true) => {
                self.true_positive_objects = self.true_positive_objects.saturating_add(1);
                self.true_positive_bytes = self.true_positive_bytes.saturating_add(bytes);
            }
            (false, false) => {
                self.true_negative_objects = self.true_negative_objects.saturating_add(1);
                self.true_negative_bytes = self.true_negative_bytes.saturating_add(bytes);
            }
            (true, false) => {
                self.false_positive_objects = self.false_positive_objects.saturating_add(1);
                self.false_positive_bytes = self.false_positive_bytes.saturating_add(bytes);
            }
            (false, true) => {
                self.false_negative_objects = self.false_negative_objects.saturating_add(1);
                self.false_negative_bytes = self.false_negative_bytes.saturating_add(bytes);
            }
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
enum AdaptivePrediction {
    Cold = 0,
    Short = 1,
    Long = 2,
}

impl AdaptivePrediction {
    #[inline]
    fn from_u8(value: u8) -> Option<Self> {
        match value {
            0 => Some(Self::Cold),
            1 => Some(Self::Short),
            2 => Some(Self::Long),
            _ => None,
        }
    }

    #[inline]
    fn placement_class(self) -> LifetimePlacementClass {
        match self {
            Self::Long => LifetimePlacementClass::LongLived,
            Self::Cold | Self::Short => LifetimePlacementClass::Ephemeral,
        }
    }
}

#[inline]
fn lifetime_class_from_u8(value: u8) -> Option<LifetimePlacementClass> {
    match value {
        0 => Some(LifetimePlacementClass::Unknown),
        1 => Some(LifetimePlacementClass::Ephemeral),
        2 => Some(LifetimePlacementClass::LongLived),
        _ => None,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct AdaptiveSiteKey {
    callsite: u64,
    type_id: u64,
    module_id: u64,
    flags: u32,
    placement_hint: u16,
    payload_capacity: usize,
    align: usize,
}

impl AdaptiveSiteKey {
    const fn empty() -> Self {
        Self {
            callsite: 0,
            type_id: 0,
            module_id: 0,
            flags: 0,
            placement_hint: 0,
            payload_capacity: 0,
            align: 0,
        }
    }

    #[inline]
    fn from_metadata(
        metadata: AllocationMetadata,
        payload_capacity: usize,
        align: usize,
    ) -> Option<Self> {
        if metadata.callsite == 0 || !metadata.has_type() || metadata.requests(FLAG_DELAYED_FREE) {
            return None;
        }
        Some(Self {
            callsite: metadata.callsite,
            type_id: metadata.type_id,
            module_id: metadata.module_id,
            flags: metadata.flags,
            placement_hint: metadata.placement_hint,
            payload_capacity,
            align,
        })
    }

    #[inline]
    fn fingerprint(self) -> u64 {
        let mut hash = 0xcbf2_9ce4_8422_2325u64;
        for value in [
            self.callsite,
            self.type_id,
            self.module_id,
            self.flags as u64,
            self.placement_hint as u64,
            self.payload_capacity as u64,
            self.align as u64,
        ] {
            hash ^= value;
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
        if hash == 0 {
            1
        } else {
            hash
        }
    }
}

#[derive(Clone, Copy)]
enum AdaptivePriorStatus {
    None,
    Pending,
    Confirmed,
    Rejected,
}

#[derive(Clone, Copy)]
struct AdaptiveSiteState {
    key: AdaptiveSiteKey,
    fingerprint: u64,
    short_votes: u32,
    long_votes: u32,
    decisive_samples: u32,
    censored_samples: u32,
    samples_since_transition: u32,
    allocations_since_sample: u32,
    tracked_inflight: u32,
    prediction: AdaptivePrediction,
    static_prior: LifetimePlacementClass,
    prior_status: AdaptivePriorStatus,
    prior_conflict: bool,
}

impl AdaptiveSiteState {
    const fn empty() -> Self {
        Self {
            key: AdaptiveSiteKey::empty(),
            fingerprint: 0,
            short_votes: 0,
            long_votes: 0,
            decisive_samples: 0,
            censored_samples: 0,
            samples_since_transition: 0,
            allocations_since_sample: 0,
            tracked_inflight: 0,
            prediction: AdaptivePrediction::Cold,
            static_prior: LifetimePlacementClass::Unknown,
            prior_status: AdaptivePriorStatus::None,
            prior_conflict: false,
        }
    }

    fn new(key: AdaptiveSiteKey, static_prior: LifetimePlacementClass) -> Self {
        let (short_votes, long_votes) = match static_prior {
            LifetimePlacementClass::Unknown => (1, 1),
            LifetimePlacementClass::Ephemeral => (3, 1),
            LifetimePlacementClass::LongLived => (1, 3),
        };
        Self {
            key,
            fingerprint: key.fingerprint(),
            short_votes,
            long_votes,
            decisive_samples: 0,
            censored_samples: 0,
            samples_since_transition: 0,
            allocations_since_sample: 0,
            tracked_inflight: 0,
            prediction: AdaptivePrediction::Cold,
            static_prior,
            prior_status: if static_prior == LifetimePlacementClass::Unknown {
                AdaptivePriorStatus::None
            } else {
                AdaptivePriorStatus::Pending
            },
            prior_conflict: false,
        }
    }

    #[inline]
    fn is_empty(self) -> bool {
        self.fingerprint == 0
    }

    #[inline]
    fn note_prior(&mut self, incoming: LifetimePlacementClass) -> bool {
        if incoming == LifetimePlacementClass::Unknown || self.prior_conflict {
            return false;
        }
        if self.static_prior == LifetimePlacementClass::Unknown {
            self.static_prior = incoming;
            match incoming {
                LifetimePlacementClass::Ephemeral => {
                    self.short_votes = self.short_votes.saturating_add(2)
                }
                LifetimePlacementClass::LongLived => {
                    self.long_votes = self.long_votes.saturating_add(2)
                }
                LifetimePlacementClass::Unknown => unreachable!(),
            }
            self.prior_status =
                if self.decisive_samples == 0 && self.prediction == AdaptivePrediction::Cold {
                    AdaptivePriorStatus::Pending
                } else {
                    AdaptivePriorStatus::Rejected
                };
            return false;
        }
        if self.static_prior != incoming && !self.prior_conflict {
            self.prior_conflict = true;
            self.static_prior = LifetimePlacementClass::Unknown;
            self.short_votes = 1;
            self.long_votes = 1;
            self.decisive_samples = 0;
            self.censored_samples = 0;
            self.prediction = AdaptivePrediction::Cold;
            self.prior_status = AdaptivePriorStatus::None;
            self.samples_since_transition = 0;
            self.allocations_since_sample = 0;
            return true;
        }
        false
    }

    #[inline]
    fn routing_prediction(self) -> AdaptivePrediction {
        if self.prediction != AdaptivePrediction::Cold {
            return self.prediction;
        }
        if !matches!(self.prior_status, AdaptivePriorStatus::Pending) {
            return AdaptivePrediction::Cold;
        }
        match self.static_prior {
            LifetimePlacementClass::Ephemeral => AdaptivePrediction::Short,
            LifetimePlacementClass::LongLived => AdaptivePrediction::Long,
            LifetimePlacementClass::Unknown => AdaptivePrediction::Cold,
        }
    }

    #[inline]
    fn long_placement_confirmed(self) -> bool {
        self.prediction == AdaptivePrediction::Long
    }

    #[inline]
    fn should_track_allocation(&mut self) -> bool {
        if matches!(self.prior_status, AdaptivePriorStatus::Pending) {
            if self.tracked_inflight >= ADAPTIVE_COLD_MAX_INFLIGHT {
                return false;
            }
            let observed_samples = self.decisive_samples.saturating_add(self.censored_samples);
            if observed_samples < ADAPTIVE_MIN_DECISIVE_SAMPLES {
                self.allocations_since_sample = 0;
                return true;
            }
        } else {
            match self.prediction {
                AdaptivePrediction::Cold => {
                    if self.tracked_inflight >= ADAPTIVE_COLD_MAX_INFLIGHT {
                        return false;
                    }
                    let observed_samples =
                        self.decisive_samples.saturating_add(self.censored_samples);
                    if observed_samples < ADAPTIVE_MIN_DECISIVE_SAMPLES {
                        self.allocations_since_sample = 0;
                        return true;
                    }
                }
                AdaptivePrediction::Long => {
                    self.allocations_since_sample = 0;
                    return true;
                }
                AdaptivePrediction::Short => {}
            }
        }
        self.allocations_since_sample = self.allocations_since_sample.saturating_add(1);
        if self.allocations_since_sample < ADAPTIVE_SHORT_SAMPLE_INTERVAL {
            return false;
        }
        self.allocations_since_sample = 0;
        true
    }

    #[inline]
    fn note_tracked_allocation(&mut self) {
        self.tracked_inflight = self.tracked_inflight.saturating_add(1);
    }

    #[inline]
    fn note_tracked_deallocation(&mut self) {
        self.tracked_inflight = self.tracked_inflight.saturating_sub(1);
    }

    fn observe(&mut self, actual_long: Option<bool>) -> AdaptiveTransition {
        let Some(actual_long) = actual_long else {
            self.censored_samples = self.censored_samples.saturating_add(1);
            return AdaptiveTransition::None;
        };
        if actual_long {
            self.long_votes = self.long_votes.saturating_add(1);
        } else {
            self.short_votes = self.short_votes.saturating_add(1);
        }
        self.decisive_samples = self.decisive_samples.saturating_add(1);
        self.samples_since_transition = self.samples_since_transition.saturating_add(1);

        if self.decisive_samples % ADAPTIVE_COUNTER_DECAY_INTERVAL == 0 {
            self.short_votes = core::cmp::max(1, self.short_votes / 2);
            self.long_votes = core::cmp::max(1, self.long_votes / 2);
        }

        if matches!(self.prior_status, AdaptivePriorStatus::Pending) {
            let prior_matches = matches!(
                (self.static_prior, actual_long),
                (LifetimePlacementClass::Ephemeral, false)
                    | (LifetimePlacementClass::LongLived, true)
            );
            if !prior_matches {
                // One contradictory decisive outcome cancels provisional
                // placement immediately. Runtime learning retains the
                // existing bounded correction threshold below.
                self.prior_status = AdaptivePriorStatus::Rejected;
            } else if self.decisive_samples >= ADAPTIVE_PRIOR_VALIDATION_SAMPLES {
                let (prediction, transition) = match self.static_prior {
                    LifetimePlacementClass::Ephemeral => {
                        (AdaptivePrediction::Short, AdaptiveTransition::PromotedShort)
                    }
                    LifetimePlacementClass::LongLived => {
                        (AdaptivePrediction::Long, AdaptiveTransition::PromotedLong)
                    }
                    LifetimePlacementClass::Unknown => unreachable!(),
                };
                self.prediction = prediction;
                self.prior_status = AdaptivePriorStatus::Confirmed;
                self.samples_since_transition = 0;
                return transition;
            } else {
                return AdaptiveTransition::None;
            }
        }
        if self.decisive_samples < ADAPTIVE_MIN_DECISIVE_SAMPLES {
            return AdaptiveTransition::None;
        }

        let total = self.short_votes.saturating_add(self.long_votes) as u64;
        let long_percent = (self.long_votes as u64).saturating_mul(100) / total;
        match self.prediction {
            AdaptivePrediction::Cold if long_percent >= 80 => {
                self.prediction = AdaptivePrediction::Long;
                self.samples_since_transition = 0;
                AdaptiveTransition::PromotedLong
            }
            AdaptivePrediction::Cold if long_percent <= 20 => {
                self.prediction = AdaptivePrediction::Short;
                self.samples_since_transition = 0;
                AdaptiveTransition::PromotedShort
            }
            AdaptivePrediction::Long
                if self.samples_since_transition >= ADAPTIVE_MIN_SAMPLES_AFTER_TRANSITION
                    && long_percent < 60 =>
            {
                self.prediction = AdaptivePrediction::Cold;
                self.prior_status = AdaptivePriorStatus::Rejected;
                self.samples_since_transition = 0;
                AdaptiveTransition::Demoted
            }
            AdaptivePrediction::Short
                if self.samples_since_transition >= ADAPTIVE_MIN_SAMPLES_AFTER_TRANSITION
                    && long_percent > 40 =>
            {
                self.prediction = AdaptivePrediction::Cold;
                self.prior_status = AdaptivePriorStatus::Rejected;
                self.samples_since_transition = 0;
                AdaptiveTransition::Demoted
            }
            _ => AdaptiveTransition::None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum AdaptiveTransition {
    None,
    PromotedShort,
    PromotedLong,
    Demoted,
}

#[derive(Clone, Copy)]
struct AdaptiveLiveSurvivorSample {
    ptr: usize,
    birth_pressure: u64,
}

impl AdaptiveLiveSurvivorSample {
    const fn empty() -> Self {
        Self {
            ptr: 0,
            birth_pressure: 0,
        }
    }
}

/// Bounded live-object samples for one production predictor site.
///
/// Cold and sampled-Short sites need a way to learn from objects that remain
/// resident until process shutdown. The eight slots match the existing Cold
/// in-flight cap, keep allocator metadata fixed-size, and never change the
/// predictor key. The birth pressure disambiguates a pointer that was freed
/// and subsequently reused before a defensive stale-entry scan.
#[derive(Clone, Copy)]
struct AdaptiveLiveSurvivorSite {
    samples: [AdaptiveLiveSurvivorSample; ADAPTIVE_LIVE_SURVIVOR_SLOTS_PER_SITE],
    active_slots: u8,
}

impl AdaptiveLiveSurvivorSite {
    const fn empty() -> Self {
        Self {
            samples: [AdaptiveLiveSurvivorSample::empty(); ADAPTIVE_LIVE_SURVIVOR_SLOTS_PER_SITE],
            active_slots: 0,
        }
    }

    #[inline]
    fn insert(&mut self, ptr: *mut u8, birth_pressure: u64) -> bool {
        for slot in 0..ADAPTIVE_LIVE_SURVIVOR_SLOTS_PER_SITE {
            let mask = 1u8 << slot;
            if self.active_slots & mask == 0 {
                self.samples[slot] = AdaptiveLiveSurvivorSample {
                    ptr: ptr as usize,
                    birth_pressure,
                };
                self.active_slots |= mask;
                return true;
            }
        }
        false
    }

    #[inline]
    fn remove(&mut self, ptr: *mut u8, birth_pressure: u64) -> bool {
        for slot in 0..ADAPTIVE_LIVE_SURVIVOR_SLOTS_PER_SITE {
            let mask = 1u8 << slot;
            if self.active_slots & mask != 0
                && self.samples[slot].ptr == ptr as usize
                && self.samples[slot].birth_pressure == birth_pressure
            {
                self.clear(slot);
                return true;
            }
        }
        false
    }

    #[inline]
    fn clear(&mut self, slot: usize) {
        self.samples[slot] = AdaptiveLiveSurvivorSample::empty();
        self.active_slots &= !(1u8 << slot);
    }

    #[inline]
    fn is_empty(self) -> bool {
        self.active_slots == 0
    }
}

#[derive(Clone, Copy)]
struct AdaptiveForceTrackAllState {
    enabled: bool,
    maximum_allocations: usize,
    maximum_requested_bytes: usize,
    admitted_allocations: usize,
    admitted_requested_bytes: usize,
    guard_bypasses: usize,
    pressure_allocations: usize,
    pressure_requested_bytes: usize,
    raw_pressure_allocations: usize,
    raw_pressure_requested_bytes: usize,
    raw_reallocation_pressure_allocations: usize,
    raw_reallocation_pressure_requested_bytes: usize,
}

impl AdaptiveForceTrackAllState {
    const fn disabled() -> Self {
        Self {
            enabled: false,
            maximum_allocations: 0,
            maximum_requested_bytes: 0,
            admitted_allocations: 0,
            admitted_requested_bytes: 0,
            guard_bypasses: 0,
            pressure_allocations: 0,
            pressure_requested_bytes: 0,
            raw_pressure_allocations: 0,
            raw_pressure_requested_bytes: 0,
            raw_reallocation_pressure_allocations: 0,
            raw_reallocation_pressure_requested_bytes: 0,
        }
    }

    fn enable(&mut self, maximum_allocations: usize, maximum_requested_bytes: usize) -> bool {
        if maximum_allocations == 0 || maximum_requested_bytes == 0 {
            return false;
        }
        *self = Self {
            enabled: true,
            maximum_allocations,
            maximum_requested_bytes,
            admitted_allocations: 0,
            admitted_requested_bytes: 0,
            guard_bypasses: 0,
            pressure_allocations: 0,
            pressure_requested_bytes: 0,
            raw_pressure_allocations: 0,
            raw_pressure_requested_bytes: 0,
            raw_reallocation_pressure_allocations: 0,
            raw_reallocation_pressure_requested_bytes: 0,
        };
        true
    }

    #[inline]
    fn allows(self, requested_bytes: usize) -> bool {
        self.enabled
            && self.admitted_allocations < self.maximum_allocations
            && matches!(
                self.admitted_requested_bytes.checked_add(requested_bytes),
                Some(total) if total <= self.maximum_requested_bytes
            )
    }

    #[inline]
    fn note_admitted(&mut self, requested_bytes: usize) {
        debug_assert!(self.allows(requested_bytes));
        self.admitted_allocations = self.admitted_allocations.saturating_add(1);
        self.admitted_requested_bytes = self
            .admitted_requested_bytes
            .saturating_add(requested_bytes);
    }

    #[inline]
    fn note_guard_bypass(&mut self) {
        self.guard_bypasses = self.guard_bypasses.saturating_add(1);
    }

    #[inline]
    fn note_pressure(&mut self, requested_bytes: usize, source: ForcePressureSource) {
        self.pressure_allocations = self.pressure_allocations.saturating_add(1);
        self.pressure_requested_bytes = self
            .pressure_requested_bytes
            .saturating_add(requested_bytes);
        match source {
            ForcePressureSource::SemanticAllocation => {}
            ForcePressureSource::RawAllocation => {
                self.raw_pressure_allocations = self.raw_pressure_allocations.saturating_add(1);
                self.raw_pressure_requested_bytes = self
                    .raw_pressure_requested_bytes
                    .saturating_add(requested_bytes);
            }
            ForcePressureSource::RawMovedReallocation => {
                self.raw_reallocation_pressure_allocations =
                    self.raw_reallocation_pressure_allocations.saturating_add(1);
                self.raw_reallocation_pressure_requested_bytes = self
                    .raw_reallocation_pressure_requested_bytes
                    .saturating_add(requested_bytes);
            }
        }
    }

    fn reset_counters(&mut self) {
        self.admitted_allocations = 0;
        self.admitted_requested_bytes = 0;
        self.guard_bypasses = 0;
        self.pressure_allocations = 0;
        self.pressure_requested_bytes = 0;
        self.raw_pressure_allocations = 0;
        self.raw_pressure_requested_bytes = 0;
        self.raw_reallocation_pressure_allocations = 0;
        self.raw_reallocation_pressure_requested_bytes = 0;
    }
}

#[derive(Clone, Copy)]
enum ForcePressureSource {
    SemanticAllocation,
    RawAllocation,
    RawMovedReallocation,
}

#[inline]
fn adaptive_placement_class(
    prediction: AdaptivePrediction,
    force_track_all: bool,
) -> LifetimePlacementClass {
    if force_track_all {
        LifetimePlacementClass::Ephemeral
    } else {
        prediction.placement_class()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct AdaptiveObservationKey {
    callsite: u64,
    type_id: u64,
    module_id: u64,
    requested_size: usize,
    align: usize,
}

impl AdaptiveObservationKey {
    const fn empty() -> Self {
        Self {
            callsite: 0,
            type_id: 0,
            module_id: 0,
            requested_size: 0,
            align: 0,
        }
    }

    fn from_site(site: AdaptiveSiteKey, requested_size: usize) -> Self {
        Self {
            callsite: site.callsite,
            type_id: site.type_id,
            module_id: site.module_id,
            requested_size,
            align: site.align,
        }
    }

    fn fingerprint(self) -> u64 {
        let mut hash = 0xcbf2_9ce4_8422_2325u64;
        for value in [
            self.callsite,
            self.type_id,
            self.module_id,
            self.requested_size as u64,
            self.align as u64,
        ] {
            hash ^= value;
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
        if hash == 0 {
            1
        } else {
            hash
        }
    }
}

/// Evaluation-only runtime ground truth at exact observed layout grain.
///
/// The production predictor keeps its established key, including flags,
/// placement hint, and rounded payload capacity. This export table is separate
/// and groups evidence by `(callsite, type_id, module_id, requested_size,
/// align)` solely for compiler/runtime analysis. Allocation counters and bytes
/// are hotness proxies; memory-access hotness is outside this telemetry.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
pub struct LifetimeAdaptiveSiteSnapshot {
    pub callsite: u64,
    pub type_id: u64,
    pub module_id: u64,
    pub requested_size: usize,
    pub align: usize,
    pub minimum_payload_capacity: usize,
    pub maximum_payload_capacity: usize,
    pub allocation_count: usize,
    pub allocation_requested_bytes: usize,
    pub allocation_payload_bytes: usize,
    pub tracked_allocations: usize,
    pub bypassed_allocations: usize,
    pub completed_outcomes: usize,
    pub short_outcomes: usize,
    pub long_outcomes: usize,
    pub censored_outcomes: usize,
    pub short_requested_bytes: usize,
    pub long_requested_bytes: usize,
    pub censored_requested_bytes: usize,
    pub total_completed_age_bytes: u64,
    pub maximum_completed_age_bytes: u64,
    pub tracked_inflight: usize,
    pub inflight_age_lower_bound_bytes: u64,
    /// Tracked objects classified Long while still live. These observations
    /// are orthogonal to completed outcomes and receive exactly one predictor
    /// vote before a later deallocation closes the allocation generation.
    pub live_survival_observations: usize,
    pub live_survival_requested_bytes: usize,
    pub live_survival_payload_bytes: usize,
    pub minimum_live_survival_age_bytes: u64,
    pub maximum_live_survival_age_bytes: u64,
    /// Live-survival observations whose allocation generation has not yet
    /// completed. `long_outcomes + current_live_survival_inflight` is the
    /// exact, non-overlapping Long truth currently known for this row.
    pub current_live_survival_inflight: usize,
    pub current_live_survival_inflight_requested_bytes: usize,
    pub current_live_survival_inflight_payload_bytes: usize,
    pub first_allocation_pressure: u64,
    pub last_allocation_pressure: u64,
    /// True when this observation row saw more than one production predictor
    /// key. This remains diagnostic and never changes predictor lookup.
    pub predictor_key_ambiguous: bool,
    pub predictor_flags_seen: u32,
    pub predictor_placement_hint_bits_union: u16,
    /// Latest learned predictor state: `0 = Cold`, `1 = Short`, `2 = Long`.
    /// A provisional static prior remains Cold until runtime confirmation.
    pub latest_prediction: u8,
    /// Latest static prior: `0 = Unknown`, `1 = advisory-short`, `2 = advisory-long`.
    pub latest_static_prior: u8,
}

impl LifetimeAdaptiveSiteSnapshot {
    pub const fn empty() -> Self {
        Self {
            callsite: 0,
            type_id: 0,
            module_id: 0,
            requested_size: 0,
            align: 0,
            minimum_payload_capacity: 0,
            maximum_payload_capacity: 0,
            allocation_count: 0,
            allocation_requested_bytes: 0,
            allocation_payload_bytes: 0,
            tracked_allocations: 0,
            bypassed_allocations: 0,
            completed_outcomes: 0,
            short_outcomes: 0,
            long_outcomes: 0,
            censored_outcomes: 0,
            short_requested_bytes: 0,
            long_requested_bytes: 0,
            censored_requested_bytes: 0,
            total_completed_age_bytes: 0,
            maximum_completed_age_bytes: 0,
            tracked_inflight: 0,
            inflight_age_lower_bound_bytes: 0,
            live_survival_observations: 0,
            live_survival_requested_bytes: 0,
            live_survival_payload_bytes: 0,
            minimum_live_survival_age_bytes: 0,
            maximum_live_survival_age_bytes: 0,
            current_live_survival_inflight: 0,
            current_live_survival_inflight_requested_bytes: 0,
            current_live_survival_inflight_payload_bytes: 0,
            first_allocation_pressure: 0,
            last_allocation_pressure: 0,
            predictor_key_ambiguous: false,
            predictor_flags_seen: 0,
            predictor_placement_hint_bits_union: 0,
            latest_prediction: AdaptivePrediction::Cold as u8,
            latest_static_prior: LifetimePlacementClass::Unknown as u8,
        }
    }
}

impl Default for LifetimeAdaptiveSiteSnapshot {
    fn default() -> Self {
        Self::empty()
    }
}

#[derive(Clone, Copy)]
struct AdaptiveObservationState {
    key: AdaptiveObservationKey,
    fingerprint: u64,
    minimum_payload_capacity: usize,
    maximum_payload_capacity: usize,
    allocation_count: usize,
    allocation_requested_bytes: usize,
    allocation_payload_bytes: usize,
    tracked_allocations: usize,
    bypassed_allocations: usize,
    short_outcomes: usize,
    long_outcomes: usize,
    censored_outcomes: usize,
    short_requested_bytes: usize,
    long_requested_bytes: usize,
    censored_requested_bytes: usize,
    total_completed_age_bytes: u64,
    maximum_completed_age_bytes: u64,
    tracked_inflight: usize,
    inflight_birth_pressure_sum: u64,
    live_survival_observations: usize,
    live_survival_requested_bytes: usize,
    live_survival_payload_bytes: usize,
    minimum_live_survival_age_bytes: u64,
    maximum_live_survival_age_bytes: u64,
    current_live_survival_inflight: usize,
    current_live_survival_inflight_requested_bytes: usize,
    current_live_survival_inflight_payload_bytes: usize,
    first_allocation_pressure: u64,
    last_allocation_pressure: u64,
    first_predictor_fingerprint: u64,
    predictor_key_ambiguous: bool,
    predictor_flags_seen: u32,
    predictor_placement_hint_bits_union: u16,
    latest_prediction: AdaptivePrediction,
    latest_static_prior: LifetimePlacementClass,
}

impl AdaptiveObservationState {
    const fn empty() -> Self {
        Self {
            key: AdaptiveObservationKey::empty(),
            fingerprint: 0,
            minimum_payload_capacity: 0,
            maximum_payload_capacity: 0,
            allocation_count: 0,
            allocation_requested_bytes: 0,
            allocation_payload_bytes: 0,
            tracked_allocations: 0,
            bypassed_allocations: 0,
            short_outcomes: 0,
            long_outcomes: 0,
            censored_outcomes: 0,
            short_requested_bytes: 0,
            long_requested_bytes: 0,
            censored_requested_bytes: 0,
            total_completed_age_bytes: 0,
            maximum_completed_age_bytes: 0,
            tracked_inflight: 0,
            inflight_birth_pressure_sum: 0,
            live_survival_observations: 0,
            live_survival_requested_bytes: 0,
            live_survival_payload_bytes: 0,
            minimum_live_survival_age_bytes: 0,
            maximum_live_survival_age_bytes: 0,
            current_live_survival_inflight: 0,
            current_live_survival_inflight_requested_bytes: 0,
            current_live_survival_inflight_payload_bytes: 0,
            first_allocation_pressure: 0,
            last_allocation_pressure: 0,
            first_predictor_fingerprint: 0,
            predictor_key_ambiguous: false,
            predictor_flags_seen: 0,
            predictor_placement_hint_bits_union: 0,
            latest_prediction: AdaptivePrediction::Cold,
            latest_static_prior: LifetimePlacementClass::Unknown,
        }
    }

    fn is_empty(self) -> bool {
        self.fingerprint == 0
    }

    fn new(key: AdaptiveObservationKey) -> Self {
        Self {
            key,
            fingerprint: key.fingerprint(),
            ..Self::empty()
        }
    }

    fn note_predictor_variant(&mut self, site: AdaptiveSiteKey) {
        let fingerprint = site.fingerprint();
        if self.first_predictor_fingerprint == 0 {
            self.first_predictor_fingerprint = fingerprint;
        } else if self.first_predictor_fingerprint != fingerprint {
            // A small exact-layout row can aggregate multiple predictor keys.
            // Record ambiguity without changing production predictor state.
            self.predictor_key_ambiguous = true;
        }
        self.predictor_flags_seen |= site.flags;
        self.predictor_placement_hint_bits_union |= site.placement_hint;
        if self.minimum_payload_capacity == 0 {
            self.minimum_payload_capacity = site.payload_capacity;
        } else {
            self.minimum_payload_capacity =
                self.minimum_payload_capacity.min(site.payload_capacity);
        }
        self.maximum_payload_capacity = self.maximum_payload_capacity.max(site.payload_capacity);
    }

    fn note_allocation(
        &mut self,
        site: AdaptiveSiteKey,
        pressure: u64,
        tracked: bool,
        learned_prediction: AdaptivePrediction,
        static_prior: LifetimePlacementClass,
    ) {
        self.note_predictor_variant(site);
        self.allocation_count = self.allocation_count.saturating_add(1);
        self.allocation_requested_bytes = self
            .allocation_requested_bytes
            .saturating_add(self.key.requested_size);
        self.allocation_payload_bytes = self
            .allocation_payload_bytes
            .saturating_add(site.payload_capacity);
        if tracked {
            self.tracked_allocations = self.tracked_allocations.saturating_add(1);
            self.tracked_inflight = self.tracked_inflight.saturating_add(1);
            self.inflight_birth_pressure_sum =
                self.inflight_birth_pressure_sum.saturating_add(pressure);
        } else {
            self.bypassed_allocations = self.bypassed_allocations.saturating_add(1);
        }
        if self.first_allocation_pressure == 0 {
            self.first_allocation_pressure = pressure;
        }
        self.last_allocation_pressure = pressure;
        self.latest_prediction = learned_prediction;
        self.latest_static_prior = static_prior;
    }

    fn note_deallocation(
        &mut self,
        birth_pressure: u64,
        age_bytes: u64,
        outcome: Option<bool>,
        live_survival_recorded: bool,
        payload_capacity: usize,
    ) {
        self.tracked_inflight = self.tracked_inflight.saturating_sub(1);
        self.inflight_birth_pressure_sum = self
            .inflight_birth_pressure_sum
            .saturating_sub(birth_pressure);
        self.total_completed_age_bytes = self.total_completed_age_bytes.saturating_add(age_bytes);
        self.maximum_completed_age_bytes = self.maximum_completed_age_bytes.max(age_bytes);
        if live_survival_recorded {
            self.current_live_survival_inflight =
                self.current_live_survival_inflight.saturating_sub(1);
            self.current_live_survival_inflight_requested_bytes = self
                .current_live_survival_inflight_requested_bytes
                .saturating_sub(self.key.requested_size);
            self.current_live_survival_inflight_payload_bytes = self
                .current_live_survival_inflight_payload_bytes
                .saturating_sub(payload_capacity);
        }
        match outcome {
            Some(false) => {
                self.short_outcomes = self.short_outcomes.saturating_add(1);
                self.short_requested_bytes = self
                    .short_requested_bytes
                    .saturating_add(self.key.requested_size);
            }
            Some(true) => {
                self.long_outcomes = self.long_outcomes.saturating_add(1);
                self.long_requested_bytes = self
                    .long_requested_bytes
                    .saturating_add(self.key.requested_size);
            }
            None => {
                self.censored_outcomes = self.censored_outcomes.saturating_add(1);
                self.censored_requested_bytes = self
                    .censored_requested_bytes
                    .saturating_add(self.key.requested_size);
            }
        }
    }

    fn note_live_survival(&mut self, age_bytes: u64, payload_capacity: usize) {
        self.live_survival_observations = self.live_survival_observations.saturating_add(1);
        self.live_survival_requested_bytes = self
            .live_survival_requested_bytes
            .saturating_add(self.key.requested_size);
        self.live_survival_payload_bytes = self
            .live_survival_payload_bytes
            .saturating_add(payload_capacity);
        if self.minimum_live_survival_age_bytes == 0 {
            self.minimum_live_survival_age_bytes = age_bytes;
        } else {
            self.minimum_live_survival_age_bytes =
                self.minimum_live_survival_age_bytes.min(age_bytes);
        }
        self.maximum_live_survival_age_bytes = self.maximum_live_survival_age_bytes.max(age_bytes);
        self.current_live_survival_inflight = self.current_live_survival_inflight.saturating_add(1);
        self.current_live_survival_inflight_requested_bytes = self
            .current_live_survival_inflight_requested_bytes
            .saturating_add(self.key.requested_size);
        self.current_live_survival_inflight_payload_bytes = self
            .current_live_survival_inflight_payload_bytes
            .saturating_add(payload_capacity);
    }

    fn snapshot(self, current_pressure: u64) -> LifetimeAdaptiveSiteSnapshot {
        LifetimeAdaptiveSiteSnapshot {
            callsite: self.key.callsite,
            type_id: self.key.type_id,
            module_id: self.key.module_id,
            requested_size: self.key.requested_size,
            align: self.key.align,
            minimum_payload_capacity: self.minimum_payload_capacity,
            maximum_payload_capacity: self.maximum_payload_capacity,
            allocation_count: self.allocation_count,
            allocation_requested_bytes: self.allocation_requested_bytes,
            allocation_payload_bytes: self.allocation_payload_bytes,
            tracked_allocations: self.tracked_allocations,
            bypassed_allocations: self.bypassed_allocations,
            completed_outcomes: self
                .short_outcomes
                .saturating_add(self.long_outcomes)
                .saturating_add(self.censored_outcomes),
            short_outcomes: self.short_outcomes,
            long_outcomes: self.long_outcomes,
            censored_outcomes: self.censored_outcomes,
            short_requested_bytes: self.short_requested_bytes,
            long_requested_bytes: self.long_requested_bytes,
            censored_requested_bytes: self.censored_requested_bytes,
            total_completed_age_bytes: self.total_completed_age_bytes,
            maximum_completed_age_bytes: self.maximum_completed_age_bytes,
            tracked_inflight: self.tracked_inflight,
            inflight_age_lower_bound_bytes: current_pressure
                .saturating_mul(self.tracked_inflight as u64)
                .saturating_sub(self.inflight_birth_pressure_sum),
            live_survival_observations: self.live_survival_observations,
            live_survival_requested_bytes: self.live_survival_requested_bytes,
            live_survival_payload_bytes: self.live_survival_payload_bytes,
            minimum_live_survival_age_bytes: self.minimum_live_survival_age_bytes,
            maximum_live_survival_age_bytes: self.maximum_live_survival_age_bytes,
            current_live_survival_inflight: self.current_live_survival_inflight,
            current_live_survival_inflight_requested_bytes: self
                .current_live_survival_inflight_requested_bytes,
            current_live_survival_inflight_payload_bytes: self
                .current_live_survival_inflight_payload_bytes,
            first_allocation_pressure: self.first_allocation_pressure,
            last_allocation_pressure: self.last_allocation_pressure,
            predictor_key_ambiguous: self.predictor_key_ambiguous,
            predictor_flags_seen: self.predictor_flags_seen,
            predictor_placement_hint_bits_union: self.predictor_placement_hint_bits_union,
            latest_prediction: self.latest_prediction as u8,
            latest_static_prior: self.latest_static_prior as u8,
        }
    }
}

#[derive(Clone, Copy)]
#[repr(C)]
struct AdaptiveSlotTrailer {
    birth_pressure: u64,
    site_fingerprint: u64,
    site_index: u32,
    requested_size: u32,
    prediction: u8,
    static_hint: u8,
    flags: u16,
    checksum: u32,
}

#[derive(Clone, Copy)]
struct AdaptiveAllocationRecord {
    birth_pressure: u64,
    site_fingerprint: u64,
    site_index: usize,
    requested_size: usize,
    learned_prediction: AdaptivePrediction,
    static_hint: LifetimePlacementClass,
    confirmed_long_placement: bool,
}

impl AdaptiveSlotTrailer {
    fn new(ptr: *mut u8, record: AdaptiveAllocationRecord) -> Self {
        let mut trailer = Self {
            birth_pressure: record.birth_pressure,
            site_fingerprint: record.site_fingerprint,
            site_index: record.site_index as u32,
            requested_size: record.requested_size as u32,
            prediction: record.learned_prediction as u8,
            static_hint: record.static_hint as u8,
            flags: ADAPTIVE_TRAILER_ACTIVE
                | if record.confirmed_long_placement {
                    ADAPTIVE_TRAILER_CONFIRMED_LONG
                } else {
                    0
                },
            checksum: 0,
        };
        trailer.checksum = trailer.expected_checksum(ptr);
        trailer
    }

    #[inline]
    fn expected_checksum(self, ptr: *mut u8) -> u32 {
        let mut hash = 0x811c_9dc5u32;
        for value in [
            self.birth_pressure,
            self.site_fingerprint,
            self.site_index as u64,
            self.requested_size as u64,
            self.prediction as u64,
            self.static_hint as u64,
            self.flags as u64,
            ptr as usize as u64,
        ] {
            hash ^= value as u32;
            hash = hash.wrapping_mul(0x0100_0193);
            hash ^= (value >> 32) as u32;
            hash = hash.wrapping_mul(0x0100_0193);
        }
        hash
    }

    #[inline]
    fn is_valid(self, ptr: *mut u8, payload_capacity: usize) -> bool {
        self.flags & ADAPTIVE_TRAILER_ACTIVE != 0
            && self.flags & !ADAPTIVE_TRAILER_KNOWN_FLAGS == 0
            && self.requested_size as usize <= payload_capacity
            && (self.site_index as usize) < ADAPTIVE_SITE_SLOTS
            && AdaptivePrediction::from_u8(self.prediction).is_some()
            && lifetime_class_from_u8(self.static_hint).is_some()
            && self.checksum == self.expected_checksum(ptr)
    }

    #[inline]
    fn confirmed_long_placement(self) -> bool {
        self.flags & ADAPTIVE_TRAILER_CONFIRMED_LONG != 0
    }

    #[inline]
    fn survival_recorded(self) -> bool {
        self.flags & ADAPTIVE_TRAILER_SURVIVAL_RECORDED != 0
    }

    #[inline]
    fn record_survival(&mut self, ptr: *mut u8) {
        debug_assert!(!self.survival_recorded());
        self.flags |= ADAPTIVE_TRAILER_SURVIVAL_RECORDED;
        self.checksum = self.expected_checksum(ptr);
    }
}

const _: () = assert!(size_of::<AdaptiveSlotTrailer>() == 32);

/// Match the semantic cache's exact reuse boundary. Callsite stays
/// diagnostic-only because allocation and matching Drop sites are distinct.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct ArenaIdentity {
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
}

impl ArenaIdentity {
    const fn unknown() -> Self {
        Self {
            type_id: 0,
            module_id: 0,
            flags: 0,
            lifetime_hint: 0,
            placement_hint: 0,
        }
    }

    const fn from_metadata(metadata: AllocationMetadata) -> Self {
        Self {
            type_id: metadata.type_id,
            module_id: metadata.module_id,
            flags: metadata.flags,
            lifetime_hint: metadata.lifetime_hint,
            placement_hint: metadata.placement_hint,
        }
    }

    const fn from_metadata_without_lifetime_hint(metadata: AllocationMetadata) -> Self {
        Self {
            type_id: metadata.type_id,
            module_id: metadata.module_id,
            flags: metadata.flags,
            lifetime_hint: 0,
            placement_hint: metadata.placement_hint,
        }
    }
}

/// Stable allocation-side key for reusing a compiler-directed extent whose
/// physical THP backing was established by a prior full cohort. `ArenaIdentity`
/// deliberately excludes callsite for ordinary region packing, while a density
/// certificate must remain tied to the exact allocation site that earned it.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct PromotedReuseIdentity {
    arena: ArenaIdentity,
    callsite: u64,
}

#[derive(Clone, Copy)]
struct IdentityRegion {
    identity: ArenaIdentity,
    callsite: u64,
    /// Allocation epoch when every live slot is known to share one epoch.
    birth_epoch: usize,
    /// A non-cohort policy reused this region across allocation epochs while
    /// older slots remained live, so per-object death epochs are ambiguous.
    epoch_mixed: bool,
    next_unused: usize,
    live: usize,
    free_head: usize,
    assigned: bool,
}

impl IdentityRegion {
    const fn empty() -> Self {
        Self {
            identity: ArenaIdentity::unknown(),
            callsite: 0,
            birth_epoch: 0,
            epoch_mixed: false,
            next_unused: 0,
            live: 0,
            free_head: NONE,
            assigned: false,
        }
    }

    #[inline]
    fn has_available_slot(self, capacity: usize) -> bool {
        self.free_head != NONE || self.next_unused < capacity
    }
}

#[derive(Clone, Copy)]
struct Extent {
    base: usize,
    slot_size: usize,
    payload_capacity: usize,
    region_capacity: usize,
    live: usize,
    /// Adaptive slots placed after runtime learning confirmed Long. Static
    /// priors share this candidate extent but never contribute to promotion.
    adaptive_confirmed_long_live: usize,
    bucket: usize,
    regions: [IdentityRegion; IDENTITY_REGIONS_PER_EXTENT],
    lifetime_class: LifetimePlacementClass,
    /// Nonzero only when policy forbids cross-epoch extent mixing.
    cohort_epoch: usize,
    /// Requested mapping mode remains stable when THP advice later changes
    /// the observed backing state.
    requested_backing: Option<RequestedBacking>,
    backing: ActualBacking,
    /// Homogeneous compiler allocation identity that filled this extent before
    /// its successful synchronous collapse. While present, the mapping is
    /// sealed against other allocation sites, including during collapse.
    promoted_reuse_identity: Option<PromotedReuseIdentity>,
    /// Whether the current activation reached full density. Reactivating an
    /// empty promoted extent clears this bit; a sparse cycle is unmapped when it
    /// drains instead of retaining an old density certificate indefinitely.
    promoted_reuse_cycle_full: bool,
    /// True only while synchronous MADV_COLLAPSE owns the mapping. Advice-only
    /// compiler-inferred promotion leaves this false even though backing is
    /// `ThpAdvisedUnverified`.
    collapse_pending: bool,
    adaptive_trailer: bool,
    available_prev: usize,
    available_next: usize,
    on_available_list: bool,
    retained_empty_prev: usize,
    retained_empty_next: usize,
    on_retained_empty_list: bool,
    next_free_descriptor: usize,
}

impl Extent {
    const fn empty() -> Self {
        Self {
            base: 0,
            slot_size: 0,
            payload_capacity: 0,
            region_capacity: 0,
            live: 0,
            adaptive_confirmed_long_live: 0,
            bucket: 0,
            regions: [IdentityRegion::empty(); IDENTITY_REGIONS_PER_EXTENT],
            lifetime_class: LifetimePlacementClass::Unknown,
            cohort_epoch: 0,
            requested_backing: None,
            backing: ActualBacking::Unmapped,
            promoted_reuse_identity: None,
            promoted_reuse_cycle_full: false,
            collapse_pending: false,
            adaptive_trailer: false,
            available_prev: NONE,
            available_next: NONE,
            on_available_list: false,
            retained_empty_prev: NONE,
            retained_empty_next: NONE,
            on_retained_empty_list: false,
            next_free_descriptor: NONE,
        }
    }

    #[inline]
    fn in_use(self) -> bool {
        self.base != 0
    }

    #[inline]
    fn matching_region_with_space(
        &self,
        identity: ArenaIdentity,
        callsite: u64,
        birth_epoch: usize,
        require_epoch_match: bool,
        require_callsite_match: bool,
    ) -> Option<usize> {
        self.regions.iter().position(|region| {
            region.assigned
                && region.identity == identity
                && (!require_callsite_match || region.callsite == callsite)
                && (!require_epoch_match || region.birth_epoch == birth_epoch)
                && region.has_available_slot(self.region_capacity)
        })
    }

    #[inline]
    fn exact_geometry_matches(&self, geometry: SlotGeometry) -> bool {
        self.slot_size == geometry.slot_size
            && self.payload_capacity == geometry.payload_capacity
            && self.region_capacity == geometry.region_capacity
            && self.bucket == geometry.bucket
            && self.adaptive_trailer == geometry.adaptive_trailer
    }

    #[inline]
    fn full_slot_count(&self) -> usize {
        self.region_capacity
            .saturating_mul(IDENTITY_REGIONS_PER_EXTENT)
    }

    #[inline]
    fn unassigned_region(&self) -> Option<usize> {
        self.regions.iter().position(|region| !region.assigned)
    }

    #[inline]
    fn has_available_slot(&self) -> bool {
        self.unassigned_region().is_some()
            || self
                .regions
                .iter()
                .any(|region| region.assigned && region.has_available_slot(self.region_capacity))
    }
}

#[inline]
fn empty_extent_retention_eligible(
    policy: LifetimeHugepagePolicy,
    backend: LifetimePageBackend,
    extent: &Extent,
) -> bool {
    if extent.live != 0
        || extent.adaptive_confirmed_long_live != 0
        || extent.cohort_epoch != 0
        || extent.lifetime_class == LifetimePlacementClass::Unknown
        || extent.regions.iter().any(|region| region.assigned)
    {
        return false;
    }
    match policy {
        LifetimeHugepagePolicy::SegregatedOrdinary
        | LifetimeHugepagePolicy::CompilerInferredOrdinary
        | LifetimeHugepagePolicy::CompilerDirectedOrdinary => {
            backend == LifetimePageBackend::TransparentHugepage
                && !extent.adaptive_trailer
                && extent.lifetime_class == LifetimePlacementClass::LongLived
                && extent.requested_backing == Some(RequestedBacking::Ordinary)
                && extent.backing == ActualBacking::Ordinary
        }
        LifetimeHugepagePolicy::CompilerInferredHugepage => {
            !extent.adaptive_trailer
                && extent.lifetime_class == LifetimePlacementClass::LongLived
                && extent.requested_backing == Some(RequestedBacking::EpochCandidate)
                && extent.backing == ActualBacking::ThpCandidateNoHugepage
        }
        LifetimeHugepagePolicy::CompilerDirectedHugepage => {
            !extent.adaptive_trailer
                && extent.lifetime_class == LifetimePlacementClass::LongLived
                && extent.requested_backing == Some(RequestedBacking::EpochCandidate)
                && (extent.backing == ActualBacking::ThpCandidateNoHugepage
                    || (extent.backing == ActualBacking::ThpCollapseSucceededPointInTime
                        && extent.promoted_reuse_identity.is_some()
                        && extent.promoted_reuse_cycle_full))
        }
        LifetimeHugepagePolicy::LongLivedHugepage => {
            !extent.adaptive_trailer
                && extent.lifetime_class == LifetimePlacementClass::LongLived
                && extent.requested_backing == Some(RequestedBacking::LargePage)
                && matches!(
                    extent.backing,
                    ActualBacking::ThpAdvisedUnverified
                        | ActualBacking::ThpCollapseSucceededPointInTime
                )
        }
        LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary => {
            backend == LifetimePageBackend::TransparentHugepage
                && extent.adaptive_trailer
                && extent.requested_backing == Some(RequestedBacking::Ordinary)
                && extent.backing == ActualBacking::Ordinary
        }
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage
        | LifetimeHugepagePolicy::AdaptiveRuntimeProfileHugepage => {
            backend == LifetimePageBackend::TransparentHugepage
                && extent.adaptive_trailer
                && ((extent.requested_backing == Some(RequestedBacking::Ordinary)
                    && extent.backing == ActualBacking::Ordinary)
                    || (extent.requested_backing == Some(RequestedBacking::EpochCandidate)
                        && extent.backing == ActualBacking::ThpCandidateNoHugepage))
        }
        _ => false,
    }
}

#[inline]
fn adaptive_thp_promotion_eligibility(extent: &Extent) -> ThpPromotionEligibility {
    if !extent.in_use()
        || extent.lifetime_class != LifetimePlacementClass::LongLived
        || !extent.adaptive_trailer
        || !matches!(
            extent.requested_backing,
            Some(RequestedBacking::EpochCandidate)
        )
        || !matches!(
            extent.backing,
            ActualBacking::ThpCandidateNoHugepage
                | ActualBacking::ThpCandidateNoHugepageAdviceFailed
        )
    {
        return ThpPromotionEligibility::Ineligible;
    }
    if extent
        .adaptive_confirmed_long_live
        .saturating_mul(extent.slot_size)
        < ADAPTIVE_THP_PROMOTION_MIN_CONFIRMED_LIVE_BYTES
    {
        ThpPromotionEligibility::LowOccupancy
    } else {
        ThpPromotionEligibility::Eligible
    }
}

#[inline]
fn thp_promotion_eligibility(extent: &Extent, current_epoch: usize) -> ThpPromotionEligibility {
    if !extent.in_use()
        || extent.live == 0
        || extent.cohort_epoch == 0
        || extent.cohort_epoch >= current_epoch
        || extent.backing.is_thp_confirmed_at_collapse()
        || !matches!(
            extent.backing,
            ActualBacking::ThpCandidateNoHugepage
                | ActualBacking::ThpCandidateNoHugepageAdviceFailed
                | ActualBacking::ThpAdvisedUnverified
        )
    {
        return ThpPromotionEligibility::Ineligible;
    }
    if extent.live.saturating_mul(extent.slot_size) < THP_PROMOTION_MIN_LIVE_BYTES {
        ThpPromotionEligibility::LowOccupancy
    } else {
        ThpPromotionEligibility::Eligible
    }
}

struct LifetimeArenaState {
    extents: [Extent; MAX_EXTENTS],
    lookup: [usize; EXTENT_LOOKUP_SLOTS],
    available_heads: [usize; BUCKET_COUNT],
    next_unused_descriptor: usize,
    free_descriptor_head: usize,
    retained_empty_head: usize,
    retained_empty_tail: usize,
    current_retained_empty_extents: usize,
    peak_retained_empty_extents: usize,
    retained_empty_extent_insertions: usize,
    retained_empty_extent_reuse_hits: usize,
    retained_empty_extent_evictions: usize,
    retained_empty_extent_trims: usize,
    current_epoch: usize,
    phase_advances: usize,
    epoch_cohort_extent_mappings: usize,
    routed_allocations: usize,
    routed_deallocations: usize,
    unknown_bypasses: usize,
    unknown_bypass_requested_bytes: usize,
    unsupported_layout_bypasses: usize,
    allocation_fallbacks: usize,
    slot_reuse_hits: usize,
    slot_bump_allocations: usize,
    identity_region_assignments: usize,
    identity_region_releases: usize,
    ordinary_extent_mappings: usize,
    hugetlb_extent_mappings: usize,
    hugetlb_fallback_extent_mappings: usize,
    thp_extent_mappings: usize,
    thp_candidate_extent_mappings: usize,
    thp_advice_attempts: usize,
    thp_advice_successes: usize,
    thp_advice_errors: usize,
    thp_collapse_eligible_extents: usize,
    thp_collapse_low_occupancy_skips: usize,
    thp_collapse_attempts: usize,
    thp_collapse_successes: usize,
    thp_collapse_errors: usize,
    thp_collapse_last_error_code: isize,
    mapping_failures: usize,
    nohugepage_advice_failures: usize,
    extent_unmaps: usize,
    extent_unmap_failures: usize,
    current_extents: usize,
    peak_extents: usize,
    current_ordinary_extents: usize,
    peak_ordinary_extents: usize,
    current_hugetlb_extents: usize,
    peak_hugetlb_extents: usize,
    current_thp_extents: usize,
    peak_thp_extents: usize,
    current_thp_collapse_confirmed_extents: usize,
    peak_thp_collapse_confirmed_extents: usize,
    live_objects: usize,
    live_ephemeral_objects: usize,
    live_long_lived_objects: usize,
    live_slot_bytes: usize,
    runtime_validated_objects: usize,
    runtime_validated_bytes: usize,
    runtime_validation_excluded_objects: usize,
    runtime_validation_excluded_bytes: usize,
    runtime_delayed_free_excluded_objects: usize,
    runtime_delayed_free_excluded_bytes: usize,
    runtime_mixed_epoch_excluded_objects: usize,
    runtime_mixed_epoch_excluded_bytes: usize,
    predictor_confusion: ConfusionCounters,
    placement_confusion: ConfusionCounters,
    current_identity_regions: usize,
    peak_identity_regions: usize,
    adaptive_sites: [AdaptiveSiteState; ADAPTIVE_SITE_SLOTS],
    adaptive_live_survivors: [AdaptiveLiveSurvivorSite; ADAPTIVE_SITE_SLOTS],
    adaptive_live_survivor_active_sites: [u64; ADAPTIVE_LIVE_SURVIVOR_ACTIVE_WORDS],
    adaptive_live_survivor_next_due_pressure: u64,
    adaptive_observations: [AdaptiveObservationState; ADAPTIVE_OBSERVATION_SLOTS],
    adaptive_pressure_bytes: u64,
    adaptive_epoch_advances: usize,
    adaptive_site_count: usize,
    adaptive_eligible_allocations: usize,
    adaptive_training_allocations: usize,
    adaptive_cold_bypassed_allocations: usize,
    adaptive_short_routed_allocations: usize,
    adaptive_short_bypassed_allocations: usize,
    adaptive_long_routed_allocations: usize,
    adaptive_missing_identity_bypasses: usize,
    adaptive_site_table_bypasses: usize,
    adaptive_prior_conflicts: usize,
    adaptive_observation_recording: bool,
    adaptive_observation_site_count: usize,
    adaptive_observation_table_bypasses: usize,
    adaptive_force_track_all: AdaptiveForceTrackAllState,
    adaptive_live_trailers: usize,
    adaptive_trailer_corruptions: usize,
    adaptive_live_survival_registrations: usize,
    adaptive_live_survival_registration_bypasses: usize,
    adaptive_live_survival_scans: usize,
    adaptive_live_survival_slots_examined: usize,
    adaptive_live_survival_observations: usize,
    adaptive_live_survival_promotions: usize,
    adaptive_live_survival_stale_abstentions: usize,
    adaptive_short_observations: usize,
    adaptive_long_observations: usize,
    adaptive_censored_observations: usize,
    adaptive_decisive_observation_bytes: usize,
    adaptive_promotions: usize,
    adaptive_demotions: usize,
    adaptive_prior_corrections: usize,
    adaptive_predictor_confusion: ConfusionCounters,
    adaptive_static_hint_confusion: ConfusionCounters,
    adaptive_static_hint_abstained_objects: usize,
    adaptive_static_hint_abstained_bytes: usize,
    compiler_directed_arena_lock_acquisitions: usize,
    compiler_directed_short_routes: usize,
    compiler_directed_short_requested_bytes: usize,
    compiler_directed_short_slot_bytes: usize,
    compiler_directed_long_routes: usize,
    compiler_directed_long_requested_bytes: usize,
    compiler_directed_long_slot_bytes: usize,
    compiler_directed_deallocations: usize,
    compiler_directed_routes_without_trailer: usize,
    compiler_directed_density_promotion_attempts: usize,
    compiler_directed_density_promotion_successes: usize,
    compiler_directed_density_promotion_errors: usize,
}

impl LifetimeArenaState {
    const fn new() -> Self {
        Self {
            extents: [Extent::empty(); MAX_EXTENTS],
            lookup: [NONE; EXTENT_LOOKUP_SLOTS],
            available_heads: [NONE; BUCKET_COUNT],
            next_unused_descriptor: 0,
            free_descriptor_head: NONE,
            retained_empty_head: NONE,
            retained_empty_tail: NONE,
            current_retained_empty_extents: 0,
            peak_retained_empty_extents: 0,
            retained_empty_extent_insertions: 0,
            retained_empty_extent_reuse_hits: 0,
            retained_empty_extent_evictions: 0,
            retained_empty_extent_trims: 0,
            current_epoch: 1,
            phase_advances: 0,
            epoch_cohort_extent_mappings: 0,
            routed_allocations: 0,
            routed_deallocations: 0,
            unknown_bypasses: 0,
            unknown_bypass_requested_bytes: 0,
            unsupported_layout_bypasses: 0,
            allocation_fallbacks: 0,
            slot_reuse_hits: 0,
            slot_bump_allocations: 0,
            identity_region_assignments: 0,
            identity_region_releases: 0,
            ordinary_extent_mappings: 0,
            hugetlb_extent_mappings: 0,
            hugetlb_fallback_extent_mappings: 0,
            thp_extent_mappings: 0,
            thp_candidate_extent_mappings: 0,
            thp_advice_attempts: 0,
            thp_advice_successes: 0,
            thp_advice_errors: 0,
            thp_collapse_eligible_extents: 0,
            thp_collapse_low_occupancy_skips: 0,
            thp_collapse_attempts: 0,
            thp_collapse_successes: 0,
            thp_collapse_errors: 0,
            thp_collapse_last_error_code: 0,
            mapping_failures: 0,
            nohugepage_advice_failures: 0,
            extent_unmaps: 0,
            extent_unmap_failures: 0,
            current_extents: 0,
            peak_extents: 0,
            current_ordinary_extents: 0,
            peak_ordinary_extents: 0,
            current_hugetlb_extents: 0,
            peak_hugetlb_extents: 0,
            current_thp_extents: 0,
            peak_thp_extents: 0,
            current_thp_collapse_confirmed_extents: 0,
            peak_thp_collapse_confirmed_extents: 0,
            live_objects: 0,
            live_ephemeral_objects: 0,
            live_long_lived_objects: 0,
            live_slot_bytes: 0,
            runtime_validated_objects: 0,
            runtime_validated_bytes: 0,
            runtime_validation_excluded_objects: 0,
            runtime_validation_excluded_bytes: 0,
            runtime_delayed_free_excluded_objects: 0,
            runtime_delayed_free_excluded_bytes: 0,
            runtime_mixed_epoch_excluded_objects: 0,
            runtime_mixed_epoch_excluded_bytes: 0,
            predictor_confusion: ConfusionCounters::new(),
            placement_confusion: ConfusionCounters::new(),
            current_identity_regions: 0,
            peak_identity_regions: 0,
            adaptive_sites: [AdaptiveSiteState::empty(); ADAPTIVE_SITE_SLOTS],
            adaptive_live_survivors: [AdaptiveLiveSurvivorSite::empty(); ADAPTIVE_SITE_SLOTS],
            adaptive_live_survivor_active_sites: [0; ADAPTIVE_LIVE_SURVIVOR_ACTIVE_WORDS],
            adaptive_live_survivor_next_due_pressure: u64::MAX,
            adaptive_observations: [AdaptiveObservationState::empty(); ADAPTIVE_OBSERVATION_SLOTS],
            adaptive_pressure_bytes: 0,
            adaptive_epoch_advances: 0,
            adaptive_site_count: 0,
            adaptive_eligible_allocations: 0,
            adaptive_training_allocations: 0,
            adaptive_cold_bypassed_allocations: 0,
            adaptive_short_routed_allocations: 0,
            adaptive_short_bypassed_allocations: 0,
            adaptive_long_routed_allocations: 0,
            adaptive_missing_identity_bypasses: 0,
            adaptive_site_table_bypasses: 0,
            adaptive_prior_conflicts: 0,
            adaptive_observation_recording: false,
            adaptive_observation_site_count: 0,
            adaptive_observation_table_bypasses: 0,
            adaptive_force_track_all: AdaptiveForceTrackAllState::disabled(),
            adaptive_live_trailers: 0,
            adaptive_trailer_corruptions: 0,
            adaptive_live_survival_registrations: 0,
            adaptive_live_survival_registration_bypasses: 0,
            adaptive_live_survival_scans: 0,
            adaptive_live_survival_slots_examined: 0,
            adaptive_live_survival_observations: 0,
            adaptive_live_survival_promotions: 0,
            adaptive_live_survival_stale_abstentions: 0,
            adaptive_short_observations: 0,
            adaptive_long_observations: 0,
            adaptive_censored_observations: 0,
            adaptive_decisive_observation_bytes: 0,
            adaptive_promotions: 0,
            adaptive_demotions: 0,
            adaptive_prior_corrections: 0,
            adaptive_predictor_confusion: ConfusionCounters::new(),
            adaptive_static_hint_confusion: ConfusionCounters::new(),
            adaptive_static_hint_abstained_objects: 0,
            adaptive_static_hint_abstained_bytes: 0,
            compiler_directed_arena_lock_acquisitions: 0,
            compiler_directed_short_routes: 0,
            compiler_directed_short_requested_bytes: 0,
            compiler_directed_short_slot_bytes: 0,
            compiler_directed_long_routes: 0,
            compiler_directed_long_requested_bytes: 0,
            compiler_directed_long_slot_bytes: 0,
            compiler_directed_deallocations: 0,
            compiler_directed_routes_without_trailer: 0,
            compiler_directed_density_promotion_attempts: 0,
            compiler_directed_density_promotion_successes: 0,
            compiler_directed_density_promotion_errors: 0,
        }
    }

    #[inline]
    fn reset_counters(&mut self, preserve_adaptive_model: bool) {
        debug_assert_eq!(self.current_extents, 0);
        debug_assert_eq!(self.current_retained_empty_extents, 0);
        self.retained_empty_head = NONE;
        self.retained_empty_tail = NONE;
        self.peak_retained_empty_extents = 0;
        self.retained_empty_extent_insertions = 0;
        self.retained_empty_extent_reuse_hits = 0;
        self.retained_empty_extent_evictions = 0;
        self.retained_empty_extent_trims = 0;
        self.current_epoch = 1;
        self.phase_advances = 0;
        self.epoch_cohort_extent_mappings = 0;
        self.routed_allocations = 0;
        self.routed_deallocations = 0;
        self.unknown_bypasses = 0;
        self.unknown_bypass_requested_bytes = 0;
        self.unsupported_layout_bypasses = 0;
        self.allocation_fallbacks = 0;
        self.slot_reuse_hits = 0;
        self.slot_bump_allocations = 0;
        self.identity_region_assignments = 0;
        self.identity_region_releases = 0;
        self.ordinary_extent_mappings = 0;
        self.hugetlb_extent_mappings = 0;
        self.hugetlb_fallback_extent_mappings = 0;
        self.thp_extent_mappings = 0;
        self.thp_candidate_extent_mappings = 0;
        self.thp_advice_attempts = 0;
        self.thp_advice_successes = 0;
        self.thp_advice_errors = 0;
        self.thp_collapse_eligible_extents = 0;
        self.thp_collapse_low_occupancy_skips = 0;
        self.thp_collapse_attempts = 0;
        self.thp_collapse_successes = 0;
        self.thp_collapse_errors = 0;
        self.thp_collapse_last_error_code = 0;
        self.mapping_failures = 0;
        self.nohugepage_advice_failures = 0;
        self.extent_unmaps = 0;
        self.extent_unmap_failures = 0;
        self.runtime_validated_objects = 0;
        self.runtime_validated_bytes = 0;
        self.runtime_validation_excluded_objects = 0;
        self.runtime_validation_excluded_bytes = 0;
        self.runtime_delayed_free_excluded_objects = 0;
        self.runtime_delayed_free_excluded_bytes = 0;
        self.runtime_mixed_epoch_excluded_objects = 0;
        self.runtime_mixed_epoch_excluded_bytes = 0;
        self.predictor_confusion = ConfusionCounters::new();
        self.placement_confusion = ConfusionCounters::new();
        if !preserve_adaptive_model {
            self.adaptive_sites = [AdaptiveSiteState::empty(); ADAPTIVE_SITE_SLOTS];
            self.adaptive_site_count = 0;
        } else {
            for site in self
                .adaptive_sites
                .iter_mut()
                .filter(|site| !site.is_empty())
            {
                site.tracked_inflight = 0;
            }
        }
        self.adaptive_live_survivors = [AdaptiveLiveSurvivorSite::empty(); ADAPTIVE_SITE_SLOTS];
        self.adaptive_live_survivor_active_sites = [0; ADAPTIVE_LIVE_SURVIVOR_ACTIVE_WORDS];
        self.adaptive_live_survivor_next_due_pressure = u64::MAX;
        self.adaptive_pressure_bytes = 0;
        self.adaptive_epoch_advances = 0;
        self.adaptive_eligible_allocations = 0;
        self.adaptive_training_allocations = 0;
        self.adaptive_cold_bypassed_allocations = 0;
        self.adaptive_short_routed_allocations = 0;
        self.adaptive_short_bypassed_allocations = 0;
        self.adaptive_long_routed_allocations = 0;
        self.adaptive_missing_identity_bypasses = 0;
        self.adaptive_site_table_bypasses = 0;
        self.adaptive_prior_conflicts = 0;
        self.adaptive_observations =
            [AdaptiveObservationState::empty(); ADAPTIVE_OBSERVATION_SLOTS];
        self.adaptive_observation_site_count = 0;
        self.adaptive_observation_table_bypasses = 0;
        self.adaptive_force_track_all.reset_counters();
        self.adaptive_live_trailers = 0;
        self.adaptive_trailer_corruptions = 0;
        self.adaptive_live_survival_registrations = 0;
        self.adaptive_live_survival_registration_bypasses = 0;
        self.adaptive_live_survival_scans = 0;
        self.adaptive_live_survival_slots_examined = 0;
        self.adaptive_live_survival_observations = 0;
        self.adaptive_live_survival_promotions = 0;
        self.adaptive_live_survival_stale_abstentions = 0;
        self.adaptive_short_observations = 0;
        self.adaptive_long_observations = 0;
        self.adaptive_censored_observations = 0;
        self.adaptive_decisive_observation_bytes = 0;
        self.adaptive_promotions = 0;
        self.adaptive_demotions = 0;
        self.adaptive_prior_corrections = 0;
        self.adaptive_predictor_confusion = ConfusionCounters::new();
        self.adaptive_static_hint_confusion = ConfusionCounters::new();
        self.adaptive_static_hint_abstained_objects = 0;
        self.adaptive_static_hint_abstained_bytes = 0;
        self.compiler_directed_arena_lock_acquisitions = 0;
        self.compiler_directed_short_routes = 0;
        self.compiler_directed_short_requested_bytes = 0;
        self.compiler_directed_short_slot_bytes = 0;
        self.compiler_directed_long_routes = 0;
        self.compiler_directed_long_requested_bytes = 0;
        self.compiler_directed_long_slot_bytes = 0;
        self.compiler_directed_deallocations = 0;
        self.compiler_directed_routes_without_trailer = 0;
        self.compiler_directed_density_promotion_attempts = 0;
        self.compiler_directed_density_promotion_successes = 0;
        self.compiler_directed_density_promotion_errors = 0;
        self.peak_extents = self.current_extents;
        self.peak_ordinary_extents = self.current_ordinary_extents;
        self.peak_hugetlb_extents = self.current_hugetlb_extents;
        self.peak_thp_extents = self.current_thp_extents;
        self.peak_thp_collapse_confirmed_extents = self.current_thp_collapse_confirmed_extents;
        self.peak_identity_regions = self.current_identity_regions;
    }

    #[inline]
    fn snapshot(&self) -> LifetimeHugepageStatsSnapshot {
        let retained_bytes = self
            .current_extents
            .saturating_mul(LIFETIME_HUGEPAGE_EXTENT_BYTES);
        let assigned_region_bytes = self
            .current_identity_regions
            .saturating_mul(LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES);
        let unassigned_region_bytes = retained_bytes.saturating_sub(assigned_region_bytes);
        let cohort_pinned_unassigned_region_bytes =
            if lifetime_hugepage_policy() == LifetimeHugepagePolicy::EpochCohortHugepage {
                self.extents
                    .iter()
                    .filter(|extent| extent.in_use() && extent.cohort_epoch != self.current_epoch)
                    .map(|extent| {
                        extent
                            .regions
                            .iter()
                            .filter(|region| !region.assigned)
                            .count()
                            .saturating_mul(LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES)
                    })
                    .fold(0usize, usize::saturating_add)
            } else {
                0
            };
        let reusable_unassigned_region_bytes =
            unassigned_region_bytes.saturating_sub(cohort_pinned_unassigned_region_bytes);
        let assigned_region_slack_bytes =
            assigned_region_bytes.saturating_sub(self.live_slot_bytes);
        let retained_slack_bytes = retained_bytes.saturating_sub(self.live_slot_bytes);
        let mut adaptive_cold_sites = 0usize;
        let mut adaptive_short_sites = 0usize;
        let mut adaptive_long_sites = 0usize;
        for site in self.adaptive_sites.iter().filter(|site| !site.is_empty()) {
            match site.prediction {
                AdaptivePrediction::Cold => adaptive_cold_sites += 1,
                AdaptivePrediction::Short => adaptive_short_sites += 1,
                AdaptivePrediction::Long => adaptive_long_sites += 1,
            }
        }
        LifetimeHugepageStatsSnapshot {
            policy: lifetime_hugepage_policy(),
            backend: lifetime_hugepage_backend(),
            extent_bytes: LIFETIME_HUGEPAGE_EXTENT_BYTES,
            current_epoch: self.current_epoch,
            phase_advances: self.phase_advances,
            epoch_cohort_extent_mappings: self.epoch_cohort_extent_mappings,
            routed_allocations: self.routed_allocations,
            routed_deallocations: self.routed_deallocations,
            unknown_bypasses: self.unknown_bypasses,
            unknown_bypass_requested_bytes: self.unknown_bypass_requested_bytes,
            unsupported_layout_bypasses: self.unsupported_layout_bypasses,
            allocation_fallbacks: self.allocation_fallbacks,
            slot_reuse_hits: self.slot_reuse_hits,
            slot_bump_allocations: self.slot_bump_allocations,
            identity_region_assignments: self.identity_region_assignments,
            identity_region_releases: self.identity_region_releases,
            ordinary_extent_mappings: self.ordinary_extent_mappings,
            hugetlb_extent_mappings: self.hugetlb_extent_mappings,
            hugetlb_fallback_extent_mappings: self.hugetlb_fallback_extent_mappings,
            thp_extent_mappings: self.thp_extent_mappings,
            thp_candidate_extent_mappings: self.thp_candidate_extent_mappings,
            thp_advice_attempts: self.thp_advice_attempts,
            thp_advice_successes: self.thp_advice_successes,
            thp_advice_errors: self.thp_advice_errors,
            thp_collapse_eligible_extents: self.thp_collapse_eligible_extents,
            thp_collapse_low_occupancy_skips: self.thp_collapse_low_occupancy_skips,
            thp_collapse_attempts: self.thp_collapse_attempts,
            thp_collapse_successes: self.thp_collapse_successes,
            thp_collapse_errors: self.thp_collapse_errors,
            thp_collapse_last_error_code: self.thp_collapse_last_error_code,
            mapping_failures: self.mapping_failures,
            nohugepage_advice_failures: self.nohugepage_advice_failures,
            extent_unmaps: self.extent_unmaps,
            extent_unmap_failures: self.extent_unmap_failures,
            current_extents: self.current_extents,
            peak_extents: self.peak_extents,
            current_ordinary_extents: self.current_ordinary_extents,
            peak_ordinary_extents: self.peak_ordinary_extents,
            current_hugetlb_extents: self.current_hugetlb_extents,
            peak_hugetlb_extents: self.peak_hugetlb_extents,
            current_thp_extents: self.current_thp_extents,
            peak_thp_extents: self.peak_thp_extents,
            current_thp_collapse_confirmed_extents: self.current_thp_collapse_confirmed_extents,
            peak_thp_collapse_confirmed_extents: self.peak_thp_collapse_confirmed_extents,
            live_objects: self.live_objects,
            live_ephemeral_objects: self.live_ephemeral_objects,
            live_long_lived_objects: self.live_long_lived_objects,
            live_slot_bytes: self.live_slot_bytes,
            runtime_validated_objects: self.runtime_validated_objects,
            runtime_validated_bytes: self.runtime_validated_bytes,
            runtime_validation_excluded_objects: self.runtime_validation_excluded_objects,
            runtime_validation_excluded_bytes: self.runtime_validation_excluded_bytes,
            runtime_delayed_free_excluded_objects: self.runtime_delayed_free_excluded_objects,
            runtime_delayed_free_excluded_bytes: self.runtime_delayed_free_excluded_bytes,
            runtime_mixed_epoch_excluded_objects: self.runtime_mixed_epoch_excluded_objects,
            runtime_mixed_epoch_excluded_bytes: self.runtime_mixed_epoch_excluded_bytes,
            predictor_true_positive_objects: self.predictor_confusion.true_positive_objects,
            predictor_true_positive_bytes: self.predictor_confusion.true_positive_bytes,
            predictor_true_negative_objects: self.predictor_confusion.true_negative_objects,
            predictor_true_negative_bytes: self.predictor_confusion.true_negative_bytes,
            predictor_false_positive_objects: self.predictor_confusion.false_positive_objects,
            predictor_false_positive_bytes: self.predictor_confusion.false_positive_bytes,
            predictor_false_negative_objects: self.predictor_confusion.false_negative_objects,
            predictor_false_negative_bytes: self.predictor_confusion.false_negative_bytes,
            placement_true_positive_objects: self.placement_confusion.true_positive_objects,
            placement_true_positive_bytes: self.placement_confusion.true_positive_bytes,
            placement_true_negative_objects: self.placement_confusion.true_negative_objects,
            placement_true_negative_bytes: self.placement_confusion.true_negative_bytes,
            placement_false_positive_objects: self.placement_confusion.false_positive_objects,
            placement_false_positive_bytes: self.placement_confusion.false_positive_bytes,
            placement_false_negative_objects: self.placement_confusion.false_negative_objects,
            placement_false_negative_bytes: self.placement_confusion.false_negative_bytes,
            current_identity_regions: self.current_identity_regions,
            peak_identity_regions: self.peak_identity_regions,
            retained_bytes,
            reusable_unassigned_region_bytes,
            cohort_pinned_unassigned_region_bytes,
            assigned_region_slack_bytes,
            retained_slack_bytes,
            stranded_bytes: retained_slack_bytes,
            all_mappings_released: self.current_extents == 0 && self.live_objects == 0,
            adaptive_pressure_bytes: self.adaptive_pressure_bytes,
            adaptive_pressure_epoch: (self.adaptive_pressure_bytes / ADAPTIVE_EPOCH_BYTES) as usize,
            adaptive_epoch_advances: self.adaptive_epoch_advances,
            adaptive_site_count: self.adaptive_site_count,
            adaptive_cold_sites,
            adaptive_short_sites,
            adaptive_long_sites,
            adaptive_eligible_allocations: self.adaptive_eligible_allocations,
            adaptive_training_allocations: self.adaptive_training_allocations,
            adaptive_cold_bypassed_allocations: self.adaptive_cold_bypassed_allocations,
            adaptive_short_routed_allocations: self.adaptive_short_routed_allocations,
            adaptive_short_bypassed_allocations: self.adaptive_short_bypassed_allocations,
            adaptive_long_routed_allocations: self.adaptive_long_routed_allocations,
            adaptive_missing_identity_bypasses: self.adaptive_missing_identity_bypasses,
            adaptive_site_table_bypasses: self.adaptive_site_table_bypasses,
            adaptive_prior_conflicts: self.adaptive_prior_conflicts,
            adaptive_observation_recording: self.adaptive_observation_recording,
            adaptive_observation_site_count: self.adaptive_observation_site_count,
            adaptive_observation_table_bypasses: self.adaptive_observation_table_bypasses,
            adaptive_force_track_all: self.adaptive_force_track_all.enabled,
            adaptive_force_track_all_maximum_allocations: self
                .adaptive_force_track_all
                .maximum_allocations,
            adaptive_force_track_all_maximum_requested_bytes: self
                .adaptive_force_track_all
                .maximum_requested_bytes,
            adaptive_force_track_all_admitted_allocations: self
                .adaptive_force_track_all
                .admitted_allocations,
            adaptive_force_track_all_admitted_requested_bytes: self
                .adaptive_force_track_all
                .admitted_requested_bytes,
            adaptive_force_track_all_guard_bypasses: self.adaptive_force_track_all.guard_bypasses,
            adaptive_force_track_all_pressure_allocations: self
                .adaptive_force_track_all
                .pressure_allocations,
            adaptive_force_track_all_pressure_requested_bytes: self
                .adaptive_force_track_all
                .pressure_requested_bytes,
            adaptive_force_track_all_raw_pressure_allocations: self
                .adaptive_force_track_all
                .raw_pressure_allocations,
            adaptive_force_track_all_raw_pressure_requested_bytes: self
                .adaptive_force_track_all
                .raw_pressure_requested_bytes,
            adaptive_force_track_all_raw_reallocation_pressure_allocations: self
                .adaptive_force_track_all
                .raw_reallocation_pressure_allocations,
            adaptive_force_track_all_raw_reallocation_pressure_requested_bytes: self
                .adaptive_force_track_all
                .raw_reallocation_pressure_requested_bytes,
            adaptive_live_trailers: self.adaptive_live_trailers,
            adaptive_trailer_corruptions: self.adaptive_trailer_corruptions,
            adaptive_live_survival_registrations: self.adaptive_live_survival_registrations,
            adaptive_live_survival_registration_bypasses: self
                .adaptive_live_survival_registration_bypasses,
            adaptive_live_survival_scans: self.adaptive_live_survival_scans,
            adaptive_live_survival_slots_examined: self.adaptive_live_survival_slots_examined,
            adaptive_live_survival_observations: self.adaptive_live_survival_observations,
            adaptive_live_survival_promotions: self.adaptive_live_survival_promotions,
            adaptive_live_survival_stale_abstentions: self.adaptive_live_survival_stale_abstentions,
            adaptive_short_observations: self.adaptive_short_observations,
            adaptive_long_observations: self.adaptive_long_observations,
            adaptive_censored_observations: self.adaptive_censored_observations,
            adaptive_decisive_observation_bytes: self.adaptive_decisive_observation_bytes,
            adaptive_promotions: self.adaptive_promotions,
            adaptive_demotions: self.adaptive_demotions,
            adaptive_prior_corrections: self.adaptive_prior_corrections,
            adaptive_predictor_true_positive_objects: self
                .adaptive_predictor_confusion
                .true_positive_objects,
            adaptive_predictor_true_positive_bytes: self
                .adaptive_predictor_confusion
                .true_positive_bytes,
            adaptive_predictor_true_negative_objects: self
                .adaptive_predictor_confusion
                .true_negative_objects,
            adaptive_predictor_true_negative_bytes: self
                .adaptive_predictor_confusion
                .true_negative_bytes,
            adaptive_predictor_false_positive_objects: self
                .adaptive_predictor_confusion
                .false_positive_objects,
            adaptive_predictor_false_positive_bytes: self
                .adaptive_predictor_confusion
                .false_positive_bytes,
            adaptive_predictor_false_negative_objects: self
                .adaptive_predictor_confusion
                .false_negative_objects,
            adaptive_predictor_false_negative_bytes: self
                .adaptive_predictor_confusion
                .false_negative_bytes,
            adaptive_static_hint_true_positive_objects: self
                .adaptive_static_hint_confusion
                .true_positive_objects,
            adaptive_static_hint_true_positive_bytes: self
                .adaptive_static_hint_confusion
                .true_positive_bytes,
            adaptive_static_hint_true_negative_objects: self
                .adaptive_static_hint_confusion
                .true_negative_objects,
            adaptive_static_hint_true_negative_bytes: self
                .adaptive_static_hint_confusion
                .true_negative_bytes,
            adaptive_static_hint_false_positive_objects: self
                .adaptive_static_hint_confusion
                .false_positive_objects,
            adaptive_static_hint_false_positive_bytes: self
                .adaptive_static_hint_confusion
                .false_positive_bytes,
            adaptive_static_hint_false_negative_objects: self
                .adaptive_static_hint_confusion
                .false_negative_objects,
            adaptive_static_hint_false_negative_bytes: self
                .adaptive_static_hint_confusion
                .false_negative_bytes,
            adaptive_static_hint_abstained_objects: self.adaptive_static_hint_abstained_objects,
            adaptive_static_hint_abstained_bytes: self.adaptive_static_hint_abstained_bytes,
            compiler_inferred_unknown_or_unproven_bypasses:
                COMPILER_INFERRED_UNKNOWN_OR_UNPROVEN_BYPASSES.load(Ordering::Acquire),
            compiler_inferred_proven_ephemeral_bypasses:
                COMPILER_INFERRED_PROVEN_EPHEMERAL_BYPASSES.load(Ordering::Acquire),
            compiler_inferred_bypass_requested_bytes: COMPILER_INFERRED_BYPASS_REQUESTED_BYTES
                .load(Ordering::Acquire),
            compiler_inferred_arena_lock_acquisitions: COMPILER_INFERRED_ARENA_LOCK_ACQUISITIONS
                .load(Ordering::Acquire),
            compiler_inferred_direct_long_routes: COMPILER_INFERRED_DIRECT_LONG_ROUTES
                .load(Ordering::Acquire),
            compiler_inferred_direct_long_requested_bytes:
                COMPILER_INFERRED_DIRECT_LONG_REQUESTED_BYTES.load(Ordering::Acquire),
            compiler_inferred_direct_long_slot_bytes: COMPILER_INFERRED_DIRECT_LONG_SLOT_BYTES
                .load(Ordering::Acquire),
            compiler_inferred_direct_long_deallocations:
                COMPILER_INFERRED_DIRECT_LONG_DEALLOCATIONS.load(Ordering::Acquire),
            compiler_inferred_direct_long_routes_without_trailer:
                COMPILER_INFERRED_DIRECT_LONG_ROUTES_WITHOUT_TRAILER.load(Ordering::Acquire),
            compiler_inferred_density_promotion_attempts:
                COMPILER_INFERRED_DENSITY_PROMOTION_ATTEMPTS.load(Ordering::Acquire),
            compiler_inferred_density_promotion_successes:
                COMPILER_INFERRED_DENSITY_PROMOTION_SUCCESSES.load(Ordering::Acquire),
            compiler_inferred_density_promotion_errors: COMPILER_INFERRED_DENSITY_PROMOTION_ERRORS
                .load(Ordering::Acquire),
            compiler_directed_telemetry: lifetime_hugepage_compiler_directed_telemetry_enabled(),
            compiler_directed_unknown_bypasses: COMPILER_DIRECTED_UNKNOWN_BYPASSES
                .load(Ordering::Acquire),
            compiler_directed_bypass_requested_bytes: COMPILER_DIRECTED_BYPASS_REQUESTED_BYTES
                .load(Ordering::Acquire),
            compiler_directed_arena_lock_acquisitions: self
                .compiler_directed_arena_lock_acquisitions,
            compiler_directed_short_routes: self.compiler_directed_short_routes,
            compiler_directed_short_requested_bytes: self.compiler_directed_short_requested_bytes,
            compiler_directed_short_slot_bytes: self.compiler_directed_short_slot_bytes,
            compiler_directed_long_routes: self.compiler_directed_long_routes,
            compiler_directed_long_requested_bytes: self.compiler_directed_long_requested_bytes,
            compiler_directed_long_slot_bytes: self.compiler_directed_long_slot_bytes,
            compiler_directed_deallocations: self.compiler_directed_deallocations,
            compiler_directed_routes_without_trailer: self.compiler_directed_routes_without_trailer,
            compiler_directed_density_promotion_attempts: self
                .compiler_directed_density_promotion_attempts,
            compiler_directed_density_promotion_successes: self
                .compiler_directed_density_promotion_successes,
            compiler_directed_density_promotion_errors: self
                .compiler_directed_density_promotion_errors,
            empty_extent_retention_capacity: LIFETIME_EMPTY_EXTENT_RETENTION_CAPACITY,
            current_retained_empty_extents: self.current_retained_empty_extents,
            peak_retained_empty_extents: self.peak_retained_empty_extents,
            current_retained_empty_bytes: self
                .current_retained_empty_extents
                .saturating_mul(LIFETIME_HUGEPAGE_EXTENT_BYTES),
            peak_retained_empty_bytes: self
                .peak_retained_empty_extents
                .saturating_mul(LIFETIME_HUGEPAGE_EXTENT_BYTES),
            retained_empty_extent_insertions: self.retained_empty_extent_insertions,
            retained_empty_extent_reuse_hits: self.retained_empty_extent_reuse_hits,
            retained_empty_extent_evictions: self.retained_empty_extent_evictions,
            retained_empty_extent_trims: self.retained_empty_extent_trims,
        }
    }

    fn adaptive_site_slot(
        &mut self,
        key: AdaptiveSiteKey,
        static_prior: LifetimePlacementClass,
    ) -> Option<usize> {
        let fingerprint = key.fingerprint();
        let start = fingerprint as usize & (ADAPTIVE_SITE_SLOTS - 1);
        for offset in 0..ADAPTIVE_SITE_PROBE_LIMIT {
            let idx = (start + offset) & (ADAPTIVE_SITE_SLOTS - 1);
            if self.adaptive_sites[idx].is_empty() {
                self.adaptive_sites[idx] = AdaptiveSiteState::new(key, static_prior);
                self.adaptive_site_count = self.adaptive_site_count.saturating_add(1);
                return Some(idx);
            }
            if self.adaptive_sites[idx].fingerprint == fingerprint
                && self.adaptive_sites[idx].key == key
            {
                if self.adaptive_sites[idx].note_prior(static_prior) {
                    self.adaptive_prior_conflicts = self.adaptive_prior_conflicts.saturating_add(1);
                }
                return Some(idx);
            }
        }
        self.adaptive_site_table_bypasses = self.adaptive_site_table_bypasses.saturating_add(1);
        None
    }

    fn adaptive_observation_slot(&mut self, key: AdaptiveObservationKey) -> Option<usize> {
        let fingerprint = key.fingerprint();
        let start = fingerprint as usize & (ADAPTIVE_OBSERVATION_SLOTS - 1);
        for offset in 0..ADAPTIVE_OBSERVATION_PROBE_LIMIT {
            let idx = (start + offset) & (ADAPTIVE_OBSERVATION_SLOTS - 1);
            if self.adaptive_observations[idx].is_empty() {
                self.adaptive_observations[idx] = AdaptiveObservationState::new(key);
                self.adaptive_observation_site_count =
                    self.adaptive_observation_site_count.saturating_add(1);
                return Some(idx);
            }
            if self.adaptive_observations[idx].fingerprint == fingerprint
                && self.adaptive_observations[idx].key == key
            {
                return Some(idx);
            }
        }
        self.adaptive_observation_table_bypasses =
            self.adaptive_observation_table_bypasses.saturating_add(1);
        None
    }

    fn adaptive_observation_find(&self, key: AdaptiveObservationKey) -> Option<usize> {
        let fingerprint = key.fingerprint();
        let start = fingerprint as usize & (ADAPTIVE_OBSERVATION_SLOTS - 1);
        for offset in 0..ADAPTIVE_OBSERVATION_PROBE_LIMIT {
            let idx = (start + offset) & (ADAPTIVE_OBSERVATION_SLOTS - 1);
            let observation = self.adaptive_observations[idx];
            if observation.is_empty() {
                return None;
            }
            if observation.fingerprint == fingerprint && observation.key == key {
                return Some(idx);
            }
        }
        None
    }

    #[inline]
    fn adaptive_observation_note_allocation(
        &mut self,
        site: AdaptiveSiteKey,
        requested_size: usize,
        pressure: u64,
        tracked: bool,
        learned_prediction: AdaptivePrediction,
        static_prior: LifetimePlacementClass,
    ) {
        if !self.adaptive_observation_recording {
            return;
        }
        let key = AdaptiveObservationKey::from_site(site, requested_size);
        let Some(idx) = self.adaptive_observation_slot(key) else {
            return;
        };
        self.adaptive_observations[idx].note_allocation(
            site,
            pressure,
            tracked,
            learned_prediction,
            static_prior,
        );
    }

    #[inline]
    fn adaptive_observation_note_deallocation(
        &mut self,
        site: AdaptiveSiteKey,
        trailer: AdaptiveSlotTrailer,
        age_bytes: u64,
        outcome: Option<bool>,
        payload_capacity: usize,
    ) {
        if !self.adaptive_observation_recording {
            return;
        }
        let key = AdaptiveObservationKey::from_site(site, trailer.requested_size as usize);
        let Some(idx) = self.adaptive_observation_find(key) else {
            return;
        };
        self.adaptive_observations[idx].note_deallocation(
            trailer.birth_pressure,
            age_bytes,
            outcome,
            trailer.survival_recorded(),
            payload_capacity,
        );
    }

    #[inline]
    fn adaptive_observation_note_live_survival(
        &mut self,
        site: AdaptiveSiteKey,
        requested_size: usize,
        age_bytes: u64,
        payload_capacity: usize,
    ) {
        if !self.adaptive_observation_recording {
            return;
        }
        let key = AdaptiveObservationKey::from_site(site, requested_size);
        let Some(idx) = self.adaptive_observation_find(key) else {
            return;
        };
        self.adaptive_observations[idx].note_live_survival(age_bytes, payload_capacity);
    }

    #[inline]
    fn adaptive_observation_note_latest_prediction(
        &mut self,
        site: AdaptiveSiteKey,
        requested_size: usize,
        prediction: AdaptivePrediction,
    ) {
        if !self.adaptive_observation_recording {
            return;
        }
        let key = AdaptiveObservationKey::from_site(site, requested_size);
        let Some(idx) = self.adaptive_observation_find(key) else {
            return;
        };
        self.adaptive_observations[idx].latest_prediction = prediction;
    }

    fn adaptive_observation_snapshot(&self, out: &mut [LifetimeAdaptiveSiteSnapshot]) -> usize {
        let mut written = 0;
        for observation in self
            .adaptive_observations
            .iter()
            .filter(|observation| !observation.is_empty())
        {
            if written < out.len() {
                out[written] = observation.snapshot(self.adaptive_pressure_bytes);
            }
            written += 1;
        }
        written
    }

    #[inline]
    fn adaptive_live_survivor_register(
        &mut self,
        site_idx: usize,
        ptr: *mut u8,
        birth_pressure: u64,
        learned_prediction: AdaptivePrediction,
    ) {
        // Learned-Long sites already route future objects as Long. Continuing
        // to sample every Long allocation would exceed the bounded Cold-site
        // purpose of this table and adds no early-learning value.
        if learned_prediction == AdaptivePrediction::Long {
            return;
        }
        if !self.adaptive_live_survivors[site_idx].insert(ptr, birth_pressure) {
            self.adaptive_live_survival_registration_bypasses = self
                .adaptive_live_survival_registration_bypasses
                .saturating_add(1);
            return;
        }
        let word = site_idx / u64::BITS as usize;
        let bit = site_idx % u64::BITS as usize;
        self.adaptive_live_survivor_active_sites[word] |= 1u64 << bit;
        self.adaptive_live_survivor_next_due_pressure = self
            .adaptive_live_survivor_next_due_pressure
            .min(birth_pressure.saturating_add(ADAPTIVE_LONG_AGE_BYTES));
        self.adaptive_live_survival_registrations =
            self.adaptive_live_survival_registrations.saturating_add(1);
    }

    #[inline]
    fn adaptive_live_survivor_unregister(
        &mut self,
        site_idx: usize,
        ptr: *mut u8,
        birth_pressure: u64,
    ) -> bool {
        if !self.adaptive_live_survivors[site_idx].remove(ptr, birth_pressure) {
            return false;
        }
        if self.adaptive_live_survivors[site_idx].is_empty() {
            let word = site_idx / u64::BITS as usize;
            let bit = site_idx % u64::BITS as usize;
            self.adaptive_live_survivor_active_sites[word] &= !(1u64 << bit);
        }
        true
    }

    /// Corrupt trailers cannot be trusted for their site index or birth
    /// pressure. Deallocation uses this bounded slow path before releasing the
    /// slot so no stale sample can later reinterpret reused storage as its old
    /// allocation generation.
    fn adaptive_live_survivor_unregister_ptr(&mut self, ptr: *mut u8) -> bool {
        for word_idx in 0..ADAPTIVE_LIVE_SURVIVOR_ACTIVE_WORDS {
            let mut active_sites = self.adaptive_live_survivor_active_sites[word_idx];
            while active_sites != 0 {
                let bit = active_sites.trailing_zeros() as usize;
                active_sites &= active_sites - 1;
                let site_idx = word_idx * u64::BITS as usize + bit;
                for slot in 0..ADAPTIVE_LIVE_SURVIVOR_SLOTS_PER_SITE {
                    let mask = 1u8 << slot;
                    if self.adaptive_live_survivors[site_idx].active_slots & mask != 0
                        && self.adaptive_live_survivors[site_idx].samples[slot].ptr == ptr as usize
                    {
                        self.adaptive_live_survivors[site_idx].clear(slot);
                        if self.adaptive_live_survivors[site_idx].is_empty() {
                            self.adaptive_live_survivor_active_sites[word_idx] &= !(1u64 << bit);
                        }
                        return true;
                    }
                }
            }
        }
        false
    }

    unsafe fn adaptive_live_survivor_trailer(
        &self,
        site_idx: usize,
        sample: AdaptiveLiveSurvivorSample,
    ) -> Option<(usize, usize, ActualBacking, AdaptiveSlotTrailer)> {
        let ptr = sample.ptr as *mut u8;
        let base = sample.ptr & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
        let extent_idx = self.lookup_extent(base)?;
        let extent = &self.extents[extent_idx];
        if !extent.in_use() || !extent.adaptive_trailer || extent.live == 0 {
            return None;
        }
        let offset = sample.ptr.checked_sub(extent.base)?;
        if offset >= LIFETIME_HUGEPAGE_EXTENT_BYTES || extent.slot_size == 0 {
            return None;
        }
        let region_idx = offset / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
        let region_offset = offset % LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
        let region = extent.regions.get(region_idx)?;
        if !region.assigned
            || region.live == 0
            || region_offset % extent.slot_size != 0
            || region_offset / extent.slot_size >= region.next_unused
        {
            return None;
        }
        let trailer = ptr
            .add(extent.payload_capacity)
            .cast::<AdaptiveSlotTrailer>()
            .read();
        if !trailer.is_valid(ptr, extent.payload_capacity)
            || trailer.site_index as usize != site_idx
            || trailer.site_fingerprint != self.adaptive_sites[site_idx].fingerprint
            || trailer.birth_pressure != sample.birth_pressure
        {
            return None;
        }
        Some((
            extent.payload_capacity,
            extent.slot_size,
            extent.backing,
            trailer,
        ))
    }

    fn adaptive_record_decisive_truth(
        &mut self,
        payload_capacity: usize,
        slot_size: usize,
        backing: ActualBacking,
        trailer: AdaptiveSlotTrailer,
        actual_long: bool,
    ) {
        self.adaptive_decisive_observation_bytes = self
            .adaptive_decisive_observation_bytes
            .saturating_add(payload_capacity);
        self.runtime_validated_objects = self.runtime_validated_objects.saturating_add(1);
        self.runtime_validated_bytes = self.runtime_validated_bytes.saturating_add(slot_size);
        let prediction = AdaptivePrediction::from_u8(trailer.prediction)
            .expect("validated adaptive trailer has an invalid prediction");
        let predicted_long = prediction == AdaptivePrediction::Long;
        // The generic matrix treats learned Cold as a negative abstention. The
        // adaptive matrix excludes Cold, while static-prior and physical-
        // placement effects retain their separate matrices.
        self.predictor_confusion
            .record(predicted_long, actual_long, slot_size);
        if prediction != AdaptivePrediction::Cold {
            self.adaptive_predictor_confusion
                .record(predicted_long, actual_long, payload_capacity);
        }
        match lifetime_class_from_u8(trailer.static_hint)
            .expect("validated adaptive trailer has an invalid static hint")
        {
            LifetimePlacementClass::Unknown => {
                self.adaptive_static_hint_abstained_objects = self
                    .adaptive_static_hint_abstained_objects
                    .saturating_add(1);
                self.adaptive_static_hint_abstained_bytes = self
                    .adaptive_static_hint_abstained_bytes
                    .saturating_add(payload_capacity);
            }
            LifetimePlacementClass::Ephemeral => {
                self.adaptive_static_hint_confusion
                    .record(false, actual_long, payload_capacity)
            }
            LifetimePlacementClass::LongLived => {
                self.adaptive_static_hint_confusion
                    .record(true, actual_long, payload_capacity)
            }
        }
        self.placement_confusion
            .record(backing.is_large_page_confirmed(), actual_long, slot_size);
    }

    fn adaptive_observe_site(
        &mut self,
        site_idx: usize,
        requested_size: usize,
        actual_long: Option<bool>,
        live_survival: bool,
    ) {
        let prior = self.adaptive_sites[site_idx].static_prior;
        let transition = self.adaptive_sites[site_idx].observe(actual_long);
        match transition {
            AdaptiveTransition::None => {}
            AdaptiveTransition::PromotedShort => {
                self.adaptive_promotions = self.adaptive_promotions.saturating_add(1);
                if prior == LifetimePlacementClass::LongLived {
                    self.adaptive_prior_corrections =
                        self.adaptive_prior_corrections.saturating_add(1);
                }
            }
            AdaptiveTransition::PromotedLong => {
                self.adaptive_promotions = self.adaptive_promotions.saturating_add(1);
                if live_survival {
                    self.adaptive_live_survival_promotions =
                        self.adaptive_live_survival_promotions.saturating_add(1);
                }
                if prior == LifetimePlacementClass::Ephemeral {
                    self.adaptive_prior_corrections =
                        self.adaptive_prior_corrections.saturating_add(1);
                }
            }
            AdaptiveTransition::Demoted => {
                self.adaptive_demotions = self.adaptive_demotions.saturating_add(1)
            }
        }
        let site = self.adaptive_sites[site_idx].key;
        let prediction = self.adaptive_sites[site_idx].prediction;
        self.adaptive_observation_note_latest_prediction(site, requested_size, prediction);
    }

    unsafe fn adaptive_scan_due_live_survivors(&mut self) {
        if self.adaptive_pressure_bytes < self.adaptive_live_survivor_next_due_pressure {
            return;
        }
        self.adaptive_live_survival_scans = self.adaptive_live_survival_scans.saturating_add(1);
        let mut next_due = u64::MAX;
        for word_idx in 0..ADAPTIVE_LIVE_SURVIVOR_ACTIVE_WORDS {
            let mut active_sites = self.adaptive_live_survivor_active_sites[word_idx];
            while active_sites != 0 {
                let bit = active_sites.trailing_zeros() as usize;
                active_sites &= active_sites - 1;
                let site_idx = word_idx * u64::BITS as usize + bit;
                for slot in 0..ADAPTIVE_LIVE_SURVIVOR_SLOTS_PER_SITE {
                    let mask = 1u8 << slot;
                    if self.adaptive_live_survivors[site_idx].active_slots & mask == 0 {
                        continue;
                    }
                    self.adaptive_live_survival_slots_examined =
                        self.adaptive_live_survival_slots_examined.saturating_add(1);
                    let sample = self.adaptive_live_survivors[site_idx].samples[slot];
                    let due = sample
                        .birth_pressure
                        .saturating_add(ADAPTIVE_LONG_AGE_BYTES);
                    if self.adaptive_pressure_bytes < due {
                        next_due = next_due.min(due);
                        continue;
                    }
                    let Some((payload_capacity, slot_size, backing, mut trailer)) =
                        self.adaptive_live_survivor_trailer(site_idx, sample)
                    else {
                        self.adaptive_live_survivors[site_idx].clear(slot);
                        self.adaptive_live_survival_stale_abstentions = self
                            .adaptive_live_survival_stale_abstentions
                            .saturating_add(1);
                        continue;
                    };
                    if trailer.survival_recorded() {
                        self.adaptive_live_survivors[site_idx].clear(slot);
                        self.adaptive_live_survival_stale_abstentions = self
                            .adaptive_live_survival_stale_abstentions
                            .saturating_add(1);
                        continue;
                    }
                    let ptr = sample.ptr as *mut u8;
                    let age_bytes = self
                        .adaptive_pressure_bytes
                        .saturating_sub(sample.birth_pressure);
                    debug_assert!(age_bytes >= ADAPTIVE_LONG_AGE_BYTES);
                    trailer.record_survival(ptr);
                    ptr.add(payload_capacity)
                        .cast::<AdaptiveSlotTrailer>()
                        .write(trailer);
                    self.adaptive_live_survivors[site_idx].clear(slot);

                    let site = self.adaptive_sites[site_idx].key;
                    self.adaptive_observation_note_live_survival(
                        site,
                        trailer.requested_size as usize,
                        age_bytes,
                        payload_capacity,
                    );
                    self.adaptive_live_survival_observations =
                        self.adaptive_live_survival_observations.saturating_add(1);
                    self.adaptive_long_observations =
                        self.adaptive_long_observations.saturating_add(1);
                    self.adaptive_record_decisive_truth(
                        payload_capacity,
                        slot_size,
                        backing,
                        trailer,
                        true,
                    );
                    self.adaptive_observe_site(
                        site_idx,
                        trailer.requested_size as usize,
                        Some(true),
                        true,
                    );
                }
                if self.adaptive_live_survivors[site_idx].is_empty() {
                    self.adaptive_live_survivor_active_sites[word_idx] &= !(1u64 << bit);
                }
            }
        }
        self.adaptive_live_survivor_next_due_pressure = next_due;
    }

    #[inline]
    fn adaptive_note_pressure(&mut self, payload_capacity: usize) -> u64 {
        let old_epoch = self.adaptive_pressure_bytes / ADAPTIVE_EPOCH_BYTES;
        self.adaptive_pressure_bytes = self
            .adaptive_pressure_bytes
            .saturating_add(payload_capacity as u64);
        let new_epoch = self.adaptive_pressure_bytes / ADAPTIVE_EPOCH_BYTES;
        self.adaptive_epoch_advances = self
            .adaptive_epoch_advances
            .saturating_add(new_epoch.saturating_sub(old_epoch) as usize);
        unsafe { self.adaptive_scan_due_live_survivors() };
        self.adaptive_pressure_bytes
    }

    #[inline]
    fn adaptive_note_force_track_pressure(
        &mut self,
        requested_bytes: usize,
        source: ForcePressureSource,
    ) -> u64 {
        debug_assert!(self.adaptive_force_track_all.enabled);
        self.adaptive_force_track_all
            .note_pressure(requested_bytes, source);
        self.adaptive_note_pressure(requested_bytes)
    }

    #[inline]
    fn adaptive_note_bypass(
        &mut self,
        payload_capacity: usize,
        learned_prediction: AdaptivePrediction,
    ) -> u64 {
        let pressure = self.adaptive_note_pressure(payload_capacity);
        self.adaptive_eligible_allocations = self.adaptive_eligible_allocations.saturating_add(1);
        match learned_prediction {
            AdaptivePrediction::Cold => {
                self.adaptive_cold_bypassed_allocations =
                    self.adaptive_cold_bypassed_allocations.saturating_add(1)
            }
            AdaptivePrediction::Short => {
                self.adaptive_short_bypassed_allocations =
                    self.adaptive_short_bypassed_allocations.saturating_add(1)
            }
            AdaptivePrediction::Long => unreachable!("learned-long allocation bypassed tracking"),
        }
        pressure
    }

    #[inline]
    fn adaptive_note_force_track_guard_bypass(&mut self) {
        self.adaptive_eligible_allocations = self.adaptive_eligible_allocations.saturating_add(1);
        self.adaptive_force_track_all.note_guard_bypass();
    }

    #[inline]
    fn adaptive_note_allocation(
        &mut self,
        site_idx: usize,
        requested_size: usize,
        payload_capacity: usize,
        learned_prediction: AdaptivePrediction,
        precounted_pressure: Option<u64>,
    ) -> u64 {
        if self.adaptive_force_track_all.enabled {
            self.adaptive_force_track_all.note_admitted(requested_size);
        }
        let birth_pressure =
            precounted_pressure.unwrap_or_else(|| self.adaptive_note_pressure(payload_capacity));
        self.adaptive_sites[site_idx].note_tracked_allocation();
        self.adaptive_eligible_allocations = self.adaptive_eligible_allocations.saturating_add(1);
        self.adaptive_live_trailers = self.adaptive_live_trailers.saturating_add(1);
        match learned_prediction {
            AdaptivePrediction::Cold => {
                self.adaptive_training_allocations =
                    self.adaptive_training_allocations.saturating_add(1)
            }
            AdaptivePrediction::Short => {
                self.adaptive_short_routed_allocations =
                    self.adaptive_short_routed_allocations.saturating_add(1)
            }
            AdaptivePrediction::Long => {
                self.adaptive_long_routed_allocations =
                    self.adaptive_long_routed_allocations.saturating_add(1)
            }
        }
        birth_pressure
    }

    fn adaptive_note_deallocation(
        &mut self,
        ptr: *mut u8,
        payload_capacity: usize,
        slot_size: usize,
        backing: ActualBacking,
        trailer: AdaptiveSlotTrailer,
    ) -> Option<bool> {
        self.adaptive_live_trailers = self.adaptive_live_trailers.saturating_sub(1);
        let candidate_site_idx = trailer.site_index as usize;
        let live_sample_removed = candidate_site_idx < ADAPTIVE_SITE_SLOTS
            && self.adaptive_live_survivor_unregister(
                candidate_site_idx,
                ptr,
                trailer.birth_pressure,
            );
        if !trailer.is_valid(ptr, payload_capacity) {
            if !live_sample_removed {
                let _ = self.adaptive_live_survivor_unregister_ptr(ptr);
            }
            self.adaptive_trailer_corruptions = self.adaptive_trailer_corruptions.saturating_add(1);
            return None;
        }
        let site_idx = trailer.site_index as usize;
        if self.adaptive_sites[site_idx].is_empty()
            || self.adaptive_sites[site_idx].fingerprint != trailer.site_fingerprint
        {
            if !live_sample_removed {
                let _ = self.adaptive_live_survivor_unregister_ptr(ptr);
            }
            self.adaptive_trailer_corruptions = self.adaptive_trailer_corruptions.saturating_add(1);
            return None;
        }
        let site = self.adaptive_sites[site_idx].key;
        self.adaptive_sites[site_idx].note_tracked_deallocation();

        let age_bytes = self
            .adaptive_pressure_bytes
            .saturating_sub(trailer.birth_pressure);
        let actual_long = adaptive_lifetime_outcome(age_bytes);
        let survival_recorded = trailer.survival_recorded();
        debug_assert!(!survival_recorded || actual_long == Some(true));
        self.adaptive_observation_note_deallocation(
            site,
            trailer,
            age_bytes,
            actual_long,
            payload_capacity,
        );
        if !survival_recorded {
            match actual_long {
                Some(false) => {
                    self.adaptive_short_observations =
                        self.adaptive_short_observations.saturating_add(1)
                }
                Some(true) => {
                    self.adaptive_long_observations =
                        self.adaptive_long_observations.saturating_add(1)
                }
                None => {
                    self.adaptive_censored_observations =
                        self.adaptive_censored_observations.saturating_add(1)
                }
            }
            if let Some(actual_long) = actual_long {
                self.adaptive_record_decisive_truth(
                    payload_capacity,
                    slot_size,
                    backing,
                    trailer,
                    actual_long,
                );
            }
            self.adaptive_observe_site(
                site_idx,
                trailer.requested_size as usize,
                actual_long,
                false,
            );
        }
        Some(trailer.confirmed_long_placement())
    }

    #[inline]
    fn allocate_descriptor(&mut self) -> Option<usize> {
        if self.free_descriptor_head != NONE {
            let idx = self.free_descriptor_head;
            self.free_descriptor_head = self.extents[idx].next_free_descriptor;
            self.extents[idx] = Extent::empty();
            return Some(idx);
        }
        if self.next_unused_descriptor == MAX_EXTENTS {
            return None;
        }
        let idx = self.next_unused_descriptor;
        self.next_unused_descriptor += 1;
        Some(idx)
    }

    #[inline]
    fn release_descriptor(&mut self, idx: usize) {
        let mut empty = Extent::empty();
        empty.next_free_descriptor = self.free_descriptor_head;
        self.extents[idx] = empty;
        self.free_descriptor_head = idx;
    }

    #[inline]
    fn add_available(&mut self, idx: usize) {
        if self.extents[idx].on_available_list {
            return;
        }
        let bucket = self.extents[idx].bucket;
        let old_head = self.available_heads[bucket];
        self.extents[idx].available_prev = NONE;
        self.extents[idx].available_next = old_head;
        self.extents[idx].on_available_list = true;
        if old_head != NONE {
            self.extents[old_head].available_prev = idx;
        }
        self.available_heads[bucket] = idx;
    }

    #[inline]
    fn remove_available(&mut self, idx: usize) {
        if !self.extents[idx].on_available_list {
            return;
        }
        let bucket = self.extents[idx].bucket;
        let prev = self.extents[idx].available_prev;
        let next = self.extents[idx].available_next;
        if prev == NONE {
            self.available_heads[bucket] = next;
        } else {
            self.extents[prev].available_next = next;
        }
        if next != NONE {
            self.extents[next].available_prev = prev;
        }
        self.extents[idx].available_prev = NONE;
        self.extents[idx].available_next = NONE;
        self.extents[idx].on_available_list = false;
    }

    #[inline]
    fn add_retained_empty(&mut self, idx: usize) {
        if self.extents[idx].on_retained_empty_list {
            return;
        }
        debug_assert_eq!(self.extents[idx].live, 0);
        let old_head = self.retained_empty_head;
        self.extents[idx].retained_empty_prev = NONE;
        self.extents[idx].retained_empty_next = old_head;
        self.extents[idx].on_retained_empty_list = true;
        if old_head != NONE {
            self.extents[old_head].retained_empty_prev = idx;
        } else {
            self.retained_empty_tail = idx;
        }
        self.retained_empty_head = idx;
        self.current_retained_empty_extents = self.current_retained_empty_extents.saturating_add(1);
    }

    #[inline]
    fn remove_retained_empty(&mut self, idx: usize) -> bool {
        if !self.extents[idx].on_retained_empty_list {
            return false;
        }
        let prev = self.extents[idx].retained_empty_prev;
        let next = self.extents[idx].retained_empty_next;
        if prev == NONE {
            self.retained_empty_head = next;
        } else {
            self.extents[prev].retained_empty_next = next;
        }
        if next == NONE {
            self.retained_empty_tail = prev;
        } else {
            self.extents[next].retained_empty_prev = prev;
        }
        self.extents[idx].retained_empty_prev = NONE;
        self.extents[idx].retained_empty_next = NONE;
        self.extents[idx].on_retained_empty_list = false;
        self.current_retained_empty_extents = self.current_retained_empty_extents.saturating_sub(1);
        true
    }

    /// Epoch-cohort extents from older phases remain valid for their live
    /// objects, while new allocations must start from a current-epoch list.
    /// Detaching once per explicit phase keeps allocation search independent
    /// of the number of surviving historical cohorts.
    fn detach_available_epoch_cohorts(&mut self) {
        self.available_heads = [NONE; BUCKET_COUNT];
        for extent in self.extents[..self.next_unused_descriptor].iter_mut() {
            if extent.in_use() {
                extent.available_prev = NONE;
                extent.available_next = NONE;
                extent.on_available_list = false;
            }
        }
    }

    fn find_available(
        &mut self,
        geometry: SlotGeometry,
        requested_backing: RequestedBacking,
        identity: ArenaIdentity,
        callsite: u64,
        birth_epoch: usize,
        require_extent_cohort: bool,
        prefer_fullest: bool,
        restrict_confirmed_compiler_reuse: bool,
    ) -> Option<AvailableRegion> {
        let bucket = geometry.bucket;
        let requested_reuse_identity = PromotedReuseIdentity {
            arena: identity,
            callsite,
        };
        let mut idx = self.available_heads[bucket];
        let mut matching_region = None;
        let mut unassigned_fallback = None;
        let mut fullest_compatible = None;
        let mut fullest_live = 0usize;
        let mut fullest_reuses_identity = false;
        let mut visited = 0usize;
        let mut fullness_candidates = 0usize;
        while idx != NONE {
            let extent = &self.extents[idx];
            if extent.bucket != bucket || !extent.on_available_list {
                panic!("corrupt lifetime-arena available list");
            }
            let cohort_matches = !require_extent_cohort || extent.cohort_epoch == birth_epoch;
            let backing_matches = extent.requested_backing == Some(requested_backing);
            let geometry_matches = extent.exact_geometry_matches(geometry);
            if cohort_matches && backing_matches && geometry_matches {
                let matching_region_idx = extent.matching_region_with_space(
                    identity,
                    callsite,
                    birth_epoch,
                    require_extent_cohort,
                    restrict_confirmed_compiler_reuse,
                );
                let exact_promoted_reuse =
                    extent.promoted_reuse_identity == Some(requested_reuse_identity);
                let sealed_for_another_identity =
                    extent.promoted_reuse_identity.is_some() && !exact_promoted_reuse;
                let confirmed_compiler_drain_only = restrict_confirmed_compiler_reuse
                    && extent.backing.is_thp_confirmed_at_collapse()
                    && extent.promoted_reuse_identity.is_none();
                let allow_unassigned =
                    !sealed_for_another_identity && !confirmed_compiler_drain_only;
                if sealed_for_another_identity {
                    idx = extent.available_next;
                    visited += 1;
                    if visited > MAX_EXTENTS {
                        panic!("cyclic lifetime-arena available list");
                    }
                    continue;
                }
                if prefer_fullest {
                    let candidate = matching_region_idx
                        .map(|region_idx| (region_idx, true))
                        .or_else(|| {
                            allow_unassigned
                                .then(|| extent.unassigned_region())
                                .flatten()
                                .map(|region_idx| (region_idx, false))
                        });
                    if let Some((region_idx, reuses_identity)) = candidate {
                        fullness_candidates += 1;
                        let replace = fullness_candidate_is_better(
                            extent.live,
                            reuses_identity,
                            fullest_compatible.map(|_| (fullest_live, fullest_reuses_identity)),
                        );
                        if replace {
                            fullest_compatible = Some(AvailableRegion {
                                extent_idx: idx,
                                region_idx,
                            });
                            fullest_live = extent.live;
                            fullest_reuses_identity = reuses_identity;
                        }
                    }
                } else if exact_promoted_reuse {
                    let region_idx = matching_region_idx.or_else(|| {
                        allow_unassigned
                            .then(|| extent.unassigned_region())
                            .flatten()
                    });
                    if let Some(region_idx) = region_idx {
                        matching_region = Some(AvailableRegion {
                            extent_idx: idx,
                            region_idx,
                        });
                        break;
                    }
                } else if let Some(region_idx) = matching_region_idx {
                    matching_region = Some(AvailableRegion {
                        extent_idx: idx,
                        region_idx,
                    });
                    break;
                } else if allow_unassigned && unassigned_fallback.is_none() {
                    if let Some(region_idx) = extent.unassigned_region() {
                        unassigned_fallback = Some(AvailableRegion {
                            extent_idx: idx,
                            region_idx,
                        });
                    }
                }
            }
            idx = extent.available_next;
            visited += 1;
            if visited > MAX_EXTENTS {
                panic!("cyclic lifetime-arena available list");
            }
            if prefer_fullest && fullness_candidates >= ADAPTIVE_FULLNESS_CANDIDATE_LIMIT {
                break;
            }
        }
        let chosen = if prefer_fullest {
            fullest_compatible
        } else {
            matching_region.or(unassigned_fallback)
        }?;
        if self.available_heads[bucket] != chosen.extent_idx {
            self.remove_available(chosen.extent_idx);
            self.add_available(chosen.extent_idx);
        }
        Some(chosen)
    }

    fn lookup_slot(base: usize) -> usize {
        let mut value = base >> 21;
        value ^= value >> 17;
        value = value.wrapping_mul(0x9e37_79b9_7f4a_7c15usize);
        (value ^ (value >> 29)) & EXTENT_LOOKUP_MASK
    }

    fn lookup_extent(&self, base: usize) -> Option<usize> {
        let start = Self::lookup_slot(base);
        let mut offset = 0usize;
        while offset < EXTENT_LOOKUP_SLOTS {
            let entry = self.lookup[(start + offset) & EXTENT_LOOKUP_MASK];
            if entry == NONE {
                return None;
            }
            if entry != TOMBSTONE
                && self.extents[entry].in_use()
                && self.extents[entry].base == base
            {
                return Some(entry);
            }
            offset += 1;
        }
        None
    }

    fn insert_lookup(&mut self, idx: usize) -> bool {
        let base = self.extents[idx].base;
        let start = Self::lookup_slot(base);
        let mut first_tombstone = NONE;
        let mut offset = 0usize;
        while offset < EXTENT_LOOKUP_SLOTS {
            let slot = (start + offset) & EXTENT_LOOKUP_MASK;
            let entry = self.lookup[slot];
            if entry == TOMBSTONE && first_tombstone == NONE {
                first_tombstone = slot;
            } else if entry == NONE {
                self.lookup[if first_tombstone == NONE {
                    slot
                } else {
                    first_tombstone
                }] = idx;
                return true;
            } else if entry != TOMBSTONE && self.extents[entry].base == base {
                return false;
            }
            offset += 1;
        }
        if first_tombstone != NONE {
            self.lookup[first_tombstone] = idx;
            true
        } else {
            false
        }
    }

    fn remove_lookup(&mut self, idx: usize) -> bool {
        let base = self.extents[idx].base;
        let start = Self::lookup_slot(base);
        let mut offset = 0usize;
        while offset < EXTENT_LOOKUP_SLOTS {
            let slot = (start + offset) & EXTENT_LOOKUP_MASK;
            let entry = self.lookup[slot];
            if entry == NONE {
                return false;
            }
            if entry == idx {
                self.lookup[slot] = TOMBSTONE;
                return true;
            }
            offset += 1;
        }
        false
    }

    fn note_mapped_extent(&mut self, backing: ActualBacking) {
        self.current_extents = self.current_extents.saturating_add(1);
        ACTIVE_EXTENTS.fetch_add(1, Ordering::Release);
        self.peak_extents = core::cmp::max(self.peak_extents, self.current_extents);
        if backing.is_hugetlb() {
            self.hugetlb_extent_mappings = self.hugetlb_extent_mappings.saturating_add(1);
            self.current_hugetlb_extents = self.current_hugetlb_extents.saturating_add(1);
            self.peak_hugetlb_extents =
                core::cmp::max(self.peak_hugetlb_extents, self.current_hugetlb_extents);
        } else if backing.is_thp_mapping() {
            self.thp_extent_mappings = self.thp_extent_mappings.saturating_add(1);
            if matches!(
                backing,
                ActualBacking::ThpCandidateNoHugepage
                    | ActualBacking::ThpCandidateNoHugepageAdviceFailed
            ) {
                self.thp_candidate_extent_mappings =
                    self.thp_candidate_extent_mappings.saturating_add(1);
            }
            self.current_thp_extents = self.current_thp_extents.saturating_add(1);
            self.peak_thp_extents = core::cmp::max(self.peak_thp_extents, self.current_thp_extents);
        } else {
            if backing == ActualBacking::HugetlbFallback {
                self.hugetlb_fallback_extent_mappings =
                    self.hugetlb_fallback_extent_mappings.saturating_add(1);
            } else {
                self.ordinary_extent_mappings = self.ordinary_extent_mappings.saturating_add(1);
            }
            self.current_ordinary_extents = self.current_ordinary_extents.saturating_add(1);
            self.peak_ordinary_extents =
                core::cmp::max(self.peak_ordinary_extents, self.current_ordinary_extents);
        }
    }

    fn note_unmapped_extent(&mut self, backing: ActualBacking) {
        self.extent_unmaps = self.extent_unmaps.saturating_add(1);
        self.current_extents = self.current_extents.saturating_sub(1);
        ACTIVE_EXTENTS.fetch_sub(1, Ordering::Release);
        if backing.is_hugetlb() {
            self.current_hugetlb_extents = self.current_hugetlb_extents.saturating_sub(1);
        } else if backing.is_thp_mapping() {
            self.current_thp_extents = self.current_thp_extents.saturating_sub(1);
            if backing.is_thp_confirmed_at_collapse() {
                self.current_thp_collapse_confirmed_extents = self
                    .current_thp_collapse_confirmed_extents
                    .saturating_sub(1);
            }
        } else {
            self.current_ordinary_extents = self.current_ordinary_extents.saturating_sub(1);
        }
        if self.current_extents == 0 {
            self.lookup = [NONE; EXTENT_LOOKUP_SLOTS];
        }
    }

    /// Release one fully empty mapping while preserving all lookup/list
    /// invariants if the kernel refuses the unmap.
    unsafe fn unmap_empty_extent(&mut self, idx: usize) -> bool {
        debug_assert!(self.extents[idx].in_use());
        debug_assert_eq!(self.extents[idx].live, 0);
        if self.extents[idx].collapse_pending {
            return false;
        }
        let extent_base = self.extents[idx].base;
        let extent_backing = self.extents[idx].backing;
        let was_retained = self.remove_retained_empty(idx);
        let was_available = self.extents[idx].on_available_list;
        self.remove_available(idx);
        if unmap_extent(extent_base as *mut u8) {
            if !self.remove_lookup(idx) {
                panic!("lifetime-arena extent index disappeared before unmap");
            }
            self.note_unmapped_extent(extent_backing);
            self.release_descriptor(idx);
            true
        } else {
            self.extent_unmap_failures = self.extent_unmap_failures.saturating_add(1);
            if was_available {
                self.add_available(idx);
            }
            if was_retained {
                self.add_retained_empty(idx);
            }
            false
        }
    }

    unsafe fn enforce_retained_empty_capacity(&mut self) {
        while self.current_retained_empty_extents > LIFETIME_EMPTY_EXTENT_RETENTION_CAPACITY {
            let victim = self.retained_empty_tail;
            debug_assert_ne!(victim, NONE);
            if !self.unmap_empty_extent(victim) {
                // A failed munmap must keep the mapping discoverable. The
                // existing failure counter makes this exceptional over-cap
                // state visible instead of discarding ownership provenance.
                break;
            }
            self.retained_empty_extent_evictions =
                self.retained_empty_extent_evictions.saturating_add(1);
        }
    }

    unsafe fn trim_retained_empty_extents(&mut self) -> bool {
        while self.retained_empty_tail != NONE {
            let victim = self.retained_empty_tail;
            if !self.unmap_empty_extent(victim) {
                return false;
            }
            self.retained_empty_extent_trims = self.retained_empty_extent_trims.saturating_add(1);
        }
        true
    }

    /// Promote prior-epoch THP candidates at a caller-provided quiescent phase
    /// boundary. This runs while the arena lock is held and MADV_COLLAPSE may
    /// perform synchronous kernel work, so callers must treat epoch advance as
    /// a potentially blocking control-plane operation.
    unsafe fn promote_surviving_thp_candidates(&mut self) {
        let mut idx = 0usize;
        while idx < self.next_unused_descriptor {
            match thp_promotion_eligibility(&self.extents[idx], self.current_epoch) {
                ThpPromotionEligibility::Ineligible => {
                    idx += 1;
                    continue;
                }
                ThpPromotionEligibility::LowOccupancy => {
                    self.thp_collapse_low_occupancy_skips =
                        self.thp_collapse_low_occupancy_skips.saturating_add(1);
                    idx += 1;
                    continue;
                }
                ThpPromotionEligibility::Eligible => {}
            }
            self.thp_collapse_eligible_extents =
                self.thp_collapse_eligible_extents.saturating_add(1);

            let extent_base = self.extents[idx].base;
            let extent_backing = self.extents[idx].backing;
            if matches!(
                extent_backing,
                ActualBacking::ThpCandidateNoHugepage
                    | ActualBacking::ThpCandidateNoHugepageAdviceFailed
            ) {
                self.thp_advice_attempts = self.thp_advice_attempts.saturating_add(1);
                if sys_alloc::advise_transparent_hugepage(
                    extent_base as *mut u8,
                    LIFETIME_HUGEPAGE_EXTENT_BYTES,
                ) {
                    self.thp_advice_successes = self.thp_advice_successes.saturating_add(1);
                    self.extents[idx].backing = ActualBacking::ThpAdvisedUnverified;
                } else {
                    self.thp_advice_errors = self.thp_advice_errors.saturating_add(1);
                    idx += 1;
                    continue;
                }
            }

            self.thp_collapse_attempts = self.thp_collapse_attempts.saturating_add(1);
            match sys_alloc::collapse_transparent_hugepage(
                extent_base as *mut u8,
                LIFETIME_HUGEPAGE_EXTENT_BYTES,
            ) {
                Ok(()) => {
                    self.thp_collapse_successes = self.thp_collapse_successes.saturating_add(1);
                    self.extents[idx].backing = ActualBacking::ThpCollapseSucceededPointInTime;
                    self.current_thp_collapse_confirmed_extents = self
                        .current_thp_collapse_confirmed_extents
                        .saturating_add(1);
                    self.peak_thp_collapse_confirmed_extents = core::cmp::max(
                        self.peak_thp_collapse_confirmed_extents,
                        self.current_thp_collapse_confirmed_extents,
                    );
                }
                Err(code) => {
                    self.thp_collapse_errors = self.thp_collapse_errors.saturating_add(1);
                    self.thp_collapse_last_error_code = code;
                }
            }
            idx += 1;
        }
    }

    /// Advise one densely packed compiler-selected Long extent for THP.
    /// `None` means the density/backing gate abstained; `Some` reports whether
    /// advice succeeded. Synchronous collapse, when requested by a policy,
    /// runs after the arena lock is released.
    unsafe fn promote_dense_compiler_long_extent(&mut self, idx: usize) -> Option<bool> {
        let extent = &self.extents[idx];
        if extent.live.saturating_mul(extent.slot_size) < THP_PROMOTION_MIN_LIVE_BYTES
            || !matches!(
                extent.backing,
                ActualBacking::ThpCandidateNoHugepage
                    | ActualBacking::ThpCandidateNoHugepageAdviceFailed
            )
        {
            return None;
        }
        self.thp_advice_attempts = self.thp_advice_attempts.saturating_add(1);
        if sys_alloc::advise_transparent_hugepage(
            extent.base as *mut u8,
            LIFETIME_HUGEPAGE_EXTENT_BYTES,
        ) {
            self.thp_advice_successes = self.thp_advice_successes.saturating_add(1);
            self.extents[idx].backing = ActualBacking::ThpAdvisedUnverified;
            Some(true)
        } else {
            self.thp_advice_errors = self.thp_advice_errors.saturating_add(1);
            // Record a terminal advice failure so every later allocation in the
            // dense extent does not repeat the same kernel call.
            self.extents[idx].backing = ActualBacking::ThpAdviceFailed;
            Some(false)
        }
    }

    /// Prepare physical THP backing for compiler-directed placement only when
    /// every slot in the extent is live. This keeps MADV_COLLAPSE from
    /// faulting a half-filled 2 MiB mapping into RSS. The blocking collapse
    /// itself runs after the arena lock is released.
    unsafe fn prepare_full_compiler_directed_long_collapse(
        &mut self,
        idx: usize,
    ) -> Option<Result<usize, ()>> {
        let extent = &self.extents[idx];
        let full_slot_count = extent
            .region_capacity
            .saturating_mul(IDENTITY_REGIONS_PER_EXTENT);
        if extent.live != full_slot_count
            || !matches!(
                extent.backing,
                ActualBacking::ThpCandidateNoHugepage
                    | ActualBacking::ThpCandidateNoHugepageAdviceFailed
            )
        {
            return None;
        }
        let extent_base = extent.base;
        let first_region = extent.regions[0];
        let promoted_reuse_identity = (first_region.assigned
            && extent.regions.iter().all(|region| {
                region.assigned
                    && region.identity == first_region.identity
                    && region.callsite == first_region.callsite
            }))
        .then_some(PromotedReuseIdentity {
            arena: first_region.identity,
            callsite: first_region.callsite,
        })?;
        self.thp_advice_attempts = self.thp_advice_attempts.saturating_add(1);
        if sys_alloc::advise_transparent_hugepage(
            extent_base as *mut u8,
            LIFETIME_HUGEPAGE_EXTENT_BYTES,
        ) {
            self.thp_advice_successes = self.thp_advice_successes.saturating_add(1);
            self.thp_collapse_attempts = self.thp_collapse_attempts.saturating_add(1);
            self.extents[idx].backing = ActualBacking::ThpAdvisedUnverified;
            self.extents[idx].promoted_reuse_identity = Some(promoted_reuse_identity);
            self.extents[idx].promoted_reuse_cycle_full = true;
            self.extents[idx].collapse_pending = true;
            self.remove_available(idx);
            Some(Ok(extent_base))
        } else {
            self.thp_advice_errors = self.thp_advice_errors.saturating_add(1);
            self.extents[idx].backing = ActualBacking::ThpAdviceFailed;
            self.extents[idx].promoted_reuse_identity = None;
            self.extents[idx].promoted_reuse_cycle_full = false;
            Some(Err(()))
        }
    }

    /// Mark a delayed adaptive candidate for synchronous collapse only after
    /// runtime-confirmed Long slots occupy at least 75% of the extent. Static
    /// priors may help fill the same candidate but never trigger this step.
    /// The collapse itself runs after the arena lock is released.
    unsafe fn prepare_dense_adaptive_long_collapse(&mut self, idx: usize) -> Option<usize> {
        if adaptive_thp_promotion_eligibility(&self.extents[idx])
            != ThpPromotionEligibility::Eligible
        {
            return None;
        }

        self.thp_advice_attempts = self.thp_advice_attempts.saturating_add(1);
        let extent_base = self.extents[idx].base;
        if sys_alloc::advise_transparent_hugepage(
            extent_base as *mut u8,
            LIFETIME_HUGEPAGE_EXTENT_BYTES,
        ) {
            self.thp_advice_successes = self.thp_advice_successes.saturating_add(1);
            self.extents[idx].backing = ActualBacking::ThpAdvisedUnverified;
            self.thp_collapse_attempts = self.thp_collapse_attempts.saturating_add(1);
            self.extents[idx].collapse_pending = true;
            self.remove_available(idx);
            Some(extent_base)
        } else {
            self.thp_advice_errors = self.thp_advice_errors.saturating_add(1);
            // A terminal state prevents repeated kernel calls for this extent.
            self.extents[idx].backing = ActualBacking::ThpAdviceFailed;
            None
        }
    }

    fn finish_dense_thp_collapse(
        &mut self,
        extent_base: usize,
        outcome: Result<(), isize>,
    ) -> bool {
        let Some(idx) = self.lookup_extent(extent_base) else {
            return false;
        };
        if !self.extents[idx].collapse_pending
            || self.extents[idx].backing != ActualBacking::ThpAdvisedUnverified
        {
            return false;
        }
        self.extents[idx].collapse_pending = false;
        match outcome {
            Ok(()) => {
                self.thp_collapse_successes = self.thp_collapse_successes.saturating_add(1);
                self.extents[idx].backing = ActualBacking::ThpCollapseSucceededPointInTime;
                self.current_thp_collapse_confirmed_extents = self
                    .current_thp_collapse_confirmed_extents
                    .saturating_add(1);
                self.peak_thp_collapse_confirmed_extents = core::cmp::max(
                    self.peak_thp_collapse_confirmed_extents,
                    self.current_thp_collapse_confirmed_extents,
                );
            }
            Err(code) => {
                self.thp_collapse_errors = self.thp_collapse_errors.saturating_add(1);
                self.thp_collapse_last_error_code = code;
                self.extents[idx].backing = ActualBacking::ThpAdviceFailed;
                self.extents[idx].promoted_reuse_identity = None;
                self.extents[idx].promoted_reuse_cycle_full = false;
            }
        }
        if self.extents[idx].live == 0 {
            let policy = LifetimeHugepagePolicy::from_usize(POLICY.load(Ordering::Acquire));
            let backend = LifetimePageBackend::from_usize(BACKEND.load(Ordering::Acquire));
            if empty_extent_retention_eligible(policy, backend, &self.extents[idx]) {
                self.add_available(idx);
                self.add_retained_empty(idx);
                self.retained_empty_extent_insertions =
                    self.retained_empty_extent_insertions.saturating_add(1);
                unsafe { self.enforce_retained_empty_capacity() };
                self.peak_retained_empty_extents = core::cmp::max(
                    self.peak_retained_empty_extents,
                    self.current_retained_empty_extents,
                );
            } else {
                let _ = unsafe { self.unmap_empty_extent(idx) };
            }
        } else if self.extents[idx].has_available_slot() {
            self.add_available(idx);
        }
        true
    }

    unsafe fn create_extent(
        &mut self,
        geometry: SlotGeometry,
        class: LifetimePlacementClass,
        requested: RequestedBacking,
        backend: LifetimePageBackend,
        birth_epoch: usize,
        epoch_cohort: bool,
    ) -> Option<usize> {
        let idx = match self.allocate_descriptor() {
            Some(idx) => idx,
            None => {
                self.mapping_failures = self.mapping_failures.saturating_add(1);
                return None;
            }
        };
        let mapping = match map_extent(requested, backend) {
            Some(mapping) => mapping,
            None => {
                self.release_descriptor(idx);
                self.mapping_failures = self.mapping_failures.saturating_add(1);
                return None;
            }
        };
        if !mapping.nohugepage_advice_ok {
            self.nohugepage_advice_failures = self.nohugepage_advice_failures.saturating_add(1);
        }
        match mapping.thp_advice {
            ThpAdviceOutcome::NotAttempted => {}
            ThpAdviceOutcome::Succeeded => {
                self.thp_advice_attempts = self.thp_advice_attempts.saturating_add(1);
                self.thp_advice_successes = self.thp_advice_successes.saturating_add(1);
            }
            ThpAdviceOutcome::Failed => {
                self.thp_advice_attempts = self.thp_advice_attempts.saturating_add(1);
                self.thp_advice_errors = self.thp_advice_errors.saturating_add(1);
            }
        }
        self.extents[idx] = Extent {
            base: mapping.base as usize,
            slot_size: geometry.slot_size,
            payload_capacity: geometry.payload_capacity,
            region_capacity: geometry.region_capacity,
            live: 0,
            adaptive_confirmed_long_live: 0,
            bucket: geometry.bucket,
            regions: [IdentityRegion::empty(); IDENTITY_REGIONS_PER_EXTENT],
            lifetime_class: class,
            cohort_epoch: if epoch_cohort { birth_epoch } else { 0 },
            requested_backing: Some(requested),
            backing: mapping.backing,
            promoted_reuse_identity: None,
            promoted_reuse_cycle_full: false,
            collapse_pending: false,
            adaptive_trailer: geometry.adaptive_trailer,
            available_prev: NONE,
            available_next: NONE,
            on_available_list: false,
            retained_empty_prev: NONE,
            retained_empty_next: NONE,
            on_retained_empty_list: false,
            next_free_descriptor: NONE,
        };
        if !self.insert_lookup(idx) {
            let _ = unmap_extent(mapping.base);
            self.release_descriptor(idx);
            self.mapping_failures = self.mapping_failures.saturating_add(1);
            return None;
        }
        self.add_available(idx);
        if epoch_cohort {
            self.epoch_cohort_extent_mappings = self.epoch_cohort_extent_mappings.saturating_add(1);
        }
        self.note_mapped_extent(mapping.backing);
        Some(idx)
    }

    unsafe fn allocate_from_extent(
        &mut self,
        available: AvailableRegion,
        identity: ArenaIdentity,
        callsite: u64,
        class: LifetimePlacementClass,
        birth_epoch: usize,
        require_epoch_match: bool,
        require_callsite_match: bool,
        adaptive_record: Option<AdaptiveAllocationRecord>,
    ) -> *mut u8 {
        let idx = available.extent_idx;
        let region_idx = available.region_idx;
        let reactivates_empty_extent = self.extents[idx].live == 0;
        if reactivates_empty_extent && self.remove_retained_empty(idx) {
            self.retained_empty_extent_reuse_hits =
                self.retained_empty_extent_reuse_hits.saturating_add(1);
        }
        if reactivates_empty_extent && self.extents[idx].promoted_reuse_identity.is_some() {
            self.extents[idx].promoted_reuse_cycle_full = false;
        }
        let region = self.extents[idx].regions[region_idx];
        if region.assigned
            && (region.identity != identity
                || (require_callsite_match && region.callsite != callsite)
                || (require_epoch_match && region.birth_epoch != birth_epoch)
                || !region.has_available_slot(self.extents[idx].region_capacity))
        {
            panic!("lifetime arena selected an incompatible identity region");
        }
        if !region.assigned {
            self.extents[idx].regions[region_idx] = IdentityRegion {
                identity,
                callsite,
                birth_epoch,
                epoch_mixed: false,
                next_unused: 0,
                live: 0,
                free_head: NONE,
                assigned: true,
            };
            self.identity_region_assignments = self.identity_region_assignments.saturating_add(1);
            self.current_identity_regions = self.current_identity_regions.saturating_add(1);
            self.peak_identity_regions =
                core::cmp::max(self.peak_identity_regions, self.current_identity_regions);
        }
        if self.extents[idx].regions[region_idx].birth_epoch != birth_epoch {
            self.extents[idx].regions[region_idx].epoch_mixed = true;
        }
        let region_base = self.extents[idx].base
            + region_idx.saturating_mul(LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES);
        let slot_index = if self.extents[idx].regions[region_idx].free_head != NONE {
            let slot_index = self.extents[idx].regions[region_idx].free_head;
            let ptr = (region_base + slot_index.saturating_mul(self.extents[idx].slot_size))
                as *mut usize;
            self.extents[idx].regions[region_idx].free_head = ptr.read();
            self.slot_reuse_hits = self.slot_reuse_hits.saturating_add(1);
            slot_index
        } else {
            let slot_index = self.extents[idx].regions[region_idx].next_unused;
            self.extents[idx].regions[region_idx].next_unused += 1;
            self.slot_bump_allocations = self.slot_bump_allocations.saturating_add(1);
            slot_index
        };
        self.extents[idx].live += 1;
        self.extents[idx].regions[region_idx].live += 1;
        if self.extents[idx].promoted_reuse_identity.is_some()
            && self.extents[idx].live == self.extents[idx].full_slot_count()
        {
            self.extents[idx].promoted_reuse_cycle_full = true;
        }
        let slot_size = self.extents[idx].slot_size;
        if !self.extents[idx].regions[region_idx]
            .has_available_slot(self.extents[idx].region_capacity)
            && !self.extents[idx].has_available_slot()
        {
            self.remove_available(idx);
        }
        self.routed_allocations = self.routed_allocations.saturating_add(1);
        self.live_objects = self.live_objects.saturating_add(1);
        LIVE_ARENA_OBJECTS.fetch_add(1, Ordering::Release);
        self.live_slot_bytes = self.live_slot_bytes.saturating_add(slot_size);
        match class {
            LifetimePlacementClass::Ephemeral => {
                self.live_ephemeral_objects = self.live_ephemeral_objects.saturating_add(1)
            }
            LifetimePlacementClass::LongLived => {
                self.live_long_lived_objects = self.live_long_lived_objects.saturating_add(1)
            }
            LifetimePlacementClass::Unknown => unreachable!(),
        }
        let ptr = (region_base + slot_index * slot_size) as *mut u8;
        if let Some(record) = adaptive_record {
            debug_assert!(self.extents[idx].adaptive_trailer);
            if record.confirmed_long_placement {
                self.extents[idx].adaptive_confirmed_long_live = self.extents[idx]
                    .adaptive_confirmed_long_live
                    .saturating_add(1);
            }
            let trailer = AdaptiveSlotTrailer::new(ptr, record);
            ptr.add(self.extents[idx].payload_capacity)
                .cast::<AdaptiveSlotTrailer>()
                .write(trailer);
            self.adaptive_live_survivor_register(
                record.site_index,
                ptr,
                record.birth_pressure,
                record.learned_prediction,
            );
        }
        ptr
    }

    #[inline]
    fn note_runtime_validation(
        &mut self,
        class: LifetimePlacementClass,
        actual_backing: ActualBacking,
        identity: ArenaIdentity,
        birth_epoch: usize,
        epoch_mixed: bool,
        bytes: usize,
    ) {
        if identity.flags & FLAG_DELAYED_FREE != 0 {
            self.runtime_validation_excluded_objects =
                self.runtime_validation_excluded_objects.saturating_add(1);
            self.runtime_validation_excluded_bytes =
                self.runtime_validation_excluded_bytes.saturating_add(bytes);
            self.runtime_delayed_free_excluded_objects =
                self.runtime_delayed_free_excluded_objects.saturating_add(1);
            self.runtime_delayed_free_excluded_bytes = self
                .runtime_delayed_free_excluded_bytes
                .saturating_add(bytes);
            return;
        }
        if epoch_mixed {
            self.runtime_validation_excluded_objects =
                self.runtime_validation_excluded_objects.saturating_add(1);
            self.runtime_validation_excluded_bytes =
                self.runtime_validation_excluded_bytes.saturating_add(bytes);
            self.runtime_mixed_epoch_excluded_objects =
                self.runtime_mixed_epoch_excluded_objects.saturating_add(1);
            self.runtime_mixed_epoch_excluded_bytes = self
                .runtime_mixed_epoch_excluded_bytes
                .saturating_add(bytes);
            return;
        }
        let actual_long = self.current_epoch != birth_epoch;
        self.runtime_validated_objects = self.runtime_validated_objects.saturating_add(1);
        self.runtime_validated_bytes = self.runtime_validated_bytes.saturating_add(bytes);
        self.predictor_confusion.record(
            class == LifetimePlacementClass::LongLived,
            actual_long,
            bytes,
        );
        self.placement_confusion.record(
            actual_backing.is_large_page_confirmed(),
            actual_long,
            bytes,
        );
    }

    unsafe fn deallocate(&mut self, ptr: *mut u8) -> bool {
        // Reconfiguration is rejected while any arena object remains live, so
        // one policy-word load is authoritative for this entire release.
        let policy_word = POLICY.load(Ordering::Acquire);
        let policy = LifetimeHugepagePolicy::from_usize(policy_word);
        let base = (ptr as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
        let idx = match self.lookup_extent(base) {
            Some(idx) => idx,
            None => return false,
        };
        let extent_base = self.extents[idx].base;
        let extent_backing = self.extents[idx].backing;
        let lifetime_class = self.extents[idx].lifetime_class;
        let cohort_epoch = self.extents[idx].cohort_epoch;
        let slot_size = self.extents[idx].slot_size;
        let payload_capacity = self.extents[idx].payload_capacity;
        let adaptive_trailer = self.extents[idx].adaptive_trailer;
        let offset = (ptr as usize).saturating_sub(extent_base);
        if offset >= LIFETIME_HUGEPAGE_EXTENT_BYTES || self.extents[idx].live == 0 {
            panic!("invalid lifetime-arena pointer release");
        }
        let region_idx = offset / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
        let region_offset = offset % LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
        let region = self.extents[idx].regions[region_idx];
        if !region.assigned
            || region_offset % slot_size != 0
            || region_offset / slot_size >= region.next_unused
            || region.live == 0
        {
            panic!("invalid lifetime-arena identity-region pointer release");
        }
        let adaptive_confirmed_long = if adaptive_trailer {
            let trailer = ptr
                .add(payload_capacity)
                .cast::<AdaptiveSlotTrailer>()
                .read();
            self.adaptive_note_deallocation(
                ptr,
                payload_capacity,
                slot_size,
                extent_backing,
                trailer,
            )
        } else if matches!(
            policy,
            LifetimeHugepagePolicy::CompilerInferredHugepage
                | LifetimeHugepagePolicy::CompilerInferredOrdinary
        ) {
            debug_assert_eq!(lifetime_class, LifetimePlacementClass::LongLived);
            COMPILER_INFERRED_DIRECT_LONG_DEALLOCATIONS.fetch_add(1, Ordering::Relaxed);
            Some(false)
        } else if policy.is_compiler_directed() {
            // The compiler already supplied the production placement decision.
            // Keep deallocation free of epoch validation and adaptive truth
            // measurement. Optional diagnostics reuse the loaded policy bit
            // and an ordinary counter protected by this arena lock.
            if policy_word & POLICY_COMPILER_DIRECTED_TELEMETRY != 0 {
                self.compiler_directed_deallocations =
                    self.compiler_directed_deallocations.saturating_add(1);
            }
            Some(false)
        } else {
            // The generic validator defines "actually long" by explicit epoch
            // transitions. Compiler-inferred regions carry a separate proof
            // contract and may run without runtime epoch markers, so feeding
            // them into this counter would report every same-epoch release as
            // a false positive even when the compiler proof was correct.
            self.note_runtime_validation(
                lifetime_class,
                extent_backing,
                region.identity,
                region.birth_epoch,
                region.epoch_mixed,
                slot_size,
            );
            Some(false)
        };
        let was_full = !self.extents[idx].has_available_slot();
        let slot_index = region_offset / slot_size;
        (ptr as *mut usize).write(region.free_head);
        self.extents[idx].regions[region_idx].free_head = slot_index;
        self.extents[idx].regions[region_idx].live -= 1;
        self.extents[idx].live -= 1;
        // Corrupt provenance cannot safely distinguish confirmed from
        // provisional occupancy. Resetting the credit prevents a later sparse
        // extent from crossing the promotion threshold.
        self.extents[idx].adaptive_confirmed_long_live = confirmed_long_live_after_deallocation(
            self.extents[idx].adaptive_confirmed_long_live,
            adaptive_confirmed_long,
        );
        if self.extents[idx].regions[region_idx].live == 0 {
            self.extents[idx].regions[region_idx] = IdentityRegion::empty();
            self.identity_region_releases = self.identity_region_releases.saturating_add(1);
            self.current_identity_regions = self.current_identity_regions.saturating_sub(1);
        }
        let extent_accepts_current_epoch = policy != LifetimeHugepagePolicy::EpochCohortHugepage
            || cohort_epoch == self.current_epoch;
        if was_full
            && extent_accepts_current_epoch
            && !self.extents[idx].collapse_pending
        {
            self.add_available(idx);
        }
        self.routed_deallocations = self.routed_deallocations.saturating_add(1);
        self.live_objects = self.live_objects.saturating_sub(1);
        let prior_live_objects = LIVE_ARENA_OBJECTS.fetch_sub(1, Ordering::Release);
        debug_assert!(prior_live_objects > 0);
        self.live_slot_bytes = self.live_slot_bytes.saturating_sub(slot_size);
        match lifetime_class {
            LifetimePlacementClass::Ephemeral => {
                self.live_ephemeral_objects = self.live_ephemeral_objects.saturating_sub(1)
            }
            LifetimePlacementClass::LongLived => {
                self.live_long_lived_objects = self.live_long_lived_objects.saturating_sub(1)
            }
            LifetimePlacementClass::Unknown => unreachable!(),
        }
        if self.extents[idx].live != 0 {
            return true;
        }

        if self.extents[idx].collapse_pending {
            self.remove_available(idx);
            return true;
        }

        if empty_extent_retention_eligible(policy, lifetime_hugepage_backend(), &self.extents[idx])
        {
            debug_assert!(extent_accepts_current_epoch);
            self.add_available(idx);
            self.add_retained_empty(idx);
            self.retained_empty_extent_insertions =
                self.retained_empty_extent_insertions.saturating_add(1);
            self.enforce_retained_empty_capacity();
            // Record the stable post-enforcement high-water mark. A transient
            // cap+1 insertion that was immediately evicted is not retained;
            // a failed munmap leaves the exceptional over-cap state visible.
            self.peak_retained_empty_extents = core::cmp::max(
                self.peak_retained_empty_extents,
                self.current_retained_empty_extents,
            );
            return true;
        }
        let _ = self.unmap_empty_extent(idx);
        true
    }
}

const POLICY_VALUE_MASK: usize = 0xff;
const POLICY_COMPILER_DIRECTED_TELEMETRY: usize = 1 << 8;

static POLICY: AtomicUsize = AtomicUsize::new(LifetimeHugepagePolicy::Disabled as usize);
static BACKEND: AtomicUsize = AtomicUsize::new(LifetimePageBackend::ExplicitHugeTLB as usize);
static ACTIVE_EXTENTS: AtomicUsize = AtomicUsize::new(0);
static LIVE_ARENA_OBJECTS: AtomicUsize = AtomicUsize::new(0);
#[cfg(test)]
static TEST_UNMAP_FAILURE_BASE: AtomicUsize = AtomicUsize::new(0);
static ADAPTIVE_FORCE_TRACK_ALL_ENABLED: AtomicBool = AtomicBool::new(false);
static COMPILER_INFERRED_UNKNOWN_OR_UNPROVEN_BYPASSES: AtomicUsize = AtomicUsize::new(0);
static COMPILER_INFERRED_PROVEN_EPHEMERAL_BYPASSES: AtomicUsize = AtomicUsize::new(0);
static COMPILER_INFERRED_BYPASS_REQUESTED_BYTES: AtomicUsize = AtomicUsize::new(0);
static COMPILER_INFERRED_ARENA_LOCK_ACQUISITIONS: AtomicUsize = AtomicUsize::new(0);
static COMPILER_INFERRED_DIRECT_LONG_ROUTES: AtomicUsize = AtomicUsize::new(0);
static COMPILER_INFERRED_DIRECT_LONG_REQUESTED_BYTES: AtomicUsize = AtomicUsize::new(0);
static COMPILER_INFERRED_DIRECT_LONG_SLOT_BYTES: AtomicUsize = AtomicUsize::new(0);
static COMPILER_INFERRED_DIRECT_LONG_DEALLOCATIONS: AtomicUsize = AtomicUsize::new(0);
static COMPILER_INFERRED_DIRECT_LONG_ROUTES_WITHOUT_TRAILER: AtomicUsize = AtomicUsize::new(0);
static COMPILER_INFERRED_DENSITY_PROMOTION_ATTEMPTS: AtomicUsize = AtomicUsize::new(0);
static COMPILER_INFERRED_DENSITY_PROMOTION_SUCCESSES: AtomicUsize = AtomicUsize::new(0);
static COMPILER_INFERRED_DENSITY_PROMOTION_ERRORS: AtomicUsize = AtomicUsize::new(0);
static COMPILER_DIRECTED_UNKNOWN_BYPASSES: AtomicUsize = AtomicUsize::new(0);
static COMPILER_DIRECTED_BYPASS_REQUESTED_BYTES: AtomicUsize = AtomicUsize::new(0);
static ARENA: Mutex<LifetimeArenaState> = Mutex::new(LifetimeArenaState::new());
#[thread_local]
static mut PHASE_FLUSH_ACTIVE: bool = false;

struct PhaseFlushGuard;

impl PhaseFlushGuard {
    #[inline]
    unsafe fn enter() -> Option<Self> {
        if PHASE_FLUSH_ACTIVE {
            return None;
        }
        PHASE_FLUSH_ACTIVE = true;
        Some(Self)
    }
}

impl Drop for PhaseFlushGuard {
    fn drop(&mut self) {
        unsafe { PHASE_FLUSH_ACTIVE = false };
    }
}

#[inline]
fn lifetime_class_index(class: LifetimePlacementClass) -> Option<usize> {
    match class {
        LifetimePlacementClass::Ephemeral => Some(0),
        LifetimePlacementClass::LongLived => Some(1),
        LifetimePlacementClass::Unknown => None,
    }
}

#[inline]
fn checked_align_up(value: usize, align: usize) -> Option<usize> {
    value
        .checked_add(align - 1)
        .map(|rounded| rounded & !(align - 1))
}

fn slot_geometry(
    layout: Layout,
    class: LifetimePlacementClass,
    adaptive_trailer: bool,
) -> Option<SlotGeometry> {
    if layout.size() == 0 || layout.align() > MAX_SLOT_ALIGN {
        return None;
    }
    let requested = core::cmp::max(layout.size(), size_of::<usize>());
    let (size_class, rounded_size) = get_size_class_tuple(requested);
    let size_class_idx = match size_class {
        SizeClass::Base(idx) => idx,
        SizeClass::Large(_) => return None,
    };
    let (payload_capacity, slot_size) = if adaptive_trailer {
        let payload_capacity =
            checked_align_up(rounded_size, core::mem::align_of::<AdaptiveSlotTrailer>())?;
        let stride_align =
            core::cmp::max(layout.align(), core::mem::align_of::<AdaptiveSlotTrailer>());
        let slot_size = payload_capacity
            .checked_add(size_of::<AdaptiveSlotTrailer>())
            .and_then(|bytes| checked_align_up(bytes, stride_align))?;
        (payload_capacity, slot_size)
    } else {
        let slot_size = checked_align_up(rounded_size, layout.align())?;
        (slot_size, slot_size)
    };
    if slot_size == 0 || slot_size > MAX_SLOT_BYTES {
        return None;
    }
    let region_capacity = LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES / slot_size;
    if region_capacity == 0 {
        return None;
    }
    let align_class = layout.align().trailing_zeros() as usize;
    if align_class >= ALIGN_CLASS_COUNT {
        return None;
    }
    let lifetime_idx = lifetime_class_index(class)?;
    let bucket =
        (lifetime_idx * TOTAL_SIZE_CLASS + size_class_idx) * ALIGN_CLASS_COUNT + align_class;
    Some(SlotGeometry {
        slot_size,
        payload_capacity,
        region_capacity,
        bucket,
        adaptive_trailer,
    })
}

unsafe fn map_extent(
    requested: RequestedBacking,
    backend: LifetimePageBackend,
) -> Option<ExtentMapping> {
    let layout = Layout::from_size_align(
        LIFETIME_HUGEPAGE_EXTENT_BYTES,
        LIFETIME_HUGEPAGE_EXTENT_BYTES,
    )
    .ok()?;
    match requested {
        RequestedBacking::Ordinary => {
            let ptr = sys_alloc::mmap_over_page_aligned(layout, prots::PROT_READ_WRITE);
            if ptr.is_null() || ptr as usize % LIFETIME_HUGEPAGE_EXTENT_BYTES != 0 {
                if !ptr.is_null() {
                    sys_alloc::munmap(ptr, LIFETIME_HUGEPAGE_EXTENT_BYTES);
                }
                return None;
            }
            Some(ExtentMapping {
                base: ptr,
                backing: ActualBacking::Ordinary,
                nohugepage_advice_ok: advise_no_hugepage(ptr),
                thp_advice: ThpAdviceOutcome::NotAttempted,
            })
        }
        RequestedBacking::EpochCandidate => {
            if backend != LifetimePageBackend::TransparentHugepage || !cfg!(target_os = "linux") {
                return None;
            }
            let ptr = sys_alloc::mmap_over_page_aligned(layout, prots::PROT_READ_WRITE);
            if ptr.is_null() || ptr as usize % LIFETIME_HUGEPAGE_EXTENT_BYTES != 0 {
                if !ptr.is_null() {
                    sys_alloc::munmap(ptr, LIFETIME_HUGEPAGE_EXTENT_BYTES);
                }
                return None;
            }
            let nohugepage_advice_ok = advise_no_hugepage(ptr);
            Some(ExtentMapping {
                base: ptr,
                backing: if nohugepage_advice_ok {
                    ActualBacking::ThpCandidateNoHugepage
                } else {
                    ActualBacking::ThpCandidateNoHugepageAdviceFailed
                },
                nohugepage_advice_ok,
                thp_advice: ThpAdviceOutcome::NotAttempted,
            })
        }
        RequestedBacking::LargePage => match backend {
            LifetimePageBackend::ExplicitHugeTLB => {
                let mapped = sys_alloc::mmap_huge_with_backing(
                    LIFETIME_HUGEPAGE_EXTENT_BYTES,
                    prots::PROT_READ_WRITE,
                );
                if !mapped.is_success() || mapped.ptr as usize % LIFETIME_HUGEPAGE_EXTENT_BYTES != 0
                {
                    if mapped.is_success() {
                        sys_alloc::munmap(mapped.ptr, LIFETIME_HUGEPAGE_EXTENT_BYTES);
                    }
                    return None;
                }
                let backing = if mapped.backing == HugePageMmapBacking::HugePage {
                    ActualBacking::Hugetlb
                } else {
                    ActualBacking::HugetlbFallback
                };
                Some(ExtentMapping {
                    base: mapped.ptr,
                    backing,
                    nohugepage_advice_ok: true,
                    thp_advice: ThpAdviceOutcome::NotAttempted,
                })
            }
            LifetimePageBackend::TransparentHugepage => {
                let mapped = sys_alloc::mmap_transparent_hugepage(
                    LIFETIME_HUGEPAGE_EXTENT_BYTES,
                    prots::PROT_READ_WRITE,
                );
                if !mapped.is_success() || mapped.ptr as usize % LIFETIME_HUGEPAGE_EXTENT_BYTES != 0
                {
                    if mapped.is_success() {
                        sys_alloc::munmap(mapped.ptr, LIFETIME_HUGEPAGE_EXTENT_BYTES);
                    }
                    return None;
                }
                Some(ExtentMapping {
                    base: mapped.ptr,
                    backing: if mapped.advice_succeeded {
                        ActualBacking::ThpAdvisedUnverified
                    } else {
                        ActualBacking::ThpAdviceFailed
                    },
                    nohugepage_advice_ok: true,
                    thp_advice: if mapped.advice_succeeded {
                        ThpAdviceOutcome::Succeeded
                    } else {
                        ThpAdviceOutcome::Failed
                    },
                })
            }
        },
    }
}

#[cfg(target_os = "linux")]
unsafe fn advise_no_hugepage(ptr: *mut u8) -> bool {
    libc::madvise(
        ptr as *mut libc::c_void,
        LIFETIME_HUGEPAGE_EXTENT_BYTES,
        libc::MADV_NOHUGEPAGE,
    ) == 0
}

#[cfg(not(target_os = "linux"))]
unsafe fn advise_no_hugepage(_ptr: *mut u8) -> bool {
    true
}

#[cfg(unix)]
unsafe fn unmap_extent(ptr: *mut u8) -> bool {
    #[cfg(test)]
    if TEST_UNMAP_FAILURE_BASE.load(Ordering::Acquire) == ptr as usize {
        return false;
    }
    libc::munmap(ptr as *mut libc::c_void, LIFETIME_HUGEPAGE_EXTENT_BYTES) == 0
}

#[cfg(not(unix))]
unsafe fn unmap_extent(ptr: *mut u8) -> bool {
    sys_alloc::munmap(ptr, LIFETIME_HUGEPAGE_EXTENT_BYTES);
    true
}

pub fn lifetime_hugepage_policy() -> LifetimeHugepagePolicy {
    LifetimeHugepagePolicy::from_usize(POLICY.load(Ordering::Acquire))
}

/// Decide whether a flags-zero compiler scope carries enough exact lifetime
/// evidence to enter the lifetime allocator path under the active policy.
///
/// Adaptive policies need every exact site, including `Unknown`, so runtime
/// outcomes can train the classifier. Compiler-inferred policies accept only
/// the bounded process-long oracle. Compiler-directed policies accept exactly
/// the stable binary Long ABI only; Short, `Unknown`, and observation-only
/// scopes stay on the base allocator before its semantic slow path. The older
/// static policies accept the two legacy routable classes with the same
/// Unknown abstention.
#[inline]
pub(crate) fn flags_zero_exact_scope_is_effective(metadata: AllocationMetadata) -> bool {
    if metadata.callsite == 0 || !metadata.has_type() {
        return false;
    }
    match LifetimeHugepagePolicy::from_usize(POLICY.load(Ordering::Acquire)) {
        LifetimeHugepagePolicy::Disabled => false,
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage
        | LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary
        | LifetimeHugepagePolicy::AdaptiveRuntimeProfileHugepage => true,
        LifetimeHugepagePolicy::CompilerInferredHugepage
        | LifetimeHugepagePolicy::CompilerInferredOrdinary => {
            compiler_bounded_lifetime_placement_class(metadata.lifetime_hint)
                == LifetimePlacementClass::LongLived
        }
        LifetimeHugepagePolicy::CompilerDirectedHugepage
        | LifetimeHugepagePolicy::CompilerDirectedOrdinary => {
            compiler_directed_lifetime_placement_class(metadata.lifetime_hint)
                == LifetimePlacementClass::LongLived
        }
        LifetimeHugepagePolicy::SegregatedOrdinary
        | LifetimeHugepagePolicy::LongLivedHugepage
        | LifetimeHugepagePolicy::SegregatedHugepage
        | LifetimeHugepagePolicy::EpochCohortHugepage => {
            lifetime_placement_class(metadata.lifetime_hint) != LifetimePlacementClass::Unknown
        }
    }
}

pub fn lifetime_hugepage_backend() -> LifetimePageBackend {
    LifetimePageBackend::from_usize(BACKEND.load(Ordering::Acquire))
}

/// Select the placement policy before the process creates routed objects.
/// Changing policy while arena mappings remain live is rejected.
pub fn lifetime_hugepage_configure(policy: LifetimeHugepagePolicy) -> bool {
    let backend = if matches!(
        policy,
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage
            | LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary
            | LifetimeHugepagePolicy::AdaptiveRuntimeProfileHugepage
            | LifetimeHugepagePolicy::CompilerInferredHugepage
            | LifetimeHugepagePolicy::CompilerInferredOrdinary
            | LifetimeHugepagePolicy::CompilerDirectedHugepage
            | LifetimeHugepagePolicy::CompilerDirectedOrdinary
    ) {
        LifetimePageBackend::TransparentHugepage
    } else {
        LifetimePageBackend::ExplicitHugeTLB
    };
    lifetime_hugepage_configure_with_backend(policy, backend)
}

/// Select placement policy and physical page backend as orthogonal controls.
/// Changing either control while arena objects remain live is rejected. Empty
/// retained extents are trimmed before publishing the new controls.
pub fn lifetime_hugepage_configure_with_backend(
    policy: LifetimeHugepagePolicy,
    backend: LifetimePageBackend,
) -> bool {
    if matches!(
        policy,
        LifetimeHugepagePolicy::AdaptiveRuntimeHugepage
            | LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary
            | LifetimeHugepagePolicy::AdaptiveRuntimeProfileHugepage
            | LifetimeHugepagePolicy::CompilerInferredHugepage
            | LifetimeHugepagePolicy::CompilerInferredOrdinary
            | LifetimeHugepagePolicy::CompilerDirectedHugepage
            | LifetimeHugepagePolicy::CompilerDirectedOrdinary
    ) && backend != LifetimePageBackend::TransparentHugepage
    {
        return false;
    }
    let mut state = ARENA.lock();
    if state.live_objects != 0 {
        return false;
    }
    if !unsafe { state.trim_retained_empty_extents() } || state.current_extents != 0 {
        return false;
    }
    if state.adaptive_force_track_all.enabled && !policy.is_adaptive() {
        return false;
    }
    BACKEND.store(backend as usize, Ordering::Release);
    POLICY.store(policy as usize, Ordering::Release);
    true
}

/// Enable diagnostic counters for compiler-directed policies at a quiescent
/// point. The enable bit shares the already-loaded policy word, leaving the
/// default performance arm free of an additional atomic read or event RMW.
pub fn lifetime_hugepage_compiler_directed_telemetry_enable() -> bool {
    let state = ARENA.lock();
    if state.live_objects != 0 || !lifetime_hugepage_policy().is_compiler_directed() {
        return false;
    }
    POLICY.fetch_or(POLICY_COMPILER_DIRECTED_TELEMETRY, Ordering::Release);
    true
}

/// Disable compiler-directed diagnostics at a quiescent point.
pub fn lifetime_hugepage_compiler_directed_telemetry_disable() -> bool {
    let state = ARENA.lock();
    if state.live_objects != 0 {
        return false;
    }
    POLICY.fetch_and(!POLICY_COMPILER_DIRECTED_TELEMETRY, Ordering::Release);
    true
}

#[inline]
pub fn lifetime_hugepage_compiler_directed_telemetry_enabled() -> bool {
    POLICY.load(Ordering::Acquire) & POLICY_COMPILER_DIRECTED_TELEMETRY != 0
}

pub fn lifetime_hugepage_stats_snapshot() -> LifetimeHugepageStatsSnapshot {
    ARENA.lock().snapshot()
}

/// Release every fully empty extent in the bounded reuse LRU. Live arena
/// objects remain valid; false reports a kernel unmap failure.
pub fn lifetime_hugepage_trim_retained_empty_extents() -> bool {
    unsafe { ARENA.lock().trim_retained_empty_extents() }
}

/// Enable evaluation-only exact-layout runtime lifetime observations.
///
/// Toggling is rejected while adaptive trailers remain live so every tracked
/// allocation has a matching deallocation outcome in the export table.
pub fn lifetime_hugepage_adaptive_site_recording_enable() -> bool {
    let mut state = ARENA.lock();
    if state.adaptive_live_trailers != 0 {
        return false;
    }
    state.adaptive_observation_recording = true;
    true
}

/// Disable exact-layout runtime lifetime observations at a quiescent point.
pub fn lifetime_hugepage_adaptive_site_recording_disable() -> bool {
    let mut state = ARENA.lock();
    if state.live_objects != 0
        || state.adaptive_live_trailers != 0
        || state.adaptive_force_track_all.enabled
    {
        return false;
    }
    if !unsafe { state.trim_retained_empty_extents() } || state.current_extents != 0 {
        return false;
    }
    state.adaptive_observation_recording = false;
    true
}

pub fn lifetime_hugepage_adaptive_site_recording_enabled() -> bool {
    ARENA.lock().adaptive_observation_recording
}

/// Enable evaluation-only complete observation under explicit hard guards.
///
/// Every eligible allocation is routed through adaptive trailer geometry on
/// ordinary pages, independent of the learned placement prediction. Once
/// either guard is exhausted, later allocations fall back safely and increment
/// `adaptive_force_track_all_guard_bypasses`; such a run is ineligible for
/// unbiased prevalence or accuracy claims. Configuration is accepted only at
/// a quiescent point after exact-site observation recording has been enabled.
pub fn lifetime_hugepage_adaptive_site_force_track_all_enable(
    maximum_allocations: usize,
    maximum_requested_bytes: usize,
) -> bool {
    let mut state = ARENA.lock();
    if !state.adaptive_observation_recording
        || state.live_objects != 0
        || state.adaptive_live_trailers != 0
    {
        return false;
    }
    if !unsafe { state.trim_retained_empty_extents() } || state.current_extents != 0 {
        return false;
    }
    let enabled = state
        .adaptive_force_track_all
        .enable(maximum_allocations, maximum_requested_bytes);
    if enabled {
        ADAPTIVE_FORCE_TRACK_ALL_ENABLED.store(true, Ordering::Release);
    }
    enabled
}

/// Disable evaluation-only complete observation at a quiescent point.
pub fn lifetime_hugepage_adaptive_site_force_track_all_disable() -> bool {
    let mut state = ARENA.lock();
    if state.live_objects != 0 || state.adaptive_live_trailers != 0 {
        return false;
    }
    if !unsafe { state.trim_retained_empty_extents() } || state.current_extents != 0 {
        return false;
    }
    state.adaptive_force_track_all = AdaptiveForceTrackAllState::disabled();
    ADAPTIVE_FORCE_TRACK_ALL_ENABLED.store(false, Ordering::Release);
    true
}

pub fn lifetime_hugepage_adaptive_site_force_track_all_enabled() -> bool {
    ADAPTIVE_FORCE_TRACK_ALL_ENABLED.load(Ordering::Acquire)
}

#[inline]
fn force_track_note_external_pressure(requested_bytes: usize, source: ForcePressureSource) {
    if requested_bytes == 0
        || !ADAPTIVE_FORCE_TRACK_ALL_ENABLED.load(Ordering::Relaxed)
        || !LifetimeHugepagePolicy::from_usize(POLICY.load(Ordering::Relaxed)).is_adaptive()
    {
        return;
    }
    let mut state = ARENA.lock();
    if state.adaptive_force_track_all.enabled && lifetime_hugepage_policy().is_adaptive() {
        state.adaptive_note_force_track_pressure(requested_bytes, source);
    }
}

/// Add one successful raw/unattributed allocation generation to the
/// evaluation-only global pressure clock. Production adaptive mode pays only
/// the relaxed disabled check above.
pub(crate) fn lifetime_hugepage_force_track_note_raw_allocation(requested_bytes: usize) {
    force_track_note_external_pressure(requested_bytes, ForcePressureSource::RawAllocation);
}

/// Add one successful semantic allocation generation that bypassed
/// `try_allocate` to the evaluation-only global pressure clock. Guard-page
/// mappings are the current caller; ordinary semantic fallbacks pass through
/// `try_allocate` and have already been counted there.
pub(crate) fn lifetime_hugepage_force_track_note_external_semantic_allocation(
    requested_bytes: usize,
) {
    force_track_note_external_pressure(requested_bytes, ForcePressureSource::SemanticAllocation);
}

/// Add one successful raw realloc that moved to a new address. In-place growth
/// retains the existing allocation generation and deliberately adds no clock
/// pressure.
pub(crate) fn lifetime_hugepage_force_track_note_raw_moved_reallocation(requested_bytes: usize) {
    force_track_note_external_pressure(requested_bytes, ForcePressureSource::RawMovedReallocation);
}

/// Copy exact-layout observation rows into `out` and return the total populated
/// row count. A shorter slice receives a prefix while the return value still
/// reports the required capacity.
pub fn lifetime_hugepage_adaptive_site_snapshot(out: &mut [LifetimeAdaptiveSiteSnapshot]) -> usize {
    ARENA.lock().adaptive_observation_snapshot(out)
}

/// Advance the process-wide logical phase used by runtime lifetime validation.
/// The caller is responsible for placing a workload barrier around the phase
/// boundary. Advancing never frees storage or invalidates live pointers.
#[inline(never)]
pub fn lifetime_hugepage_advance_epoch() -> usize {
    let mut state = ARENA.lock();
    if lifetime_hugepage_policy() == LifetimeHugepagePolicy::Disabled {
        return state.current_epoch;
    }
    state.current_epoch = state.current_epoch.wrapping_add(1);
    if state.current_epoch == 0 {
        state.current_epoch = 1;
    }
    state.phase_advances = state.phase_advances.saturating_add(1);
    if lifetime_hugepage_policy() == LifetimeHugepagePolicy::EpochCohortHugepage {
        state.detach_available_epoch_cohorts();
        if lifetime_hugepage_backend() == LifetimePageBackend::TransparentHugepage {
            // The caller-provided phase barrier makes this synchronous kernel
            // promotion safe with respect to allocator payload access.
            unsafe { state.promote_surviving_thp_candidates() };
        }
    }
    state.current_epoch
}

/// End a lifetime phase on the current thread by terminally releasing its
/// retained semantic cache and delayed-free entries. This makes phase
/// boundaries actionable for extent reclamation instead of waiting for TLS
/// teardown. A same-thread reentrant call is conservatively ignored.
pub fn lifetime_hugepage_phase_flush_current_thread() -> usize {
    unsafe {
        let Some(_guard) = PhaseFlushGuard::enter() else {
            return 0;
        };
        super::type_isolation::drain_current_thread_semantic_retained_state(
            &crate::cache::RustAllocator::new(),
        )
    }
}

/// Reset diagnostics only after every arena-owned mapping has been released.
/// An active adaptive policy preserves learned site states while starting a
/// fresh pressure/telemetry window. Configure `Disabled` before reset when an
/// experiment explicitly needs to discard the learned classifier model.
pub fn lifetime_hugepage_stats_reset() -> bool {
    let mut state = ARENA.lock();
    if state.live_objects != 0 {
        return false;
    }
    if !unsafe { state.trim_retained_empty_extents() } || state.current_extents != 0 {
        return false;
    }
    let preserve_adaptive_model = lifetime_hugepage_policy().is_adaptive();
    state.reset_counters(preserve_adaptive_model);
    COMPILER_INFERRED_UNKNOWN_OR_UNPROVEN_BYPASSES.store(0, Ordering::Release);
    COMPILER_INFERRED_PROVEN_EPHEMERAL_BYPASSES.store(0, Ordering::Release);
    COMPILER_INFERRED_BYPASS_REQUESTED_BYTES.store(0, Ordering::Release);
    COMPILER_INFERRED_ARENA_LOCK_ACQUISITIONS.store(0, Ordering::Release);
    COMPILER_INFERRED_DIRECT_LONG_ROUTES.store(0, Ordering::Release);
    COMPILER_INFERRED_DIRECT_LONG_REQUESTED_BYTES.store(0, Ordering::Release);
    COMPILER_INFERRED_DIRECT_LONG_SLOT_BYTES.store(0, Ordering::Release);
    COMPILER_INFERRED_DIRECT_LONG_DEALLOCATIONS.store(0, Ordering::Release);
    COMPILER_INFERRED_DIRECT_LONG_ROUTES_WITHOUT_TRAILER.store(0, Ordering::Release);
    COMPILER_INFERRED_DENSITY_PROMOTION_ATTEMPTS.store(0, Ordering::Release);
    COMPILER_INFERRED_DENSITY_PROMOTION_SUCCESSES.store(0, Ordering::Release);
    COMPILER_INFERRED_DENSITY_PROMOTION_ERRORS.store(0, Ordering::Release);
    COMPILER_DIRECTED_UNKNOWN_BYPASSES.store(0, Ordering::Release);
    COMPILER_DIRECTED_BYPASS_REQUESTED_BYTES.store(0, Ordering::Release);
    true
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CompilerInferredAdmission {
    BypassUnknownOrUnproven,
    DirectBoundedLong,
}

#[inline]
fn compiler_inferred_admission(lifetime_hint: u16) -> CompilerInferredAdmission {
    match compiler_bounded_lifetime_placement_class(lifetime_hint) {
        LifetimePlacementClass::Unknown => CompilerInferredAdmission::BypassUnknownOrUnproven,
        LifetimePlacementClass::Ephemeral => CompilerInferredAdmission::BypassUnknownOrUnproven,
        LifetimePlacementClass::LongLived => CompilerInferredAdmission::DirectBoundedLong,
    }
}

#[inline]
fn note_compiler_inferred_prelock_bypass(
    admission: CompilerInferredAdmission,
    requested_bytes: usize,
) {
    match admission {
        CompilerInferredAdmission::BypassUnknownOrUnproven => {
            COMPILER_INFERRED_UNKNOWN_OR_UNPROVEN_BYPASSES.fetch_add(1, Ordering::Relaxed);
        }
        CompilerInferredAdmission::DirectBoundedLong => {
            unreachable!("bounded-long admission cannot be recorded as a bypass")
        }
    }
    COMPILER_INFERRED_BYPASS_REQUESTED_BYTES.fetch_add(requested_bytes, Ordering::Relaxed);
}

#[inline]
fn note_compiler_directed_prelock_bypass(telemetry: bool, requested_bytes: usize) {
    if !telemetry {
        return;
    }
    COMPILER_DIRECTED_UNKNOWN_BYPASSES.fetch_add(1, Ordering::Relaxed);
    COMPILER_DIRECTED_BYPASS_REQUESTED_BYTES.fetch_add(requested_bytes, Ordering::Relaxed);
}

#[inline]
fn dynamic_buffer_observation_candidate(lifetime_hint: u16) -> bool {
    lifetime_hint == LIFETIME_HINT_DYNAMIC_BUFFER_OBSERVE
}

#[inline]
fn dynamic_buffer_observation_layout_admitted(layout: Layout) -> bool {
    (DYNAMIC_BUFFER_OBSERVE_MIN_BYTES..=DYNAMIC_BUFFER_OBSERVE_MAX_BYTES).contains(&layout.size())
}

#[inline]
fn compiler_directed_long_layout_admitted(layout: Layout, lifetime_hint: u16) -> bool {
    compiler_directed_lifetime_placement_class(lifetime_hint) == LifetimePlacementClass::LongLived
        && (COMPILER_DIRECTED_LONG_MIN_BYTES..=COMPILER_DIRECTED_LONG_MAX_BYTES)
            .contains(&layout.size())
}

/// Try to route one exact semantic allocation into a lifetime/size arena.
/// `None` preserves the existing allocator path.
pub(crate) unsafe fn try_allocate(layout: Layout, metadata: AllocationMetadata) -> Option<*mut u8> {
    let observed_policy_word = POLICY.load(Ordering::Acquire);
    let observed_policy = LifetimeHugepagePolicy::from_usize(observed_policy_word);
    if observed_policy == LifetimeHugepagePolicy::Disabled {
        return None;
    }
    if observed_policy.is_compiler_directed()
        && !compiler_directed_long_layout_admitted(layout, metadata.lifetime_hint)
    {
        note_compiler_directed_prelock_bypass(
            observed_policy_word & POLICY_COMPILER_DIRECTED_TELEMETRY != 0,
            layout.size(),
        );
        return None;
    }
    let dynamic_buffer_observation = dynamic_buffer_observation_candidate(metadata.lifetime_hint);
    if dynamic_buffer_observation
        && (!observed_policy.is_adaptive() || !dynamic_buffer_observation_layout_admitted(layout))
    {
        // The compiler tag requests bounded observation only. Out-of-band
        // layouts and placement policies without runtime learning stay on the
        // existing allocator path without taking ARENA's lock.
        return None;
    }
    let observed_compiler_inferred = matches!(
        observed_policy,
        LifetimeHugepagePolicy::CompilerInferredHugepage
            | LifetimeHugepagePolicy::CompilerInferredOrdinary
    );
    if observed_compiler_inferred {
        let admission = compiler_inferred_admission(metadata.lifetime_hint);
        if admission != CompilerInferredAdmission::DirectBoundedLong {
            note_compiler_inferred_prelock_bypass(admission, layout.size());
            return None;
        }
    }
    let mut state = ARENA.lock();
    // Configuration also holds ARENA while publishing BACKEND and POLICY.
    // Reload both controls after locking so a bounded-long allocation that
    // waited behind reconfiguration never combines an old policy with a new
    // backend. Pre-lock bypasses linearize at their stable policy load above.
    let policy_word = POLICY.load(Ordering::Acquire);
    let policy = LifetimeHugepagePolicy::from_usize(policy_word);
    if policy == LifetimeHugepagePolicy::Disabled {
        return None;
    }
    let compiler_directed = policy.is_compiler_directed();
    let compiler_directed_telemetry =
        compiler_directed && policy_word & POLICY_COMPILER_DIRECTED_TELEMETRY != 0;
    if compiler_directed && !compiler_directed_long_layout_admitted(layout, metadata.lifetime_hint)
    {
        note_compiler_directed_prelock_bypass(compiler_directed_telemetry, layout.size());
        return None;
    }
    if dynamic_buffer_observation
        && (!policy.is_adaptive() || !dynamic_buffer_observation_layout_admitted(layout))
    {
        return None;
    }
    let backend = lifetime_hugepage_backend();
    let compiler_inferred = matches!(
        policy,
        LifetimeHugepagePolicy::CompilerInferredHugepage
            | LifetimeHugepagePolicy::CompilerInferredOrdinary
    );
    if compiler_inferred {
        let admission = compiler_inferred_admission(metadata.lifetime_hint);
        if admission != CompilerInferredAdmission::DirectBoundedLong {
            note_compiler_inferred_prelock_bypass(admission, layout.size());
            return None;
        }
        COMPILER_INFERRED_ARENA_LOCK_ACQUISITIONS.fetch_add(1, Ordering::Relaxed);
    }
    if compiler_directed_telemetry {
        state.compiler_directed_arena_lock_acquisitions = state
            .compiler_directed_arena_lock_acquisitions
            .saturating_add(1);
    }
    let static_class = if policy.ignores_static_lifetime_prior() {
        LifetimePlacementClass::Unknown
    } else if compiler_directed {
        compiler_directed_lifetime_placement_class(metadata.lifetime_hint)
    } else {
        lifetime_placement_class(metadata.lifetime_hint)
    };
    let adaptive = policy.is_adaptive();
    let force_track_pressure = if adaptive && state.adaptive_force_track_all.enabled {
        Some(state.adaptive_note_force_track_pressure(
            layout.size(),
            ForcePressureSource::SemanticAllocation,
        ))
    } else {
        None
    };
    let (class, geometry, adaptive_site, adaptive_long_confirmed) = if adaptive {
        let cold_geometry = match slot_geometry(layout, LifetimePlacementClass::Ephemeral, true) {
            Some(geometry) => geometry,
            None => {
                state.unsupported_layout_bypasses =
                    state.unsupported_layout_bypasses.saturating_add(1);
                return None;
            }
        };
        let key = match AdaptiveSiteKey::from_metadata(
            metadata,
            cold_geometry.payload_capacity,
            layout.align(),
        ) {
            Some(key) => key,
            None => {
                state.adaptive_missing_identity_bypasses =
                    state.adaptive_missing_identity_bypasses.saturating_add(1);
                return None;
            }
        };
        let site_idx = state.adaptive_site_slot(key, static_class)?;
        let routing_prediction = state.adaptive_sites[site_idx].routing_prediction();
        let learned_prediction = state.adaptive_sites[site_idx].prediction;
        let long_placement_confirmed = state.adaptive_sites[site_idx].long_placement_confirmed();
        let force_track_all = state.adaptive_force_track_all.enabled;
        if force_track_all && !state.adaptive_force_track_all.allows(layout.size()) {
            let pressure = force_track_pressure
                .expect("force-track allocation pressure must be counted before admission");
            state.adaptive_note_force_track_guard_bypass();
            state.adaptive_observation_note_allocation(
                key,
                layout.size(),
                pressure,
                false,
                learned_prediction,
                static_class,
            );
            return None;
        }
        if !force_track_all && !state.adaptive_sites[site_idx].should_track_allocation() {
            let pressure =
                state.adaptive_note_bypass(cold_geometry.payload_capacity, learned_prediction);
            state.adaptive_observation_note_allocation(
                key,
                layout.size(),
                pressure,
                false,
                learned_prediction,
                static_class,
            );
            return None;
        }
        let class = adaptive_placement_class(routing_prediction, force_track_all);
        let geometry = if class == LifetimePlacementClass::Ephemeral {
            cold_geometry
        } else {
            slot_geometry(layout, class, true).expect("adaptive geometry changed across class")
        };
        (
            class,
            geometry,
            Some((site_idx, learned_prediction)),
            long_placement_confirmed,
        )
    } else {
        if static_class == LifetimePlacementClass::Unknown {
            state.unknown_bypasses = state.unknown_bypasses.saturating_add(1);
            state.unknown_bypass_requested_bytes = state
                .unknown_bypass_requested_bytes
                .saturating_add(layout.size());
            return None;
        }
        let geometry = match slot_geometry(layout, static_class, false) {
            Some(geometry) => geometry,
            None => {
                if static_class != LifetimePlacementClass::Unknown {
                    state.unsupported_layout_bypasses =
                        state.unsupported_layout_bypasses.saturating_add(1);
                }
                return None;
            }
        };
        (static_class, geometry, None, true)
    };
    let requested_backing =
        match requested_backing_for_allocation(policy, class, backend, adaptive_long_confirmed) {
            Some(backing) => backing,
            None => {
                if class == LifetimePlacementClass::Unknown {
                    state.unknown_bypasses = state.unknown_bypasses.saturating_add(1);
                    state.unknown_bypass_requested_bytes = state
                        .unknown_bypass_requested_bytes
                        .saturating_add(layout.size());
                }
                return None;
            }
        };
    let identity = if policy.ignores_static_lifetime_prior() {
        ArenaIdentity::from_metadata_without_lifetime_hint(metadata)
    } else {
        ArenaIdentity::from_metadata(metadata)
    };
    let birth_epoch = state.current_epoch;
    let epoch_cohort = policy.separates_epoch_extents();
    let prefer_fullest = policy.is_adaptive_thp()
        && backend == LifetimePageBackend::TransparentHugepage
        && class == LifetimePlacementClass::LongLived
        && requested_backing == RequestedBacking::EpochCandidate;
    let available = if let Some(available) = state.find_available(
        geometry,
        requested_backing,
        identity,
        metadata.callsite,
        birth_epoch,
        epoch_cohort,
        prefer_fullest,
        policy == LifetimeHugepagePolicy::CompilerDirectedHugepage,
    ) {
        available
    } else {
        match state.create_extent(
            geometry,
            class,
            requested_backing,
            backend,
            birth_epoch,
            epoch_cohort,
        ) {
            Some(idx) => AvailableRegion {
                extent_idx: idx,
                region_idx: 0,
            },
            None => {
                state.allocation_fallbacks = state.allocation_fallbacks.saturating_add(1);
                return None;
            }
        }
    };
    let adaptive_record = adaptive_site.map(|(site_idx, learned_prediction)| {
        let birth_pressure = state.adaptive_note_allocation(
            site_idx,
            layout.size(),
            geometry.payload_capacity,
            learned_prediction,
            force_track_pressure,
        );
        let site = state.adaptive_sites[site_idx].key;
        state.adaptive_observation_note_allocation(
            site,
            layout.size(),
            birth_pressure,
            true,
            learned_prediction,
            static_class,
        );
        AdaptiveAllocationRecord {
            birth_pressure,
            site_fingerprint: state.adaptive_sites[site_idx].fingerprint,
            site_index: site_idx,
            requested_size: layout.size(),
            learned_prediction,
            static_hint: static_class,
            confirmed_long_placement: class == LifetimePlacementClass::LongLived
                && adaptive_long_confirmed,
        }
    });
    let allocated_extent_idx = available.extent_idx;
    let ptr = state.allocate_from_extent(
        available,
        identity,
        metadata.callsite,
        class,
        birth_epoch,
        epoch_cohort,
        policy == LifetimeHugepagePolicy::CompilerDirectedHugepage,
        adaptive_record,
    );
    if compiler_inferred {
        debug_assert_eq!(class, LifetimePlacementClass::LongLived);
        COMPILER_INFERRED_DIRECT_LONG_ROUTES.fetch_add(1, Ordering::Relaxed);
        COMPILER_INFERRED_DIRECT_LONG_REQUESTED_BYTES.fetch_add(layout.size(), Ordering::Relaxed);
        COMPILER_INFERRED_DIRECT_LONG_SLOT_BYTES.fetch_add(geometry.slot_size, Ordering::Relaxed);
        if !geometry.adaptive_trailer {
            COMPILER_INFERRED_DIRECT_LONG_ROUTES_WITHOUT_TRAILER.fetch_add(1, Ordering::Relaxed);
        }
        if policy == LifetimeHugepagePolicy::CompilerInferredHugepage
            && backend == LifetimePageBackend::TransparentHugepage
        {
            if let Some(succeeded) = state.promote_dense_compiler_long_extent(allocated_extent_idx)
            {
                COMPILER_INFERRED_DENSITY_PROMOTION_ATTEMPTS.fetch_add(1, Ordering::Relaxed);
                if succeeded {
                    COMPILER_INFERRED_DENSITY_PROMOTION_SUCCESSES.fetch_add(1, Ordering::Relaxed);
                } else {
                    COMPILER_INFERRED_DENSITY_PROMOTION_ERRORS.fetch_add(1, Ordering::Relaxed);
                }
            }
        }
    }
    let mut compiler_directed_collapse_base = None;
    if compiler_directed {
        if compiler_directed_telemetry {
            match class {
                LifetimePlacementClass::Ephemeral => {
                    state.compiler_directed_short_routes =
                        state.compiler_directed_short_routes.saturating_add(1);
                    state.compiler_directed_short_requested_bytes = state
                        .compiler_directed_short_requested_bytes
                        .saturating_add(layout.size());
                    state.compiler_directed_short_slot_bytes = state
                        .compiler_directed_short_slot_bytes
                        .saturating_add(geometry.slot_size);
                }
                LifetimePlacementClass::LongLived => {
                    state.compiler_directed_long_routes =
                        state.compiler_directed_long_routes.saturating_add(1);
                    state.compiler_directed_long_requested_bytes = state
                        .compiler_directed_long_requested_bytes
                        .saturating_add(layout.size());
                    state.compiler_directed_long_slot_bytes = state
                        .compiler_directed_long_slot_bytes
                        .saturating_add(geometry.slot_size);
                }
                LifetimePlacementClass::Unknown => unreachable!(),
            }
            if !geometry.adaptive_trailer {
                state.compiler_directed_routes_without_trailer = state
                    .compiler_directed_routes_without_trailer
                    .saturating_add(1);
            }
        }
        if policy == LifetimeHugepagePolicy::CompilerDirectedHugepage
            && class == LifetimePlacementClass::LongLived
            && backend == LifetimePageBackend::TransparentHugepage
        {
            if let Some(prepared) =
                state.prepare_full_compiler_directed_long_collapse(allocated_extent_idx)
            {
                if compiler_directed_telemetry {
                    state.compiler_directed_density_promotion_attempts = state
                        .compiler_directed_density_promotion_attempts
                        .saturating_add(1);
                    if prepared.is_err() {
                        state.compiler_directed_density_promotion_errors = state
                            .compiler_directed_density_promotion_errors
                            .saturating_add(1);
                    }
                }
                if let Ok(extent_base) = prepared {
                    compiler_directed_collapse_base = Some(extent_base);
                }
            }
        }
    }
    let adaptive_collapse_base = if policy.is_adaptive_thp()
        && backend == LifetimePageBackend::TransparentHugepage
        && class == LifetimePlacementClass::LongLived
    {
        state.prepare_dense_adaptive_long_collapse(allocated_extent_idx)
    } else {
        None
    };
    drop(state);
    if let Some(extent_base) = adaptive_collapse_base {
        let outcome = sys_alloc::collapse_transparent_hugepage(
            extent_base as *mut u8,
            LIFETIME_HUGEPAGE_EXTENT_BYTES,
        );
        let _ = ARENA
            .lock()
            .finish_dense_thp_collapse(extent_base, outcome);
    }
    if let Some(extent_base) = compiler_directed_collapse_base {
        let outcome = sys_alloc::collapse_transparent_hugepage(
            extent_base as *mut u8,
            LIFETIME_HUGEPAGE_EXTENT_BYTES,
        );
        let succeeded = outcome.is_ok();
        let mut state = ARENA.lock();
        let finished = state.finish_dense_thp_collapse(extent_base, outcome);
        if compiler_directed_telemetry {
            if finished && succeeded {
                state.compiler_directed_density_promotion_successes = state
                    .compiler_directed_density_promotion_successes
                    .saturating_add(1);
            } else {
                state.compiler_directed_density_promotion_errors = state
                    .compiler_directed_density_promotion_errors
                    .saturating_add(1);
            }
        }
    }
    Some(ptr)
}

#[inline]
pub(crate) fn owns(ptr: *mut u8) -> bool {
    if ptr.is_null() || LIVE_ARENA_OBJECTS.load(Ordering::Acquire) == 0 {
        return false;
    }
    let base = (ptr as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
    ARENA.lock().lookup_extent(base).is_some()
}

/// Return `None` for ordinary pointers and `Some(fits)` for arena pointers.
pub(crate) fn realloc_in_place_supported(ptr: *mut u8, new_layout: Layout) -> Option<bool> {
    if ptr.is_null() || LIVE_ARENA_OBJECTS.load(Ordering::Acquire) == 0 {
        return None;
    }
    let base = (ptr as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
    let state = ARENA.lock();
    let idx = state.lookup_extent(base)?;
    let extent = &state.extents[idx];
    let offset = (ptr as usize).saturating_sub(extent.base);
    let region_idx = offset / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
    let region_offset = offset % LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
    let region = extent.regions.get(region_idx)?;
    Some(
        offset < LIFETIME_HUGEPAGE_EXTENT_BYTES
            && region.assigned
            && region_offset % extent.slot_size == 0
            && region_offset / extent.slot_size < region.next_unused
            && new_layout.size() <= extent.payload_capacity
            && (ptr as usize) % new_layout.align() == 0,
    )
}

/// Release an arena pointer. False means the ordinary backend owns it.
pub(crate) unsafe fn try_deallocate(ptr: *mut u8) -> bool {
    if ptr.is_null() || LIVE_ARENA_OBJECTS.load(Ordering::Acquire) == 0 {
        return false;
    }
    ARENA.lock().deallocate(ptr)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::alloc_api::type_isolation::{
        LIFETIME_HINT_BOUNDED_PROCESS_LONG, LIFETIME_HINT_DYNAMIC_BUFFER_OBSERVE,
        LIFETIME_HINT_EPHEMERAL, LIFETIME_HINT_LOCAL_DROP_FACT, LIFETIME_HINT_LONG_LIVED,
    };
    use alloc::vec::Vec;

    static COMPILER_INFERRED_TEST_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn compiler_fact_values_preserve_drop_unknown_and_bounded_long_semantics() {
        assert_eq!(
            lifetime_placement_class(LIFETIME_HINT_EPHEMERAL),
            LifetimePlacementClass::Ephemeral
        );
        assert_eq!(
            lifetime_placement_class(LIFETIME_HINT_LONG_LIVED),
            LifetimePlacementClass::LongLived
        );
        assert_eq!(
            lifetime_placement_class(LIFETIME_HINT_LOCAL_DROP_FACT),
            LifetimePlacementClass::Unknown
        );
        assert_eq!(
            lifetime_placement_class(LIFETIME_HINT_BOUNDED_PROCESS_LONG),
            LifetimePlacementClass::LongLived
        );
        assert_eq!(
            lifetime_placement_class(LIFETIME_HINT_DYNAMIC_BUFFER_OBSERVE),
            LifetimePlacementClass::Unknown
        );
        assert_eq!(
            compiler_bounded_lifetime_placement_class(LIFETIME_HINT_EPHEMERAL),
            LifetimePlacementClass::Unknown
        );
        assert_eq!(
            compiler_bounded_lifetime_placement_class(LIFETIME_HINT_LONG_LIVED),
            LifetimePlacementClass::Unknown
        );
        assert_eq!(
            compiler_bounded_lifetime_placement_class(LIFETIME_HINT_LOCAL_DROP_FACT),
            LifetimePlacementClass::Unknown
        );
        assert_eq!(
            compiler_bounded_lifetime_placement_class(LIFETIME_HINT_DYNAMIC_BUFFER_OBSERVE),
            LifetimePlacementClass::Unknown
        );
        assert!(dynamic_buffer_observation_layout_admitted(
            Layout::from_size_align(4 * 1024, 8).unwrap()
        ));
        assert!(dynamic_buffer_observation_layout_admitted(
            Layout::from_size_align(32 * 1024, 8).unwrap()
        ));
        assert!(!dynamic_buffer_observation_layout_admitted(
            Layout::from_size_align(4 * 1024 - 1, 8).unwrap()
        ));
        assert!(!dynamic_buffer_observation_layout_admitted(
            Layout::from_size_align(32 * 1024 + 1, 8).unwrap()
        ));
        if COMPILER_DIRECTED_LONG_MAX_BYTES >= COMPILER_DIRECTED_LONG_MIN_BYTES {
            for size in [
                COMPILER_DIRECTED_LONG_MIN_BYTES,
                COMPILER_DIRECTED_LONG_MAX_BYTES,
            ] {
                let layout = Layout::from_size_align(size, 8).unwrap();
                assert!(compiler_directed_long_layout_admitted(
                    layout,
                    LIFETIME_HINT_LONG_LIVED
                ));
                assert!(!compiler_directed_long_layout_admitted(
                    layout,
                    LIFETIME_HINT_EPHEMERAL
                ));
                assert!(!compiler_directed_long_layout_admitted(layout, 0));
            }
            for size in [
                COMPILER_DIRECTED_LONG_MIN_BYTES - 1,
                COMPILER_DIRECTED_LONG_MAX_BYTES + 1,
            ] {
                assert!(!compiler_directed_long_layout_admitted(
                    Layout::from_size_align(size, 8).unwrap(),
                    LIFETIME_HINT_LONG_LIVED
                ));
            }
        } else {
            assert!(!compiler_directed_long_layout_admitted(
                Layout::from_size_align(COMPILER_DIRECTED_LONG_MIN_BYTES, 8).unwrap(),
                LIFETIME_HINT_LONG_LIVED
            ));
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn dynamic_buffer_observation_stays_ordinary_until_runtime_confirms_long() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage
        ));
        assert!(lifetime_hugepage_stats_reset());
        assert!(lifetime_hugepage_adaptive_site_recording_enable());

        let candidate = AllocationMetadata::for_type(0xADAA_4103)
            .with_module(0xA110_C413)
            .with_lifetime_hint(LIFETIME_HINT_DYNAMIC_BUFFER_OBSERVE);

        // Compiler candidates outside the actual 4--32 KiB Layout band never
        // enter adaptive state or acquire an arena extent.
        let tiny = Layout::from_size_align(1024, 8).unwrap();
        assert!(unsafe { try_allocate(tiny, candidate.with_callsite(0x51_D0)) }.is_none());
        let after_tiny = lifetime_hugepage_stats_snapshot();
        assert_eq!(after_tiny.adaptive_site_count, 0);
        assert_eq!(after_tiny.current_extents, 0);

        // A short-lived borrowed Vec site receives bounded observation on
        // ordinary pages and then becomes a sampled/bypassed Short site.
        let short_layout = Layout::from_size_align(8 * 1024, 8).unwrap();
        let short_metadata = candidate.with_callsite(0x51_D1);
        for _ in 0..ADAPTIVE_MIN_DECISIVE_SAMPLES {
            let ptr = unsafe { try_allocate(short_layout, short_metadata) }
                .expect("unconfirmed candidate should be sampled on ordinary pages");
            let state = ARENA.lock();
            let base = ptr as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
            let idx = state.lookup_extent(base).unwrap();
            assert_eq!(
                state.extents[idx].requested_backing,
                Some(RequestedBacking::Ordinary)
            );
            drop(state);
            assert!(unsafe { try_deallocate(ptr) });
        }
        assert!(unsafe { try_allocate(short_layout, short_metadata) }.is_none());

        // An independent candidate also trains on ordinary pages. Only the
        // allocation after eight decisive Long outcomes enters the Long lane.
        let long_layout = Layout::from_size_align(16 * 1024, 8).unwrap();
        let long_metadata = candidate.with_callsite(0x51_D2);
        for _ in 0..ADAPTIVE_MIN_DECISIVE_SAMPLES {
            let ptr = unsafe { try_allocate(long_layout, long_metadata) }
                .expect("unconfirmed candidate should remain observable");
            let state = ARENA.lock();
            let base = ptr as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
            let idx = state.lookup_extent(base).unwrap();
            assert_eq!(
                state.extents[idx].requested_backing,
                Some(RequestedBacking::Ordinary)
            );
            drop(state);
            ARENA
                .lock()
                .adaptive_note_pressure(ADAPTIVE_LONG_AGE_BYTES as usize);
            assert!(unsafe { try_deallocate(ptr) });
        }

        let confirmed_ptr = unsafe { try_allocate(long_layout, long_metadata) }
            .expect("runtime-confirmed Long site should enter the Long lane");
        {
            let state = ARENA.lock();
            let base = confirmed_ptr as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
            let idx = state.lookup_extent(base).unwrap();
            assert_eq!(
                state.extents[idx].requested_backing,
                Some(RequestedBacking::EpochCandidate)
            );
        }
        let mut observations = [LifetimeAdaptiveSiteSnapshot::empty(); 2];
        assert_eq!(
            lifetime_hugepage_adaptive_site_snapshot(&mut observations),
            2
        );
        let short = observations
            .iter()
            .find(|row| row.callsite == 0x51_D1)
            .unwrap();
        let long = observations
            .iter()
            .find(|row| row.callsite == 0x51_D2)
            .unwrap();
        assert_eq!(short.latest_prediction, AdaptivePrediction::Short as u8);
        assert_eq!(
            short.latest_static_prior,
            LifetimePlacementClass::Unknown as u8
        );
        assert_eq!(short.short_outcomes, ADAPTIVE_MIN_DECISIVE_SAMPLES as usize);
        assert_eq!(long.latest_prediction, AdaptivePrediction::Long as u8);
        assert_eq!(
            long.latest_static_prior,
            LifetimePlacementClass::Unknown as u8
        );
        assert_eq!(long.long_outcomes, ADAPTIVE_MIN_DECISIVE_SAMPLES as usize);

        assert!(unsafe { try_deallocate(confirmed_ptr) });
        assert!(lifetime_hugepage_adaptive_site_recording_disable());
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn runtime_profile_policy_masks_static_long_prior_and_regular_adaptive_restores_it() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        let layout = Layout::from_size_align(16 * 1024, 8).unwrap();
        let metadata = AllocationMetadata::for_type(0xADAA_4111)
            .with_module(0xA110_C421)
            .with_callsite(0x51_E1)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);

        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::AdaptiveRuntimeProfileHugepage
        ));
        assert!(lifetime_hugepage_stats_reset());
        assert!(lifetime_hugepage_adaptive_site_recording_enable());
        let runtime_only = unsafe { try_allocate(layout, metadata) }
            .expect("runtime-profile policy should cold-track the exact site");
        {
            let state = ARENA.lock();
            let base = runtime_only as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
            let idx = state.lookup_extent(base).unwrap();
            let region_idx =
                (runtime_only as usize - base) / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
            assert_eq!(
                state.extents[idx].regions[region_idx]
                    .identity
                    .lifetime_hint,
                0
            );
        }
        let mut runtime_only_rows = [LifetimeAdaptiveSiteSnapshot::empty(); 1];
        assert_eq!(
            lifetime_hugepage_adaptive_site_snapshot(&mut runtime_only_rows),
            1
        );
        assert_eq!(
            runtime_only_rows[0].latest_static_prior,
            LifetimePlacementClass::Unknown as u8
        );
        assert_eq!(
            runtime_only_rows[0].latest_prediction,
            AdaptivePrediction::Cold as u8
        );
        assert!(unsafe { try_deallocate(runtime_only) });
        assert!(lifetime_hugepage_adaptive_site_recording_disable());
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());

        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage
        ));
        assert!(lifetime_hugepage_stats_reset());
        assert!(lifetime_hugepage_adaptive_site_recording_enable());
        let adaptive_with_prior = unsafe { try_allocate(layout, metadata) }
            .expect("regular adaptive policy should use the static prior");
        {
            let state = ARENA.lock();
            let base = adaptive_with_prior as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
            let idx = state.lookup_extent(base).unwrap();
            let region_idx =
                (adaptive_with_prior as usize - base) / LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES;
            assert_eq!(
                state.extents[idx].regions[region_idx]
                    .identity
                    .lifetime_hint,
                LIFETIME_HINT_LONG_LIVED
            );
        }
        let mut prior_rows = [LifetimeAdaptiveSiteSnapshot::empty(); 1];
        assert_eq!(lifetime_hugepage_adaptive_site_snapshot(&mut prior_rows), 1);
        assert_eq!(
            prior_rows[0].latest_static_prior,
            LifetimePlacementClass::LongLived as u8
        );
        assert_eq!(
            prior_rows[0].latest_prediction,
            AdaptivePrediction::Cold as u8
        );
        assert!(unsafe { try_deallocate(adaptive_with_prior) });
        assert!(lifetime_hugepage_adaptive_site_recording_disable());
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[test]
    fn compiler_inferred_admission_trusts_only_bounded_long_oracle() {
        assert_eq!(
            compiler_inferred_admission(0),
            CompilerInferredAdmission::BypassUnknownOrUnproven
        );
        assert_eq!(
            compiler_inferred_admission(LIFETIME_HINT_EPHEMERAL),
            CompilerInferredAdmission::BypassUnknownOrUnproven
        );
        assert_eq!(
            compiler_inferred_admission(LIFETIME_HINT_LONG_LIVED),
            CompilerInferredAdmission::BypassUnknownOrUnproven
        );
        assert_eq!(
            compiler_inferred_admission(LIFETIME_HINT_LOCAL_DROP_FACT),
            CompilerInferredAdmission::BypassUnknownOrUnproven
        );
        assert_eq!(
            compiler_inferred_admission(LIFETIME_HINT_BOUNDED_PROCESS_LONG),
            CompilerInferredAdmission::DirectBoundedLong
        );
        assert_eq!(
            LifetimeHugepagePolicy::from_usize(6),
            LifetimeHugepagePolicy::CompilerInferredHugepage
        );
        assert_eq!(
            LifetimeHugepagePolicy::from_usize(7),
            LifetimeHugepagePolicy::CompilerInferredOrdinary
        );
        assert_eq!(
            LifetimeHugepagePolicy::from_usize(8),
            LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary
        );
        assert_eq!(
            LifetimeHugepagePolicy::CompilerInferredHugepage.requested_backing(
                LifetimePlacementClass::Ephemeral,
                LifetimePageBackend::TransparentHugepage,
            ),
            None
        );
        assert_eq!(
            LifetimeHugepagePolicy::CompilerInferredHugepage.requested_backing(
                LifetimePlacementClass::LongLived,
                LifetimePageBackend::TransparentHugepage,
            ),
            Some(RequestedBacking::EpochCandidate)
        );
        assert_eq!(
            LifetimeHugepagePolicy::CompilerInferredOrdinary.requested_backing(
                LifetimePlacementClass::Ephemeral,
                LifetimePageBackend::TransparentHugepage,
            ),
            None
        );
        assert_eq!(
            LifetimeHugepagePolicy::CompilerInferredOrdinary.requested_backing(
                LifetimePlacementClass::LongLived,
                LifetimePageBackend::TransparentHugepage,
            ),
            Some(RequestedBacking::Ordinary)
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn compiler_inferred_policy_bypasses_prelock_and_routes_long_without_trailers() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::CompilerInferredHugepage
        ));
        assert!(lifetime_hugepage_stats_reset());
        let layout = Layout::from_size_align(4096, 64).unwrap();
        let base_metadata = AllocationMetadata::for_type(0xC011_1FE7)
            .with_module(0xA110_C001)
            .with_callsite(0x51_7E);

        assert!(unsafe { try_allocate(layout, base_metadata) }.is_none());
        assert!(unsafe {
            try_allocate(
                layout,
                base_metadata.with_lifetime_hint(LIFETIME_HINT_LOCAL_DROP_FACT),
            )
        }
        .is_none());
        let bypass = lifetime_hugepage_stats_snapshot();
        assert_eq!(bypass.compiler_inferred_unknown_or_unproven_bypasses, 2);
        assert_eq!(bypass.compiler_inferred_proven_ephemeral_bypasses, 0);
        assert_eq!(
            bypass.compiler_inferred_bypass_requested_bytes,
            layout.size() * 2
        );
        assert_eq!(bypass.compiler_inferred_arena_lock_acquisitions, 0);
        assert_eq!(bypass.compiler_inferred_direct_long_routes, 0);
        assert_eq!(
            bypass.compiler_inferred_direct_long_routes_without_trailer,
            0
        );
        assert_eq!(bypass.adaptive_training_allocations, 0);
        assert_eq!(bypass.adaptive_live_trailers, 0);

        const ROUTES_TO_DENSITY_THRESHOLD: usize = THP_PROMOTION_MIN_LIVE_BYTES / 4096;
        let mut pointers = [core::ptr::null_mut(); ROUTES_TO_DENSITY_THRESHOLD];
        let long_metadata = base_metadata.with_lifetime_hint(LIFETIME_HINT_BOUNDED_PROCESS_LONG);
        for ptr in &mut pointers {
            *ptr = unsafe { try_allocate(layout, long_metadata) }
                .expect("bounded-long compiler oracle should route directly");
        }

        let routed = lifetime_hugepage_stats_snapshot();
        assert_eq!(
            routed.policy,
            LifetimeHugepagePolicy::CompilerInferredHugepage
        );
        assert_eq!(routed.backend, LifetimePageBackend::TransparentHugepage);
        assert_eq!(
            routed.compiler_inferred_arena_lock_acquisitions,
            ROUTES_TO_DENSITY_THRESHOLD
        );
        assert_eq!(
            routed.compiler_inferred_direct_long_routes,
            ROUTES_TO_DENSITY_THRESHOLD
        );
        assert_eq!(
            routed.compiler_inferred_direct_long_requested_bytes,
            ROUTES_TO_DENSITY_THRESHOLD * layout.size()
        );
        assert_eq!(
            routed.compiler_inferred_direct_long_slot_bytes,
            ROUTES_TO_DENSITY_THRESHOLD * 4096
        );
        assert_eq!(routed.compiler_inferred_direct_long_deallocations, 0);
        assert_eq!(
            routed.compiler_inferred_direct_long_routes_without_trailer,
            ROUTES_TO_DENSITY_THRESHOLD
        );
        assert_eq!(routed.adaptive_site_count, 0);
        assert_eq!(routed.adaptive_training_allocations, 0);
        assert_eq!(routed.adaptive_live_trailers, 0);
        assert_eq!(routed.thp_candidate_extent_mappings, 1);
        assert_eq!(routed.compiler_inferred_density_promotion_attempts, 1);
        assert_eq!(
            routed.compiler_inferred_density_promotion_successes
                + routed.compiler_inferred_density_promotion_errors,
            1
        );
        assert_eq!(routed.thp_collapse_attempts, 0);
        if routed.compiler_inferred_density_promotion_successes == 1 {
            let state = ARENA.lock();
            let extent_base = pointers[0] as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
            let idx = state
                .lookup_extent(extent_base)
                .expect("advice-only compiler extent should remain mapped while live");
            assert_eq!(
                state.extents[idx].backing,
                ActualBacking::ThpAdvisedUnverified
            );
            assert!(
                !state.extents[idx].collapse_pending,
                "advice-only promotion must not acquire the synchronous-collapse pin"
            );
        }

        for ptr in pointers {
            assert!(unsafe { try_deallocate(ptr) });
        }
        let released = lifetime_hugepage_stats_snapshot();
        if routed.compiler_inferred_density_promotion_successes == 1 {
            assert!(released.all_mappings_released);
        } else {
            assert_eq!(released.current_retained_empty_extents, 1);
            assert!(!released.all_mappings_released);
        }
        assert_eq!(
            released.compiler_inferred_direct_long_deallocations,
            ROUTES_TO_DENSITY_THRESHOLD
        );
        assert_eq!(released.runtime_validated_objects, 0);
        assert_eq!(released.adaptive_short_observations, 0);
        assert_eq!(released.adaptive_long_observations, 0);
        assert_eq!(released.adaptive_censored_observations, 0);
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn compiler_inferred_ordinary_matches_admission_and_disables_thp() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::CompilerInferredOrdinary
        ));
        assert_eq!(
            lifetime_hugepage_backend(),
            LifetimePageBackend::TransparentHugepage
        );
        assert!(lifetime_hugepage_stats_reset());
        let layout = Layout::from_size_align(4096, 64).unwrap();
        let base_metadata = AllocationMetadata::for_type(0xC011_0FF7)
            .with_module(0xA110_C001)
            .with_callsite(0x51_7E);

        assert!(unsafe { try_allocate(layout, base_metadata) }.is_none());
        assert!(unsafe {
            try_allocate(
                layout,
                base_metadata.with_lifetime_hint(LIFETIME_HINT_LOCAL_DROP_FACT),
            )
        }
        .is_none());

        const MATCHED_ROUTES: usize = THP_PROMOTION_MIN_LIVE_BYTES / 4096;
        let mut pointers = [core::ptr::null_mut(); MATCHED_ROUTES];
        let long_metadata = base_metadata.with_lifetime_hint(LIFETIME_HINT_BOUNDED_PROCESS_LONG);
        for ptr in &mut pointers {
            *ptr = unsafe { try_allocate(layout, long_metadata) }
                .expect("bounded-long compiler oracle should route into the ordinary control");
        }

        let routed = lifetime_hugepage_stats_snapshot();
        assert_eq!(
            routed.policy,
            LifetimeHugepagePolicy::CompilerInferredOrdinary
        );
        assert_eq!(routed.compiler_inferred_unknown_or_unproven_bypasses, 2);
        assert_eq!(routed.compiler_inferred_proven_ephemeral_bypasses, 0);
        assert_eq!(
            routed.compiler_inferred_arena_lock_acquisitions,
            MATCHED_ROUTES
        );
        assert_eq!(routed.compiler_inferred_direct_long_routes, MATCHED_ROUTES);
        assert_eq!(
            routed.compiler_inferred_direct_long_requested_bytes,
            MATCHED_ROUTES * layout.size()
        );
        assert_eq!(
            routed.compiler_inferred_direct_long_slot_bytes,
            MATCHED_ROUTES * 4096
        );
        assert_eq!(routed.compiler_inferred_direct_long_deallocations, 0);
        assert_eq!(
            routed.compiler_inferred_direct_long_routes_without_trailer,
            MATCHED_ROUTES
        );
        assert_eq!(routed.ordinary_extent_mappings, 1);
        assert_eq!(routed.thp_extent_mappings, 0);
        assert_eq!(routed.thp_candidate_extent_mappings, 0);
        assert_eq!(routed.thp_advice_attempts, 0);
        assert_eq!(routed.compiler_inferred_density_promotion_attempts, 0);
        assert_eq!(routed.adaptive_site_count, 0);
        assert_eq!(routed.adaptive_training_allocations, 0);
        assert_eq!(routed.adaptive_live_trailers, 0);

        for ptr in pointers {
            assert!(unsafe { try_deallocate(ptr) });
        }
        let released = lifetime_hugepage_stats_snapshot();
        assert_eq!(released.current_retained_empty_extents, 1);
        assert!(!released.all_mappings_released);
        assert_eq!(
            released.compiler_inferred_direct_long_deallocations,
            MATCHED_ROUTES
        );
        assert_eq!(released.runtime_validated_objects, 0);
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    fn retention_test_extent(
        requested_backing: RequestedBacking,
        backing: ActualBacking,
    ) -> Extent {
        Extent {
            base: LIFETIME_HUGEPAGE_EXTENT_BYTES,
            slot_size: 4096,
            payload_capacity: 4096,
            region_capacity: LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES / 4096,
            live: 0,
            adaptive_confirmed_long_live: 0,
            bucket: 0,
            regions: [IdentityRegion::empty(); IDENTITY_REGIONS_PER_EXTENT],
            lifetime_class: LifetimePlacementClass::LongLived,
            cohort_epoch: 0,
            requested_backing: Some(requested_backing),
            backing,
            promoted_reuse_identity: None,
            promoted_reuse_cycle_full: false,
            collapse_pending: false,
            adaptive_trailer: false,
            available_prev: NONE,
            available_next: NONE,
            on_available_list: false,
            retained_empty_prev: NONE,
            retained_empty_next: NONE,
            on_retained_empty_list: false,
            next_free_descriptor: NONE,
        }
    }

    #[test]
    fn empty_extent_retention_requires_exact_policy_and_successful_backing() {
        let ordinary = retention_test_extent(RequestedBacking::Ordinary, ActualBacking::Ordinary);
        assert!(empty_extent_retention_eligible(
            LifetimeHugepagePolicy::SegregatedOrdinary,
            LifetimePageBackend::TransparentHugepage,
            &ordinary
        ));
        assert!(!empty_extent_retention_eligible(
            LifetimeHugepagePolicy::SegregatedOrdinary,
            LifetimePageBackend::ExplicitHugeTLB,
            &ordinary
        ));
        assert!(empty_extent_retention_eligible(
            LifetimeHugepagePolicy::CompilerInferredOrdinary,
            LifetimePageBackend::TransparentHugepage,
            &ordinary
        ));
        assert!(!empty_extent_retention_eligible(
            LifetimeHugepagePolicy::LongLivedHugepage,
            LifetimePageBackend::TransparentHugepage,
            &ordinary
        ));

        let mut adaptive_ordinary = ordinary;
        adaptive_ordinary.adaptive_trailer = true;
        adaptive_ordinary.lifetime_class = LifetimePlacementClass::Ephemeral;
        assert!(empty_extent_retention_eligible(
            LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary,
            LifetimePageBackend::TransparentHugepage,
            &adaptive_ordinary
        ));
        assert!(empty_extent_retention_eligible(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
            LifetimePageBackend::TransparentHugepage,
            &adaptive_ordinary
        ));
        assert!(!empty_extent_retention_eligible(
            LifetimeHugepagePolicy::SegregatedOrdinary,
            LifetimePageBackend::TransparentHugepage,
            &adaptive_ordinary
        ));

        let eager_thp = retention_test_extent(
            RequestedBacking::LargePage,
            ActualBacking::ThpAdvisedUnverified,
        );
        assert!(empty_extent_retention_eligible(
            LifetimeHugepagePolicy::LongLivedHugepage,
            LifetimePageBackend::TransparentHugepage,
            &eager_thp
        ));
        assert!(!empty_extent_retention_eligible(
            LifetimeHugepagePolicy::CompilerInferredHugepage,
            LifetimePageBackend::TransparentHugepage,
            &eager_thp
        ));

        let candidate = retention_test_extent(
            RequestedBacking::EpochCandidate,
            ActualBacking::ThpCandidateNoHugepage,
        );
        assert!(empty_extent_retention_eligible(
            LifetimeHugepagePolicy::CompilerInferredHugepage,
            LifetimePageBackend::TransparentHugepage,
            &candidate
        ));
        assert!(empty_extent_retention_eligible(
            LifetimeHugepagePolicy::CompilerDirectedHugepage,
            LifetimePageBackend::TransparentHugepage,
            &candidate
        ));
        let mut adaptive_candidate = candidate;
        adaptive_candidate.adaptive_trailer = true;
        assert!(empty_extent_retention_eligible(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
            LifetimePageBackend::TransparentHugepage,
            &adaptive_candidate
        ));
        assert!(!empty_extent_retention_eligible(
            LifetimeHugepagePolicy::CompilerInferredHugepage,
            LifetimePageBackend::TransparentHugepage,
            &adaptive_candidate
        ));
        adaptive_candidate.adaptive_confirmed_long_live = 1;
        assert!(!empty_extent_retention_eligible(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
            LifetimePageBackend::TransparentHugepage,
            &adaptive_candidate
        ));
        for promoted in [
            ActualBacking::ThpAdvisedUnverified,
            ActualBacking::ThpCollapseSucceededPointInTime,
        ] {
            let mut promoted_candidate =
                retention_test_extent(RequestedBacking::EpochCandidate, promoted);
            assert!(!empty_extent_retention_eligible(
                LifetimeHugepagePolicy::CompilerInferredHugepage,
                LifetimePageBackend::TransparentHugepage,
                &promoted_candidate
            ));
            promoted_candidate.adaptive_trailer = true;
            assert!(!empty_extent_retention_eligible(
                LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
                LifetimePageBackend::TransparentHugepage,
                &promoted_candidate
            ));
        }

        let mut keyed_promoted = retention_test_extent(
            RequestedBacking::EpochCandidate,
            ActualBacking::ThpCollapseSucceededPointInTime,
        );
        keyed_promoted.promoted_reuse_identity = Some(PromotedReuseIdentity {
            arena: ArenaIdentity {
                type_id: 1,
                module_id: 2,
                flags: 0,
                lifetime_hint: LIFETIME_HINT_LONG_LIVED,
                placement_hint: 0,
            },
            callsite: 3,
        });
        keyed_promoted.promoted_reuse_cycle_full = true;
        assert!(empty_extent_retention_eligible(
            LifetimeHugepagePolicy::CompilerDirectedHugepage,
            LifetimePageBackend::TransparentHugepage,
            &keyed_promoted
        ));
        keyed_promoted.promoted_reuse_cycle_full = false;
        assert!(!empty_extent_retention_eligible(
            LifetimeHugepagePolicy::CompilerDirectedHugepage,
            LifetimePageBackend::TransparentHugepage,
            &keyed_promoted
        ));

        for failed in [
            ActualBacking::ThpAdviceFailed,
            ActualBacking::ThpCandidateNoHugepageAdviceFailed,
            ActualBacking::Hugetlb,
            ActualBacking::HugetlbFallback,
        ] {
            let extent = retention_test_extent(RequestedBacking::LargePage, failed);
            assert!(!empty_extent_retention_eligible(
                LifetimeHugepagePolicy::LongLivedHugepage,
                LifetimePageBackend::TransparentHugepage,
                &extent
            ));
            let mut adaptive_extent =
                retention_test_extent(RequestedBacking::EpochCandidate, failed);
            adaptive_extent.adaptive_trailer = true;
            assert!(!empty_extent_retention_eligible(
                LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
                LifetimePageBackend::TransparentHugepage,
                &adaptive_extent
            ));
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn retained_empty_lru_bounds_reuses_and_trims_static_ordinary_extents() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure_with_backend(
            LifetimeHugepagePolicy::SegregatedOrdinary,
            LifetimePageBackend::TransparentHugepage,
        ));
        assert!(lifetime_hugepage_stats_reset());

        let layout = Layout::from_size_align(24 * 1024, 64).unwrap();
        let geometry = slot_geometry(layout, LifetimePlacementClass::LongLived, false).unwrap();
        let extent_capacity = geometry.region_capacity * IDENTITY_REGIONS_PER_EXTENT;
        let extent_count = LIFETIME_EMPTY_EXTENT_RETENTION_CAPACITY + 1;
        let allocation_count = extent_count * extent_capacity;
        let long_metadata = AllocationMetadata::for_type(0xE11E_0001)
            .with_module(0xE11E_1001)
            .with_callsite(0xE11E_2001)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);
        let mut pointers = Vec::with_capacity(allocation_count);
        for _ in 0..allocation_count {
            pointers.push(unsafe { try_allocate(layout, long_metadata) }.unwrap());
        }
        for ptr in pointers.drain(..) {
            assert!(unsafe { try_deallocate(ptr) });
        }

        let retained = lifetime_hugepage_stats_snapshot();
        assert_eq!(retained.ordinary_extent_mappings, extent_count);
        assert_eq!(
            retained.current_extents,
            LIFETIME_EMPTY_EXTENT_RETENTION_CAPACITY
        );
        assert_eq!(
            retained.current_retained_empty_extents,
            LIFETIME_EMPTY_EXTENT_RETENTION_CAPACITY
        );
        assert_eq!(
            retained.current_retained_empty_bytes,
            LIFETIME_EMPTY_EXTENT_RETENTION_CAPACITY * LIFETIME_HUGEPAGE_EXTENT_BYTES
        );
        assert_eq!(retained.retained_empty_extent_evictions, 1);
        assert_eq!(
            retained.peak_retained_empty_extents,
            LIFETIME_EMPTY_EXTENT_RETENTION_CAPACITY
        );
        assert_eq!(retained.extent_unmaps, 1);
        assert_eq!(LIVE_ARENA_OBJECTS.load(Ordering::Acquire), 0);
        assert!(!retained.all_mappings_released);

        for _ in 0..extent_capacity {
            pointers.push(unsafe { try_allocate(layout, long_metadata) }.unwrap());
        }
        let reused = lifetime_hugepage_stats_snapshot();
        assert_eq!(reused.ordinary_extent_mappings, extent_count);
        assert_eq!(reused.retained_empty_extent_reuse_hits, 1);
        assert_eq!(
            reused.current_retained_empty_extents,
            LIFETIME_EMPTY_EXTENT_RETENTION_CAPACITY - 1
        );
        for ptr in pointers.drain(..) {
            assert!(unsafe { try_deallocate(ptr) });
        }

        assert!(lifetime_hugepage_trim_retained_empty_extents());
        let trimmed = lifetime_hugepage_stats_snapshot();
        assert_eq!(trimmed.current_retained_empty_extents, 0);
        assert_eq!(
            trimmed.retained_empty_extent_trims,
            LIFETIME_EMPTY_EXTENT_RETENTION_CAPACITY
        );
        assert_eq!(trimmed.current_extents, 0);
        assert!(trimmed.all_mappings_released);
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn failed_unmap_keeps_detached_epoch_cohort_off_available_list() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure_with_backend(
            LifetimeHugepagePolicy::EpochCohortHugepage,
            LifetimePageBackend::TransparentHugepage,
        ));
        assert!(lifetime_hugepage_stats_reset());

        let layout = Layout::from_size_align(4096, 64).unwrap();
        let long_metadata = AllocationMetadata::for_type(0xE11E_0002)
            .with_module(0xE11E_1002)
            .with_callsite(0xE11E_2002)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);
        let ptr = unsafe { try_allocate(layout, long_metadata) }.unwrap();
        let base = (ptr as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
        assert_eq!(lifetime_hugepage_advance_epoch(), 2);

        TEST_UNMAP_FAILURE_BASE.store(base, Ordering::Release);
        let released = unsafe { try_deallocate(ptr) };
        TEST_UNMAP_FAILURE_BASE.store(0, Ordering::Release);
        assert!(released);

        {
            let mut state = ARENA.lock();
            let idx = state
                .lookup_extent(base)
                .expect("failed unmap keeps lookup provenance");
            assert_eq!(state.extents[idx].cohort_epoch, 1);
            assert!(!state.extents[idx].on_available_list);
            assert!(!state.extents[idx].on_retained_empty_list);
            assert_eq!(state.extent_unmap_failures, 1);
            assert!(unsafe { state.unmap_empty_extent(idx) });
        }
        assert!(lifetime_hugepage_stats_snapshot().all_mappings_released);
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn adaptive_retained_empty_extents_reuse_trailer_geometry_and_exact_backing() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        let layout = Layout::from_size_align(4096, 64).unwrap();
        let arms = [
            (
                LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary,
                AllocationMetadata::for_type(0xE11E_A001)
                    .with_module(0xE11E_A101)
                    .with_callsite(0xE11E_A201),
                false,
            ),
            (
                LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
                AllocationMetadata::for_type(0xE11E_A002)
                    .with_module(0xE11E_A102)
                    .with_callsite(0xE11E_A202)
                    .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED),
                true,
            ),
        ];

        for (policy, metadata, expect_thp) in arms {
            assert!(lifetime_hugepage_configure(policy));
            assert!(lifetime_hugepage_stats_reset());

            let first = unsafe { try_allocate(layout, metadata) }.unwrap();
            if expect_thp {
                ARENA
                    .lock()
                    .adaptive_note_pressure(ADAPTIVE_LONG_AGE_BYTES as usize);
            }
            assert!(unsafe { try_deallocate(first) });
            let retained = lifetime_hugepage_stats_snapshot();
            assert_eq!(retained.current_retained_empty_extents, 1);
            assert_eq!(retained.adaptive_live_trailers, 0);
            assert_eq!(retained.retained_empty_extent_insertions, 1);
            assert_eq!(retained.ordinary_extent_mappings, usize::from(!expect_thp));
            assert_eq!(retained.thp_extent_mappings, usize::from(expect_thp));
            assert_eq!(retained.nohugepage_advice_failures, 0);

            let second = unsafe { try_allocate(layout, metadata) }.unwrap();
            let reused = lifetime_hugepage_stats_snapshot();
            assert_eq!(
                reused.retained_empty_extent_reuse_hits, 1,
                "policy={policy:?} stats={reused:?}"
            );
            assert_eq!(reused.current_retained_empty_extents, 0);
            assert_eq!(reused.ordinary_extent_mappings, usize::from(!expect_thp));
            assert_eq!(reused.thp_extent_mappings, usize::from(expect_thp));
            if expect_thp {
                ARENA
                    .lock()
                    .adaptive_note_pressure(ADAPTIVE_LONG_AGE_BYTES as usize);
            }
            assert!(unsafe { try_deallocate(second) });

            assert!(lifetime_hugepage_trim_retained_empty_extents());
            let trimmed = lifetime_hugepage_stats_snapshot();
            assert_eq!(trimmed.current_retained_empty_extents, 0);
            assert_eq!(trimmed.retained_empty_extent_trims, 1);
            assert!(trimmed.all_mappings_released);
        }
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn retained_empty_extents_are_trimmed_before_backing_reconfiguration_and_reset() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        let layout = Layout::from_size_align(4096, 64).unwrap();
        let long_metadata = AllocationMetadata::for_type(0xE11E_0002)
            .with_module(0xE11E_1002)
            .with_callsite(0xE11E_2002)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);

        assert!(lifetime_hugepage_configure_with_backend(
            LifetimeHugepagePolicy::SegregatedOrdinary,
            LifetimePageBackend::TransparentHugepage,
        ));
        assert!(lifetime_hugepage_stats_reset());
        let ordinary = unsafe { try_allocate(layout, long_metadata) }.unwrap();
        assert!(unsafe { try_deallocate(ordinary) });
        assert_eq!(
            lifetime_hugepage_stats_snapshot().current_retained_empty_extents,
            1
        );

        assert!(lifetime_hugepage_configure_with_backend(
            LifetimeHugepagePolicy::LongLivedHugepage,
            LifetimePageBackend::TransparentHugepage,
        ));
        let reconfigured = lifetime_hugepage_stats_snapshot();
        assert_eq!(reconfigured.current_extents, 0);
        assert_eq!(reconfigured.retained_empty_extent_trims, 1);
        let thp = unsafe { try_allocate(layout, long_metadata) }.unwrap();
        assert!(unsafe { try_deallocate(thp) });
        let separate_backing = lifetime_hugepage_stats_snapshot();
        assert_eq!(separate_backing.ordinary_extent_mappings, 1);
        assert_eq!(separate_backing.thp_extent_mappings, 1);

        assert!(lifetime_hugepage_stats_reset());
        let reset = lifetime_hugepage_stats_snapshot();
        assert_eq!(reset.current_extents, 0);
        assert_eq!(reset.current_retained_empty_extents, 0);
        assert_eq!(reset.ordinary_extent_mappings, 0);
        assert_eq!(reset.thp_extent_mappings, 0);
        assert!(reset.all_mappings_released);
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
    }

    #[test]
    fn page_backend_changes_backing_without_changing_lifetime_selection() {
        assert_eq!(
            LifetimeHugepagePolicy::LongLivedHugepage.requested_backing(
                LifetimePlacementClass::Ephemeral,
                LifetimePageBackend::ExplicitHugeTLB,
            ),
            Some(RequestedBacking::Ordinary)
        );
        assert_eq!(
            LifetimeHugepagePolicy::LongLivedHugepage.requested_backing(
                LifetimePlacementClass::LongLived,
                LifetimePageBackend::ExplicitHugeTLB,
            ),
            Some(RequestedBacking::LargePage)
        );
        assert_eq!(
            LifetimeHugepagePolicy::LongLivedHugepage.requested_backing(
                LifetimePlacementClass::LongLived,
                LifetimePageBackend::TransparentHugepage,
            ),
            Some(RequestedBacking::LargePage)
        );
    }

    #[test]
    fn epoch_thp_delays_only_selected_largepage_placements() {
        assert_eq!(
            LifetimeHugepagePolicy::EpochCohortHugepage.requested_backing(
                LifetimePlacementClass::LongLived,
                LifetimePageBackend::TransparentHugepage,
            ),
            Some(RequestedBacking::EpochCandidate)
        );
        assert_eq!(
            LifetimeHugepagePolicy::EpochCohortHugepage.requested_backing(
                LifetimePlacementClass::Ephemeral,
                LifetimePageBackend::TransparentHugepage,
            ),
            Some(RequestedBacking::Ordinary)
        );
        assert_eq!(
            LifetimeHugepagePolicy::EpochCohortHugepage.requested_backing(
                LifetimePlacementClass::LongLived,
                LifetimePageBackend::ExplicitHugeTLB,
            ),
            Some(RequestedBacking::LargePage)
        );
    }

    #[test]
    fn advice_only_backing_never_counts_as_confirmed_largepage() {
        assert!(!ActualBacking::ThpAdvisedUnverified.is_large_page_confirmed());
        assert!(!ActualBacking::ThpAdviceFailed.is_large_page_confirmed());
        assert!(ActualBacking::ThpCollapseSucceededPointInTime.is_large_page_confirmed());
        assert!(ActualBacking::Hugetlb.is_large_page_confirmed());
    }

    #[test]
    fn epoch_promotion_skips_candidates_below_preregistered_density() {
        let extent = Extent {
            base: LIFETIME_HUGEPAGE_EXTENT_BYTES,
            slot_size: size_of::<usize>(),
            payload_capacity: size_of::<usize>(),
            region_capacity: LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES / size_of::<usize>(),
            live: 1,
            adaptive_confirmed_long_live: 0,
            bucket: 0,
            regions: [IdentityRegion::empty(); IDENTITY_REGIONS_PER_EXTENT],
            lifetime_class: LifetimePlacementClass::LongLived,
            cohort_epoch: 1,
            requested_backing: Some(RequestedBacking::EpochCandidate),
            backing: ActualBacking::ThpCandidateNoHugepage,
            promoted_reuse_identity: None,
            promoted_reuse_cycle_full: false,
            collapse_pending: false,
            adaptive_trailer: false,
            available_prev: NONE,
            available_next: NONE,
            on_available_list: false,
            retained_empty_prev: NONE,
            retained_empty_next: NONE,
            on_retained_empty_list: false,
            next_free_descriptor: NONE,
        };

        assert_eq!(
            thp_promotion_eligibility(&extent, 2),
            ThpPromotionEligibility::LowOccupancy
        );

        let mut dense_survivor = extent;
        dense_survivor.live = THP_PROMOTION_MIN_LIVE_BYTES / dense_survivor.slot_size;
        assert_eq!(
            thp_promotion_eligibility(&dense_survivor, 2),
            ThpPromotionEligibility::Eligible
        );
        dense_survivor.cohort_epoch = 2;
        assert_eq!(
            thp_promotion_eligibility(&dense_survivor, 2),
            ThpPromotionEligibility::Ineligible
        );
    }

    #[test]
    fn adaptive_thp_promotion_counts_only_confirmed_long_slot_bytes() {
        let slot_size = 4096;
        let threshold_slots = ADAPTIVE_THP_PROMOTION_MIN_CONFIRMED_LIVE_BYTES / slot_size;
        let mut extent = Extent {
            base: LIFETIME_HUGEPAGE_EXTENT_BYTES,
            slot_size,
            payload_capacity: slot_size - size_of::<AdaptiveSlotTrailer>(),
            region_capacity: LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES / slot_size,
            live: threshold_slots,
            adaptive_confirmed_long_live: 0,
            bucket: 0,
            regions: [IdentityRegion::empty(); IDENTITY_REGIONS_PER_EXTENT],
            lifetime_class: LifetimePlacementClass::LongLived,
            cohort_epoch: 0,
            requested_backing: Some(RequestedBacking::EpochCandidate),
            backing: ActualBacking::ThpCandidateNoHugepage,
            promoted_reuse_identity: None,
            promoted_reuse_cycle_full: false,
            collapse_pending: false,
            adaptive_trailer: true,
            available_prev: NONE,
            available_next: NONE,
            on_available_list: false,
            retained_empty_prev: NONE,
            retained_empty_next: NONE,
            on_retained_empty_list: false,
            next_free_descriptor: NONE,
        };

        assert_eq!(
            adaptive_thp_promotion_eligibility(&extent),
            ThpPromotionEligibility::LowOccupancy
        );
        extent.adaptive_confirmed_long_live = threshold_slots - 1;
        assert_eq!(
            adaptive_thp_promotion_eligibility(&extent),
            ThpPromotionEligibility::LowOccupancy
        );
        extent.adaptive_confirmed_long_live = threshold_slots;
        assert_eq!(
            adaptive_thp_promotion_eligibility(&extent),
            ThpPromotionEligibility::Eligible
        );
        extent.backing = ActualBacking::ThpAdvisedUnverified;
        assert_eq!(
            adaptive_thp_promotion_eligibility(&extent),
            ThpPromotionEligibility::Ineligible
        );
    }

    #[test]
    fn adaptive_fullness_selection_packs_denser_compatible_extents() {
        assert!(fullness_candidate_is_better(12, false, Some((3, true))));
        assert!(fullness_candidate_is_better(12, true, Some((12, false))));
        assert!(!fullness_candidate_is_better(3, true, Some((12, false))));
        assert!(!fullness_candidate_is_better(12, false, Some((12, true))));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn adaptive_fullness_search_chooses_denser_extent_over_list_head() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage
        ));
        assert!(lifetime_hugepage_stats_reset());

        let layout = Layout::from_size_align(4096, 64).unwrap();
        let geometry = slot_geometry(layout, LifetimePlacementClass::LongLived, true).unwrap();
        let extent_capacity = geometry.region_capacity * IDENTITY_REGIONS_PER_EXTENT;
        let second_extent_live = extent_capacity * 3 / 4;
        let sparse_survivors = 8;
        let metadata = AllocationMetadata::for_type(0xADAA_1909)
            .with_module(0xA110_C199)
            .with_callsite(0x51_AA)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);

        for _ in 0..ADAPTIVE_PRIOR_VALIDATION_SAMPLES {
            let training_ptr = unsafe { try_allocate(layout, metadata) }
                .expect("a pending Long prior should remain tracked");
            ARENA
                .lock()
                .adaptive_note_pressure(ADAPTIVE_LONG_AGE_BYTES as usize);
            assert!(unsafe { try_deallocate(training_ptr) });
        }

        let mut pointers = Vec::with_capacity(extent_capacity + second_extent_live);
        for _ in 0..(extent_capacity + second_extent_live) {
            pointers.push(
                unsafe { try_allocate(layout, metadata) }
                    .expect("confirmed Long slots should create two candidate extents"),
            );
        }
        let first_base = pointers[0] as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
        let second_base =
            pointers[extent_capacity] as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
        assert_ne!(first_base, second_base);

        ARENA
            .lock()
            .adaptive_note_pressure(ADAPTIVE_LONG_AGE_BYTES as usize);
        for pointer in &pointers[..extent_capacity - sparse_survivors] {
            assert!(unsafe { try_deallocate(*pointer) });
        }

        let selected = unsafe { try_allocate(layout, metadata) }
            .expect("the fullness search should find reusable storage");
        let selected_base = selected as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
        assert_eq!(selected_base, second_base);

        assert!(unsafe { try_deallocate(selected) });
        for pointer in &pointers[extent_capacity - sparse_survivors..] {
            assert!(unsafe { try_deallocate(*pointer) });
        }
        assert!(lifetime_hugepage_trim_retained_empty_extents());
        assert!(lifetime_hugepage_stats_snapshot().all_mappings_released);
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[test]
    fn adaptive_corrupt_trailer_resets_confirmed_long_promotion_credit() {
        assert_eq!(confirmed_long_live_after_deallocation(12, Some(true)), 11);
        assert_eq!(confirmed_long_live_after_deallocation(12, Some(false)), 12);
        assert_eq!(confirmed_long_live_after_deallocation(12, None), 0);
    }

    fn adaptive_test_key(callsite: u64, type_id: u64) -> AdaptiveSiteKey {
        AdaptiveSiteKey {
            callsite,
            type_id,
            module_id: 7,
            flags: 1,
            placement_hint: 0,
            payload_capacity: 64,
            align: 8,
        }
    }

    #[test]
    fn adaptive_thp_long_placements_share_density_gated_candidates() {
        assert_eq!(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage.requested_backing(
                AdaptivePrediction::Cold.placement_class(),
                LifetimePageBackend::TransparentHugepage,
            ),
            Some(RequestedBacking::Ordinary)
        );
        assert_eq!(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage.requested_backing(
                AdaptivePrediction::Short.placement_class(),
                LifetimePageBackend::TransparentHugepage,
            ),
            Some(RequestedBacking::Ordinary)
        );
        assert_eq!(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage.requested_backing(
                AdaptivePrediction::Long.placement_class(),
                LifetimePageBackend::TransparentHugepage,
            ),
            Some(RequestedBacking::EpochCandidate)
        );
        assert_eq!(
            requested_backing_for_allocation(
                LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
                LifetimePlacementClass::LongLived,
                LifetimePageBackend::TransparentHugepage,
                false,
            ),
            Some(RequestedBacking::EpochCandidate)
        );
        assert_eq!(
            requested_backing_for_allocation(
                LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
                LifetimePlacementClass::LongLived,
                LifetimePageBackend::TransparentHugepage,
                true,
            ),
            Some(RequestedBacking::EpochCandidate)
        );
        for prediction in [
            AdaptivePrediction::Cold,
            AdaptivePrediction::Short,
            AdaptivePrediction::Long,
        ] {
            assert_eq!(
                LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary.requested_backing(
                    prediction.placement_class(),
                    LifetimePageBackend::TransparentHugepage,
                ),
                Some(RequestedBacking::Ordinary)
            );
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn adaptive_runtime_ordinary_reuses_force_track_pressure_and_reset() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary
        ));
        assert_eq!(
            lifetime_hugepage_backend(),
            LifetimePageBackend::TransparentHugepage
        );
        assert!(lifetime_hugepage_stats_reset());
        assert!(lifetime_hugepage_adaptive_site_recording_enable());
        assert!(lifetime_hugepage_adaptive_site_force_track_all_enable(
            8, 4096
        ));

        lifetime_hugepage_force_track_note_raw_allocation(128);
        assert_eq!(
            lifetime_hugepage_stats_snapshot().adaptive_force_track_all_raw_pressure_allocations,
            1
        );
        assert!(lifetime_hugepage_stats_reset());
        let reset = lifetime_hugepage_stats_snapshot();
        assert!(reset.adaptive_force_track_all);
        assert_eq!(reset.adaptive_force_track_all_raw_pressure_allocations, 0);

        lifetime_hugepage_force_track_note_raw_allocation(128);
        let layout = Layout::from_size_align(64, 8).unwrap();
        let metadata = AllocationMetadata::for_type(0xADAA_0008)
            .with_module(0xA110_C008)
            .with_callsite(0x51_8E)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);
        let ptr = unsafe { try_allocate(layout, metadata) }
            .expect("adaptive ordinary should use the adaptive allocation path");
        let routed = lifetime_hugepage_stats_snapshot();
        assert_eq!(
            routed.policy,
            LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary
        );
        assert_eq!(routed.adaptive_site_count, 1);
        assert_eq!(routed.adaptive_live_trailers, 1);
        assert_eq!(routed.ordinary_extent_mappings, 1);
        assert_eq!(routed.thp_extent_mappings, 0);
        assert_eq!(routed.thp_advice_attempts, 0);
        assert_eq!(routed.adaptive_force_track_all_raw_pressure_allocations, 1);

        assert!(unsafe { try_deallocate(ptr) });
        assert!(lifetime_hugepage_adaptive_site_force_track_all_disable());
        assert!(lifetime_hugepage_adaptive_site_recording_disable());
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn adaptive_provisional_long_prior_segregates_without_premature_thp() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage
        ));
        assert!(lifetime_hugepage_stats_reset());

        let layout = Layout::from_size_align(64, 8).unwrap();
        let base = AllocationMetadata::for_type(0xADAA_1008)
            .with_module(0xA110_C108)
            .with_callsite(0x51_9E);
        let cold_ptr = unsafe { try_allocate(layout, base) }
            .expect("Cold training should use the adaptive ordinary path");
        let provisional_long_ptr = unsafe {
            try_allocate(
                layout,
                base.with_callsite(0x51_AE)
                    .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED),
            )
        }
        .expect("a Long prior should use the adaptive Long cohort");

        let routed = lifetime_hugepage_stats_snapshot();
        assert_eq!(routed.ordinary_extent_mappings, 1);
        assert_eq!(routed.thp_extent_mappings, 1);
        assert_eq!(routed.thp_candidate_extent_mappings, 1);
        assert_eq!(routed.thp_advice_attempts, 0);
        assert_eq!(routed.live_ephemeral_objects, 1);
        assert_eq!(routed.live_long_lived_objects, 1);

        assert!(unsafe { try_deallocate(cold_ptr) });
        assert!(unsafe { try_deallocate(provisional_long_ptr) });
        assert!(lifetime_hugepage_trim_retained_empty_extents());
        assert!(lifetime_hugepage_stats_snapshot().all_mappings_released);
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn adaptive_confirmed_long_reuses_provisional_candidate_without_early_promotion() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage
        ));
        assert!(lifetime_hugepage_stats_reset());

        let layout = Layout::from_size_align(64, 8).unwrap();
        let metadata = AllocationMetadata::for_type(0xADAA_1808)
            .with_module(0xA110_C188)
            .with_callsite(0x51_A8)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);
        let provisional_ptr = unsafe { try_allocate(layout, metadata) }
            .expect("a provisional Long prior should route on ordinary backing");
        let provisional_base = (provisional_ptr as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);

        for _ in 0..ADAPTIVE_PRIOR_VALIDATION_SAMPLES {
            let training_ptr = unsafe { try_allocate(layout, metadata) }
                .expect("a pending Long prior should remain tracked");
            ARENA
                .lock()
                .adaptive_note_pressure(ADAPTIVE_LONG_AGE_BYTES as usize);
            assert!(unsafe { try_deallocate(training_ptr) });
        }

        {
            let state = ARENA.lock();
            let provisional_idx = state
                .lookup_extent(provisional_base)
                .expect("the provisional ordinary extent must remain live");
            assert_eq!(
                state.extents[provisional_idx].requested_backing,
                Some(RequestedBacking::EpochCandidate)
            );
            assert_eq!(state.extents[provisional_idx].live, 1);
            assert_eq!(
                state.extents[provisional_idx].adaptive_confirmed_long_live,
                0
            );
        }
        let confirmed = lifetime_hugepage_stats_snapshot();
        assert_eq!(confirmed.adaptive_long_sites, 1);
        assert_eq!(confirmed.current_extents, 1);

        let confirmed_ptr = unsafe { try_allocate(layout, metadata) }
            .expect("a confirmed Long site should route on THP-requested backing");
        let confirmed_base = (confirmed_ptr as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
        {
            let state = ARENA.lock();
            let provisional_idx = state
                .lookup_extent(provisional_base)
                .expect("the provisional ordinary extent must remain live");
            let confirmed_idx = state
                .lookup_extent(confirmed_base)
                .expect("the shared THP candidate extent must be indexed");
            assert_eq!(confirmed_idx, provisional_idx);
            assert_eq!(confirmed_base, provisional_base);
            assert_eq!(
                state.extents[provisional_idx].requested_backing,
                Some(RequestedBacking::EpochCandidate)
            );
            assert_eq!(state.extents[confirmed_idx].adaptive_confirmed_long_live, 1);
            assert!(matches!(
                state.extents[confirmed_idx].backing,
                ActualBacking::ThpCandidateNoHugepage
                    | ActualBacking::ThpCandidateNoHugepageAdviceFailed
            ));
        }

        let routed = lifetime_hugepage_stats_snapshot();
        assert_eq!(routed.ordinary_extent_mappings, 0);
        assert_eq!(routed.thp_extent_mappings, 1);
        assert_eq!(routed.thp_candidate_extent_mappings, 1);
        assert_eq!(routed.thp_advice_attempts, 0);
        assert_eq!(routed.current_extents, 1);

        assert!(unsafe { try_deallocate(confirmed_ptr) });
        assert!(unsafe { try_deallocate(provisional_ptr) });
        assert!(lifetime_hugepage_trim_retained_empty_extents());
        assert!(lifetime_hugepage_stats_snapshot().all_mappings_released);
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn adaptive_thp_promotes_at_confirmed_long_density_threshold() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage
        ));
        assert!(lifetime_hugepage_stats_reset());

        let layout = Layout::from_size_align(4096, 64).unwrap();
        let geometry = slot_geometry(layout, LifetimePlacementClass::LongLived, true).unwrap();
        let threshold_slots =
            (ADAPTIVE_THP_PROMOTION_MIN_CONFIRMED_LIVE_BYTES + geometry.slot_size - 1)
                / geometry.slot_size;
        let metadata = AllocationMetadata::for_type(0xADAA_1908)
            .with_module(0xA110_C198)
            .with_callsite(0x51_A9)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);
        let provisional_ptr = unsafe { try_allocate(layout, metadata) }
            .expect("a provisional Long prior should enter a THP candidate");

        for _ in 0..ADAPTIVE_PRIOR_VALIDATION_SAMPLES {
            let training_ptr = unsafe { try_allocate(layout, metadata) }
                .expect("a pending Long prior should remain tracked");
            ARENA
                .lock()
                .adaptive_note_pressure(ADAPTIVE_LONG_AGE_BYTES as usize);
            assert!(unsafe { try_deallocate(training_ptr) });
        }

        let mut confirmed = Vec::with_capacity(threshold_slots);
        for _ in 0..(threshold_slots - 1) {
            confirmed.push(
                unsafe { try_allocate(layout, metadata) }
                    .expect("confirmed Long slots should fill the shared candidate"),
            );
        }
        assert_eq!(lifetime_hugepage_stats_snapshot().thp_advice_attempts, 0);

        confirmed.push(
            unsafe { try_allocate(layout, metadata) }
                .expect("the density-crossing Long slot should remain routable"),
        );
        let promoted = lifetime_hugepage_stats_snapshot();
        assert_eq!(promoted.current_extents, 1);
        assert_eq!(promoted.thp_candidate_extent_mappings, 1);
        assert_eq!(promoted.thp_advice_attempts, 1);
        assert_eq!(
            promoted.thp_advice_successes + promoted.thp_advice_errors,
            1
        );
        if promoted.thp_advice_successes == 1 {
            assert_eq!(promoted.thp_collapse_attempts, 1);
            assert_eq!(
                promoted.thp_collapse_successes + promoted.thp_collapse_errors,
                1
            );
            assert_eq!(
                promoted.current_thp_collapse_confirmed_extents,
                promoted.thp_collapse_successes
            );
        }

        for ptr in confirmed {
            assert!(unsafe { try_deallocate(ptr) });
        }
        assert!(unsafe { try_deallocate(provisional_ptr) });
        let released = lifetime_hugepage_stats_snapshot();
        assert!(released.all_mappings_released);
        assert_eq!(released.current_thp_collapse_confirmed_extents, 0);
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn adaptive_live_survival_promotes_before_drop_without_retroactive_density_credit() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage
        ));
        assert!(lifetime_hugepage_stats_reset());
        assert!(lifetime_hugepage_adaptive_site_recording_enable());

        let layout = Layout::from_size_align(4096, 64).unwrap();
        let metadata = AllocationMetadata::for_type(0xADAA_3001)
            .with_module(0xA110_C301)
            .with_callsite(0x51_C1)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);
        let mut survivors = [core::ptr::null_mut(); ADAPTIVE_PRIOR_VALIDATION_SAMPLES as usize];
        for ptr in &mut survivors {
            *ptr = unsafe { try_allocate(layout, metadata) }
                .expect("a pending Long prior should be sampled");
        }
        let candidate_base = survivors[0] as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
        {
            let state = ARENA.lock();
            let idx = state.lookup_extent(candidate_base).unwrap();
            assert_eq!(state.extents[idx].adaptive_confirmed_long_live, 0);
        }

        ARENA
            .lock()
            .adaptive_note_pressure(ADAPTIVE_LONG_AGE_BYTES as usize);
        let promoted = lifetime_hugepage_stats_snapshot();
        assert_eq!(promoted.adaptive_live_survival_registrations, 4);
        assert_eq!(promoted.adaptive_live_survival_registration_bypasses, 0);
        assert_eq!(promoted.adaptive_live_survival_scans, 1);
        assert_eq!(promoted.adaptive_live_survival_slots_examined, 4);
        assert_eq!(promoted.adaptive_live_survival_observations, 4);
        assert_eq!(promoted.adaptive_live_survival_promotions, 1);
        assert_eq!(promoted.adaptive_long_observations, 4);
        assert_eq!(promoted.adaptive_long_sites, 1);
        assert_eq!(promoted.adaptive_long_routed_allocations, 0);
        assert_eq!(promoted.runtime_validated_objects, 4);
        {
            let state = ARENA.lock();
            let idx = state.lookup_extent(candidate_base).unwrap();
            assert_eq!(state.extents[idx].adaptive_confirmed_long_live, 0);
        }

        let mut observations = [LifetimeAdaptiveSiteSnapshot::empty(); 1];
        assert_eq!(
            lifetime_hugepage_adaptive_site_snapshot(&mut observations),
            1
        );
        assert_eq!(observations[0].live_survival_observations, 4);
        assert_eq!(observations[0].current_live_survival_inflight, 4);
        assert_eq!(observations[0].completed_outcomes, 0);
        assert_eq!(observations[0].tracked_inflight, 4);
        assert_eq!(
            observations[0].latest_prediction,
            AdaptivePrediction::Long as u8
        );

        let future = unsafe { try_allocate(layout, metadata) }
            .expect("the next allocation should use the learned Long route");
        assert_eq!(
            future as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1),
            candidate_base
        );
        let future_routed = lifetime_hugepage_stats_snapshot();
        assert_eq!(future_routed.adaptive_long_routed_allocations, 1);
        {
            let state = ARENA.lock();
            let idx = state.lookup_extent(candidate_base).unwrap();
            assert_eq!(state.extents[idx].adaptive_confirmed_long_live, 1);
        }

        for ptr in survivors {
            assert!(unsafe { try_deallocate(ptr) });
        }
        let after_completed_survivors = lifetime_hugepage_stats_snapshot();
        assert_eq!(after_completed_survivors.adaptive_long_observations, 4);
        assert_eq!(after_completed_survivors.runtime_validated_objects, 4);
        assert_eq!(
            lifetime_hugepage_adaptive_site_snapshot(&mut observations),
            1
        );
        assert_eq!(observations[0].long_outcomes, 4);
        assert_eq!(observations[0].current_live_survival_inflight, 0);
        assert_eq!(observations[0].tracked_inflight, 1);
        assert_eq!(observations[0].completed_outcomes, 4);
        assert_eq!(
            observations[0].tracked_allocations,
            observations[0].completed_outcomes + observations[0].tracked_inflight
        );

        assert!(unsafe { try_deallocate(future) });
        assert!(lifetime_hugepage_adaptive_site_recording_disable());
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn adaptive_live_survival_abstains_below_long_threshold() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary
        ));
        assert!(lifetime_hugepage_stats_reset());
        assert!(lifetime_hugepage_adaptive_site_recording_enable());

        let layout = Layout::from_size_align(4096, 64).unwrap();
        let metadata = AllocationMetadata::for_type(0xADAA_3002)
            .with_module(0xA110_C302)
            .with_callsite(0x51_C2);
        let ptr = unsafe { try_allocate(layout, metadata) }.unwrap();
        ARENA
            .lock()
            .adaptive_note_pressure((ADAPTIVE_LONG_AGE_BYTES - 1) as usize);
        let before_drop = lifetime_hugepage_stats_snapshot();
        assert_eq!(before_drop.adaptive_live_survival_scans, 0);
        assert_eq!(before_drop.adaptive_live_survival_observations, 0);
        assert_eq!(before_drop.adaptive_long_sites, 0);

        assert!(unsafe { try_deallocate(ptr) });
        let after_drop = lifetime_hugepage_stats_snapshot();
        assert_eq!(after_drop.adaptive_long_observations, 0);
        assert_eq!(after_drop.adaptive_censored_observations, 1);
        let mut observations = [LifetimeAdaptiveSiteSnapshot::empty(); 1];
        assert_eq!(
            lifetime_hugepage_adaptive_site_snapshot(&mut observations),
            1
        );
        assert_eq!(observations[0].live_survival_observations, 0);
        assert_eq!(observations[0].censored_outcomes, 1);
        assert_eq!(observations[0].tracked_inflight, 0);

        assert!(lifetime_hugepage_adaptive_site_recording_disable());
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn adaptive_corrupt_trailer_removes_live_sample_before_slot_reuse() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary
        ));
        assert!(lifetime_hugepage_stats_reset());

        let layout = Layout::from_size_align(4096, 64).unwrap();
        let metadata = AllocationMetadata::for_type(0xADAA_3003)
            .with_module(0xA110_C303)
            .with_callsite(0x51_C3);
        let ptr = unsafe { try_allocate(layout, metadata) }.unwrap();
        let payload_capacity = {
            let state = ARENA.lock();
            let base = ptr as usize & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
            let idx = state.lookup_extent(base).unwrap();
            state.extents[idx].payload_capacity
        };
        unsafe {
            let trailer_ptr = ptr.add(payload_capacity).cast::<AdaptiveSlotTrailer>();
            let mut trailer = trailer_ptr.read();
            trailer.site_index = u32::MAX;
            trailer.checksum ^= 1;
            trailer_ptr.write(trailer);
        }
        assert!(unsafe { try_deallocate(ptr) });
        ARENA
            .lock()
            .adaptive_note_pressure(ADAPTIVE_LONG_AGE_BYTES as usize);
        let stats = lifetime_hugepage_stats_snapshot();
        assert_eq!(stats.adaptive_trailer_corruptions, 1);
        assert_eq!(stats.adaptive_live_survival_observations, 0);
        assert_eq!(stats.adaptive_long_observations, 0);

        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn adaptive_pending_prior_stays_out_of_learned_prediction_telemetry() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::AdaptiveRuntimeHugepage
        ));
        assert!(lifetime_hugepage_stats_reset());
        assert!(lifetime_hugepage_adaptive_site_recording_enable());

        let layout = Layout::from_size_align(64, 8).unwrap();
        let metadata = AllocationMetadata::for_type(0xADAA_2008)
            .with_module(0xA110_C208)
            .with_callsite(0x51_BE)
            .with_lifetime_hint(LIFETIME_HINT_LONG_LIVED);
        for _ in 0..ADAPTIVE_PRIOR_VALIDATION_SAMPLES {
            let ptr = unsafe { try_allocate(layout, metadata) }
                .expect("a pending Long prior should be tracked");
            ARENA
                .lock()
                .adaptive_note_pressure(ADAPTIVE_LONG_AGE_BYTES as usize);
            assert!(unsafe { try_deallocate(ptr) });
        }

        let confirmed = lifetime_hugepage_stats_snapshot();
        assert_eq!(confirmed.adaptive_cold_sites, 0);
        assert_eq!(confirmed.adaptive_long_sites, 1);
        assert_eq!(confirmed.adaptive_training_allocations, 4);
        assert_eq!(confirmed.adaptive_long_routed_allocations, 0);
        assert_eq!(confirmed.predictor_true_positive_objects, 0);
        assert_eq!(confirmed.predictor_false_negative_objects, 4);
        assert_eq!(confirmed.adaptive_predictor_true_positive_objects, 0);
        assert_eq!(confirmed.adaptive_predictor_false_negative_objects, 0);
        assert_eq!(confirmed.adaptive_static_hint_true_positive_objects, 4);

        let mut observations = [LifetimeAdaptiveSiteSnapshot::empty(); 1];
        assert_eq!(
            lifetime_hugepage_adaptive_site_snapshot(&mut observations),
            1
        );
        assert_eq!(
            observations[0].latest_prediction,
            AdaptivePrediction::Long as u8
        );
        assert_eq!(
            observations[0].latest_static_prior,
            LifetimePlacementClass::LongLived as u8
        );

        let confirmed_ptr = unsafe { try_allocate(layout, metadata) }
            .expect("a confirmed Long site should keep using the adaptive path");
        let learned = lifetime_hugepage_stats_snapshot();
        assert_eq!(learned.adaptive_long_routed_allocations, 1);
        assert_eq!(
            lifetime_hugepage_adaptive_site_snapshot(&mut observations),
            1
        );
        assert_eq!(
            observations[0].latest_prediction,
            AdaptivePrediction::Long as u8
        );

        assert!(unsafe { try_deallocate(confirmed_ptr) });
        assert!(lifetime_hugepage_adaptive_site_recording_disable());
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[test]
    fn adaptive_concrete_priors_confirm_after_four_decisive_outcomes() {
        for (offset, prior, actual_long, expected) in [
            (
                0,
                LifetimePlacementClass::Ephemeral,
                false,
                AdaptivePrediction::Short,
            ),
            (
                1,
                LifetimePlacementClass::LongLived,
                true,
                AdaptivePrediction::Long,
            ),
        ] {
            let mut site =
                AdaptiveSiteState::new(adaptive_test_key(21 + offset, 31 + offset), prior);
            assert_eq!(site.routing_prediction(), expected);
            assert!(!site.long_placement_confirmed());
            for _ in 0..3 {
                assert_eq!(site.observe(Some(actual_long)), AdaptiveTransition::None);
                assert_eq!(site.prediction, AdaptivePrediction::Cold);
                assert_eq!(site.routing_prediction(), expected);
            }
            assert_eq!(
                site.observe(Some(actual_long)),
                if actual_long {
                    AdaptiveTransition::PromotedLong
                } else {
                    AdaptiveTransition::PromotedShort
                }
            );
            assert_eq!(site.prediction, expected);
            assert_eq!(site.routing_prediction(), expected);
            assert_eq!(site.long_placement_confirmed(), actual_long);
        }
    }

    #[test]
    fn adaptive_provisional_and_confirmed_long_share_thp_candidate_backing() {
        let mut site =
            AdaptiveSiteState::new(adaptive_test_key(23, 33), LifetimePlacementClass::LongLived);
        assert_eq!(site.routing_prediction(), AdaptivePrediction::Long);
        let provisional_class = adaptive_placement_class(site.routing_prediction(), false);
        assert_eq!(provisional_class, LifetimePlacementClass::LongLived);
        assert_eq!(
            requested_backing_for_allocation(
                LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
                provisional_class,
                LifetimePageBackend::TransparentHugepage,
                site.long_placement_confirmed(),
            ),
            Some(RequestedBacking::EpochCandidate)
        );
        for _ in 0..4 {
            let _ = site.observe(Some(true));
        }
        assert!(site.long_placement_confirmed());
        assert_eq!(
            requested_backing_for_allocation(
                LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
                adaptive_placement_class(site.routing_prediction(), false),
                LifetimePageBackend::TransparentHugepage,
                site.long_placement_confirmed(),
            ),
            Some(RequestedBacking::EpochCandidate)
        );
    }

    #[test]
    fn adaptive_force_track_all_uses_ordinary_placement_and_hard_guards() {
        assert_eq!(
            adaptive_placement_class(AdaptivePrediction::Long, true),
            LifetimePlacementClass::Ephemeral
        );
        assert_eq!(
            adaptive_placement_class(AdaptivePrediction::Long, false),
            LifetimePlacementClass::LongLived
        );

        let mut guard = AdaptiveForceTrackAllState::disabled();
        assert!(!guard.enable(0, 128));
        assert!(!guard.enable(2, 0));
        assert!(guard.enable(2, 128));
        assert!(guard.allows(64));
        guard.note_admitted(64);
        assert!(guard.allows(64));
        guard.note_admitted(64);
        assert!(!guard.allows(1));
        guard.note_guard_bypass();
        assert_eq!(guard.admitted_allocations, 2);
        assert_eq!(guard.admitted_requested_bytes, 128);
        assert_eq!(guard.guard_bypasses, 1);

        guard.reset_counters();
        assert!(guard.enabled);
        assert_eq!(guard.maximum_allocations, 2);
        assert_eq!(guard.maximum_requested_bytes, 128);
        assert_eq!(guard.admitted_allocations, 0);
        assert_eq!(guard.guard_bypasses, 0);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn adaptive_live_survival_due_scan_abstains_from_stale_unmapped_pointer() {
        let _semantic_guard = crate::alloc_api::type_isolation::semantic_test_guard();
        let _guard = COMPILER_INFERRED_TEST_LOCK.lock();
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::AdaptiveRuntimeOrdinary
        ));
        assert!(lifetime_hugepage_stats_reset());
        {
            let mut state = ARENA.lock();
            let site_idx = state
                .adaptive_site_slot(adaptive_test_key(24, 34), LifetimePlacementClass::Unknown)
                .unwrap();
            state.adaptive_live_survivor_register(
                site_idx,
                LIFETIME_HUGEPAGE_EXTENT_BYTES as *mut u8,
                0,
                AdaptivePrediction::Cold,
            );
            state.adaptive_note_pressure(ADAPTIVE_LONG_AGE_BYTES as usize);

            assert_eq!(state.adaptive_live_survival_scans, 1);
            assert_eq!(state.adaptive_live_survival_slots_examined, 1);
            assert_eq!(state.adaptive_live_survival_stale_abstentions, 1);
            assert_eq!(state.adaptive_live_survival_observations, 0);
            assert!(state.adaptive_live_survivors[site_idx].is_empty());
            assert_eq!(
                state.adaptive_live_survivor_active_sites[site_idx / u64::BITS as usize],
                0
            );
            assert_eq!(state.adaptive_live_survivor_next_due_pressure, u64::MAX);
        }
        assert!(lifetime_hugepage_configure(
            LifetimeHugepagePolicy::Disabled
        ));
        assert!(lifetime_hugepage_stats_reset());
    }

    #[test]
    fn adaptive_age_thresholds_are_exact_and_marker_free() {
        assert_eq!(adaptive_lifetime_outcome(0), Some(false));
        assert_eq!(
            adaptive_lifetime_outcome(ADAPTIVE_EPOCH_BYTES - 1),
            Some(false)
        );
        assert_eq!(adaptive_lifetime_outcome(ADAPTIVE_EPOCH_BYTES), None);
        assert_eq!(adaptive_lifetime_outcome(ADAPTIVE_LONG_AGE_BYTES - 1), None);
        assert_eq!(
            adaptive_lifetime_outcome(ADAPTIVE_LONG_AGE_BYTES),
            Some(true)
        );
    }

    #[test]
    fn adaptive_site_requires_runtime_evidence_and_learns_both_classes() {
        let key = adaptive_test_key(11, 21);
        let mut long = AdaptiveSiteState::new(key, LifetimePlacementClass::Unknown);
        for _ in 0..(ADAPTIVE_MIN_DECISIVE_SAMPLES - 1) {
            assert_eq!(long.observe(Some(true)), AdaptiveTransition::None);
            assert_eq!(long.prediction, AdaptivePrediction::Cold);
        }
        assert_eq!(long.observe(Some(true)), AdaptiveTransition::PromotedLong);
        assert_eq!(long.prediction, AdaptivePrediction::Long);

        let mut short = AdaptiveSiteState::new(key, LifetimePlacementClass::Unknown);
        for _ in 0..(ADAPTIVE_MIN_DECISIVE_SAMPLES - 1) {
            assert_eq!(short.observe(Some(false)), AdaptiveTransition::None);
        }
        assert_eq!(
            short.observe(Some(false)),
            AdaptiveTransition::PromotedShort
        );
        assert_eq!(short.prediction, AdaptivePrediction::Short);
    }

    #[test]
    fn adaptive_censored_samples_do_not_vote_and_hysteresis_delays_demotion() {
        let mut site =
            AdaptiveSiteState::new(adaptive_test_key(12, 22), LifetimePlacementClass::Unknown);
        for _ in 0..32 {
            assert_eq!(site.observe(None), AdaptiveTransition::None);
        }
        assert_eq!(site.decisive_samples, 0);
        assert_eq!(site.censored_samples, 32);
        assert_eq!((site.short_votes, site.long_votes), (1, 1));
        for _ in 0..(ADAPTIVE_SHORT_SAMPLE_INTERVAL - 1) {
            assert!(!site.should_track_allocation());
        }
        assert!(site.should_track_allocation());
        for _ in 0..ADAPTIVE_MIN_DECISIVE_SAMPLES {
            let _ = site.observe(Some(true));
        }
        assert_eq!(site.prediction, AdaptivePrediction::Long);
        for _ in 0..5 {
            assert_eq!(site.observe(Some(false)), AdaptiveTransition::None);
            assert_eq!(site.prediction, AdaptivePrediction::Long);
        }
        assert_eq!(site.observe(Some(false)), AdaptiveTransition::Demoted);
        assert_eq!(site.prediction, AdaptivePrediction::Cold);
    }

    #[test]
    fn adaptive_runtime_corrects_a_wrong_static_prior() {
        for (offset, prior, actual_long, corrected) in [
            (
                0,
                LifetimePlacementClass::LongLived,
                false,
                AdaptivePrediction::Short,
            ),
            (
                1,
                LifetimePlacementClass::Ephemeral,
                true,
                AdaptivePrediction::Long,
            ),
        ] {
            let mut site =
                AdaptiveSiteState::new(adaptive_test_key(13 + offset, 23 + offset), prior);
            assert_eq!(
                site.routing_prediction(),
                match prior {
                    LifetimePlacementClass::Ephemeral => AdaptivePrediction::Short,
                    LifetimePlacementClass::LongLived => AdaptivePrediction::Long,
                    LifetimePlacementClass::Unknown => unreachable!(),
                }
            );
            assert_eq!(site.observe(Some(actual_long)), AdaptiveTransition::None);
            assert_eq!(site.routing_prediction(), AdaptivePrediction::Cold);
            for _ in 1..10 {
                assert_eq!(site.observe(Some(actual_long)), AdaptiveTransition::None);
            }
            assert_eq!(
                site.observe(Some(actual_long)),
                if actual_long {
                    AdaptiveTransition::PromotedLong
                } else {
                    AdaptiveTransition::PromotedShort
                }
            );
            assert_eq!(site.prediction, corrected);
            assert_eq!(site.static_prior, prior);
        }
    }

    #[test]
    fn adaptive_conflicting_priors_are_neutralized_before_runtime_learning() {
        let mut site =
            AdaptiveSiteState::new(adaptive_test_key(14, 24), LifetimePlacementClass::LongLived);
        assert!(site.note_prior(LifetimePlacementClass::Ephemeral));
        assert!(site.prior_conflict);
        assert_eq!(site.static_prior, LifetimePlacementClass::Unknown);
        assert_eq!((site.short_votes, site.long_votes), (1, 1));
        assert_eq!(site.routing_prediction(), AdaptivePrediction::Cold);

        // Once compiler priors disagree, later hints stay advisory-only and
        // eight fresh decisive runtime outcomes retain final authority.
        assert!(!site.note_prior(LifetimePlacementClass::LongLived));
        for _ in 0..(ADAPTIVE_MIN_DECISIVE_SAMPLES - 1) {
            assert_eq!(site.observe(Some(true)), AdaptiveTransition::None);
        }
        assert_eq!(site.observe(Some(true)), AdaptiveTransition::PromotedLong);
        assert_eq!(site.routing_prediction(), AdaptivePrediction::Long);
    }

    #[test]
    fn adaptive_late_concrete_prior_contributes_weak_votes_once() {
        let mut site =
            AdaptiveSiteState::new(adaptive_test_key(15, 25), LifetimePlacementClass::Unknown);
        let _ = site.observe(Some(false));
        assert_eq!((site.short_votes, site.long_votes), (2, 1));

        assert!(!site.note_prior(LifetimePlacementClass::LongLived));
        assert_eq!(site.static_prior, LifetimePlacementClass::LongLived);
        assert_eq!((site.short_votes, site.long_votes), (2, 3));
        assert_eq!(site.routing_prediction(), AdaptivePrediction::Cold);
        assert!(!site.note_prior(LifetimePlacementClass::LongLived));
        assert_eq!((site.short_votes, site.long_votes), (2, 3));
    }

    #[test]
    fn adaptive_geometry_keeps_trailer_outside_payload_and_checksums_identity() {
        let layout = Layout::from_size_align(4096, 64).unwrap();
        let geometry = slot_geometry(layout, LifetimePlacementClass::Ephemeral, true).unwrap();
        assert_eq!(geometry.payload_capacity, 4096);
        assert!(geometry.slot_size >= geometry.payload_capacity + size_of::<AdaptiveSlotTrailer>());
        assert_eq!(geometry.slot_size % layout.align(), 0);

        let mut storage = [0u8; 8192];
        let ptr = storage.as_mut_ptr();
        let trailer = AdaptiveSlotTrailer::new(
            ptr,
            AdaptiveAllocationRecord {
                birth_pressure: 99,
                site_fingerprint: 101,
                site_index: 3,
                requested_size: layout.size(),
                learned_prediction: AdaptivePrediction::Long,
                static_hint: LifetimePlacementClass::Ephemeral,
                confirmed_long_placement: false,
            },
        );
        assert!(trailer.is_valid(ptr, geometry.payload_capacity));
        assert!(!trailer.is_valid(unsafe { ptr.add(1) }, geometry.payload_capacity));
    }

    #[test]
    fn adaptive_site_key_uses_full_allocation_identity() {
        let first = adaptive_test_key(17, 27);
        let mut second = first;
        second.type_id += 1;
        assert_ne!(first, second);
        second = first;
        second.payload_capacity *= 2;
        assert_ne!(first, second);
        second = first;
        second.align *= 2;
        assert_ne!(first, second);
    }

    #[test]
    fn adaptive_observation_key_adds_exact_requested_size_without_rekeying_predictor() {
        let predictor = adaptive_test_key(18, 28);
        let small = AdaptiveObservationKey::from_site(predictor, 4096);
        let large = AdaptiveObservationKey::from_site(predictor, 8192);

        assert_ne!(small, large);
        assert_eq!(small.callsite, large.callsite);
        assert_eq!(small.type_id, large.type_id);
        assert_eq!(small.module_id, large.module_id);
        assert_eq!(small.align, large.align);
        assert_eq!(predictor.payload_capacity, 64);
        assert_eq!(predictor.flags, 1);
        assert_eq!(predictor.placement_hint, 0);
    }

    #[test]
    fn adaptive_observation_accounts_outcomes_and_live_right_censoring() {
        let predictor = adaptive_test_key(19, 29);
        let key = AdaptiveObservationKey::from_site(predictor, 4096);
        let mut observation = AdaptiveObservationState::new(key);

        for pressure in [100, 200, 300, 400] {
            observation.note_allocation(
                predictor,
                pressure,
                true,
                AdaptivePrediction::Cold,
                LifetimePlacementClass::Unknown,
            );
        }
        observation.note_allocation(
            predictor,
            500,
            false,
            AdaptivePrediction::Short,
            LifetimePlacementClass::Ephemeral,
        );
        observation.note_live_survival(ADAPTIVE_LONG_AGE_BYTES, predictor.payload_capacity);
        observation.note_deallocation(
            100,
            ADAPTIVE_EPOCH_BYTES - 1,
            Some(false),
            false,
            predictor.payload_capacity,
        );
        observation.note_deallocation(
            200,
            ADAPTIVE_LONG_AGE_BYTES,
            Some(true),
            true,
            predictor.payload_capacity,
        );
        observation.note_deallocation(
            300,
            ADAPTIVE_EPOCH_BYTES,
            None,
            false,
            predictor.payload_capacity,
        );

        let snapshot = observation.snapshot(ADAPTIVE_LONG_AGE_BYTES + 400);
        assert_eq!(snapshot.allocation_count, 5);
        assert_eq!(snapshot.tracked_allocations, 4);
        assert_eq!(snapshot.bypassed_allocations, 1);
        assert_eq!(snapshot.completed_outcomes, 3);
        assert_eq!(snapshot.short_outcomes, 1);
        assert_eq!(snapshot.long_outcomes, 1);
        assert_eq!(snapshot.censored_outcomes, 1);
        assert_eq!(snapshot.tracked_inflight, 1);
        assert_eq!(snapshot.live_survival_observations, 1);
        assert_eq!(snapshot.current_live_survival_inflight, 0);
        assert_eq!(snapshot.live_survival_requested_bytes, key.requested_size);
        assert_eq!(
            snapshot.live_survival_payload_bytes,
            predictor.payload_capacity
        );
        assert_eq!(
            snapshot.inflight_age_lower_bound_bytes,
            ADAPTIVE_LONG_AGE_BYTES
        );
        assert_eq!(
            snapshot.allocation_requested_bytes,
            snapshot.allocation_count * key.requested_size
        );
        assert_eq!(
            snapshot.tracked_allocations,
            snapshot.completed_outcomes + snapshot.tracked_inflight
        );
    }

    #[test]
    fn adaptive_observation_reports_predictor_key_ambiguity_without_merging_predictor_state() {
        let first = adaptive_test_key(20, 30);
        let mut second = first;
        second.flags = 4;
        second.placement_hint = 2;
        second.payload_capacity = 128;
        let key = AdaptiveObservationKey::from_site(first, 48);
        assert_eq!(key, AdaptiveObservationKey::from_site(second, 48));
        assert_ne!(first, second);

        let mut observation = AdaptiveObservationState::new(key);
        observation.note_allocation(
            first,
            64,
            false,
            AdaptivePrediction::Cold,
            LifetimePlacementClass::Unknown,
        );
        observation.note_allocation(
            second,
            192,
            false,
            AdaptivePrediction::Short,
            LifetimePlacementClass::Ephemeral,
        );
        let snapshot = observation.snapshot(192);
        assert!(snapshot.predictor_key_ambiguous);
        assert_eq!(snapshot.minimum_payload_capacity, 64);
        assert_eq!(snapshot.maximum_payload_capacity, 128);
        assert_eq!(snapshot.predictor_flags_seen, 5);
        assert_eq!(snapshot.predictor_placement_hint_bits_union, 2);
    }
}
