//! Feature-gated payload arenas driven by exact lifetime metadata.
//!
//! Classified objects carry one of the stable ABI values Unknown/Ephemeral/
//! LongLived. A process-wide logical epoch records whether each routed object
//! dies within its allocation phase or survives a phase boundary, producing
//! object- and rounded-slot-byte-weighted predictor and placement confusion
//! matrices. Regions reused across epochs by a non-cohort policy are excluded
//! when individual object epochs cannot be recovered without side metadata. The
//! epoch-cohort policy also keeps different birth epochs out of the same 2 MiB
//! extent so runtime truth can guide reclaim-oriented placement experiments.
//!
//! The implementation deliberately owns all bookkeeping in fixed process
//! tables. Mapping or releasing an extent therefore cannot recurse through the
//! allocator whose payload path it is implementing.

use super::type_isolation::{
    lifetime_placement_class, AllocationMetadata, LifetimePlacementClass, FLAG_DELAYED_FREE,
};
use crate::pal::sys_alloc::{self, prots, HugePageMmapBacking};
use crate::size_class::{get_size_class_tuple, SizeClass, TOTAL_SIZE_CLASS};
use core::alloc::Layout;
use core::mem::size_of;
use core::sync::atomic::{AtomicUsize, Ordering};
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
}

impl LifetimeHugepagePolicy {
    #[inline]
    fn from_usize(value: usize) -> Self {
        match value {
            1 => Self::SegregatedOrdinary,
            2 => Self::LongLivedHugepage,
            3 => Self::SegregatedHugepage,
            4 => Self::EpochCohortHugepage,
            _ => Self::Disabled,
        }
    }

    #[inline]
    fn requested_backing(
        self,
        class: LifetimePlacementClass,
        backend: LifetimePageBackend,
    ) -> Option<RequestedBacking> {
        let requested = match (self, class) {
            (Self::Disabled, _) | (_, LifetimePlacementClass::Unknown) => None,
            (Self::SegregatedOrdinary, _) => Some(RequestedBacking::Ordinary),
            (
                Self::LongLivedHugepage | Self::EpochCohortHugepage,
                LifetimePlacementClass::Ephemeral,
            ) => Some(RequestedBacking::Ordinary),
            (
                Self::LongLivedHugepage | Self::EpochCohortHugepage,
                LifetimePlacementClass::LongLived,
            )
            | (Self::SegregatedHugepage, _) => Some(RequestedBacking::LargePage),
        };
        if backend == LifetimePageBackend::TransparentHugepage
            && self == Self::EpochCohortHugepage
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
enum RequestedBacking {
    Ordinary = 1,
    LargePage = 2,
    EpochCandidate = 3,
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
    /// `MADV_COLLAPSE` succeeded for this range at a phase boundary. Linux may
    /// split the THP later, so this is a point-in-time observation.
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
}

#[derive(Clone, Copy)]
struct SlotGeometry {
    slot_size: usize,
    region_capacity: usize,
    bucket: usize,
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
}

#[derive(Clone, Copy)]
struct IdentityRegion {
    identity: ArenaIdentity,
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
    region_capacity: usize,
    live: usize,
    bucket: usize,
    regions: [IdentityRegion; IDENTITY_REGIONS_PER_EXTENT],
    lifetime_class: LifetimePlacementClass,
    /// Nonzero only when policy forbids cross-epoch extent mixing.
    cohort_epoch: usize,
    backing: ActualBacking,
    available_prev: usize,
    available_next: usize,
    on_available_list: bool,
    next_free_descriptor: usize,
}

impl Extent {
    const fn empty() -> Self {
        Self {
            base: 0,
            slot_size: 0,
            region_capacity: 0,
            live: 0,
            bucket: 0,
            regions: [IdentityRegion::empty(); IDENTITY_REGIONS_PER_EXTENT],
            lifetime_class: LifetimePlacementClass::Unknown,
            cohort_epoch: 0,
            backing: ActualBacking::Unmapped,
            available_prev: NONE,
            available_next: NONE,
            on_available_list: false,
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
        birth_epoch: usize,
        require_epoch_match: bool,
    ) -> Option<usize> {
        self.regions.iter().position(|region| {
            region.assigned
                && region.identity == identity
                && (!require_epoch_match || region.birth_epoch == birth_epoch)
                && region.has_available_slot(self.region_capacity)
        })
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
}

impl LifetimeArenaState {
    const fn new() -> Self {
        Self {
            extents: [Extent::empty(); MAX_EXTENTS],
            lookup: [NONE; EXTENT_LOOKUP_SLOTS],
            available_heads: [NONE; BUCKET_COUNT],
            next_unused_descriptor: 0,
            free_descriptor_head: NONE,
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
        }
    }

    #[inline]
    fn reset_counters(&mut self) {
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
        }
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
        bucket: usize,
        identity: ArenaIdentity,
        birth_epoch: usize,
        require_extent_cohort: bool,
    ) -> Option<usize> {
        let mut idx = self.available_heads[bucket];
        let mut unassigned_fallback = NONE;
        let mut visited = 0usize;
        while idx != NONE {
            let extent = &self.extents[idx];
            if extent.bucket != bucket || !extent.on_available_list {
                panic!("corrupt lifetime-arena available list");
            }
            let cohort_matches = !require_extent_cohort || extent.cohort_epoch == birth_epoch;
            if cohort_matches
                && extent
                    .matching_region_with_space(identity, birth_epoch, require_extent_cohort)
                    .is_some()
            {
                break;
            }
            if cohort_matches && unassigned_fallback == NONE && extent.unassigned_region().is_some()
            {
                unassigned_fallback = idx;
            }
            idx = extent.available_next;
            visited += 1;
            if visited > MAX_EXTENTS {
                panic!("cyclic lifetime-arena available list");
            }
        }
        let chosen = if idx != NONE {
            idx
        } else {
            unassigned_fallback
        };
        if chosen == NONE {
            return None;
        }
        if self.available_heads[bucket] != chosen {
            self.remove_available(chosen);
            self.add_available(chosen);
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
            region_capacity: geometry.region_capacity,
            live: 0,
            bucket: geometry.bucket,
            regions: [IdentityRegion::empty(); IDENTITY_REGIONS_PER_EXTENT],
            lifetime_class: class,
            cohort_epoch: if epoch_cohort { birth_epoch } else { 0 },
            backing: mapping.backing,
            available_prev: NONE,
            available_next: NONE,
            on_available_list: false,
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
        idx: usize,
        identity: ArenaIdentity,
        class: LifetimePlacementClass,
        birth_epoch: usize,
        require_epoch_match: bool,
    ) -> *mut u8 {
        debug_assert!(self.extents[idx].has_available_slot());
        let region_idx = match self.extents[idx].matching_region_with_space(
            identity,
            birth_epoch,
            require_epoch_match,
        ) {
            Some(region_idx) => region_idx,
            None => {
                let region_idx = self.extents[idx]
                    .unassigned_region()
                    .expect("available extent has no identity region");
                self.extents[idx].regions[region_idx] = IdentityRegion {
                    identity,
                    birth_epoch,
                    epoch_mixed: false,
                    next_unused: 0,
                    live: 0,
                    free_head: NONE,
                    assigned: true,
                };
                self.identity_region_assignments =
                    self.identity_region_assignments.saturating_add(1);
                self.current_identity_regions = self.current_identity_regions.saturating_add(1);
                self.peak_identity_regions =
                    core::cmp::max(self.peak_identity_regions, self.current_identity_regions);
                region_idx
            }
        };
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
        let slot_size = self.extents[idx].slot_size;
        if !self.extents[idx].has_available_slot() {
            self.remove_available(idx);
        }
        self.routed_allocations = self.routed_allocations.saturating_add(1);
        self.live_objects = self.live_objects.saturating_add(1);
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
        (region_base + slot_index * slot_size) as *mut u8
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
        self.note_runtime_validation(
            lifetime_class,
            extent_backing,
            region.identity,
            region.birth_epoch,
            region.epoch_mixed,
            slot_size,
        );
        let was_full = !self.extents[idx].has_available_slot();
        let slot_index = region_offset / slot_size;
        (ptr as *mut usize).write(region.free_head);
        self.extents[idx].regions[region_idx].free_head = slot_index;
        self.extents[idx].regions[region_idx].live -= 1;
        self.extents[idx].live -= 1;
        if self.extents[idx].regions[region_idx].live == 0 {
            self.extents[idx].regions[region_idx] = IdentityRegion::empty();
            self.identity_region_releases = self.identity_region_releases.saturating_add(1);
            self.current_identity_regions = self.current_identity_regions.saturating_sub(1);
        }
        let extent_accepts_current_epoch = lifetime_hugepage_policy()
            != LifetimeHugepagePolicy::EpochCohortHugepage
            || cohort_epoch == self.current_epoch;
        if was_full && extent_accepts_current_epoch {
            self.add_available(idx);
        }
        self.routed_deallocations = self.routed_deallocations.saturating_add(1);
        self.live_objects = self.live_objects.saturating_sub(1);
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

        self.remove_available(idx);
        if unmap_extent(extent_base as *mut u8) {
            if !self.remove_lookup(idx) {
                panic!("lifetime-arena extent index disappeared before unmap");
            }
            self.note_unmapped_extent(extent_backing);
            self.release_descriptor(idx);
        } else {
            // Preserve provenance and reuse capability if the kernel refused
            // the terminal release.
            self.extent_unmap_failures = self.extent_unmap_failures.saturating_add(1);
            if extent_accepts_current_epoch {
                self.add_available(idx);
            }
        }
        true
    }
}

static POLICY: AtomicUsize = AtomicUsize::new(LifetimeHugepagePolicy::Disabled as usize);
static BACKEND: AtomicUsize = AtomicUsize::new(LifetimePageBackend::ExplicitHugeTLB as usize);
static ACTIVE_EXTENTS: AtomicUsize = AtomicUsize::new(0);
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

fn slot_geometry(layout: Layout, class: LifetimePlacementClass) -> Option<SlotGeometry> {
    if layout.size() == 0 || layout.align() > MAX_SLOT_ALIGN {
        return None;
    }
    let requested = core::cmp::max(layout.size(), size_of::<usize>());
    let (size_class, rounded_size) = get_size_class_tuple(requested);
    let size_class_idx = match size_class {
        SizeClass::Base(idx) => idx,
        SizeClass::Large(_) => return None,
    };
    let slot_size = checked_align_up(rounded_size, layout.align())?;
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
        region_capacity,
        bucket,
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

pub fn lifetime_hugepage_backend() -> LifetimePageBackend {
    LifetimePageBackend::from_usize(BACKEND.load(Ordering::Acquire))
}

/// Select the placement policy before the process creates routed objects.
/// Changing policy while arena mappings remain live is rejected.
pub fn lifetime_hugepage_configure(policy: LifetimeHugepagePolicy) -> bool {
    lifetime_hugepage_configure_with_backend(policy, LifetimePageBackend::ExplicitHugeTLB)
}

/// Select placement policy and physical page backend as orthogonal controls.
/// Changing either control while arena mappings remain live is rejected.
pub fn lifetime_hugepage_configure_with_backend(
    policy: LifetimeHugepagePolicy,
    backend: LifetimePageBackend,
) -> bool {
    let state = ARENA.lock();
    if state.live_objects != 0 || state.current_extents != 0 {
        return false;
    }
    BACKEND.store(backend as usize, Ordering::Release);
    POLICY.store(policy as usize, Ordering::Release);
    true
}

pub fn lifetime_hugepage_stats_snapshot() -> LifetimeHugepageStatsSnapshot {
    ARENA.lock().snapshot()
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
pub fn lifetime_hugepage_stats_reset() -> bool {
    let mut state = ARENA.lock();
    if state.live_objects != 0 || state.current_extents != 0 {
        return false;
    }
    state.reset_counters();
    true
}

/// Try to route one exact semantic allocation into a lifetime/size arena.
/// `None` preserves the existing allocator path.
pub(crate) unsafe fn try_allocate(layout: Layout, metadata: AllocationMetadata) -> Option<*mut u8> {
    if POLICY.load(Ordering::Acquire) == LifetimeHugepagePolicy::Disabled as usize {
        return None;
    }
    let class = lifetime_placement_class(metadata.lifetime_hint);
    let mut state = ARENA.lock();
    let policy = lifetime_hugepage_policy();
    let backend = lifetime_hugepage_backend();
    let requested_backing = match policy.requested_backing(class, backend) {
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
    let geometry = match slot_geometry(layout, class) {
        Some(geometry) => geometry,
        None => {
            state.unsupported_layout_bypasses = state.unsupported_layout_bypasses.saturating_add(1);
            return None;
        }
    };
    let identity = ArenaIdentity::from_metadata(metadata);
    let birth_epoch = state.current_epoch;
    let epoch_cohort = policy.separates_epoch_extents();
    let idx = if let Some(idx) =
        state.find_available(geometry.bucket, identity, birth_epoch, epoch_cohort)
    {
        idx
    } else {
        match state.create_extent(
            geometry,
            class,
            requested_backing,
            backend,
            birth_epoch,
            epoch_cohort,
        ) {
            Some(idx) => idx,
            None => {
                state.allocation_fallbacks = state.allocation_fallbacks.saturating_add(1);
                return None;
            }
        }
    };
    Some(state.allocate_from_extent(idx, identity, class, birth_epoch, epoch_cohort))
}

#[inline]
pub(crate) fn owns(ptr: *mut u8) -> bool {
    if ptr.is_null() || ACTIVE_EXTENTS.load(Ordering::Acquire) == 0 {
        return false;
    }
    let base = (ptr as usize) & !(LIFETIME_HUGEPAGE_EXTENT_BYTES - 1);
    ARENA.lock().lookup_extent(base).is_some()
}

/// Return `None` for ordinary pointers and `Some(fits)` for arena pointers.
pub(crate) fn realloc_in_place_supported(ptr: *mut u8, new_layout: Layout) -> Option<bool> {
    if ptr.is_null() || ACTIVE_EXTENTS.load(Ordering::Acquire) == 0 {
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
            && new_layout.size() <= extent.slot_size
            && (ptr as usize) % new_layout.align() == 0,
    )
}

/// Release an arena pointer. False means the ordinary backend owns it.
pub(crate) unsafe fn try_deallocate(ptr: *mut u8) -> bool {
    if ptr.is_null() || ACTIVE_EXTENTS.load(Ordering::Acquire) == 0 {
        return false;
    }
    ARENA.lock().deallocate(ptr)
}

#[cfg(test)]
mod tests {
    use super::*;

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
            region_capacity: LIFETIME_HUGEPAGE_IDENTITY_REGION_BYTES / size_of::<usize>(),
            live: 1,
            bucket: 0,
            regions: [IdentityRegion::empty(); IDENTITY_REGIONS_PER_EXTENT],
            lifetime_class: LifetimePlacementClass::LongLived,
            cohort_epoch: 1,
            backing: ActualBacking::ThpCandidateNoHugepage,
            available_prev: NONE,
            available_next: NONE,
            on_available_list: false,
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
}
